"""Testes do relatorio_ds: gera os PDFs numa base sintética completa."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kit_ds as k  # noqa: E402
import relatorio_ds as r  # noqa: E402

pytest.importorskip("matplotlib")


@pytest.fixture(scope="module")
def cenario():
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.default_rng(11)
    n = 12_000
    df = pd.DataFrame({f"f{i}": rng.normal(size=n) for i in range(5)})
    df.loc[rng.random(n) < 0.06, "f2"] = -9999
    df.loc[rng.random(n) < 0.04, "f3"] = np.nan
    df["s"] = rng.integers(0, 15, n)
    df["safra"] = (pd.Period("2025-01", "M") + df["s"]).astype(str)
    logit = -2.2 + 0.9 * df.f0 - 0.6 * df.f1 + 0.4 * (df.f2 == -9999)
    df["alvo"] = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(float)
    df["alvo_alt"] = (rng.random(n) < 1 / (1 + np.exp(-(logit - 0.7)))).astype(float)
    df["amostra"] = np.select([df.s >= 12, df.s >= 10], ["OOT", "VAL"], "DES")
    df["segmento"] = rng.choice(["novo", "recorrente"], n)
    df["score_bureau"] = -(0.8 * df.f0 - 0.3 * df.f1) * 100 + rng.normal(0, 70, n)   # alto = bom
    df.loc[rng.random(n) < 0.1, "score_bureau"] = np.nan
    feats = [f"f{i}" for i in range(5)]
    des = df[df.amostra == "DES"]
    m = lgb.LGBMClassifier(n_estimators=120, learning_rate=0.05, verbose=-1, random_state=1).fit(des[feats], des["alvo"])
    df["pd"] = m.predict_proba(df[feats])[:, 1]
    return df, m, feats


def test_relatorio_completo(cenario, tmp_path):
    pytest.importorskip("shap")
    df, m, feats = cenario
    res = r.relatorio_pdf(tmp_path / "modelo.pdf", df, "alvo", "pd", "amostra", "safra",
                          titulo="M1 sintético", info={"run_id": "teste", "seed": 42},
                          benchmarks={"bureau": ("score_bureau", False)}, alvos_alternativos=["alvo_alt"],
                          col_segmento="segmento", modelo=m, features=feats, sentinelas=(-9999,),
                          n_shap=1500, top_features=3, swap_contra="bureau", taxa_aprovacao=0.6)
    assert Path(res["caminho"]).stat().st_size > 50_000
    assert res["n_paginas"] >= 17
    t = res["tabelas"]
    met = t["metricas"]
    assert set(met["amostra"]) == {"DES", "VAL", "OOT"} and set(met["alvo"]) == {"alvo", "alvo_alt"}
    assert met.set_index(["alvo", "amostra"]).loc[("alvo", "VAL"), "tipo"] == "seleção (otimista)"
    b = t["benchmarks"]
    assert (b.groupby("amostra")["n"].nunique() == 1).all()           # mesma população por amostra
    assert b.set_index(["amostra", "score"]).loc[("OOT", "bureau"), "auc"] > 0.5   # direção fixa correta
    assert set(t["swap"]["quadrante"]) >= {"mantidos", "swap_in", "swap_out", "reprovados"}
    assert t["shap_importancia"]["feature"].iloc[0] == "f0"
    assert {"pd", "f0"} <= set(t["psi"]["variavel"])
    assert set(t["hosmer_lemeshow"]["amostra"]) == {"DES", "VAL", "OOT"}


def test_relatorio_minimo(cenario, tmp_path):
    df, _, _ = cenario
    res = r.relatorio_pdf(tmp_path / "min.pdf", df, "alvo", "pd", "amostra", "safra")
    assert res["n_paginas"] >= 6 and "benchmarks" not in res["tabelas"]


def test_hosmer_lemeshow_detecta_descalibracao():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.01, 0.3, 20_000)
    y = (rng.random(20_000) < p).astype(float)
    assert r.hosmer_lemeshow(y, p)["p_valor"] > 0.01
    assert r.hosmer_lemeshow(y, np.clip(p * 2, 0, 1))["p_valor"] < 1e-6


def test_curva_aprovacao_monotona():
    rng = np.random.default_rng(4)
    risco = rng.normal(size=5000)
    y = (rng.random(5000) < 1 / (1 + np.exp(-(risco - 2)))).astype(float)
    c = r.curva_aprovacao(y, risco)
    assert c["bad_rate_aprovados"].iloc[0] < c["bad_rate_aprovados"].iloc[-1]
    assert c["bad_rate_aprovados"].iloc[-1] == pytest.approx(y.mean())


def test_relatorio_variaveis(cenario, tmp_path):
    df, _, feats = cenario
    res = r.relatorio_variaveis_pdf(tmp_path / "vars.pdf", df, feats, "safra",
                                    mascara_treino=df["amostra"].eq("DES"), col_y="alvo",
                                    sentinelas=(-9999,))
    assert res["n_paginas"] == 1 + len(feats)
    resumo = res["resumo"].set_index("feature")
    assert resumo.loc["f2", "pct_sentinela"] > 4 and resumo.loc["f3", "pct_nulo"] > 2
    assert (resumo["psi_max"] < 0.10).all()                          # sem deriva na base sintética
