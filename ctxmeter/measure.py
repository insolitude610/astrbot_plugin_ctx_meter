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

# Section labels shown by the renderer, in display order.
#
# `req.tool_calls_result` is deliberately NOT a section.  It is empty at hook time
# in the framework's own flow (the runner appends tool results only after the
# round's first request), and when a third-party plugin does supply it, its wire
# form is not `to_openai_messages()` either: before the HTTP call the provider
# rewrites the message list, which can change assistant content and drop orphaned
# tool messages.  In `astrbot/core/provider/sources/openai_source.py`:
# `_sanitize_assistant_messages` is defined at :446 and applied at :560
# (non-stream) and :618 (stream), rewriting content at :483-486 and dropping
# orphans at :508-522; `_finally_convert_payload` is defined at :995, called at
# :991, with its rewrite loop at :1016-1049.  Reproducing that lossy transform to
# the byte was not worth it for a section that is normally zero, so it is left out
# entirely rather than reported with a number we cannot stand behind.
_SECTION_LABELS = (
    "系统提示词",
    "历史上下文",
    "本轮输入",
    "额外内容块",
    "工具 Schema",
)

# "There is a value here, but we cannot express its wire form."  Sections holding
# one of these contribute nothing, which keeps the reported number from claiming
# more precision than we actually have.
_SKIP = object()


def wire_bytes(obj: Any) -> int:
    """UTF-8 byte length of ``obj`` as httpx would put it on the wire.

    Returns 0 when the wire kwargs cannot encode it.  There is deliberately
    **no** ``default=str`` fallback: httpx has no ``default=`` hook either, so a
    value that needs one never reaches the wire, and inventing a number for it
    would break the lower-bound contract.  That is precisely how an earlier
    revision overcounted ``ToolCallsResult`` -- a dataclass repr is longer than
    the JSON the provider actually appends.

    The ``encode`` call belongs INSIDE the try: ``ensure_ascii=False`` keeps any
    lone surrogate in the string, and ``str.encode`` then raises
    ``UnicodeEncodeError`` (a ``ValueError``).  Text decoded by a platform adapter
    can carry one, so leaving the encode outside made ``measure`` raise for a
    perfectly realistic input -- earlier revisions had it outside.
    """

    if obj is None:
        return 0
    try:
        text = json.dumps(obj, **WIRE_DUMPS)
        return len(text.encode("utf-8"))
    except Exception:
        # TypeError/ValueError for odd values, UnicodeEncodeError for surrogates,
        # RecursionError for cycles.  ``BaseException`` is deliberately NOT caught:
        # swallowing a task cancellation here would be worse than a zero section.
        return 0


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """``getattr`` that survives a hostile property.

    A third-party plugin can put anything into a ``ProviderRequest`` field, and a
    property that raises would otherwise escape :func:`measure`.
    """

    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _call_dump(obj: Any, name: str) -> Any:
    """Call ``obj.name()`` if it exists and works, else ``_SKIP``."""

    method = _attr(obj, name)
    if not callable(method):
        return _SKIP
    try:
        produced = method()
    except Exception:
        return _SKIP
    return _SKIP if produced is None else produced


def _dump_extra_part(part: Any) -> Any:
    """Wire form of one ``extra_user_content_parts`` entry, or ``_SKIP``.

    ``model_dump_for_context`` only exists from AstrBot 4.24.1 on, so older
    builds step down to ``model_dump``, then to a plain dict already in wire
    shape.  A part with none of those contributes nothing rather than a made-up
    size.
    """

    for name in ("model_dump_for_context", "model_dump"):
        produced = _call_dump(part, name)
        if produced is not _SKIP:
            return produced
    try:
        if isinstance(part, dict):
            return part
    except Exception:
        return _SKIP
    return _SKIP


