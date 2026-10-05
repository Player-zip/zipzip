"""Unidades canônicas das métricas de performance.

Toda métrica de taxa (win rate, ROI, repetibilidade) circula no pipeline como
razão: 0.65 = 65%. Cada adaptador de provedor converte na saída e marca o ROI
com ``realized_roi_unit = "ratio"``; os scores só leem razões.
"""
from __future__ import annotations

import math

ROI_UNIT_KEY = "realized_roi_unit"
RATIO = "ratio"


def as_float(value) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ratio_from_any(value) -> float | None:
    """Win rate/taxa em razão ou pontos percentuais -> razão.

    0..1 é razão; (1, 100] é ponto percentual. Qualquer outro valor é inválido
    e vira ``None`` (nunca é cortado para 100%).
    """
    number = as_float(value)
    if number is None or number < 0.0:
        return None
    if number <= 1.0:
        return number
    if number <= 100.0:
        return number / 100.0
    return None


def strict_ratio(value) -> float | None:
    """Aceita apenas razão 0..1; o resto é ambíguo e vira ``None``."""
    number = as_float(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def percent_to_ratio(value) -> float | None:
    number = as_float(value)
    return None if number is None else number / 100.0
