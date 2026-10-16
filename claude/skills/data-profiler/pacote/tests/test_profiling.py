"""Testes do profiling (Polars) com casos de borda."""

from __future__ import annotations

import datetime as dt
import json
import math

import pandas as pd
import polars as pl
import pytest

from data_profiler import ProfileConfig, profile


@pytest.fixture(scope="module")
def res(base):
    return profile(base, coluna_safra="dt_ref", target="target")


def _linha(df: pl.DataFrame, var: str) -> dict:
    return df.filter(pl.col("variavel") == var).row(0, named=True)


def test_schema_e_metadados(res):
    assert res.metadados["backend"] == "polars"
    assert res.metadados["total_linhas"] == 20_000
    assert {"variavel", "variavel_eleita", "motivo_nao_eleita", "p_50", "iqr", "quase_constante"} <= set(res.numericas.columns)
    assert {"top_n", "entropia_normalizada", "pct_valores_suspeitos"} <= set(res.categoricas.columns)
    assert set(res.datas_safra.columns) == {"variavel", "safra", "contagem", "pct_total", "variacao_mom"}


def test_identificador_e_constante(res):
    assert _linha(res.numericas, "id_cliente")["tipo_detectado"] == "identificador"
    assert _linha(res.numericas, "id_cliente")["variavel_eleita"] == "Nao Eleita"
    # contínuas com casas decimais nunca são identificador
    assert _linha(res.numericas, "renda")["tipo_detectado"] == "numerica"
    const = _linha(res.categoricas, "constante")
    assert const["constante"] and const["variavel_eleita"] == "Nao Eleita" and "constante" in const["motivo_nao_eleita"]


def test_nulos_nan_inf(res):
    nulos = _linha(res.numericas, "com_nulos")
    assert nulos["flag_pct_nulo"] == 1 and nulos["variavel_eleita"] == "Nao Eleita" and "nulos" in nulos["motivo_nao_eleita"]
    assert abs(nulos["pct_nulos"] - 20.25) < 0.01
    nan = _linha(res.numericas, "com_nan")
    assert nan["nan_count"] == 200 and nan["inf_count"] == 20
    assert nan["contagem"] == 20_000 - 220 and math.isfinite(nan["media"]) and math.isfinite(nan["maximo"])


def test_baixa_cardinalidade_em_ambas_tabelas(res):
    assert _linha(res.numericas, "flag_restricao")["tratada_como_categorica"] is True
    assert "flag_restricao" in res.categoricas["variavel"].to_list()


def test_categoricas(res):
    tv = _linha(res.categoricas, "texto_vazio")
    assert tv["vazios"] > 0 and tv["pct_valores_suspeitos"] > 0
    uf = _linha(res.categoricas, "uf")
    top = json.loads(uf["top_n"])
    assert top[0]["valor"] == "SP" and uf["moda"] == "SP"
    assert 0 < uf["entropia_normalizada"] < 1
    assert _linha(res.categoricas, "str_numero")["tipo_sugerido"] == "numerica"


def test_datas(res):
    ev = _linha(res.datas_resumo, "dt_evento")
    assert ev["datas_futuras"] == 50 and ev["datas_invalidas"] == 10
    ref = _linha(res.datas_resumo, "dt_ref")
    assert ref["qtd_safras"] == 12 and ref["qtd_safras_faltantes"] == 0
    safras = res.datas_safra.filter(pl.col("variavel") == "dt_ref")
    assert abs(safras["pct_total"].sum() - 100) < 0.5 and safras["variacao_mom"][0] is None


def test_estabilidade_e_psi(res):
    assert res.estabilidade is not None and res.psi is not None and res.bad_rate_safra is not None
    psi = res.psi.filter(pl.col("variavel") == "score_bureau")
    assert psi.height == 12 and (psi["psi"] < 0.10).all()
    assert psi.filter(pl.col("safra") == "2024-01")["psi"][0] == 0.0
    assert 15 < res.bad_rate_safra["bad_rate"].mean() < 35


def test_resumo_eleitas(res):
    assert "score_bureau" in res.eleitas() and "id_cliente" not in res.eleitas()
    r = res.resumo()
    assert r["qtd"].sum() == res.numericas.height + res.categoricas.height


def test_tipos_forcados_e_ignorar(base):
    r = profile(base, tipos={"str_numero": "numerica", "score_bureau": "categorica"}, ignorar=["vazada"])
    assert "str_numero" in r.numericas["variavel"].to_list()
    assert "score_bureau" in r.categoricas["variavel"].to_list()
    assert r.metadados["colunas_ignoradas"]["vazada"] == "ignorada pelo usuario"


def test_tabela_vazia_e_tipos_nao_suportados():
    vazio = pl.DataFrame({"a": pl.Series([], dtype=pl.Float64), "b": pl.Series([], dtype=pl.Utf8)})
    r = profile(vazio)
    assert r.numericas.height == 0 and r.metadados["total_linhas"] == 0
    com_lista = pl.DataFrame({"a": [1.0, 2.0, 3.0], "lista": [[1], [2], [3]]})
    r2 = profile(com_lista)
    assert r2.metadados["colunas_ignoradas"]["lista"] == "tipo nao suportado"
    assert r2.numericas.height == 1


def test_coluna_totalmente_nula_e_valor_unico():
    df = pl.DataFrame({"nula": pl.Series([None] * 200, dtype=pl.Float64), "unico": [5.0] * 200, "ok": list(range(200))})
    r = profile(df)
    n = r.numericas.filter(pl.col("variavel") == "nula").row(0, named=True)
    assert n["motivo_nao_eleita"] == "totalmente nula" and n["pct_nulos"] == 100.0
    u = r.numericas.filter(pl.col("variavel") == "unico").row(0, named=True)
    assert u["quase_constante"] and not u["variabilidade"] and "valor unico" in u["obs"]


def test_entrada_pandas_e_warning_tamanho(base):
    pdf = base.head(500).to_pandas()
    assert isinstance(pdf, pd.DataFrame)
    with pytest.warns(RuntimeWarning, match="max_linhas_polars"):
        r = profile(pdf, config=ProfileConfig(max_linhas_polars=100))
    assert r.metadados["backend"] == "polars" and r.metadados["total_linhas"] == 500


def test_exportacoes(res, tmp_path):
    xlsx = res.to_excel(str(tmp_path / "p.xlsx"))
    arquivos = res.to_parquet(str(tmp_path / "pq"))
    assert xlsx.endswith(".xlsx") and (tmp_path / "p.xlsx").exists()
    assert any(a.endswith("numericas.parquet") for a in arquivos)
    assert pl.read_parquet(tmp_path / "pq" / "numericas.parquet").height == res.numericas.height
