"""Funções utilitárias compartilhadas (sem dependência de backend)."""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from typing import Iterator, Sequence

import numpy as np
import polars as pl

from ._types import PREFIXO_ESPECIAL

logger = logging.getLogger("data_profiler")

FORMATOS_DATA_PADRAO: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y",
    "%Y%m%d",
    "%Y-%m",
)
"""Formatos tentados (nesta ordem, via ``coalesce``) ao converter strings em datas."""


@contextmanager
def cronometro(etapa: str, tempos: dict[str, float] | None = None) -> Iterator[None]:
    """Mede e registra em log o tempo de uma etapa."""
    inicio = time.perf_counter()
    logger.info("Iniciando etapa: %s", etapa)
    try:
        yield
    finally:
        dur = time.perf_counter() - inicio
        logger.info("Etapa %s concluída em %.2fs", etapa, dur)
        if tempos is not None:
            tempos[etapa] = round(dur, 3)


def codigo_especial(valor: float) -> str:
    """Código de faixa para um valor especial (idêntico em todos os backends)."""
    v = float(valor)
    txt = str(int(v)) if v.is_integer() else repr(v)
    return f"{PREFIXO_ESPECIAL}{txt}"


def pct(numerador: float | int | None, denominador: float | int | None) -> float | None:
    """Percentual 0–100 com 2 casas; ``None`` se indefinido."""
    if numerador is None or denominador in (None, 0):
        return None
    return round(100.0 * float(numerador) / float(denominador), 2)


def arredondar(valor: float | None, casas: int = 6) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, float) and (math.isnan(valor) or math.isinf(valor)):
        return None
    return round(float(valor), casas)


def nome_percentil(p: float) -> str:
    """``0.25 → "p_25"``, ``0.01 → "p_1"``."""
    return f"p_{int(round(p * 100))}"


def ks_auc_de_distribuicao(tabela: pl.DataFrame) -> tuple[float | None, float | None]:
    """KS e AUC a partir de uma distribuição agrupada e **ordenada** pelo valor.

    Args:
        tabela: colunas ``w1`` (peso de maus) e ``w0`` (peso de bons) por valor/faixa
            distinta, já ordenada de forma crescente pelo valor.

    Returns:
        ``(ks, auc)`` com ``auc = P(x_mau > x_bom) + 0.5·P(empate)``. Pode ser < 0.5
        quando valores maiores indicam menos risco.
    """
    if tabela.is_empty():
        return None, None
    w1 = tabela["w1"].to_numpy().astype(float)
    w0 = tabela["w0"].to_numpy().astype(float)
    tot1, tot0 = w1.sum(), w0.sum()
    if tot1 <= 0 or tot0 <= 0:
        return None, None
    cum0_antes = np.concatenate(([0.0], np.cumsum(w0)[:-1]))
    auc = float(np.sum(w1 * (cum0_antes + 0.5 * w0)) / (tot1 * tot0))
    ks = float(np.max(np.abs(np.cumsum(w1) / tot1 - np.cumsum(w0) / tot0)))
    return ks, auc


def entropia(freqs: Sequence[float]) -> tuple[float | None, float | None]:
    """Entropia (nats) e entropia normalizada (0–1) de uma distribuição de frequências."""
    arr = np.asarray([f for f in freqs if f and f > 0], dtype=float)
    if arr.size == 0:
        return None, None
    p = arr / arr.sum()
    h = float(-(p * np.log(p)).sum())
    k = arr.size
    hn = h / math.log(k) if k > 1 else 0.0
    return round(h, 6), round(hn, 6)


def psi(pct_ref: Sequence[float], pct_atual: Sequence[float], eps: float = 1e-6) -> float:
    """Population Stability Index entre duas distribuições (em proporções 0–1)."""
    a = np.clip(np.asarray(pct_ref, dtype=float), eps, None)
    b = np.clip(np.asarray(pct_atual, dtype=float), eps, None)
    return float(np.sum((b - a) * np.log(b / a)))


def classificar_psi(valor: float | None, atencao: float, instavel: float) -> str | None:
    if valor is None:
        return None
    if valor < atencao:
        return "estavel"
    if valor <= instavel:
        return "atencao"
    return "instavel"


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman entre dois vetores pequenos (usa ``scipy`` se disponível)."""
    if len(x) < 3 or len(y) < 3 or len(x) != len(y):
        return None
    try:
        from scipy.stats import spearmanr

        r = spearmanr(x, y).statistic
        return None if r is None or np.isnan(r) else float(r)
    except Exception:  # pragma: no cover - scipy é dependência obrigatória
        return None


def meses_entre(inicio: str, fim: str) -> list[str]:
    """Lista de safras ``yyyy-MM`` entre duas safras (inclusive)."""
    a_ano, a_mes = int(inicio[:4]), int(inicio[5:7])
    b_ano, b_mes = int(fim[:4]), int(fim[5:7])
    saida = []
    while (a_ano, a_mes) <= (b_ano, b_mes):
        saida.append(f"{a_ano:04d}-{a_mes:02d}")
        a_mes += 1
        if a_mes > 12:
            a_mes = 1
            a_ano += 1
    return saida


def garantir_dependencia(modulo: str, funcionalidade: str) -> object | None:
    """Importa dependência opcional; devolve ``None`` com warning se ausente."""
    import importlib
    import warnings

    try:
        return importlib.import_module(modulo)
    except ImportError:
        warnings.warn(
            f"Dependência opcional '{modulo}' não instalada; '{funcionalidade}' será ignorada.",
            RuntimeWarning,
            stacklevel=3,
        )
        return None
