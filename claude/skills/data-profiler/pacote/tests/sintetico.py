"""Gerador de base sintética de crédito com casos de borda conhecidos."""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl


def gerar_base(n: int = 20_000, seed: int = 7, n_safras: int = 12) -> pl.DataFrame:
    """Base sintética.

    Variáveis e comportamento esperado:
        * ``score_bureau``: relação forte e monotônica com o target (IV alto, decrescente).
        * ``renda``: relação fraca.
        * ``aleatoria``: sem relação (IV ≈ 0).
        * ``vazada``: cópia ruidosa do target → deve ser sinalizada em ``suspeita_vazamento``.
        * ``flag_restricao``: 0/1 (numérica de baixa cardinalidade → também categórica).
        * ``uf``: categórica com bad rate distinto por UF e categorias raras.
        * ``constante``: valor único.
        * ``id_cliente``: identificador.
        * ``com_nulos`` (20% nulos), ``com_nan`` (NaN e inf), ``texto_vazio`` ("" e "N/A").
        * ``dt_ref``: safra; ``dt_evento``: datas futuras e inválidas; ``str_numero``: string numérica.
    """
    rng = np.random.default_rng(seed)
    score = rng.normal(600, 100, n).clip(300, 1000)
    renda = np.exp(rng.normal(8, 0.6, n))
    ufs = rng.choice(["SP", "MG", "RJ", "BA", "RS", "AC", "RR"], size=n, p=[0.35, 0.2, 0.2, 0.1, 0.1, 0.03, 0.02])
    efeito_uf = np.select([ufs == "RJ", ufs == "BA", ufs == "AC"], [0.6, 0.4, 1.0], 0.0)
    logit = -1.5 - 0.007 * (score - 600) - 0.15 * (np.log(renda) - 8) + efeito_uf
    p = 1 / (1 + np.exp(-logit))
    target = rng.binomial(1, p)
    vazada = np.where(rng.random(n) < 0.05, 1 - target, target).astype(float) + rng.normal(0, 0.05, n)
    com_nan = rng.normal(0, 1, n)
    com_nan[: n // 100] = np.nan
    com_nan[n // 100 : n // 100 + 20] = np.inf
    com_nulos = rng.normal(50, 10, n)
    mask_nulo = rng.random(n) < 0.2
    inicio = dt.date(2024, 1, 1)
    meses = rng.integers(0, n_safras, n)
    dt_ref = [dt.date(inicio.year + (inicio.month - 1 + int(m)) // 12, (inicio.month - 1 + int(m)) % 12 + 1, 1) for m in meses]
    dt_evento = [d + dt.timedelta(days=int(x)) for d, x in zip(dt_ref, rng.integers(0, 60, n))]
    dt_evento[:50] = [dt.date(2099, 1, 1)] * 50
    dt_evento[50:60] = [dt.date(1800, 1, 1)] * 10
    texto = rng.choice(["ok", "pendente", "", "  ", "N/A", "aprovado"], size=n, p=[0.4, 0.3, 0.1, 0.05, 0.05, 0.1])
    df = pl.DataFrame(
        {
            "id_cliente": np.arange(1, n + 1),
            "dt_ref": dt_ref,
            "dt_evento": dt_evento,
            "score_bureau": score.round(0),
            "renda": renda.round(2),
            "aleatoria": rng.random(n),
            "vazada": vazada,
            "flag_restricao": rng.binomial(1, 0.15, n),
            "uf": ufs,
            "constante": ["A"] * n,
            "com_nulos": com_nulos,
            "com_nan": com_nan,
            "texto_vazio": texto,
            "str_numero": [str(v) for v in rng.integers(100, 999, n)],
            "peso": rng.uniform(0.5, 1.5, n).round(3),
            "target": target,
        }
    ).with_columns(pl.when(pl.Series(mask_nulo)).then(None).otherwise(pl.col("com_nulos")).alias("com_nulos"))
    return df
