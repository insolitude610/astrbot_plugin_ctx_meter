"""Byte accounting for the request AstrBot is about to send to the model.

Framework-free on purpose: the offline test suite drives this module directly,
so it never imports ``astrbot``.  Everything is read through ``getattr`` with a
default, because the shapes involved have changed across AstrBot releases.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

# The exact kwargs httpx uses to serialize a JSON request body
# (``venv/Lib/site-packages/httpx/_content.py:176-178``).  openai's client hands
# the body to httpx via ``json=`` (``openai/_base_client.py:586``) with no orjson
# bypass, so reproducing these kwargs is what makes our number match the wire.
#
# Do NOT relax this to the ``json.dumps`` defaults: ``ensure_ascii=True`` escapes
# every CJK character as ``\uXXXX`` and inflates the count roughly threefold.
# ``tests/test_measure.py`` locks that in.
WIRE_DUMPS: dict[str, Any] = {
    "ensure_ascii": False,
    "separators": (",", ":"),
    "allow_nan": False,
}

_FALLBACK_DUMPS: dict[str, Any] = {"ensure_ascii": False, "default": str}

# Section labels shown by the renderer, in display order.
_SECTION_LABELS = (
    "系统提示词",
    "历史上下文",
    "本轮输入",
    "额外内容块",
    "工具 Schema",
    "工具调用结果",
)


def wire_bytes(obj: Any) -> int:
    """UTF-8 byte length of ``obj`` as httpx would put it on the wire."""

    if obj is None:
        return 0
    try:
        text = json.dumps(obj, **WIRE_DUMPS)
    except (TypeError, ValueError):
        try:
            text = json.dumps(obj, **_FALLBACK_DUMPS)
        except Exception:
            return 0
    return len(text.encode("utf-8"))


def _dump_extra_part(part: Any) -> Any:
    """Serialize one entry of ``req.extra_user_content_parts``.

    ``model_dump_for_context`` only exists from AstrBot 4.24.1 on, so older
    builds step down to ``model_dump``, then to a plain dict, then to ``str``.
    """

    for name in ("model_dump_for_context", "model_dump"):
        dump = getattr(part, name, None)
        if callable(dump):
            try:
                return dump()
            except Exception:
                continue
    if isinstance(part, dict):
        return part
    return str(part)


def _measure_tools(func_tool: Any) -> tuple[int, int]:
    """Return ``(bytes, count)`` for the request's tool set."""

    if func_tool is None:
        return 0, 0

    count = 0
    try:
        count = len(func_tool)
    except Exception:
        names = getattr(func_tool, "names", None)
        if callable(names):
            try:
                count = len(names())
            except Exception:
                count = 0

    schema: Any = None
    dump = getattr(func_tool, "openai_schema", None)
    if callable(dump):
        try:
            schema = dump()
        except Exception:
            schema = None
    if schema is None:
        # Degrade to bare names: still a usable lower bound, never a crash.
        try:
            schema = [getattr(tool, "name", None) or str(tool) for tool in func_tool]
        except Exception:
            schema = []

    if not schema and count == 0:
        return 0, 0
    return wire_bytes(schema), count


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one request would have cost on the wire.

    ``total_bytes`` is a lower bound on the real body: it covers the message and
    tool sections but not the provider envelope (``model``, ``stream``,
    ``stream_options``, ``tool_choice``, ``temperature``, ...).
    """

    system_bytes: int = 0
    history_bytes: int = 0
    prompt_bytes: int = 0
    extra_bytes: int = 0
    tools_bytes: int = 0
    toolcalls_bytes: int = 0
    history_messages: int = 0
    tool_count: int = 0
    image_refs: int = 0
    audio_refs: int = 0
    model: str | None = None
    taken_at: float = 0.0

    @property
    def total_bytes(self) -> int:
        return (
            self.system_bytes
            + self.history_bytes
            + self.prompt_bytes
            + self.extra_bytes
            + self.tools_bytes
            + self.toolcalls_bytes
        )

    @property
    def sections(self) -> tuple[tuple[str, int], ...]:
        """Every section as ``(label, bytes)``, including the zero ones."""

        return tuple(zip(_SECTION_LABELS, self._section_values(), strict=True))

    def _section_values(self) -> tuple[int, ...]:
        return (
            self.system_bytes,
            self.history_bytes,
            self.prompt_bytes,
            self.extra_bytes,
            self.tools_bytes,
            self.toolcalls_bytes,
        )


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def measure(req: Any, now: float | None = None) -> Snapshot:
    """Take a byte snapshot of a fully built ``ProviderRequest``.

    Never raises: every section degrades to zero on its own, so a shape change in
    a future AstrBot release cannot break a live conversation.
    """

    system = _as_text(getattr(req, "system_prompt", ""))
    contexts = _as_list(getattr(req, "contexts", None))
    prompt = _as_text(getattr(req, "prompt", ""))
    parts = _as_list(getattr(req, "extra_user_content_parts", None))
    images = _as_list(getattr(req, "image_urls", None))
    audios = _as_list(getattr(req, "audio_urls", None))
    tool_calls = getattr(req, "tool_calls_result", None)

    dumped_parts = [_dump_extra_part(part) for part in parts]
    tools_bytes, tool_count = _measure_tools(getattr(req, "func_tool", None))

    return Snapshot(
        system_bytes=wire_bytes(system) if system else 0,
        history_bytes=wire_bytes(contexts) if contexts else 0,
        prompt_bytes=wire_bytes(prompt) if prompt else 0,
        extra_bytes=wire_bytes(dumped_parts) if dumped_parts else 0,
        tools_bytes=tools_bytes,
        toolcalls_bytes=wire_bytes(tool_calls) if tool_calls else 0,
        history_messages=len(contexts),
        tool_count=tool_count,
        image_refs=len(images),
        audio_refs=len(audios),
        model=getattr(req, "model", None) or None,
        taken_at=float(now) if now is not None else time.time(),
    )
