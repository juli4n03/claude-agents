"""Testes do poder preditivo (Polars)."""

from __future__ import annotations

import json

import polars as pl
import pytest

from data_profiler import PoderPreditivoConfig, PoderPreditivoResult, poder_preditivo, profile

COLS = ["score_bureau", "renda", "aleatoria", "vazada", "flag_restricao", "uf", "com_nulos", "constante", "texto_vazio"]


@pytest.fixture(scope="module")
def res(base, base_validacao):
    return poder_preditivo(base, "target", colunas=COLS, coluna_safra="dt_ref", df_validacao=base_validacao)


def _linha(df: pl.DataFrame, var: str) -> dict:
    return df.filter(pl.col("variavel") == var).row(0, named=True)


def test_metadados_target(res):
    m = res.metadados
    assert m["tipo_target"] == "binario" and m["linhas_validas"] == 20_000
    assert m["qtd_maus"] + m["qtd_bons"] == 20_000 and 15 < m["bad_rate_geral"] < 35
    assert len(m["bad_rate_por_safra"]) == 12


def test_variavel_forte_monotonica(res):
    s = _linha(res.resumo, "score_bureau")
    assert s["iv"] > 0.3 and s["classificacao_iv"] in ("forte", "medio")
    assert s["monotonicidade"] == -1.0 and s["qtd_inversoes"] == 0
    assert s["direcao"] == "valor maior -> menos risco" and s["gini_bruto"] > 0.3
    assert s["recomendacao"] == "Selecionar" and s["ranking"] <= 2
    assert s["p_valor_mann_whitney"] < 1e-6 and s["ponto_bisserial"] < -0.2
    assert s["cv_iv"] < 0.3 and s["estabilidade_ordem"] == 100.0 and s["gini_validacao"] > 0.25


def test_variavel_aleatoria(res):
    a = _linha(res.resumo, "aleatoria")
    assert a["iv"] < 0.02 and a["classificacao_iv"] == "sem poder" and a["recomendacao"] == "Descartar"
    assert a["p_valor_mann_whitney"] > 0.01


def test_vazamento_sinalizado(res):
    v = _linha(res.resumo, "vazada")
    assert v["suspeita_vazamento"] is True and v["iv"] > 0.5 and v["auc_bruto"] > 0.85
    assert v["recomendacao"] == "Revisar" and "vazamento" in v["obs"]
    # também por nome
    import numpy as np

    rng = np.random.default_rng(0)
    base_nome = pl.DataFrame({"x": rng.normal(size=300), "dt_pagamento_atraso": rng.normal(size=300), "t": [i % 2 for i in range(300)]})
    r = poder_preditivo(base_nome, "t", colunas=["x", "dt_pagamento_atraso"])
    assert _linha(r.resumo, "dt_pagamento_atraso")["suspeita_vazamento"]
    assert not _linha(r.resumo, "x")["suspeita_vazamento"]


def test_tabela_faixas(res):
    f = res.faixas.filter(pl.col("variavel") == "score_bureau")
    assert f.height == 10 and f["ordem_faixa"].to_list() == list(range(10))
    assert abs(f["pct_qtd"].sum() - 100) < 0.5 and (f["pct_qtd"] >= 5).all()
    assert abs(f["iv_faixa"].sum() - _linha(res.resumo, "score_bureau")["iv"]) < 1e-6
    # WoE positivo = concentra bons (bad rate baixa)
    assert f.sort("woe")["bad_rate"].to_list() == sorted(f["bad_rate"].to_list(), reverse=True)
    assert f["maus_acum"][-1] == 100.0 and f["bons_acum"][-1] == 100.0
    nulo = res.faixas.filter((pl.col("variavel") == "com_nulos") & (pl.col("codigo") == "NULO"))
    assert nulo.height == 1 and abs(nulo["pct_qtd"][0] - 20.25) < 0.1
    assert _linha(res.resumo, "com_nulos")["poder_do_nulo"] is not None


def test_categoricas_e_outros(res):
    uf = res.faixas.filter(pl.col("variavel") == "uf")
    assert "OUTROS" in uf["faixa"].to_list()  # AC e RR (< 5%) agrupadas
    u = _linha(res.resumo, "uf")
    assert u["cramer_v"] is not None and u["p_valor_qui_quadrado"] < 0.01 and u["iv"] > 0.02
    assert _linha(res.resumo, "constante")["iv"] == 0.0


