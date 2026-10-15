"""Paridade Polars × Spark: schema idêntico; valores iguais, com tolerância onde o Spark é aproximado."""

from __future__ import annotations

import math

import polars as pl
import pytest

from data_profiler import correlate, poder_preditivo, profile

pytestmark = pytest.mark.spark

COLS_PP = ["score_bureau", "renda", "vazada", "uf", "com_nulos", "flag_restricao", "texto_vazio", "aleatoria"]
# ``nulos`` e ``nan_count`` são comparados somados: a conversão pandas -> Spark transforma NaN em null.
EXATAS_NUM = ["contagem", "inf_count", "zeros", "negativos", "variavel_eleita", "tipo_detectado", "quase_constante"]
APROX_NUM = {"media": 1e-6, "desvio_padrao": 1e-6, "variancia": 1e-3, "minimo": 1e-9, "maximo": 1e-9, "assimetria": 1e-4, "curtose": 1e-4}


def _comparar(a: pl.DataFrame, b: pl.DataFrame, chave: list[str], exatas: list[str], aprox: dict[str, float]):
    assert a.columns == b.columns, "schema deve ser idêntico entre backends"
    j = a.join(b, on=chave, suffix="_spark")
    assert j.height == a.height == b.height
    for c in exatas:
        assert j[c].to_list() == j[f"{c}_spark"].to_list(), c
    for c, tol in aprox.items():
        for x, y in zip(j[c].to_list(), j[f"{c}_spark"].to_list()):
            if x is None or y is None:
                assert x is None and y is None, c
            else:
                assert math.isclose(x, y, rel_tol=tol, abs_tol=tol), (c, x, y)


@pytest.fixture(scope="module")
def perfis(base, base_spark):
    return profile(base, coluna_safra="dt_ref", target="target"), profile(base_spark, coluna_safra="dt_ref", target="target")


def test_profile_numericas(perfis):
    p, s = perfis
    assert s.metadados["backend"] == "spark" and s.metadados["exato"] is False
    _comparar(p.numericas, s.numericas, ["variavel"], EXATAS_NUM, APROX_NUM)
    j = p.numericas.join(s.numericas, on="variavel", suffix="_spark")
    assert (j["nulos"] + j["nan_count"]).to_list() == (j["nulos_spark"] + j["nan_count_spark"]).to_list()
    # percentis aproximados: desvio medido em desvios-padrão da variável (critério correto perto de zero)
    for c in ("p_25", "p_50", "p_75"):
        for x, y, dp in zip(j[c].to_list(), j[f"{c}_spark"].to_list(), j["desvio_padrao"].to_list()):
            if x is not None and y is not None and dp:
                assert abs(x - y) <= 0.02 * dp, (c, x, y)
    # distintos aproximados (rsd 1%)
    for x, y in zip(j["distintos"].to_list(), j["distintos_spark"].to_list()):
        assert abs(x - y) <= max(3, 0.03 * x)


def test_profile_categoricas_datas(perfis):
    p, s = perfis
    _comparar(p.categoricas, s.categoricas, ["variavel"], ["contagem", "nulos", "vazios", "moda", "pct_moda", "constante", "variavel_eleita", "tipo_sugerido"], {"entropia": 1e-3, "tamanho_medio": 1e-6})
    _comparar(p.datas_resumo, s.datas_resumo, ["variavel"], ["contagem", "data_min", "data_max", "qtd_safras", "datas_futuras", "datas_invalidas"], {})
    _comparar(p.datas_safra, s.datas_safra, ["variavel", "safra"], ["contagem", "pct_total"], {"variacao_mom": 1e-6})
    _comparar(p.bad_rate_safra, s.bad_rate_safra, ["safra"], ["contagem"], {"bad_rate": 1e-6})
    psi_p = p.psi.filter(pl.col("variavel") == "uf")
    psi_s = s.psi.filter(pl.col("variavel") == "uf")
    _comparar(psi_p, psi_s, ["variavel", "safra"], ["classificacao_psi"], {"psi": 1e-6})  # categórica: PSI exato nos dois


@pytest.fixture(scope="module")
def poderes(base, base_spark):
    return poder_preditivo(base, "target", colunas=COLS_PP, coluna_safra="dt_ref"), poder_preditivo(base_spark, "target", colunas=COLS_PP, coluna_safra="dt_ref")


def test_poder_preditivo_resumo(poderes):
    p, s = poderes
    assert p.resumo.columns == s.resumo.columns and p.faixas.columns == s.faixas.columns
    j = p.resumo.join(s.resumo, on="variavel", suffix="_spark")
    assert j["recomendacao"].to_list() == j["recomendacao_spark"].to_list()
    assert j["suspeita_vazamento"].to_list() == j["suspeita_vazamento_spark"].to_list()
    for c, tol in {"iv": 0.03, "gini_woe": 0.02, "ks_faixas": 0.02, "cv_iv": 0.05, "psi_faixas_max": 0.01}.items():
        for x, y in zip(j[c].to_list(), j[f"{c}_spark"].to_list()):
            if x is not None and y is not None:
                assert abs(x - y) <= tol * max(1.0, abs(x)), (c, x, y)
    # KS/AUC brutos: Spark aproxima por 200 faixas finas
    for c in ("ks_bruto", "gini_bruto"):
        for x, y in zip(j[c].to_list(), j[f"{c}_spark"].to_list()):
            if x is not None:
                assert abs(x - y) < 0.02, (c, x, y)
    assert j["ponto_bisserial"].to_list() == pytest.approx(j["ponto_bisserial_spark"].to_list(), abs=1e-6)


def test_poder_preditivo_categorica_exata(poderes):
    p, s = poderes
    fp = p.faixas.filter(pl.col("variavel") == "uf").sort("faixa")
    fs = s.faixas.filter(pl.col("variavel") == "uf").sort("faixa")
    assert fp["faixa"].to_list() == fs["faixa"].to_list()
    assert fp["qtd"].to_list() == fs["qtd"].to_list() and fp["woe"].to_list() == fs["woe"].to_list()
    assert p.cortes["uf"] == s.cortes["uf"]


def test_aplicar_woe_spark(poderes, base_spark):
    _, s = poderes
    out = s.aplicar_woe(base_spark.limit(20), colunas=["score_bureau", "uf"])
    pdf = out.select("uf", "uf_woe", "score_bureau_woe").toPandas()
    assert pdf["uf_woe"].notna().all() and pdf["score_bureau_woe"].notna().all()


def test_correlacao(base, base_spark):
    cols = ["score_bureau", "renda", "vazada", "uf", "texto_vazio"]
    p = correlate(base, colunas=cols)
    s = correlate(base_spark, colunas=cols)
    assert p.pares.columns == s.pares.columns
    jp = p.pares.sort(["metodo", "var_1", "var_2"])
    js = s.pares.sort(["metodo", "var_1", "var_2"])
    assert jp.select("metodo", "var_1", "var_2").rows() == js.select("metodo", "var_1", "var_2").rows()
    for x, y in zip(jp["valor"].to_list(), js["valor"].to_list()):
        assert math.isclose(x, y, abs_tol=1e-6)
    assert "listwise" in s.metadados["obs"][0]
