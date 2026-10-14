"""Framework-free core of ``astrbot_plugin_ctx_meter``.

``main.py`` is wiring only; the behaviour lives here so the whole suite runs
with a plain ``python -m pytest`` and no AstrBot installation.
"""

from .measure import WIRE_DUMPS, ImageStats, Snapshot, count_images, measure, wire_bytes
from .render import NO_DATA, Tokens, format_bytes, render
from .service import CtxMeterService, Entry

__all__ = [
    "NO_DATA",
    "WIRE_DUMPS",
    "CtxMeterService",
    "Entry",
    "ImageStats",
    "Snapshot",
    "Tokens",
    "count_images",
    "format_bytes",
    "measure",
    "render",
    "wire_bytes",
]
