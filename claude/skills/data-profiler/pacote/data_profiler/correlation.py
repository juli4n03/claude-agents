"""Correlação e associação entre variáveis (seção 8)."""

from __future__ import annotations

import datetime as dt
import math
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import polars as pl

from ._types import COD_NULO, BinSpec
from ._utils import arredondar, cronometro, garantir_dependencia, logger
from .backends import BaseBackend, selecionar_backend
from .config import Backend, CorrelacaoConfig
from .profiling import ProfileResult

SCHEMA_PARES = {"var_1": pl.Utf8, "var_2": pl.Utf8, "tipo_par": pl.Utf8, "metodo": pl.Utf8, "valor": pl.Float64, "p_valor": pl.Float64, "n_obs": pl.Float64, "obs": pl.Utf8}


@dataclass
class CorrelacaoResult:
    """Resultado de :func:`correlate`.

    Attributes:
        pares: tabela longa ``var_1, var_2, tipo_par, metodo, valor, p_valor, n_obs, obs``.
        matrizes: uma matriz quadrada (Polars) por método.
        pares_altos: pares com associação acima dos limites da configuração.
        vif: VIF das numéricas (``None`` se desativado).
    """

    pares: pl.DataFrame
    matrizes: dict[str, pl.DataFrame]
    pares_altos: pl.DataFrame
    vif: pl.DataFrame | None = None
    metadados: dict[str, Any] = field(default_factory=dict)

    def to_excel(self, path: str) -> str:
        from .report import correlacao_para_excel

        return correlacao_para_excel(self, path)


def correlate(
    df: Any,
    *,
    colunas: Sequence[str] | None = None,
    metodos: Sequence[str] = ("pearson", "spearman", "cramer_v"),
    amostra: float | int | None = None,
    tipos: dict[str, str] | None = None,
    profile_result: ProfileResult | None = None,
    backend: Backend = "auto",
    config: CorrelacaoConfig | None = None,
) -> CorrelacaoResult:
    """Calcula associações entre pares de variáveis.

    Métodos: ``pearson``, ``spearman`` (numérica × numérica); ``cramer_v`` e
    ``qui_quadrado`` (categórica × categórica); ``eta`` e ``ponto_bisserial``
    (numérica × categórica); opcionais ``phik`` e ``informacao_mutua``.
    ``eta`` é calculado sempre que houver pares mistos.

    Args:
        amostra: fração ou nº de linhas (``seed`` em ``config``); registra ``amostral`` em metadados.
        tipos: tipo por coluna (``numerica``/``categorica``); padrão: do ``profile_result`` ou do schema.
    """
    cfg = config or CorrelacaoConfig()
    if amostra is not None:
        cfg.amostra = amostra
    be = selecionar_backend(df, backend)
    be.persistir()
    try:
        tipos_ok = {**(profile_result.tipos if profile_result else {}), **(tipos or {})}
        return correlate_backend(be, colunas, metodos, tipos_ok, cfg, profile_result)
    finally:
        be.liberar()


