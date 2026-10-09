"""Генерация .ass-субтитров из word-таймингов озвучки.
Стиль «как в вирусных Shorts»: крупные слова по центру группами по 2-3,
karaoke-подсветка текущего произносимого слова + акцент ключевых слов.
В конце — CTA-плашка «забирай в Telegram»."""
from __future__ import annotations

from pathlib import Path

from .tts import Word

# Сколько слов на экране одновременно и максимальная длина группы, сек
_GROUP_WORDS = 3
_GROUP_MAX_SEC = 1.6

# Цвета в формате ASS (&HBBGGRR&). Активное слово — сочный жёлтый, ключевые — салатовый.
_ACTIVE = r"{\c&H2DFFFF&}"   # жёлтый — текущее произносимое слово
_KEY = r"{\c&H50FF50&}"      # зелёный — ключевые слова (бесплатно/халява/...)
_WHITE = r"{\c&HFFFFFF&}"

# Слова, которые всегда подсвечиваем зелёным (по корню, в верхнем регистре)
_KEYWORDS = ("БЕСПЛАТ", "ХАЛЯВ", "FREE", "ДАРОМ", "$0", "СКИДК", "ПРОМОКОД",
             "КРЕДИТ", "ГРАНТ", "БОНУС", "ЛИМИТ")

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Arial,100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,12,3,5,60,60,0,1
Style: CTA,Arial,74,&H0000FFFF,&H0000FFFF,&H00000000,&HC8000000,-1,0,0,0,100,100,0,0,1,10,2,2,50,50,190,1
Style: Footer,Arial,46,&H00D0D0D0,&H00D0D0D0,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,5,0,2,60,60,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(sec: float) -> str:
    """Секунды -> формат ASS 'H:MM:SS.CC'."""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return (text or "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _is_key(word_upper: str) -> bool:
    return any(k in word_upper for k in _KEYWORDS)


def _groups(words: list[Word]) -> list[list[Word]]:
    """Режем поток слов на экранные группы (списки Word)."""
    out: list[list[Word]] = []
    buf: list[Word] = []
    for w in words:
        if buf and (len(buf) >= _GROUP_WORDS or w.end - buf[0].start > _GROUP_MAX_SEC):
            out.append(buf)
            buf = []
        buf.append(w)
    if buf:
        out.append(buf)
    return out


def _render_group(group: list[Word], active_idx: int) -> str:
    """Текст группы капсом: активное слово — жёлтым, ключевые — зелёным, прочие — белым."""
    parts = []
    for i, w in enumerate(group):
        up = _esc(w.text.upper())
        if i == active_idx:
            parts.append(f"{_ACTIVE}{up}{_WHITE}")
        elif _is_key(up):
            parts.append(f"{_KEY}{up}{_WHITE}")
        else:
            parts.append(up)
    return " ".join(parts)


def write_ass(words: list[Word], duration: float, out_path: Path,
              footer: str = "@ai_free_tips",
              cta_text: str = "ПОЛНЫЙ СПИСОК В TELEGRAM") -> None:
    lines = [_HEADER]
    groups = _groups(words)

    for gi, group in enumerate(groups):
        # конец показа группы — до старта следующей (без мигания пустым экраном)
        group_end = groups[gi + 1][0].start if gi + 1 < len(groups) else group[-1].end
        for wi, w in enumerate(group):
            seg_start = w.start
            # активная подсветка держится до следующего слова (или до конца группы)
            seg_end = group[wi + 1].start if wi + 1 < len(group) else group_end
            if seg_end <= seg_start:
                seg_end = seg_start + 0.05
            text = _render_group(group, wi)
            lines.append(
                f"Dialogue: 0,{_ts(seg_start)},{_ts(seg_end)},Main,,0,0,0,,{text}"
            )

    # CTA-плашка в конце: последние ~3 сек — крупный призыв в телегу.
    # БЕЗ эмодзи: шрифты на сервере (Liberation/Arial) их не рисуют — вместо
    # символа вылезает пустой квадрат «тофу».
    cta_start = max(0.0, duration - 3.0)
    lines.append(
        f"Dialogue: 1,{_ts(cta_start)},{_ts(duration)},CTA,,0,0,0,,"
        r"{\fad(300,0)}" + _esc(cta_text)
    )

    if footer:
        lines.append(
            f"Dialogue: 0,{_ts(0)},{_ts(duration)},Footer,,0,0,0,,{_esc(footer)}"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
