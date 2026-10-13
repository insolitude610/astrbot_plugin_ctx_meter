"""Turn a :class:`~ctxmeter.measure.Snapshot` into chat text."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .measure import Snapshot

NO_DATA = "还没有记录到本会话的请求，先随便聊一句再来看看。"
TOKEN_UNKNOWN = "🔢 本轮最后一次调用 token：未知"


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _as_int(obj: Any, name: str) -> int:
    try:
        return int(getattr(obj, name, 0) or 0)
    except Exception:
        return 0


@dataclass(frozen=True, slots=True)
class Tokens:
    total: int
    input_cached: int = 0
    input_other: int = 0
    output: int = 0
    at: float | None = None

    @classmethod
    def from_usage(cls, usage: Any, now: float | None = None) -> Tokens | None:
        """Build from ``LLMResponse.usage``, or ``None`` when it is unknown.

        Two different "unknown" shapes collapse here.  A missing usage object, and
        an all-zero ``TokenUsage``: a provider that reports nothing gets
        ``TokenUsage()`` rather than ``None``
        (``provider/sources/openai_source.py``: ``... if completion.usage else
        TokenUsage()``), and printing ``0`` for that would be a lie.
        """

        if usage is None:
            return None
        try:
            total = int(usage.total or 0)
        except Exception:
            return None
        if total <= 0:
            return None
        return cls(
            total=total,
            input_cached=_as_int(usage, "input_cached"),
            input_other=_as_int(usage, "input_other"),
            output=_as_int(usage, "output"),
            at=now,
        )


def _token_line(tokens: Tokens | None) -> str:
    if tokens is None:
        return TOKEN_UNKNOWN
    # Deliberately NOT "上一次请求": the byte snapshot is the round's first
    # request while this value is the round's last provider call, and in a
    # tool-loop round those are different HTTP requests.
    return (
        f"🔢 本轮最后一次调用 token：{tokens.total:,}"
        f"（缓存输入 {tokens.input_cached:,} · 其它输入 {tokens.input_other:,}"
        f" · 输出 {tokens.output:,}）"
    )


def _clock(taken_at: float, now: float | None) -> str:
    try:
        stamp = taken_at or now or time.time()
        return time.strftime("%H:%M:%S", time.localtime(stamp))
    except Exception:
        return "--:--:--"


def render(
    snapshot: Snapshot | None,
    tokens: Tokens | None,
    detail: bool = True,
    now: float | None = None,
) -> str:
    if snapshot is None:
        return NO_DATA

    total = snapshot.total_bytes
    lines = [f"📦 当前对话请求体 ≈ {format_bytes(total)} ({total:,} bytes)"]

    if detail:
        rows: list[str] = []
        for label, size in snapshot.sections:
            if size <= 0:
                continue
            suffix = ""
            if label == "历史上下文" and snapshot.history_messages:
                suffix = f"  ({snapshot.history_messages} 条消息)"
            elif label == "工具 Schema" and snapshot.tool_count:
                suffix = f"  ({snapshot.tool_count} 个)"
            rows.append(f"{label}  {format_bytes(size)}{suffix}")
        for index, row in enumerate(rows):
            glyph = "└" if index == len(rows) - 1 else "├"
            lines.append(f"{glyph} {row}")

    lines.append(_token_line(tokens))

    if snapshot.image_refs or snapshot.audio_refs:
        notes = []
        if snapshot.image_refs:
            notes.append(f"{snapshot.image_refs} 张图片")
        if snapshot.audio_refs:
            notes.append(f"{snapshot.audio_refs} 段音频")
        lines.append(f"🖼 本轮附带 {'、'.join(notes)}（尚未编码，未计入）")

    lines.append(
        f"🤖 模型 {snapshot.model or '默认'} · 快照 {_clock(snapshot.taken_at, now)}"
    )
    return "\n".join(lines)