def correlate_backend(be: BaseBackend, colunas, metodos, tipos: dict[str, str], cfg: CorrelacaoConfig, profile_result: ProfileResult | None = None) -> CorrelacaoResult:
    inicio = time.perf_counter()
    tempos: dict[str, float] = {}
    schema = be.schema()
    tipos = dict(tipos)
    for c, fam in schema.items():
        tipos.setdefault(c, {"numerica": "numerica", "string": "categorica", "booleana": "categorica", "data": "data"}.get(fam, "nao_suportado"))
    cols = [c for c in (colunas or list(schema)) if c in schema]
    numericas = [c for c in cols if tipos.get(c) == "numerica"]
    categoricas = [c for c in cols if tipos.get(c) in ("categorica", "booleana")]
    metodos = tuple(metodos)
    pairwise = cfg.tratamento_nulos == "pairwise"
    linhas: list[dict] = []
    amostral = False
    obs_global: list[str] = []

    # --- numérica × numérica ----------------------------------------------------------
    with cronometro("numerica_numerica", tempos):
        for metodo in [m for m in ("pearson", "spearman") if m in metodos]:
            if len(numericas) < 2:
                break
            tab, am = be.correlacao_numerica(numericas, metodo, pairwise, cfg.amostra, cfg.seed)
            amostral |= am
            obs = ("amostral; " if am else "") + ("listwise" if (not pairwise or (be.nome == "spark" and metodo == "spearman")) else "pairwise")
            if be.nome == "spark" and metodo == "spearman" and pairwise:
                obs_global.append("Spearman no Spark e calculado listwise (ml.stat.Correlation)")
            for r in tab.iter_rows(named=True):
                linhas.append({"var_1": r["var_1"], "var_2": r["var_2"], "tipo_par": "num_num", "metodo": metodo, "valor": arredondar(r["valor"]), "p_valor": _p_correlacao(r["valor"], r["n_obs"]), "n_obs": r["n_obs"], "obs": obs})

    # --- categórica × categórica -----------------------------------------------------
    specs_cat = {c: BinSpec(c, "categorica", categorias=[v for v in be.top_categorias(c, cfg.max_categorias) if v is not None]) for c in categoricas} if categoricas else {}
    with cronometro("categorica_categorica", tempos):
        if "cramer_v" in metodos or "qui_quadrado" in metodos:
            for i, a in enumerate(categoricas):
                for b in categoricas[i + 1 :]:
                    cont = be.contingencia(specs_cat[a], specs_cat[b])
                    linhas += _linhas_qui_quadrado(a, b, cont, cfg, metodos)

    # --- numérica × categórica -------------------------------------------------------
    with cronometro("numerica_categorica", tempos):
        if numericas and categoricas:
            for n in numericas:
                for c in categoricas:
                    grp = be.estatisticas_por_grupo(n, c, cfg.max_categorias).filter(pl.col("grupo") != COD_NULO)
                    linhas += _linhas_eta(n, c, grp)

    # --- opcionais --------------------------------------------------------------------
    if cfg.calcular_phik or "phik" in metodos:
        with cronometro("phik", tempos):
            linhas += _linhas_phik(be, [*numericas, *categoricas], tipos, cfg)
    if cfg.calcular_informacao_mutua or "informacao_mutua" in metodos:
        with cronometro("informacao_mutua", tempos):
            linhas += _linhas_mi(be, numericas, categoricas, specs_cat, cfg)

    pares = pl.DataFrame(linhas, schema=SCHEMA_PARES) if linhas else pl.DataFrame(schema=SCHEMA_PARES)
    matrizes = {m: _matriz(pares.filter(pl.col("metodo") == m)) for m in pares["metodo"].unique().to_list()} if pares.height else {}
    altos = _pares_altos(pares, cfg)
    vif = None
    if cfg.calcular_vif and "pearson" in matrizes:
        elegiveis = [c for c in numericas if profile_result is None or c in set(profile_result.eleitas())]
        vif = _vif(matrizes["pearson"], elegiveis)
    metadados = {"backend": be.nome, "amostral": amostral, "tratamento_nulos": cfg.tratamento_nulos, "metodos": list(metodos), "numericas": numericas, "categoricas": categoricas, "obs": obs_global, "timestamp": dt.datetime.now().isoformat(timespec="seconds"), "tempos": tempos, "tempo_total_s": round(time.perf_counter() - inicio, 3), "config": cfg.to_dict()}
    return CorrelacaoResult(pares, matrizes, altos, vif, metadados)


# --------------------------------------------------------------------------- #
def _p_correlacao(r: float | None, n: float | None) -> float | None:
    """p-valor bicaudal (teste t) de uma correlação; aproximado para Spearman."""
    if r is None or n is None or n < 3 or abs(r) >= 1:
        return None if r is None or n is None or n < 3 else 0.0
    from scipy.stats import t

    est = r * math.sqrt((n - 2) / (1 - r * r))
    return float(2 * (1 - t.cdf(abs(est), n - 2)))


