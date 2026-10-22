"""Testes do kit_ds com dados sintéticos de verdade conhecida."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kit_ds as k  # noqa: E402


@pytest.fixture(scope="module")
def base():
    rng = np.random.default_rng(0)
    n_cli = 3000
    linhas = []
    for c in range(n_cli):
        for s in rng.choice(np.arange(12), size=rng.integers(1, 4), replace=False):
            linhas.append((c, int(s)))
    df = pd.DataFrame(linhas, columns=["cliente", "safra_idx"])
    df["safra"] = pd.Period("2024-01", "M") + df["safra_idx"]
    df["x1"] = rng.normal(size=len(df))
    df["x2"] = rng.normal(size=len(df))
    df["ruido"] = rng.normal(size=len(df))
    logit = -2 + 1.2 * df["x1"] - 0.8 * df["x2"]
    df["target"] = (rng.random(len(df)) < 1 / (1 + np.exp(-logit))).astype(float)
    df.loc[df["safra_idx"] >= 10, "target"] = np.nan          # duas safras imaturas
    return df


# --- desenho amostral -------------------------------------------------------- #
def test_split_por_entidade_sem_vazamento(base):
    split = k.split_por_entidade(base, "cliente", frac_teste=0.3, seed=1)
    lados = base.assign(split=split).groupby("cliente")["split"].nunique()
    assert (lados == 1).all(), "cliente apareceu em treino e teste"
    frac = base.assign(split=split).groupby("cliente")["split"].first().eq("teste").mean()
    assert abs(frac - 0.3) < 0.02
    assert split.equals(k.split_por_entidade(base, "cliente", frac_teste=0.3, seed=1))


def test_maturacao(base):
    tab = k.tabela_maturacao(base, "safra", ["target"])
    assert set(tab.columns) >= {"safra", "alvo", "n", "pct_maduro", "bad_rate"}
    assert k.ultima_safra_madura(tab, "target") == pd.Period("2024-10", "M")


# --- montagem da base -------------------------------------------------------- #
def test_left_join_validado():
    esq = pd.DataFrame({"id": [1, 2, 3, 4], "safra": ["a", "a", "b", "b"]})
    dir_ = pd.DataFrame({"id": [1, 1, 3], "versao": [1, 2, 1], "v": [10, 11, 30]})
    with pytest.raises(ValueError, match="duplicada"):
        k.left_join_validado(esq, dir_, "id", "fonte")
    out, cob = k.left_join_validado(esq, dir_, "id", "fonte", col_safra="safra", ordem_dedup="versao")
    assert len(out) == 4
    assert out.set_index("id").loc[1, "v"] == 11           # ficou a versão mais recente
    assert out["match_fonte"].tolist() == [True, False, True, False]
    assert cob.loc["a", "pct_match"] == 50


def test_join_pit_respeita_lag():
    esq = pd.DataFrame({"id": [1, 1, 2], "data": pd.to_datetime(["2025-01-10", "2025-02-10", "2025-01-10"])})
    dir_ = pd.DataFrame({"id": [1, 1, 2], "data": pd.to_datetime(["2025-01-05", "2025-02-01", "2025-01-20"]),
                         "score": [100, 200, 300]})
    sem_lag = k.join_pit(esq, dir_, "id", "data", "data", "bureau")
    assert sem_lag["score"].tolist()[:2] == [100, 200]
    com_lag = k.join_pit(esq, dir_, "id", "data", "data", "bureau", lag_dias=15)
    assert pd.isna(com_lag["score"].iloc[0])                  # 05/01 + 15 dias > 10/01
    assert com_lag["score"].iloc[1] == 100                    # 01/02 + 15 > 10/02: usa a anterior
    assert not com_lag["pit_bureau"].iloc[2]                  # observação futura não entra
    assert com_lag.index.tolist() == esq.index.tolist()


def test_reconcilia_fontes():
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0, np.nan], "b": [1.0, 2.5, 3.0, 4.0]})
    r = k.reconcilia_fontes(df, "a", "b")
    assert r.loc[0, "n_overlap"] == 3 and abs(r.loc[0, "pct_igual"] - 200 / 3) < 1e-9
    assert r.loc[0, "diff_max"] == 0.5


def test_checa_vazamento():
    k.checa_vazamento(["x1", "renda"], {"alvo": ["ever\\d+"]})
    with pytest.raises(AssertionError, match="ever60"):
        k.checa_vazamento(["x1", "flag_ever60m3"], {"alvo": ["ever\\d+"], "chave": ["^cpf$"]})


# --- métricas ---------------------------------------------------------------- #
def test_metricas_nao_invertem(base):
    d = base.dropna(subset=["target"])
    risco = d["x1"] - d["x2"]
    m = k.metricas(d["target"], risco)
    assert m["auc"] > 0.7 and abs(m["gini"] - (2 * m["auc"] - 1)) < 1e-12
    with pytest.warns(UserWarning, match="direção"):
        inv = k.metricas(d["target"], k.orienta(risco, maior_eh_risco=False))
    assert inv["auc"] < 0.3 and inv["ks"] < 0.05           # KS não é "consertado" para 1-KS


def test_delta_pareado_detecta_ganho(base):
    d = base.dropna(subset=["target"])
    r = k.delta_pareado(d["target"], d["x1"], d["x1"] - d["x2"], n_boot=200)
    auc = r.set_index("metrica").loc["auc"]
    assert auc["delta"] > 0 and auc["ic_low"] > 0
    nulo = k.delta_pareado(d["target"], d["x1"], d["x1"] + 1e-9 * d["ruido"], n_boot=200)
    assert nulo.set_index("metrica").loc["auc", "ic_low"] <= 0 <= nulo.set_index("metrica").loc["auc", "ic_high"]


def test_compara_scores_mesma_populacao(base):
    d = base.dropna(subset=["target"]).copy()
    d["score_bom_alto"] = -(d["x1"] - d["x2"])                 # convenção de bureau: alto = bom
    d.loc[d.index[:500], "score_bom_alto"] = np.nan             # cobertura parcial
    tab, deltas = k.compara_scores(d, "target", {"base": ("x1", True), "bureau": ("score_bom_alto", False)},
                                   referencia="base", n_boot=100)
    assert tab["n"].nunique() == 1                              # mesmas linhas para todos
    assert tab.set_index("score").loc["bureau", "auc"] > tab.set_index("score").loc["base", "auc"]
    assert deltas.set_index("metrica").loc["auc", "delta"] > 0


def test_metricas_por_grupo_n_min(base):
    d = base.dropna(subset=["target"]).assign(risco=lambda t: t["x1"])
    t = k.metricas_por_grupo(d, "target", "risco", "safra", n_min=10_000)
    assert t["auc"].isna().all()


# --- decis e PSI ------------------------------------------------------------- #
def test_decis_e_estabilidade(base):
    cortes = k.cortes_decis(base["x1"], 10)
    faixa = k.aplica_decis(np.append(base["x1"].to_numpy(), np.nan), cortes)
    assert faixa[-1] == 0 and set(faixa[:-1]) == set(range(1, 11))
    est = k.estabilidade_decis(base.assign(f=faixa[:-1]), "safra", "f", "target")
    assert np.allclose(est["composicao_pct"].sum(axis=1), 100)


def test_psi_conta_nulos_e_sentinelas():
    rng = np.random.default_rng(1)
    a = rng.normal(size=20_000)
    assert k.psi(a, rng.normal(size=20_000)) < 0.01
    b = a.copy()
    b[:4000] = np.nan                                          # 20% passa a vir nulo
    assert k.psi(a, b) > 0.25
    c = a.copy()
    c[:4000] = -9999
    assert k.psi(a, c, especiais=(-9999,)) > 0.25


# --- swap ------------------------------------------------------------------- #
def test_swap(base):
    d = base.copy()
    d["atual"] = d["x1"]
    d["novo"] = d["x1"] - d["x2"]
    t = k.swap(d, "target", "atual", "novo", taxa_aprovacao=0.6).set_index("quadrante")
    quad = t.loc[["mantidos", "swap_in", "swap_out", "reprovados"]]
    assert quad["n"].sum() == len(d)
    assert abs(t.loc["carteira_atual", "pct"] - 60) < 0.5 and abs(t.loc["carteira_nova", "pct"] - 60) < 0.5
    assert t.loc["swap_in", "bad_rate"] < t.loc["swap_out", "bad_rate"]
    assert t.loc["swap_in", "n_sem_alvo"] > 0                   # imaturos ficam explícitos


# --- LightGBM ---------------------------------------------------------------- #
def _janelas(base):
    d = base.dropna(subset=["target"])
    feats = ["x1", "x2", "ruido"]
    opt, val = d[d["safra_idx"] < 8], d[d["safra_idx"].between(8, 9)]
    return d, feats, opt, val


def test_tuning_refit_poda(base):
    pytest.importorskip("lightgbm")
    pytest.importorskip("optuna")
    d, feats, opt, val = _janelas(base)
    res = k.tune_lgbm_temporal(opt[feats], opt["target"], val[feats], val["target"],
                               n_trials=4, gap_max=0.10, max_arvores=300, paciencia=20)
    assert res.best_iter > 0 and len(res.trials) == 4
    assert res.gap <= 0.10 or res.aviso
    res2 = k.tune_lgbm_temporal(opt[feats], opt["target"], val[feats], val["target"],
                                n_trials=4, gap_max=0.10, max_arvores=300, paciencia=20)
    assert res2.params == res.params                            # seed fixa reproduz
    m = k.refit_final(res, pd.concat([opt, val])[feats], pd.concat([opt, val])["target"])
    assert m.n_estimators == res.best_iter
    tab = k.importancia_acumulada(m, metodo="gain")
    assert tab["acumulado_pct"].iloc[-1] == pytest.approx(100)
    assert k.corta_por_importancia(tab, 1)[0] in ("x1", "x2")


def test_shap_por_faixa(base):
    lgb = pytest.importorskip("lightgbm")
    pytest.importorskip("shap")
    d = base.dropna(subset=["target"]).copy()
    d.loc[d.index[:400], "x1"] = -9999                         # sentinela "sem histórico"
    d.loc[d.index[400:600], "x1"] = np.nan
    m = lgb.LGBMClassifier(n_estimators=80, verbose=-1, random_state=1).fit(d[["x1", "x2"]], d["target"])
    t = k.shap_por_faixa(m, d[["x1", "x2"]].head(4000), "x1", sentinelas=(-9999,)).set_index("faixa")
    assert {"nulo", "sentinela -9999"} <= set(t.index)
    quartis = t.drop(index=["nulo", "sentinela -9999"])
    assert quartis["shap_medio"].iloc[0] < quartis["shap_medio"].iloc[-1]   # x1 alto = mais risco


def test_monotone(base):
    pytest.importorskip("lightgbm")
    d, feats, opt, val = _janelas(base)
    res = k.tune_lgbm_temporal(opt[feats], opt["target"], val[feats], val["target"],
                               n_trials=2, max_arvores=200, paciencia=20, monotone=[1, -1, 0])
    m = k.refit_final(res, opt[feats], opt["target"], monotone=[1, -1, 0])
    grade = pd.DataFrame({"x1": np.linspace(-3, 3, 50), "x2": 0.0, "ruido": 0.0})
    p = m.predict_proba(grade)[:, 1]
    assert np.all(np.diff(p) >= -1e-12)


# --- produção ---------------------------------------------------------------- #
def test_contrato_onnx_valida_ledger(base, tmp_path):
    lgb = pytest.importorskip("lightgbm")
    d = base.dropna(subset=["target"]).copy()
    d.loc[d.index[:300], "x2"] = -9999
    feats = ["x1", "x2"]
    m = lgb.LGBMClassifier(n_estimators=50, verbose=-1, random_state=1).fit(d[feats], d["target"])
    c = k.contrato_modelo(m, d, feats, alvo="target", sentinelas=(-9999,), janelas={"des": "2024-01/2024-08"})
    assert [f["nome"] for f in c["features"]] == feats
    assert c["features"][1]["pct_sentinela"]["-9999"] > 0
    assert c["features"][1]["percentis"]["p99"] < 10            # percentis sem a sentinela
    assert c["hash_treino"] != k.contrato_modelo(m, d.assign(x1=d["x1"] + 1), feats, "target")["hash_treino"]
    p = k.salva_contrato(c, tmp_path / "contrato.json")
    assert p.exists()

    assert list(k.valida_entrada(d[["x2", "x1", "target"]], c).columns) == feats
    with pytest.raises(KeyError, match="x2"):
        k.valida_entrada(d[["x1"]], c)

    pytest.importorskip("onnxmltools")
    pytest.importorskip("onnxruntime")
    rel = k.exporta_onnx(m, d[feats].head(3000), tmp_path / "m.onnx", tol_prob=1e-4, cortes=(0.1, 0.2))
    assert rel["diff_max_prob"] <= 1e-4 and (tmp_path / "m.onnx.io.json").exists()

    k.registra_run(tmp_path / "ledger.csv", variante="m1", seed=42, params={"a": 1})
    led = k.registra_run(tmp_path / "ledger.csv", variante="m2", seed=42, params={"a": 2})
    assert led["variante"].tolist() == ["m1", "m2"]
