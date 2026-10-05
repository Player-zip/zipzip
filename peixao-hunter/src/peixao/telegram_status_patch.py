from __future__ import annotations

from . import telegram_auth as _telegram_auth
from .status_v23 import enhance_status_text


if not getattr(_telegram_auth._status_text, "_v23_efficiency_patch", False):
    _base_status_text = _telegram_auth._status_text

    def _enhanced_status_text(data_dir):
        return enhance_status_text(_base_status_text(data_dir), data_dir)

    _enhanced_status_text._v23_efficiency_patch = True
    _telegram_auth._status_text = _enhanced_status_text
