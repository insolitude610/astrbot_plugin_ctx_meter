"""astrbot_plugin_ctx_meter - ``/ctx`` reports how big the LLM request body is.

Wiring only: every behaviour lives in the dependency-free :mod:`ctxmeter` package.
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ctxmeter.service import CtxMeterService

PLUGIN_NAME = "astrbot_plugin_ctx_meter"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESC = (
    "输入 /ctx 查看当前对话上一次请求的请求体大小"
    "（字节数 + 系统提示词/历史上下文/本轮输入/额外内容块/工具 Schema 分段 + 该轮 token 数）"
)
PLUGIN_AUTHOR = "insolitude610"
PLUGIN_REPO = "https://github.com/insolitude610/astrbot_plugin_ctx_meter"


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class CtxMeterPlugin(Star):
    """Snapshot every LLM request; report the last one on demand."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._service = CtxMeterService(
            config=config if config is not None else {},
            logger=logger,
        )

    @filter.on_llm_request()
    async def _record_request(self, event: AstrMessageEvent, req) -> None:
        """Snapshot the fully built request, before it reaches the provider.

        Every failure is contained here: a bug in the measurement must never take
        down a real conversation.
        """

        try:
            self._service.record_request(str(event.unified_msg_origin), req)
        except Exception:
            logger.debug(f"{PLUGIN_NAME}: record_request failed", exc_info=True)

    @filter.on_llm_response()
    async def _record_response(self, event: AstrMessageEvent, response) -> None:
        """Pick up the usage of this round's last provider call."""

        try:
            self._service.record_response(
                str(event.unified_msg_origin),
                getattr(response, "usage", None),
            )
        except Exception:
            logger.debug(f"{PLUGIN_NAME}: record_response failed", exc_info=True)

    @filter.command("ctx", alias={"请求体", "ctxsize"})
    async def ctx(self, event: AstrMessageEvent):
        """Report the size of the request body that was last sent.

        One ``yield``, one message.  Everything that decides the outcome --
        the ``should_call_llm(True)`` guard above all -- lives in
        ``CtxMeterService.command`` so it can be exercised without AstrBot; see
        DESIGN.md section 3.2 for why that guard is not optional.
        """

        yield event.plain_result(self._service.command(event))
