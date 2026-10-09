"""Минимальный MCP-клиент (JSON-RPC 2.0 поверх stdio) — без внешних зависимостей,
работает на Python 3.9. Поднимает MCP-сервер как подпроцесс (напр. exa-mcp-server
через npx) и логирует весь обмен (initialize / tools/list / tools/call) в шину.

Транспорт stdio у MCP — построчный JSON: одна JSON-RPC-строка = одно сообщение.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from .logbus import bus


class MCPError(Exception):
    pass


class MCPStdioClient:
    def __init__(self, command: str, args: list[str], env: dict | None = None, name: str = "MCP"):
        # На Windows npx — это npx.cmd
        if command == "npx" and sys.platform == "win32":
            command = "npx.cmd"
        self.command = command
        self.args = args
        self.name = name
        self.env = {**os.environ, **(env or {})}
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def __aenter__(self) -> "MCPStdioClient":
        await bus.log(f"🔌 [MCP:{self.name}] запускаю сервер: {self.command} {' '.join(self.args)}")
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._reader_task:
                self._reader_task.cancel()
            if self.proc and self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
            if self.proc and self.proc.returncode is None:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.proc.kill()
        except Exception:
            pass

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                fut = self._pending.pop(mid)
                if not fut.done():
                    fut.set_result(msg)

    async def _request(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        assert self.proc and self.proc.stdin
        self._id += 1
        mid = self._id
        payload = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise MCPError(f"тайм-аут ответа на {method}")
        if "error" in msg:
            raise MCPError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def _notify(self, method: str, params: dict | None = None) -> None:
        assert self.proc and self.proc.stdin
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    # --- высокоуровневые шаги MCP ---
    async def initialize(self) -> None:
        await bus.log(f"🔌 [MCP:{self.name}] ➡️ initialize")
        res = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ai-free-tips", "version": "1.0"},
        })
        srv = (res.get("serverInfo") or {}).get("name", "?")
        await bus.log(f"🔌 [MCP:{self.name}] ⬅️ сервер готов: {srv}")
        await self._notify("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        res = await self._request("tools/list")
        tools = res.get("tools", [])
        names = ", ".join(t.get("name", "?") for t in tools)
        await bus.log(f"🔌 [MCP:{self.name}] ⬅️ tools/list: [{names}]")
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        await bus.log(f"🔌 [MCP:{self.name}] ➡️ tools/call {name} {json.dumps(arguments, ensure_ascii=False)}")
        res = await self._request("tools/call", {"name": name, "arguments": arguments}, timeout=90)
        return res
