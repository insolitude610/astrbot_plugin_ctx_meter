"""Hook bookkeeping for the ``/ctx`` command.

Framework-free: the event, the request and the usage object are all touched
through duck typing, so the offline suite drives this class directly.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .measure import Snapshot, measure
from .render import Tokens, render

DEFAULT_MAX_SESSIONS = 64

DISABLED = "请求体计量插件已关闭（/ctx 不可用）。"
DENIED = "权限不足：/ctx 当前只对管理员开放。"


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass
class Entry:
    """The cached state of one session.  ``tokens`` is cleared with the snapshot."""

    snapshot: Snapshot | None = None
    tokens: Tokens | None = None


class CtxMeterService:
    def __init__(self, config: Any = None, logger: Any = None) -> None:
        self._config = config if config is not None else {}
        self._logger = logger
        self._sessions: OrderedDict[str, Entry] = OrderedDict()

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _max_sessions(self) -> int:
        try:
            value = int(self._cfg("max_sessions", DEFAULT_MAX_SESSIONS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_SESSIONS
        return value if value > 0 else DEFAULT_MAX_SESSIONS

    def _log(self, level: str, message: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            getattr(logger, level, logger.info)(message)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # read-only accessors (also the test seam)
    # ------------------------------------------------------------------
    def snapshot_for(self, umo: str) -> Snapshot | None:
        entry = self._sessions.get(umo)
        return entry.snapshot if entry else None

    def tokens_for(self, umo: str) -> Tokens | None:
        entry = self._sessions.get(umo)
        return entry.tokens if entry else None

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    def record_request(self, umo: str, req: Any, now: float | None = None) -> None:
        """Snapshot a request that is about to go to the provider.

        A disabled plugin records nothing at all, which is what the ``enable``
        config hint promises.

        The previous round's tokens are dropped in the same step.  A round whose
        provider call fails still fires ``on_llm_response`` but carries no usage,
        so keeping the old value would show this round's bytes next to the last
        round's tokens -- a number that looks like it belongs to this request.

        A measurement failure leaves the previous entry alone: a transient bug
        here must not erase a good number, and the footer already prints the
        snapshot time so staleness is visible.
        """

        if not self._enabled():
            return
        try:
            snapshot = measure(req, now=now if now is not None else time.time())
        except Exception as exc:
            self._log("debug", f"ctx meter: measurement failed: {exc}")
            return
        self._store(umo, Entry(snapshot=snapshot, tokens=None))

    def record_response(self, umo: str, usage: Any, now: float | None = None) -> None:
        """Attach the usage of this round's last provider call."""

        if not self._enabled():
            return
        entry = self._sessions.get(umo)
        if entry is None:
            return
        try:
            tokens = Tokens.from_usage(
                usage, now=now if now is not None else time.time()
            )
        except Exception as exc:
            self._log("debug", f"ctx meter: could not read the usage: {exc}")
            return
        if tokens is not None:
            entry.tokens = tokens
        self._sessions.move_to_end(umo)

    # ------------------------------------------------------------------
    # command
    # ------------------------------------------------------------------
    def view(self, event: Any, now: float | None = None) -> str:
        """The text ``/ctx`` should reply with.

        Pure: it never writes to the conversation, the database or the cache.
        ``main.py`` still wraps the call, so a failure here produces the fallback
        reply rather than a silent one.
        """

        if not self._enabled():
            return DISABLED
        if self._admin_only() and not self._permitted(event):
            return DENIED

        entry = self._sessions.get(self._umo(event))
        return render(
            entry.snapshot if entry else None,
            entry.tokens if entry else None,
            detail=_as_bool(self._cfg("detail", True)),
            now=now if now is not None else time.time(),
        )

    def _enabled(self) -> bool:
        return _as_bool(self._cfg("enable", True))

    def _admin_only(self) -> bool:
        return str(self._cfg("permission_mode", "everyone")).strip().lower() == "admin"

    @staticmethod
    def _permitted(event: Any) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _umo(event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    # ------------------------------------------------------------------
    def _store(self, umo: str, entry: Entry) -> None:
        self._sessions[umo] = entry
        self._sessions.move_to_end(umo)
        limit = self._max_sessions()
        while len(self._sessions) > limit:
            self._sessions.popitem(last=False)
