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

# Which stored part types are images.  ``image_url`` is what AstrBot persists
# (``ImageURLPart.model_dump()`` -> ``{"type": "image_url", "image_url": {...,
# "id": ...}}``, ``core/agent/message.py:112-125``); ``image`` is the other
# spelling ``core/provider/modalities.py:94`` recognises.
_IMAGE_PART_TYPES = ("image_url", "image")


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
    The media fields (``history_images`` … ``current_data_urls``) are *counts* of
    image references found in the request at hook time; they are never added to
    ``total_bytes``, and ``render`` states that what the model finally receives
    can differ.
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

    # --- media accounting (see :class:`ImageStats`) --------------------------
    #
    # Defaulted so that every existing construction keeps working.  ``image_refs``
    # above deliberately stays ``len(req.image_urls)``: it documents the raw list
    # the request carries, and a de-duplicating count would both under-report that
    # list and break the hostile-input pin in ``tests/test_measure.py``.
    history_images: int = 0
    history_base64: int = 0
    history_images_messages: int = 0
    history_parts_undecodable: int = 0
    extra_images: int = 0
    extra_images_base64: int = 0
    extra_parts_unsized: bool = False
    current_data_urls: int = 0

    @property
    def context_images(self) -> int:
        """History + current turn + extra blocks, as counted at hook time."""

        return self.history_images + self.image_refs + self.extra_images

    @property
    def context_base64(self) -> int:
        """Images already carrying inline bytes (history + extra blocks)."""

        return self.history_base64 + self.extra_images_base64

    @property
    def counted_images(self) -> int:
        """Images whose string form sits inside one of the five byte sections."""

        return self.history_images + self.extra_images

    @property
    def context_data_urls(self) -> int:
        """Every already-encoded image: counted sections plus this turn's refs."""

        return self.context_base64 + self.current_data_urls

    @property
    def history_serialisable(self) -> bool:
        """``history_bytes`` survived: a hostile sibling can zero the whole section."""

        return self.history_bytes > 0

    @property
    def extra_serialisable(self) -> bool:
        return self.extra_bytes > 0

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


def _is_dict(value: Any) -> bool:
    """``dict`` check, guarded for the same reason as :func:`_is_sequence`."""

    try:
        return isinstance(value, dict)
    except Exception:
        return False


def _is_str(value: Any) -> bool:
    """``str`` check, guarded for the same reason as :func:`_is_sequence`."""

    try:
        return isinstance(value, str)
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


def _get_both(container: Any, key: str) -> tuple[Any, bool]:
    """``(value, found)`` for ``container[key]``, guarded.

    ``container`` is always a plain ``dict`` by the time this runs (the iterators
    check ``isinstance`` first), so the only failure mode is a dict subclass with
    a hostile ``__getitem__``.
    """

    try:
        return container[key], True
    except Exception:
        return None, False


def _part_discriminator(part: Any) -> str:
    """The ``type`` of one content part, attribute first then key, as plain text."""

    value = _attr(part, "type")
    if not _is_str(value):
        value, _ = _get_both(part, "type") if _is_dict(part) else (None, False)
    return _to_plain_str(value) if value is not None else ""


def _payload_url(part: Any) -> tuple[str, bool]:
    """``(url, readable)`` for one image part.

    Shapes seen in the wild, all of which must read the same way:

    * ``{"type": "image_url", "image_url": {"url": "data:...", "id": "..."}}``
      -- what AstrBot persists (``ImageURLPart.model_dump()``);
    * ``{"type": "image_url", "image_url": "https://..."}`` -- the flat form
      ``ImageURLPart``'s own docstring shows;
    * an object exposing ``.image_url`` as either of the above.

    ``readable`` is False when the part is an image but carries no usable url
    (missing key, ``None``, ``""`` or a non-str).  Such a part is *not* counted
    as an image: the framework skips it too (``core/provider/entities.py:220-247``
    and ``image_input.py:44-48`` both dereference the nested dict), so counting it
    would report an image that cannot be sent.
    """

    payload = _attr(part, "image_url")
    if payload is None and _is_dict(part):
        payload, _ = _get_both(part, "image_url")

    candidate: Any = None
    if _is_dict(payload):
        candidate, _ = _get_both(payload, "url")
    elif _is_str(payload):
        candidate = payload
    if candidate is None:
        candidate = _attr(payload, "url")

    # Only an exact str counts: a part whose url is a list or a number is an
    # image the framework cannot send either (``entities.py:220-247`` dereferences
    # the same field), so it is reported as undecodable rather than as an image.
    url = _to_plain_str(candidate) if _is_str(candidate) else ""
    return url, bool(url)


def _is_data_url(url: str) -> bool:
    """Whether ``url`` carries its own bytes as a data URL.

    Keyed on ``data:`` rather than ``data:image/``: the media layer can hand back
    a generic mime (``media_utils`` falls back to ``application/octet-stream``),
    and every ``data:`` URL is still an inline payload rather than a reference.
    """

    return url.startswith("data:")


def _looks_like_part(value: Any) -> bool:
    """Whether ``value`` could be a content part (``Message.content`` item).

    ``Message.content`` is typed ``list[ContentPart]``, so the items are normally
    pydantic objects rather than the dicts the persisted JSON carries; both have
    to be read.  Strings are excluded explicitly: a plain ``"data:..."`` item in
    the list is not the shape either branch produces.
    """

    try:
        if isinstance(value, (str, bytes)):
            return False
    except Exception:
        return False
    return True


