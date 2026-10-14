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


def _media_sentence(img: int, aud: int) -> str:
    """This turn's attachments, in terms of what is *always* true about them.

    No "尚未编码": a current-turn reference can already be a data URL (a plugin,
    an ``event.request_llm`` caller or the third-party runner stage can put one
    there), so the claim would be false.  What holds in every case is that these
    two lists are not part of any of the five measured sections -- ``measure``
    reads them as ``len()`` only.
    """

    notes = []
    if img:
        notes.append(f"{img} 张图片")
    if aud:
        notes.append(f"{aud} 段音频")
    if not notes:
        return ""
    return f"🖼 本轮附带 {' 与 '.join(notes)}（其字节未计入）"


def _image_block(snapshot: Snapshot) -> list[str]:
    """The media accounting block, one line per true statement.

    Every line is printed only when its own condition holds, and the two
    populations are never mixed: a reference in ``req.image_urls`` is in no
    section at all (``measure`` reads that list as ``len()``), while a history or
    extra-block image is serialised as an entry of its own section -- so only its
    payload can be missing.  The block never says "字节未计入" of the latter.
    """

    hist = snapshot.history_images
    extra = snapshot.extra_images
    current = snapshot.image_refs
    total = snapshot.context_images
    undecodable = snapshot.history_parts_undecodable + int(snapshot.extra_parts_unsized)

    if total == 0 and not undecodable:
        return []

    lines = [
        f"🖼 上下文图片 {total} 张"
        f"（历史 {hist} · 额外块 {extra} · 本轮引用 {current}）"
    ]

    if snapshot.history_images_messages > 1:
        lines.append(f"   └ 历史图片分布在 {snapshot.history_images_messages} 条消息里")

    # Every section figure is split three ways -- counted as inline bytes,
    # counted as a reference entry, or not counted at all because that section
    # failed to serialize -- and each printed number is derived from the SAME
    # pair, so no line can credit a failed section with anything.  ``max`` keeps
    # a hand-built Snapshot whose base64 count exceeds its image count from
    # producing a negative remainder.
    hist_ok = snapshot.history_base64 if snapshot.history_serialisable else 0
    extra_ok = snapshot.extra_images_base64 if snapshot.extra_serialisable else 0
    hist_bad = max(snapshot.history_base64 - hist_ok, 0)
    extra_bad = max(snapshot.extra_images_base64 - extra_ok, 0)
    ok = hist_ok + extra_ok
    bad = hist_bad + extra_bad
    hist_rest = max(hist - hist_ok, 0) if snapshot.history_serialisable else 0
    extra_rest = max(extra - extra_ok, 0) if snapshot.extra_serialisable else 0
    counted_rest = hist_rest + extra_rest
    failed_rest = max(hist - hist_ok - hist_rest, 0) + max(
        extra - extra_ok - extra_rest, 0
    )

    if ok > 0:
        lines.append(f"   └ 历史与额外块中 {ok} 张已是 base64，已计入所在分段")
    if bad > 0:
        lines.append(f"   └ 历史与额外块中有 {bad} 张是 data URL，但所在分段未能序列化，字节计 0")
    if snapshot.current_data_urls > 0:
        lines.append(
            f"   └ 本轮引用里有 {snapshot.current_data_urls} 张已是 data URL，不计入任何分段"
        )
    if snapshot.extra_parts_unsized:
        lines.append(
            "   └ 额外内容块里有无法序列化的块，其字节与图片数都未计入，也无法判断其中是否有图片"
        )
    if snapshot.history_parts_undecodable > 0:
        lines.append(
            f"   └ 历史里有 {snapshot.history_parts_undecodable} 个图片块读不出地址，未计入"
        )

    # Two different populations, two different truths.  ``req.image_urls`` is read
    # as ``len()`` only, so its references really are in no section; history and
    # extra-block images are serialised into their own section, so only the payload
    # is missing while the entry's text is counted -- saying "未计入任何分段" of
    # those would contradict the section rows printed on the same screen.
    current_rest = current - snapshot.current_data_urls
    if current_rest > 0:
        lines.append(
            f"   └ 本轮引用还有 {current_rest} 张没有内嵌字节（路径、URL 或裸 base64），"
            "其字节未计入任何分段"
        )
    if counted_rest > 0:
        lead = "里的" if snapshot.context_base64 == 0 else "中另有"
        lines.append(
            f"   └ 历史与额外块{lead} {counted_rest} 张不是 data URL：图片本体没内嵌，"
            "条目文本仍计入所在分段"
        )
    if failed_rest > 0:
        lines.append(
            f"   └ 历史与额外块里有 {failed_rest} 张不是 data URL，且所在分段未能序列化，"
            "条目文本与字节都没计入"
        )

    lines.append(
        "   ※ 计数取的是钩子时刻的请求条目：本轮引用此前已过发送前的一次图片准备"
        "（去重、失败即丢弃），早于本钩子的处理器再追加的引用尚未经过这一步；"
        "第三方 agent runner 阶段则直接给出裸 base64，既不参与去重也不参与本地化。"
        "此后 provider 可能把引用展开成 base64，provider 的 modalities 不含 image 时"
        "它们会被替换成 [Image] 占位符，工具循环的后续轮次还会继续追加内容。"
        "以下内容在 /ctx 里不可见：本钩子之后运行的 on_llm_request 处理器、"
        "全局「可用插件」（plugin_set）白名单把本插件排除的轮次、"
        "运行器内 on_agent_begin 追加的内容，以及有意不量的工具调用结果字段；此列表不穷尽。"
    )
    return lines


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

    if detail:
        lines.extend(_image_block(snapshot))

    lines.append(_token_line(tokens))

    media = _media_sentence(snapshot.image_refs, snapshot.audio_refs)
    if media:
        lines.append(media)

    lines.append(
        f"🤖 模型 {snapshot.model or '默认'} · 快照 {_clock(snapshot.taken_at, now)}"
    )
    return "\n".join(lines)