def _measure_tools(func_tool: Any) -> tuple[int, int]:
    """Return ``(bytes, count)`` for the request's tool set."""

    if func_tool is None:
        return 0, 0

    count = 0
    try:
        count = len(func_tool)
    except Exception:
        names = _attr(func_tool, "names")
        if callable(names):
            try:
                count = len(names())
            except Exception:
                count = 0

    produced: Any = None
    dump = _attr(func_tool, "openai_schema")
    if callable(dump):
        try:
            produced = dump()
        except Exception:
            produced = None
    if produced is None:
        # No schema at all: missing method, it raised, or it returned None.
        # Degrade to bare names -- coarse, but a real part of the schema rather
        # than a fabricated size.  Testing ``produced is None`` rather than
        # ``not schema`` is deliberate: routing a None return through ``_as_list``
        # would turn it into an empty list, i.e. "a schema exists but is empty",
        # and the name fallback would be skipped, silently undercounting.
        try:
            schema = [_as_opt_str(_attr(tool, "name")) or "?" for tool in func_tool]
        except Exception:
            schema = []
    else:
        schema = _as_list(produced)

    if not schema and count == 0:
        return 0, 0
    return wire_bytes(schema), count


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one request would have cost on the wire.

    ``total_bytes`` covers five sections: system prompt, history, this turn's
    input, extra content parts and the tool schema.  It is NOT an equality with
    whichever body the provider finally sends, and it is not a clean lower bound
    either: see DESIGN.md section 3.1 for the provider envelope fields it omits
    and for the framework settings (``skills_like`` tool trimming, modality
    placeholders) that make some of those same fields an overcount.
    ``req.tool_calls_result`` is excluded on purpose (see ``_SECTION_LABELS``).
    """

    system_bytes: int = 0
    history_bytes: int = 0
    prompt_bytes: int = 0
    extra_bytes: int = 0
    tools_bytes: int = 0
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
        )


def _to_plain_str(value: Any) -> str:
    """``value`` as an **exact** ``str``, or ``""``.

    Every branch has to end at an exact ``str``, because a later ``if system:``
    would otherwise call a subclass's ``__bool__``.  Two traps:

    * ``str(value)`` dispatches to ``__str__``, which may raise; and
    * CPython lets a non-str object's ``__str__`` return a ``str`` **subclass**,
      so the result of ``str(value)`` cannot be trusted either.

    ``str.__str__(value)`` -- the base slot called explicitly -- always yields an
    exact ``str`` for a str instance (established by probe), and it raises
    ``TypeError`` for anything else, which is the fallback path.
    """

    if type(value) is str:
        return value

    produced: Any = None
    try:
        produced = str.__str__(value)
    except Exception:
        try:
            produced = str(value)
        except Exception:
            return ""

    if type(produced) is str:
        return produced

    # A str subclass got in through __str__; normalise it the same way.
    try:
        return str.__str__(produced)
    except Exception:
        return ""


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return _to_plain_str(value)


def _as_opt_str(value: Any) -> str | None:
    """``value`` as a non-empty exact ``str``, or ``None``.

    Nothing here calls a dunder the value itself defines: ``str.__str__`` is the
    base slot, and the truthiness test runs on the plain ``str`` it returns.
    """

    if value is None:
        return None
    return _to_plain_str(value) or None


def _is_sequence(value: Any) -> bool:
    """``list``/``tuple`` check that a hostile ``__getattribute__`` cannot break.

    ``isinstance`` resolves ``__class__`` through attribute lookup, so an object
    that raises there takes the check down with it unless it is wrapped.
    """

    try:
        return isinstance(value, (list, tuple))
    except Exception:
        return False


def _as_list(value: Any) -> list:
    """``value`` as a **plain** list.

    Normalising here is what keeps the rest of the module safe: once a field is a
    builtin list, ``len()``, truthiness and iteration downstream cannot surprise
    us.  A ``list`` subclass whose ``__len__`` or ``__iter__`` raises is caught
    here instead of escaping from a later ``if contexts``.
    """

    if value is None:
        return []
    if _is_sequence(value):
        try:
            return list(value)
        except Exception:
            return []
    return [value]


def measure(req: Any, now: float | None = None) -> Snapshot:
    """Take a byte snapshot of a fully built ``ProviderRequest``.

    Contract: every *section* degrades to zero on its own, every attribute is read
    through :func:`_attr`, and nothing here raises for ordinary hostile input --
    a wrong type, a missing field, a property that raises.  What is NOT swallowed
    is :class:`BaseException`, so a task cancellation still propagates.  Callers
    must therefore still contain this call: :meth:`CtxMeterService.record_request`
    does, and keeps the previous snapshot rather than recording a fake zero.
    """

    system = _as_text(_attr(req, "system_prompt", ""))
    contexts = _as_list(_attr(req, "contexts"))
    prompt = _as_text(_attr(req, "prompt", ""))
    parts = _as_list(_attr(req, "extra_user_content_parts"))
    images = _as_list(_attr(req, "image_urls"))
    audios = _as_list(_attr(req, "audio_urls"))

    dumped_parts = [
        dumped for dumped in (_dump_extra_part(part) for part in parts)
        if dumped is not _SKIP
    ]
    tools_bytes, tool_count = _measure_tools(_attr(req, "func_tool"))

    return Snapshot(
        system_bytes=wire_bytes(system) if system else 0,
        history_bytes=wire_bytes(contexts) if contexts else 0,
        prompt_bytes=wire_bytes(prompt) if prompt else 0,
        extra_bytes=wire_bytes(dumped_parts) if dumped_parts else 0,
        tools_bytes=tools_bytes,
        history_messages=len(contexts),
        tool_count=tool_count,
        image_refs=len(images),
        audio_refs=len(audios),
        model=_as_opt_str(_attr(req, "model")),
        taken_at=float(now) if now is not None else time.time(),
    )