def cramer_v_corrigido(tabela: np.ndarray) -> tuple[float, float, int, float, float]:
    """V de Cramér com correção de viés (Bergsma, 2013).

    Returns:
        ``(v_corrigido, qui_quadrado, graus_liberdade, p_valor, pct_celulas_esperado_baixo)``.
    """
    from scipy.stats import chi2_contingency

    tab = tabela[tabela.sum(axis=1) > 0][:, tabela.sum(axis=0) > 0]
    n = tab.sum()
    r, k = tab.shape
    if n <= 1 or r < 2 or k < 2:
        return 0.0, 0.0, 0, 1.0, 0.0
    chi2, p, dof, esperado = chi2_contingency(tab, correction=False)
    phi2 = chi2 / n
    phi2c = max(0.0, phi2 - (k - 1) * (r - 1) / (n - 1))
    kc = k - (k - 1) ** 2 / (n - 1)
    rc = r - (r - 1) ** 2 / (n - 1)
    den = min(kc - 1, rc - 1)
    v = math.sqrt(phi2c / den) if den > 0 else 0.0
    pct_baixo = 100.0 * (esperado < 5).sum() / esperado.size
    return v, float(chi2), int(dof), float(p), float(pct_baixo)


def _linhas_qui_quadrado(a: str, b: str, cont: pl.DataFrame, cfg: CorrelacaoConfig, metodos) -> list[dict]:
    cont = cont.filter((pl.col("faixa_a") != COD_NULO) & (pl.col("faixa_b") != COD_NULO))
    if cont.height < 2:
        return []
    piv = cont.pivot(on="faixa_b", index="faixa_a", values="qtd", aggregate_function="sum").fill_null(0.0)
    tab = piv.drop("faixa_a").to_numpy().astype(float)
    v, chi2, dof, p, pct_baixo = cramer_v_corrigido(tab)
    n = float(tab.sum())
    obs = []
    if pct_baixo > cfg.pct_celulas_esperado_baixo:
        obs.append(f"{pct_baixo:.0f}% das celulas com esperado < {cfg.min_esperado:g}: qui-quadrado pouco confiavel")
    linhas = []
    if "cramer_v" in metodos:
        linhas.append({"var_1": a, "var_2": b, "tipo_par": "cat_cat", "metodo": "cramer_v", "valor": arredondar(v), "p_valor": p, "n_obs": n, "obs": "; ".join(["correcao de Bergsma", *obs])})
    if "qui_quadrado" in metodos:
        linhas.append({"var_1": a, "var_2": b, "tipo_par": "cat_cat", "metodo": "qui_quadrado", "valor": arredondar(chi2, 4), "p_valor": p, "n_obs": n, "obs": "; ".join([f"gl={dof}", *obs])})
    return linhas


def _linhas_eta(n: str, c: str, grp: pl.DataFrame) -> list[dict]:
    if grp.height < 2:
        return []
    cnt = grp["n"].to_numpy().astype(float)
    m = grp["media"].fill_null(0.0).to_numpy()
    var = grp["variancia"].fill_null(0.0).to_numpy()
    total = cnt.sum()
    media = (cnt * m).sum() / total
    ss_between = float((cnt * (m - media) ** 2).sum())
    ss_within = float(((cnt - 1) * var).sum())
    ss_total = ss_between + ss_within
    eta = math.sqrt(ss_between / ss_total) if ss_total > 0 else 0.0
    out = [{"var_1": n, "var_2": c, "tipo_par": "num_cat", "metodo": "eta", "valor": arredondar(eta), "p_valor": None, "n_obs": total, "obs": None}]
    if grp.height == 2:
        sd = math.sqrt(ss_total / total) if total else 0.0
        if sd > 0:
            rpb = (m[1] - m[0]) / sd * math.sqrt(cnt[0] * cnt[1] / total**2)
            out.append({"var_1": n, "var_2": c, "tipo_par": "num_cat", "metodo": "ponto_bisserial", "valor": arredondar(rpb), "p_valor": _p_correlacao(rpb, total), "n_obs": total, "obs": f"grupo 1 = {grp['grupo'][1]}"})
    return out


