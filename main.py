"""astrbot_plugin_ctx_meter - ``/ctx`` reports how big the LLM request body is.

Wiring only: every behaviour lives in the dependency-free :mod:`ctxmeter` package.
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ctxmeter.service import CtxMeterService

PLUGIN_NAME = "astrbot_plugin_ctx_meter"
PLUGIN_VERSION = "1.1.0"
PLUGIN_DESC = (
    "输入 /ctx 查看当前对话上一次请求的请求体大小"
    "（字节数 + 系统提示词/历史上下文/本轮输入/额外内容块/工具 Schema 分段"
    " + 该轮 token 数 + 上下文里的图片张数与 base64 分布）"
)
PLUGIN_AUTHOR = "insolitude610"
PLUGIN_REPO = "https://github.com/insolitude610/astrbot_plugin_ctx_meter"

#: Dispatch order of ``on_llm_request`` handlers is the registry's list order,
#: which ``astrbot/core/star/star_handler.py:19-26`` keeps sorted by
#: ``-extras_configs["priority"]`` (missing key = 0, stable ties).  A *negative*
#: value therefore runs **last**: every plugin that leaves priority at 0 has
#: already injected by the time this recorder snapshots the request.
#:
#: Why that matters here: the plugin is loaded in ``os.listdir`` order
#: (`astrbot/core/star/star_manager.py:291-320`), which put this directory before
#: ``astrbot_plugin_qzone_reader`` on the live install -- and that plugin injects
#: from its own ``on_llm_request`` handler, so at the default priority the
#: snapshot missed exactly those injections.  (A plugin that instead goes through
#: ``event.request_llm`` is assembled before the hook and was never affected.)
#:
#: The value is a local constant on purpose: no such name exists anywhere in the
#: framework, and reading it at import time is impossible (per-plugin config is
#: built after the module is imported: ``star_manager.py:1132`` vs ``:1157-1230``).
LLM_REQUEST_PRIORITY = -10000


def _register_recorder(handler, name: str = PLUGIN_NAME):
    """Register ``handler`` for ``OnLLMRequestEvent`` at the late priority.

    The ``TypeError`` branch is pure defence and is expected never to run: the
    registrar is ``def register_on_llm_request(**kwargs)`` in every AstrBot tree
    inspected (v4.0.0 … v4.28.2), and this plugin declares ``>=4.10.0``.  If it
    ever does run, the handler lands at priority 0 -- i.e. back to load order,
    which can hide the injections this plugin is meant to measure -- so it is
    worth a warning rather than a silent degrade.
    """

    try:
        return filter.on_llm_request(priority=LLM_REQUEST_PRIORITY)(handler)
    except TypeError:
        logger.warning(
            f"{name}: this AstrBot build rejects on_llm_request(priority=...); "
            "the snapshot will follow plugin load order instead and may miss "
            "injections made later in the on_llm_request chain."
        )
        return filter.on_llm_request()(handler)



@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class CtxMeterPlugin(Star):
    """Snapshot every LLM request; report the last one on demand."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._service = CtxMeterService(
            config=config if config is not None else {},
            logger=logger,
        )

    @_register_recorder
    async def _record_request(self, event: AstrMessageEvent, req) -> None:
        """Snapshot the fully built request, before it reaches the provider.

        Registered through :func:`_register_recorder` so the priority kwarg --
        which the decorator form cannot express while also staying compatible with
        a build that rejects it -- is applied in one place.

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