def test_por_safra_e_cortes(res):
    s = res.por_safra.filter(pl.col("variavel") == "score_bureau")
    assert s.height == 12 and s.filter(pl.col("referencia"))["psi_faixas"][0] == 0.0
    assert set(res.cortes) == set(COLS)
    js = json.loads(res.cortes_json())
    assert len(js["score_bureau"]["spec"]["cortes"]) == 9


def test_aplicar_woe_e_reconstrucao(res, base_validacao):
    out = res.aplicar_woe(base_validacao, colunas=["score_bureau", "uf"])
    assert "score_bureau_woe" in out.columns and out["score_bureau_woe"].null_count() == 0
    mapa = {r["faixa"]: r["woe"] for r in res.faixas.filter(pl.col("variavel") == "uf").iter_rows(named=True)}
    linha = out.filter(pl.col("uf") == "SP").row(0, named=True)
    assert abs(linha["uf_woe"] - mapa["SP"]) < 1e-9
    recon = PoderPreditivoResult.de_cortes(res.cortes_json())
    out2 = recon.aplicar_woe(base_validacao.head(100), colunas=["score_bureau"])
    assert out2["score_bureau_woe"].to_list() == out.head(100)["score_bureau_woe"].to_list()
    pdf_out = res.aplicar_woe(base_validacao.head(10).to_pandas(), colunas=["uf"])
    assert "uf_woe" in pdf_out.columns  # pandas entra, pandas sai


def test_reaproveita_profile(base):
    # score com 10% de nulos: tem poder preditivo, mas não é eleita no profiling (nulos > 5%)
    df = base.with_columns(pl.when(pl.col("id_cliente") % 10 == 0).then(None).otherwise(pl.col("score_bureau")).alias("score_nulos"))
    pr = profile(df, ignorar=["target"])
    r = poder_preditivo(df, "target", colunas=["score_bureau", "score_nulos"], profile_result=pr)
    s = _linha(r.resumo, "score_nulos")
    assert s["iv"] > 0.02 and s["eleita_profiling"] is False and s["recomendacao"] == "Descartar"
    assert "nao eleita no profiling" in s["motivo_recomendacao"] and "nulos" in s["motivo_recomendacao"]


def test_peso_e_valores_especiais(base):
    df = base.with_columns(pl.when(pl.col("score_bureau") < 400).then(-1.0).otherwise(pl.col("score_bureau")).alias("score_bureau"))
    cfg = PoderPreditivoConfig(valores_especiais=(-1,))
    r = poder_preditivo(df, "target", colunas=["score_bureau"], coluna_peso="peso", config=cfg)
    f = r.faixas.filter(pl.col("variavel") == "score_bureau")
    assert "ESPECIAL_-1" in f["faixa"].to_list()
    assert abs(r.metadados["qtd_maus_ponderado"] - base.filter(pl.col("target") == 1)["peso"].sum()) < 1e-6


def test_mapa_target_e_min_maus(base):
    df = base.with_columns(pl.when(pl.col("target") == 1).then(pl.lit("mau")).otherwise(pl.lit("bom")).alias("t"))
    r = poder_preditivo(df, "t", colunas=["score_bureau"], config=PoderPreditivoConfig(mapa_target={"mau": 1, "bom": 0}))
    assert r.metadados["qtd_maus"] == base["target"].sum()
    with pytest.raises(ValueError, match="maus"):
        poder_preditivo(base.head(60), "target", colunas=["score_bureau"])


def test_target_continuo(base):
    r = poder_preditivo(base, "renda", colunas=["score_bureau", "aleatoria", "uf"], tipo_target="continuo")
    assert r.metadados["tipo_target"] == "continuo"
    assert {"media_target", "mediana_target", "desvio_target"} <= set(r.faixas.columns)
    s = _linha(r.resumo, "score_bureau")
    assert s["pearson"] is not None and s["eta"] is not None and s["r2_faixas"] is not None and s["informacao_mutua"] is not None


def test_binning_arvore(base):
    pytest.importorskip("sklearn")
    r = poder_preditivo(base, "target", colunas=["score_bureau"], config=PoderPreditivoConfig(metodo_binning="arvore"))
    assert _linha(r.resumo, "score_bureau")["metodo_binning"] == "arvore" and _linha(r.resumo, "score_bureau")["iv"] > 0.3


def test_excel(res, tmp_path):
    p = res.to_excel(str(tmp_path / "pp.xlsx"))
    assert (tmp_path / "pp.xlsx").exists()
