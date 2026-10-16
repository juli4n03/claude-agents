"""Testes da função de correlação/associação."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from data_profiler import CorrelacaoConfig, correlate, profile
from data_profiler.correlation import cramer_v_corrigido

COLS = ["score_bureau", "renda", "vazada", "target", "uf", "texto_vazio", "flag_restricao", "com_nulos"]


@pytest.fixture(scope="module")
def res(base):
    pr = profile(base)
    return correlate(base, colunas=COLS, metodos=("pearson", "spearman", "cramer_v", "qui_quadrado", "informacao_mutua"), profile_result=pr, tipos={"flag_restricao": "categorica"})


def _par(df, a, b, metodo):
    f = df.filter((pl.col("metodo") == metodo) & (((pl.col("var_1") == a) & (pl.col("var_2") == b)) | ((pl.col("var_1") == b) & (pl.col("var_2") == a))))
    return f.row(0, named=True)


def test_schema_e_metodos(res):
    assert set(res.pares.columns) == {"var_1", "var_2", "tipo_par", "metodo", "valor", "p_valor", "n_obs", "obs"}
    assert {"pearson", "spearman", "cramer_v", "qui_quadrado", "eta", "informacao_mutua"} <= set(res.matrizes)


def test_pearson_pairwise(res, base):
    p = _par(res.pares, "vazada", "target", "pearson")
    esperado = np.corrcoef(base["vazada"].to_numpy(), base["target"].to_numpy())[0, 1]
    assert abs(p["valor"] - esperado) < 1e-6 and p["n_obs"] == 20_000 and p["p_valor"] < 1e-6
    q = _par(res.pares, "score_bureau", "com_nulos", "pearson")
    assert q["n_obs"] == 20_000 - base["com_nulos"].null_count()  # pairwise deletion


def test_pares_altos_e_vif(res):
    altos = res.pares_altos.filter(pl.col("metodo") == "pearson")
    assert {"vazada", "target"} == set(altos.row(0, named=True)[k] for k in ("var_1", "var_2"))
    assert res.vif is not None and (res.vif["vif"] >= 1).all()
    assert res.vif.filter(pl.col("variavel") == "vazada")["vif"][0] > res.vif.filter(pl.col("variavel") == "renda")["vif"][0]


def test_cramer_v_bergsma_independentes():
    rng = np.random.default_rng(1)
    tab = rng.multinomial(5000, [1 / 12] * 12).reshape(3, 4).astype(float)
    v, chi2, dof, p, pct = cramer_v_corrigido(tab)
    assert dof == 6 and v < 0.05  # correção leva V de variáveis independentes para ~0
    perfeito = np.diag([100.0, 100.0, 100.0])
    assert cramer_v_corrigido(perfeito)[0] > 0.95


def test_qui_quadrado_e_eta(res):
    q = _par(res.pares, "uf", "texto_vazio", "qui_quadrado")
    assert q["obs"].startswith("gl=") and 0 <= q["p_valor"] <= 1
    e = _par(res.pares, "score_bureau", "uf", "eta")
    assert 0 <= e["valor"] <= 1
    assert res.pares.filter(pl.col("metodo") == "ponto_bisserial").height >= 1  # categóricas binárias


def test_listwise_e_amostra(base):
    r = correlate(base, colunas=["score_bureau", "com_nulos", "renda"], metodos=("pearson",), config=CorrelacaoConfig(tratamento_nulos="listwise"))
    assert (r.pares["n_obs"] == 20_000 - base["com_nulos"].null_count()).all()
    r2 = correlate(base, colunas=["score_bureau", "renda"], metodos=("spearman",), amostra=0.2)
    assert r2.metadados["amostral"] is True and 3_500 < r2.pares["n_obs"][0] < 4_500


def test_matriz_simetrica(res):
    m = res.matrizes["pearson"]
    vars_ = m["variavel"].to_list()
    arr = m.select(vars_).to_numpy()
    assert np.allclose(arr, arr.T, equal_nan=True) and np.allclose(np.diag(arr), 1.0)


def test_excel(res, tmp_path):
    pytest.importorskip("openpyxl")
    res.to_excel(str(tmp_path / "c.xlsx"))
    assert (tmp_path / "c.xlsx").exists()