def _iter_content_items(content: Any):
    """Yield ``content``'s items when it is a list, else nothing.

    Only list-shaped content is inspected: v4.10.0 already stores multimodal
    history as a list of parts, and a plain string that merely *contains* a
    data-url (e.g. the user pasted one) is not an image.
    """

    if not _is_sequence(content):
        return
    try:
        items = list(content)
    except Exception:
        return
    for item in items:
        if _looks_like_part(item):
            yield item


@dataclass(frozen=True, slots=True)
class ImageStats:
    """Image/audio accounting for the request as snapshotted at hook time.

    Every count describes *this* ``ProviderRequest`` object, not the body that is
    finally sent: after the hook the framework can replace or drop references
    (``image_input.py``), substitute placeholders for non-vision providers
    (``modalities.py:94-106``) or compress the history.  ``render`` says so.
    """

    history_images: int = 0
    """Image parts inside ``req.contexts`` (the persisted conversation history)."""

    history_base64: int = 0
    """Of those, the ones already carrying inline bytes as a ``data:`` URL."""

    history_images_messages: int = 0
    """How many history messages hold at least one of them."""

    history_parts_undecodable: int = 0
    """Image parts in history whose url was missing/empty/non-str (not counted)."""

    current_images: int = 0
    """``len(req.image_urls)`` -- this turn's references, whatever form they take."""

    current_data_urls: int = 0
    """Of those, the ones already a ``data:`` URL."""

    extra_images: int = 0
    """Image parts among ``req.extra_user_content_parts`` we could read."""

    extra_images_base64: int = 0
    """Of those, the ones already carrying inline bytes as a ``data:`` URL."""

    extra_parts_unsized: bool = False
    """At least one extra part could not be dumped at all: it contributes to
    neither ``extra_bytes`` nor ``extra_images``, so the block says so."""

    history_serialisable: bool = True
    """``history_bytes`` came out non-zero (a hostile sibling can zero it)."""

    extra_serialisable: bool = True
    """``extra_bytes`` came out non-zero."""

    @property
    def context_images(self) -> int:
        return self.history_images + self.current_images + self.extra_images

    @property
    def context_base64(self) -> int:
        return self.history_base64 + self.extra_images_base64

    @property
    def counted(self) -> int:
        """Images whose string form is inside one of the five byte sections.

        ``req.image_urls``/``req.audio_urls`` are never serialised into a section
        (``measure`` reads them as ``len()`` only), so the current-turn references
        are excluded on purpose.
        """

        return self.history_images + self.extra_images

    @property
    def data_urls(self) -> int:
        return self.context_base64 + self.current_data_urls


def count_images(
    contexts: list,
    image_urls: list,
    parts: list,
    dumps_by_index: dict,
) -> ImageStats:
    """Count the media in a request without touching the filesystem.

    ``parts`` is the raw ``req.extra_user_content_parts`` list (already
    normalised by :func:`_as_list`) and ``dumps_by_index`` maps the **raw position**
    to the wire form ``measure`` produced for exactly that part; a position that is
    absent from the map produced no dump.  Pairing by position rather than by list
    order is what keeps a part that could not be dumped from displacing the ones
    behind it: ``dumped_parts`` is a *filtered* list, so its entries carry no
    positional information of their own.

    Contract, same as :func:`measure`: every count degrades to 0 (or the part is
    skipped) on its own, nothing here raises for ordinary hostile input.
    """

    history_images = 0
    history_base64 = 0
    history_messages = 0
    undecodable = 0
    for message in contexts:
        if not _is_dict(message):
            continue
        content = _attr(message, "content")
        if content is None:
            content, _ = _get_both(message, "content")
        found_here = 0
        for item in _iter_content_items(content):
            if _part_discriminator(item) not in _IMAGE_PART_TYPES:
                continue
            url, readable = _payload_url(item)
            if not readable:
                undecodable += 1
                continue
            found_here += 1
            if _is_data_url(url):
                history_base64 += 1
        if found_here:
            history_images += found_here
            history_messages += 1

    current_images = 0
    current_data_urls = 0
    for ref in image_urls:
        current_images += 1
        if not _is_str(ref):
            continue
        url = _to_plain_str(ref)
        if url and _is_data_url(url):
            current_data_urls += 1

    extra_images = 0
    extra_base64 = 0
    unsized = False
    for index in range(len(parts)):
        dump = dumps_by_index.get(index, _SKIP)
        if dump is _SKIP:
            unsized = True
            continue
        if _part_discriminator(dump) not in _IMAGE_PART_TYPES:
            continue
        url, readable = _payload_url(dump)
        if not readable:
            continue
        extra_images += 1
        if _is_data_url(url):
            extra_base64 += 1

    return ImageStats(
        history_images=history_images,
        history_base64=history_base64,
        history_images_messages=history_messages,
        history_parts_undecodable=undecodable,
        current_images=current_images,
        current_data_urls=current_data_urls,
        extra_images=extra_images,
        extra_images_base64=extra_base64,
        extra_parts_unsized=unsized,
    )


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

    dumped_parts: list[Any] = []
    dumps_by_index: dict[int, Any] = {}
    for index, part in enumerate(parts):
        dumped = _dump_extra_part(part)
        if dumped is _SKIP:
            continue
        dumped_parts.append(dumped)
        dumps_by_index[index] = dumped
    tools_bytes, tool_count = _measure_tools(_attr(req, "func_tool"))

    stats = count_images(contexts, images, parts, dumps_by_index)

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
        history_images=stats.history_images,
        history_base64=stats.history_base64,
        history_images_messages=stats.history_images_messages,
        history_parts_undecodable=stats.history_parts_undecodable,
        extra_images=stats.extra_images,
        extra_images_base64=stats.extra_images_base64,
        extra_parts_unsized=stats.extra_parts_unsized,
        current_data_urls=stats.current_data_urls,
    )
