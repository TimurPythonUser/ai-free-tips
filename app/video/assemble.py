"""Сборка ролика через FFmpeg (бинарник из imageio-ffmpeg, ничего ставить не надо).
Фон: стоковые клипы, склеенные посегментно, или анимированный градиент, если стока нет.
Поверх: лёгкое затемнение + ASS-субтитры + голос. Выход: 1080x1920, H.264+AAC."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import imageio_ffmpeg

from .. import config

# Размер из конфига (на слабом сервере — 720x1280). Субтитры не трогаем:
# libass сам масштабирует ASS под фактическое разрешение кадра.
W, H, FPS = config.VIDEO_W, config.VIDEO_H, 30


def _build_cmd(clips: list[Path], voice_mp3: Path, subs_name: str,
               duration: float, out_mp4: Path, music: Path | None = None) -> list[str]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []

    if clips:
        # Динамика: меняем клип каждые ~3 сек (не «один клип на весь ролик»).
        # Клипов может быть меньше числа сегментов — гоняем их по кругу.
        import math
        n_seg = max(len(clips), math.ceil(duration / 3.0))
        seg = duration / n_seg
        segs = [clips[i % len(clips)] for i in range(n_seg)]
        for i, clip in enumerate(segs):
            # -stream_loop -1: клип короче сегмента — просто зациклится
            cmd += ["-stream_loop", "-1", "-i", str(clip)]
            filters.append(
                f"[{i}:v]trim=duration={seg:.3f},setpts=PTS-STARTPTS,"
                f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={FPS},format=yuv420p,setsar=1[v{i}]"
            )
        chain = "".join(f"[v{i}]" for i in range(n_seg))
        filters.append(f"{chain}concat=n={n_seg}:v=1:a=0[bg]")
        bg = "[bg]"
        n_inputs = n_seg
    else:
        cmd += ["-f", "lavfi",
                "-i", f"gradients=size={W}x{H}:duration={duration:.3f}:speed=0.03:rate={FPS}"]
        bg = "[0:v]"
        n_inputs = 1

    audio_idx = n_inputs
    cmd += ["-i", str(voice_mp3)]

    # фоновая музыка (опционально): тихо под голосом, зациклена, с fade-out в конце
    if music and music.exists():
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        music_idx = audio_idx + 1
        fade_st = max(0.0, duration - 2.0)
        filters.append(
            f"[{music_idx}:a]atrim=0:{duration:.3f},volume=0.14,"
            f"afade=t=out:st={fade_st:.3f}:d=2[mus]"
        )
        # normalize=0 — не занижать громкость голоса при миксе
        filters.append(f"[{audio_idx}:a][mus]amix=inputs=2:duration=first:normalize=0[aout]")
        amap = "[aout]"
    else:
        amap = f"{audio_idx}:a"

    # затемнение, чтобы белые субтитры читались на любом фоне; subs_name — относительный
    # путь (ffmpeg запускается с cwd=workdir), иначе Windows-пути ломают парсер фильтров
    filters.append(f"{bg}eq=brightness=-0.08:saturation=1.05,ass={subs_name}[outv]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", amap,
        "-t", f"{duration:.3f}",
        # -threads 1: на сервере с 1 ГБ RAM без свопа многопоточный x264 раздувает
        # пик памяти и ловит OOM (SIGKILL). Один поток медленнее, но не падает.
        "-threads", "1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_mp4),
    ]
    return cmd


async def build(clips: list[Path], voice_mp3: Path, subs_ass: Path,
                duration: float, out_mp4: Path, music: Path | None = None) -> None:
    """Собирает ролик. Кидает RuntimeError с хвостом stderr, если FFmpeg упал."""
    workdir = subs_ass.parent
    cmd = _build_cmd(clips, voice_mp3, subs_ass.name, duration, out_mp4, music)

    def _run():
        return subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    proc = await asyncio.to_thread(_run)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-800:]
        raise RuntimeError(f"FFmpeg завершился с кодом {proc.returncode}:\n{tail}")
    if not out_mp4.exists() or out_mp4.stat().st_size == 0:
        raise RuntimeError("FFmpeg отработал, но файл ролика пустой")
