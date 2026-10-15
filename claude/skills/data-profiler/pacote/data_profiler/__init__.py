"""``data_profiler`` — profiling, poder preditivo e associação entre variáveis.

Interface pública::

    from data_profiler import profile, poder_preditivo, correlate

Backends: Polars (pandas/Polars) e PySpark, selecionados automaticamente.
Resultados têm o mesmo schema independentemente do backend.
"""

from __future__ import annotations

import logging

from ._types import BinSpec
from .config import CorrelacaoConfig, PoderPreditivoConfig, ProfileConfig
from .correlation import CorrelacaoResult, correlate
from .predictive_power import PoderPreditivoResult, poder_preditivo
from .profiling import ProfileResult, profile

__version__ = "1.0.0"
__all__ = [
    "profile",
    "poder_preditivo",
    "correlate",
    "ProfileResult",
    "PoderPreditivoResult",
    "CorrelacaoResult",
    "ProfileConfig",
    "PoderPreditivoConfig",
    "CorrelacaoConfig",
    "BinSpec",
]

logging.getLogger("data_profiler").addHandler(logging.NullHandler())
