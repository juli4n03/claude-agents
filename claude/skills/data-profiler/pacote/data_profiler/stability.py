"""Estabilidade da base por safra (seção 6) e utilitários de PSI reutilizados na seção 7.6."""

from __future__ import annotations

from typing import Sequence

import polars as pl

from ._types import COD_NULO, BinSpec
from ._utils import classificar_psi, logger, psi
from .backends import BaseBackend
from .config import PoderPreditivoConfig, ProfileConfig

SCHEMA_PSI = {"variavel": pl.Utf8, "safra": pl.Utf8, "safra_referencia": pl.Utf8, "psi": pl.Float64, "classificacao_psi": pl.Utf8}


def psi_por_safra(longa: pl.DataFrame, safra_referencia: str, atencao: float, instavel: float) -> pl.DataFrame:
    """PSI de cada variável em cada safra vs. a safra de referência.

    ``longa`` deve ter ``variavel, faixa, safra, qtd``. Faixas ausentes numa safra
    entram com frequência zero (truncadas em ``eps`` no cálculo).
    """
    if not longa.height:
        return pl.DataFrame(schema=SCHEMA_PSI)
    linhas = []
    for var in longa["variavel"].unique().sort().to_list():
        sub = longa.filter(pl.col("variavel") == var)
        pivot = sub.pivot(on="safra", index="faixa", values="qtd", aggregate_function="sum").fill_null(0.0)
        if safra_referencia not in pivot.columns:
            logger.warning("Safra de referência %s sem dados para %s; PSI ignorado.", safra_referencia, var)
            continue
        ref = pivot[safra_referencia].to_numpy()
        ref = ref / ref.sum() if ref.sum() else ref
        for safra in [c for c in pivot.columns if c != "faixa"]:
            atual = pivot[safra].to_numpy()
            atual = atual / atual.sum() if atual.sum() else atual
            valor = round(psi(ref, atual), 6)
            linhas.append({"variavel": var, "safra": safra, "safra_referencia": safra_referencia, "psi": valor, "classificacao_psi": classificar_psi(valor, atencao, instavel)})
    return pl.DataFrame(linhas, schema=SCHEMA_PSI).sort(["variavel", "safra"])


def specs_para_psi(be_ref: BaseBackend, numericas: Sequence[str], categoricas: Sequence[str], n_decis: int, max_categorias: int) -> list[BinSpec]:
    """Decis da referência para numéricas e top-K categorias para categóricas."""
    probs = [i / n_decis for i in range(1, n_decis)]
    quantis = be_ref.quantis(numericas, probs) if numericas else {}
    specs = [BinSpec(c, "numerica", cortes=sorted({float(v) for v in quantis[c] if v is not None}), metodo="quantil") for c in numericas]
    specs += [BinSpec(c, "categorica", categorias=[v for v in be_ref.top_categorias(c, max_categorias) if v is not None]) for c in categoricas]
    return specs


def estabilidade_por_safra(
    be: BaseBackend,
    coluna_safra: str,
    safra_referencia: str | None,
    target: str | None,
    numericas: Sequence[str],
    categoricas: Sequence[str],
    config: ProfileConfig,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame | None]:
    """Preenchimento por safra, PSI vs. referência e bad rate por safra."""
    numericas = [c for c in numericas if c != coluna_safra and c != target]
    categoricas = [c for c in categoricas if c != coluna_safra and c != target and c not in numericas]

    preench = be.preenchimento_por_safra(coluna_safra, numericas, categoricas)
    if config.estabilidade_moda_categoricas and categoricas:
        modas = []
        for c in categoricas:
            m = be.moda_por_safra(coluna_safra, c).with_columns(pl.lit(c).alias("variavel"))
            modas.append(m)
        if modas:
            preench = preench.join(pl.concat(modas), on=["safra", "variavel"], how="left")
    if "moda" not in preench.columns:
        preench = preench.with_columns(pl.lit(None, dtype=pl.Utf8).alias("moda"), pl.lit(None, dtype=pl.Float64).alias("pct_moda"))
    preench = preench.select("safra", "variavel", "contagem", "pct_nulos", "media", "moda", "pct_moda").sort(["variavel", "safra"])

    safras = preench["safra"].unique().sort().to_list()
    if not safras:
        return preench, pl.DataFrame(schema=SCHEMA_PSI), None
    ref = safra_referencia or safras[0]
    be_ref = be.filtrar_safra(coluna_safra, ref)
    specs = specs_para_psi(be_ref, numericas, categoricas, config.n_decis_psi, config.max_distintos_alta_cardinalidade)
    longa = be.tabela_faixas_longa(specs, None, None, coluna_safra, None) if specs else pl.DataFrame()
    psi_df = psi_por_safra(longa, ref, config.psi_atencao, config.psi_instavel)

    badrate = None
    if target:
        badrate = be.media_target_por_safra(coluna_safra, target, None).rename({"media_target": "bad_rate"}).with_columns((pl.col("bad_rate") * 100).round(4))
    return preench, psi_df, badrate