def _linhas_phik(be: BaseBackend, cols: list[str], tipos: dict[str, str], cfg: CorrelacaoConfig) -> list[dict]:
    phik = garantir_dependencia("phik", "coeficiente phik")
    if phik is None or len(cols) < 2:
        return []
    pdf = be.amostra_pandas(cols, cfg.n_amostra_phik, cfg.seed)
    interval = [c for c in cols if tipos.get(c) == "numerica"]
    try:
        m = pdf.phik_matrix(interval_cols=interval)
    except Exception as exc:
        logger.warning("phik falhou: %s", exc)
        return []
    out = []
    for i, a in enumerate(cols):
        for b in cols[i + 1 :]:
            if a in m.index and b in m.columns:
                out.append({"var_1": a, "var_2": b, "tipo_par": "misto", "metodo": "phik", "valor": arredondar(float(m.loc[a, b])), "p_valor": None, "n_obs": float(len(pdf)), "obs": "amostral"})
    return out


def _linhas_mi(be: BaseBackend, numericas, categoricas, specs_cat, cfg: CorrelacaoConfig) -> list[dict]:
    from .predictive_power import _mi_normalizada

    probs = [i / cfg.n_faixas_mi for i in range(1, cfg.n_faixas_mi)]
    q = be.quantis(numericas, probs) if numericas else {}
    specs = {c: BinSpec(c, "numerica", cortes=sorted({float(v) for v in q[c] if v is not None})) for c in numericas}
    specs.update(specs_cat)
    cols = [*numericas, *categoricas]
    out = []
    for i, a in enumerate(cols):
        for b in cols[i + 1 :]:
            cont = be.contingencia(specs[a], specs[b]).filter((pl.col("faixa_a") != COD_NULO) & (pl.col("faixa_b") != COD_NULO))
            if cont.height < 2:
                continue
            piv = cont.pivot(on="faixa_b", index="faixa_a", values="qtd", aggregate_function="sum").fill_null(0.0)
            tab = piv.drop("faixa_a").to_numpy().astype(float)
            out.append({"var_1": a, "var_2": b, "tipo_par": "misto", "metodo": "informacao_mutua", "valor": _mi_normalizada(tab), "p_valor": None, "n_obs": float(tab.sum()), "obs": "normalizada por min(H)"})
    return out


def _matriz(pares: pl.DataFrame) -> pl.DataFrame:
    vars_ = sorted(set(pares["var_1"].to_list()) | set(pares["var_2"].to_list()))
    idx = {v: i for i, v in enumerate(vars_)}
    m = np.full((len(vars_), len(vars_)), np.nan)
    np.fill_diagonal(m, 1.0)
    for r in pares.iter_rows(named=True):
        if r["valor"] is not None:
            m[idx[r["var_1"]], idx[r["var_2"]]] = m[idx[r["var_2"]], idx[r["var_1"]]] = r["valor"]
    return pl.DataFrame({"variavel": vars_, **{v: m[:, i].tolist() for v, i in idx.items()}})


def _pares_altos(pares: pl.DataFrame, cfg: CorrelacaoConfig) -> pl.DataFrame:
    if not pares.height:
        return pares
    lim_num, lim_cat = cfg.limite_associacao_alta_num, cfg.limite_associacao_alta_cat
    return pares.filter(
        ((pl.col("metodo").is_in(["pearson", "spearman", "eta", "ponto_bisserial"])) & (pl.col("valor").abs() > lim_num))
        | ((pl.col("metodo").is_in(["cramer_v", "phik", "informacao_mutua"])) & (pl.col("valor") > lim_cat))
    ).sort(pl.col("valor").abs(), descending=True)


def _vif(matriz_pearson: pl.DataFrame, colunas: list[str]) -> pl.DataFrame | None:
    cols = [c for c in colunas if c in matriz_pearson.columns]
    if len(cols) < 2:
        return None
    r = matriz_pearson.filter(pl.col("variavel").is_in(cols)).sort(pl.col("variavel").cast(pl.Enum(cols)))
    m = np.nan_to_num(r.select(cols).to_numpy().astype(float), nan=0.0)
    np.fill_diagonal(m, 1.0)
    inv = np.linalg.pinv(m)
    vif = np.clip(np.diag(inv), 1.0, None)
    return pl.DataFrame({"variavel": cols, "vif": [round(float(v), 4) for v in vif]}).with_columns(
        pl.when(pl.col("vif") > 10).then(pl.lit("alta")).when(pl.col("vif") > 5).then(pl.lit("moderada")).otherwise(pl.lit("baixa")).alias("multicolinearidade")
    ).sort("vif", descending=True)
