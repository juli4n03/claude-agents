"""Discretização (binning) e tabela de faixas (seções 7.2 e 7.3).

Fluxo:
    1. :func:`construir_specs` calcula os cortes iniciais (quantil, ótimo ou árvore)
       na base/safra de referência e as categorias mantidas das categóricas.
    2. O backend agrega a base em formato longo (``variavel, faixa, safra, qtd, maus``).
    3. :func:`ajustar_specs` decide, **apenas na referência**, quais faixas fundir
       (``min_pct_faixa``, zero bons/maus) e devolve os specs finais.
    4. :func:`remapear` aplica o mapeamento código original → código final em
       toda a tabela longa (todas as safras e a validação), sem recalcular cortes.
    5. :func:`tabela_faixas` calcula WoE/IV/KS/lift por faixa a partir dos agregados.
"""

from __future__ import annotations

import math
import warnings
from typing import Sequence

import numpy as np
import polars as pl

from ._types import COD_NULO, COD_OUTROS, COD_VAZIO, PREFIXO_BIN, PREFIXO_CAT, PREFIXO_ESPECIAL, BinSpec
from ._utils import garantir_dependencia, logger
from .backends import BaseBackend
from .config import PoderPreditivoConfig

ORDEM_ESPECIAL_BASE = 1_000
ORDEM_VAZIO = 9_997
ORDEM_OUTROS = 9_998
ORDEM_NULO = 9_999


# ----------------------------------------------------------------------------
# 1. Cortes iniciais
# ----------------------------------------------------------------------------
def construir_specs(
    be: BaseBackend,
    numericas: Sequence[str],
    categoricas: Sequence[str],
    cfg: PoderPreditivoConfig,
    target: str | None = None,
    tipo_target: str = "binario",
    coluna_peso: str | None = None,
) -> list[BinSpec]:
    """Calcula os specs iniciais de binning na base de referência."""
    specs: list[BinSpec] = []
    metodo = cfg.metodo_binning
    if numericas:
        if metodo == "quantil" or target is None:
            specs += _specs_quantil(be, numericas, cfg)
        elif metodo == "otimo":
            specs += _specs_otimo_numerico(be, numericas, cfg, target, tipo_target, coluna_peso)
        elif metodo == "arvore":
            specs += _specs_arvore(be, numericas, cfg, target, tipo_target, coluna_peso)
        else:
            raise ValueError(f"metodo_binning inválido: {metodo!r}")
    for c in categoricas:
        cats = [v for v in be.top_categorias(c, cfg.max_categorias) if v is not None and v.strip() != ""]
        spec = BinSpec(c, "categorica", categorias=cats, metodo="categorias")
        if cfg.agrupamento_otimo_categorias and target is not None:
            spec.grupos = _grupos_otimos_categoricos(be, c, cats, cfg, target, tipo_target)
            if spec.grupos:
                spec.metodo = "otimo"
        specs.append(spec)
    return specs


def _specs_quantil(be: BaseBackend, numericas: Sequence[str], cfg: PoderPreditivoConfig) -> list[BinSpec]:
    probs = [i / cfg.n_faixas for i in range(1, cfg.n_faixas)]
    quantis = be.quantis(numericas, probs)
    out = []
    for c in numericas:
        cortes = _limpar_cortes(quantis.get(c, []), cfg.valores_especiais)
        out.append(BinSpec(c, "numerica", cortes=cortes, valores_especiais=list(cfg.valores_especiais), metodo="quantil"))
    return out


def _limpar_cortes(valores: Sequence[float | None], especiais: Sequence[float]) -> list[float]:
    """Remove nulos, duplicatas e cortes iguais a valores especiais; ordena."""
    esp = {float(v) for v in especiais}
    return sorted({float(v) for v in valores if v is not None and not math.isnan(float(v)) and float(v) not in esp})


def _amostra(be: BaseBackend, colunas: Sequence[str], cfg: PoderPreditivoConfig, target: str, coluna_peso: str | None):
    cols = [*colunas, target, *([coluna_peso] if coluna_peso else [])]
    pdf = be.amostra_pandas(cols, cfg.n_amostra_binning, cfg.seed, estratificar_por=target)
    return pdf[pdf[target].notna()]


def _specs_otimo_numerico(be, numericas, cfg, target, tipo_target, coluna_peso) -> list[BinSpec]:
    ob = garantir_dependencia("optbinning", "binning otimo (fallback: quantil)")
    if ob is None:
        return _specs_quantil(be, numericas, cfg)
    pdf = _amostra(be, numericas, cfg, target, coluna_peso)
    y = pdf[target].astype(float).to_numpy()
    w = pdf[coluna_peso].astype(float).to_numpy() if coluna_peso else None
    out = []
    for c in numericas:
        x = pdf[c].astype(float).to_numpy()
        mask = np.isfinite(x) & ~np.isin(x, list(cfg.valores_especiais))
        try:
            if tipo_target == "binario":
                binner = ob.OptimalBinning(
                    name=c, dtype="numerical", monotonic_trend=None if cfg.monotonicidade == "none" else cfg.monotonicidade,
                    min_prebin_size=cfg.min_pct_faixa / 100, min_bin_size=cfg.min_pct_faixa / 100, max_n_bins=cfg.n_faixas,
                )
            else:
                binner = ob.ContinuousOptimalBinning(name=c, dtype="numerical", min_prebin_size=cfg.min_pct_faixa / 100, min_bin_size=cfg.min_pct_faixa / 100, max_n_bins=cfg.n_faixas)
            binner.fit(x[mask], y[mask], sample_weight=None if w is None else w[mask])
            cortes = _limpar_cortes(list(binner.splits), cfg.valores_especiais)
            out.append(BinSpec(c, "numerica", cortes=cortes, valores_especiais=list(cfg.valores_especiais), metodo="otimo"))
        except Exception as exc:  # degradação controlada
            warnings.warn(f"optbinning falhou em '{c}' ({exc}); usando quantis.", RuntimeWarning, stacklevel=2)
            out += _specs_quantil(be, [c], cfg)
    return out


def _specs_arvore(be, numericas, cfg, target, tipo_target, coluna_peso) -> list[BinSpec]:
    sk = garantir_dependencia("sklearn.tree", "binning por arvore (fallback: quantil)")
    if sk is None:
        return _specs_quantil(be, numericas, cfg)
    pdf = _amostra(be, numericas, cfg, target, coluna_peso)
    y = pdf[target].astype(float).to_numpy()
    w = pdf[coluna_peso].astype(float).to_numpy() if coluna_peso else None
    out = []
    for c in numericas:
        x = pdf[c].astype(float).to_numpy()
        mask = np.isfinite(x) & ~np.isin(x, list(cfg.valores_especiais))
        if mask.sum() < 10:
            out += _specs_quantil(be, [c], cfg)
            continue
        cls = sk.DecisionTreeClassifier if tipo_target == "binario" else sk.DecisionTreeRegressor
        arvore = cls(min_samples_leaf=max(cfg.min_samples_leaf_pct / 100, 1e-4), max_leaf_nodes=cfg.n_faixas, random_state=cfg.seed)
        arvore.fit(x[mask].reshape(-1, 1), y[mask], sample_weight=None if w is None else w[mask])
        thr = arvore.tree_.threshold[arvore.tree_.feature >= 0]
        out.append(BinSpec(c, "numerica", cortes=_limpar_cortes(list(thr), cfg.valores_especiais), valores_especiais=list(cfg.valores_especiais), metodo="arvore"))
    return out


def _grupos_otimos_categoricos(be, coluna, cats, cfg, target, tipo_target) -> dict[str, str] | None:
    ob = garantir_dependencia("optbinning", "agrupamento otimo de categorias")
    if ob is None or tipo_target != "binario" or not cats:
        return None
    pdf = _amostra(be, [coluna], cfg, target, None)
    x = pdf[coluna].astype(str).where(pdf[coluna].notna(), None)
    mask = x.isin(cats).to_numpy()
    try:
        binner = ob.OptimalBinning(name=coluna, dtype="categorical", min_prebin_size=cfg.min_pct_faixa / 100, min_bin_size=cfg.min_pct_faixa / 100, max_n_bins=cfg.n_faixas)
        binner.fit(x.to_numpy()[mask], pdf[target].astype(float).to_numpy()[mask])
        grupos: dict[str, str] = {}
        for i, grupo in enumerate(binner.splits):
            for cat in grupo:
                grupos[str(cat)] = f"G{i + 1:02d}"
        return grupos or None
    except Exception as exc:
        warnings.warn(f"optbinning categorico falhou em '{coluna}' ({exc}).", RuntimeWarning, stacklevel=2)
        return None


# ----------------------------------------------------------------------------
# 3 e 4. Fusão de faixas e remapeamento de códigos
# ----------------------------------------------------------------------------
def ajustar_specs(longa_ref: pl.DataFrame, specs: Sequence[BinSpec], cfg: PoderPreditivoConfig, binario: bool) -> tuple[list[BinSpec], dict[str, bool]]:
    """Funde faixas que violam ``min_pct_faixa`` ou têm zero bons/maus (apenas numéricas
    regulares). Categorias raras vão para ``OUTROS``.

    Returns:
        specs finais e ``{variavel: faixa_pequena}`` (alguma faixa ficou abaixo do mínimo
        mesmo após fusões — ex.: NULO/ESPECIAL, que nunca são fundidas).
    """
    finais: list[BinSpec] = []
    pequenas: dict[str, bool] = {}
    for spec in specs:
        sub = longa_ref.filter(pl.col("variavel") == spec.variavel)
        total = float(sub["qtd"].sum()) if sub.height else 0.0
        if total == 0:
            finais.append(spec)
            pequenas[spec.variavel] = False
            continue
        if spec.tipo == "numerica":
            novo = _fundir_numerica(sub, spec, cfg, total, binario)
        else:
            novo = _ajustar_categorica(sub, spec, cfg, total)
        finais.append(novo)
        # faixa pequena = alguma faixa (após fusão) com pct < min_pct_faixa
        remap = remapear(sub, [novo], [spec]).group_by("faixa").agg(pl.col("qtd").sum())
        pequenas[spec.variavel] = bool((remap["qtd"] / total * 100 < cfg.min_pct_faixa).any())
    return finais, pequenas


def _fundir_numerica(sub: pl.DataFrame, spec: BinSpec, cfg: PoderPreditivoConfig, total: float, binario: bool) -> BinSpec:
    k = len(spec.cortes) + 1
    qtd = np.zeros(k)
    maus = np.zeros(k)
    for r in sub.iter_rows(named=True):
        f = r["faixa"]
        if f.startswith(PREFIXO_BIN):
            j = int(f[len(PREFIXO_BIN):])
            qtd[j] += r["qtd"]
            if binario:
                maus[j] += r["maus"] or 0.0
    # grupos de bins adjacentes: lista de listas de índices originais
    grupos: list[list[int]] = [[j] for j in range(k)]

    def viola(g: list[int]) -> bool:
        q = qtd[g].sum()
        if q / total * 100 < cfg.min_pct_faixa:
            return True
        if binario:
            m = maus[g].sum()
            if q > 0 and (m <= 0 or m >= q):
                return True
        return False

    while len(grupos) > 1:
        idx = next((i for i, g in enumerate(grupos) if viola(g)), None)
        if idx is None:
            break
        # funde com o vizinho de bad rate mais próxima (ou o único existente)
        if idx == 0:
            viz = 1
        elif idx == len(grupos) - 1:
            viz = idx - 1
        else:
            br = lambda g: (maus[g].sum() / qtd[g].sum()) if qtd[g].sum() else 0.0  # noqa: E731
            viz = idx - 1 if abs(br(grupos[idx - 1]) - br(grupos[idx])) <= abs(br(grupos[idx + 1]) - br(grupos[idx])) else idx + 1
        a, b = sorted((idx, viz))
        grupos[a:b + 1] = [grupos[a] + grupos[b]]
    # cortes finais = fronteiras entre grupos
    cortes = [spec.cortes[g[0] - 1] for g in grupos[1:]]
    return BinSpec(spec.variavel, "numerica", cortes=cortes, valores_especiais=list(spec.valores_especiais), metodo=spec.metodo)


def _ajustar_categorica(sub: pl.DataFrame, spec: BinSpec, cfg: PoderPreditivoConfig, total: float) -> BinSpec:
    mantidas = []
    freq = {r["faixa"]: r["qtd"] for r in sub.iter_rows(named=True)}
    if spec.grupos:
        # com agrupamento ótimo, avalia o tamanho do grupo, não da categoria
        tam: dict[str, float] = {}
        for cat in spec.categorias:
            tam[spec.grupos.get(cat, cat)] = tam.get(spec.grupos.get(cat, cat), 0.0) + freq.get(f"{PREFIXO_CAT}{cat}", 0.0)
        mantidas = [c for c in spec.categorias if tam[spec.grupos.get(c, c)] / total * 100 >= cfg.min_pct_faixa]
    else:
        mantidas = [c for c in spec.categorias if freq.get(f"{PREFIXO_CAT}{c}", 0.0) / total * 100 >= cfg.min_pct_faixa]
    grupos = {c: g for c, g in (spec.grupos or {}).items() if c in mantidas} or None
    return BinSpec(spec.variavel, "categorica", categorias=mantidas, grupos=grupos, metodo=spec.metodo)


def mapear_codigo(spec_original: BinSpec, spec_final: BinSpec, codigo: str) -> str:
    """Traduz um código de faixa emitido com ``spec_original`` para ``spec_final``."""
    if codigo in (COD_NULO, COD_VAZIO, COD_OUTROS) or codigo.startswith(PREFIXO_ESPECIAL):
        return codigo
    if spec_final.tipo == "numerica":
        j = int(codigo[len(PREFIXO_BIN):])
        if j == 0 or not spec_final.cortes:
            return f"{PREFIXO_BIN}00"
        limite_inf = spec_original.cortes[j - 1]
        novo = sum(1 for c in spec_final.cortes if c <= limite_inf)
        return f"{PREFIXO_BIN}{novo:02d}"
    cat = codigo[len(PREFIXO_CAT):]
    if cat not in spec_final.categorias:
        return COD_OUTROS
    if spec_final.grupos:
        return f"{PREFIXO_CAT}{spec_final.grupos.get(cat, cat)}"
    return codigo


def remapear(longa: pl.DataFrame, specs_finais: Sequence[BinSpec], specs_originais: Sequence[BinSpec] | None = None) -> pl.DataFrame:
    """Aplica :func:`mapear_codigo` a toda a tabela longa e reagrega."""
    orig = {s.variavel: s for s in (specs_originais or specs_finais)}
    fin = {s.variavel: s for s in specs_finais}
    if not longa.height:
        return longa
    novos = [mapear_codigo(orig[v], fin[v], f) for v, f in zip(longa["variavel"].to_list(), longa["faixa"].to_list())]
    agg_cols = [c for c in longa.columns if c not in ("variavel", "faixa", "safra")]
    aggs = [pl.col(c).sum() if c != "mediana" else pl.col(c).mean() for c in agg_cols]
    return (
        longa.with_columns(pl.Series("faixa", novos))
        .group_by(["variavel", "faixa", "safra"])
        .agg(aggs)
        .sort(["variavel", "safra", "faixa"])
    )


# ----------------------------------------------------------------------------
# Rótulos e ordem
# ----------------------------------------------------------------------------
def _fmt(v: float) -> str:
    if v == float("inf"):
        return "+inf"
    if v == float("-inf"):
        return "-inf"
    return f"{v:.6g}"


def rotulo_e_limites(spec: BinSpec, codigo: str) -> tuple[str, int, float | None, float | None]:
    """Devolve ``(faixa_legivel, ordem_faixa, limite_inf, limite_sup)`` para um código."""
    if codigo == COD_NULO:
        return "NULO", ORDEM_NULO, None, None
    if codigo == COD_VAZIO:
        return "VAZIO", ORDEM_VAZIO, None, None
    if codigo == COD_OUTROS:
        return "OUTROS", ORDEM_OUTROS, None, None
    if codigo.startswith(PREFIXO_ESPECIAL):
        v = codigo[len(PREFIXO_ESPECIAL):]
        idx = [str(e) for e in spec.valores_especiais]
        try:
            pos = [_fmt(float(e)) for e in spec.valores_especiais].index(_fmt(float(v)))
        except ValueError:
            pos = len(idx)
        return f"ESPECIAL_{v}", ORDEM_ESPECIAL_BASE + pos, float(v), float(v)
    if spec.tipo == "numerica":
        j = int(codigo[len(PREFIXO_BIN):])
        bordas = [float("-inf"), *spec.cortes, float("inf")]
        li, ls = bordas[j], bordas[j + 1]
        abre = "(" if li == float("-inf") else "["
        return f"{abre}{_fmt(li)}, {_fmt(ls)})", j, (None if li == float("-inf") else li), (None if ls == float("inf") else ls)
    cat = codigo[len(PREFIXO_CAT):]
    if spec.grupos:
        membros = sorted(c for c, g in spec.grupos.items() if g == cat)
        return (", ".join(membros) if membros else cat), 0, None, None
    return cat, 0, None, None


# ----------------------------------------------------------------------------
# 5. Tabela de faixas
# ----------------------------------------------------------------------------
def tabela_faixas(agregado: pl.DataFrame, specs: Sequence[BinSpec], cfg: PoderPreditivoConfig, tipo_target: str) -> pl.DataFrame:
    """Tabela de faixas (uma linha por variável × faixa) a partir dos agregados.

    Para target binário: ``woe = ln(pct_bons / pct_maus)`` — WoE **positivo** indica
    concentração de bons (menor risco). ``iv_faixa = (pct_bons - pct_maus)·woe``.
    Suavização ``suavizacao_woe`` é somada a bons **e** maus de todas as faixas da
    variável quando alguma faixa tem zero em um dos lados.
    Percentuais em 0–100.
    """
    por_var = {s.variavel: s for s in specs}
    linhas: list[dict] = []
    for var, spec in por_var.items():
        sub = agregado.filter(pl.col("variavel") == var)
        if not sub.height:
            continue
        sub = sub.group_by("faixa").agg([pl.col(c).sum() if c != "mediana" else pl.col(c).mean() for c in sub.columns if c not in ("variavel", "faixa", "safra")])
        regs = []
        for r in sub.iter_rows(named=True):
            rot, ordem, li, ls = rotulo_e_limites(spec, r["faixa"])
            regs.append({**r, "codigo": r["faixa"], "faixa": rot, "ordem_faixa": ordem, "limite_inf": li, "limite_sup": ls})
        total = sum(r["qtd"] for r in regs)
        if tipo_target == "binario":
            _metricas_binarias(regs, total, cfg, spec)
        else:
            _metricas_continuas(regs, total, spec)
        for r in regs:
            r["variavel"] = var
        linhas += regs
    if not linhas:
        return pl.DataFrame(schema=COLUNAS_FAIXAS_BIN if tipo_target == "binario" else COLUNAS_FAIXAS_CONT)
    cols = COLUNAS_FAIXAS_BIN if tipo_target == "binario" else COLUNAS_FAIXAS_CONT
    return pl.DataFrame(linhas, schema_overrides=cols).select(list(cols)).sort(["variavel", "ordem_faixa"])


def _metricas_binarias(regs: list[dict], total: float, cfg: PoderPreditivoConfig, spec: BinSpec) -> None:
    for r in regs:
        r["maus"] = float(r.get("maus") or 0.0)
        r["bons"] = float(r["qtd"]) - r["maus"]
    tot_maus = sum(r["maus"] for r in regs)
    tot_bons = sum(r["bons"] for r in regs)
    suavizar = any(r["maus"] <= 0 or r["bons"] <= 0 for r in regs)
    s = cfg.suavizacao_woe if suavizar else 0.0
    tb, tm = tot_bons + s * len(regs), tot_maus + s * len(regs)
    br_geral = tot_maus / total if total else None
    # ordem para acumulados: numéricas pela ordem natural; categóricas por bad rate crescente
    if spec.tipo == "categorica":
        regs.sort(key=lambda r: ((r["maus"] / r["qtd"]) if r["qtd"] else 0.0, r["faixa"]))
        for i, r in enumerate(regs):
            r["ordem_faixa"] = i
    else:
        regs.sort(key=lambda r: r["ordem_faixa"])
    acum_m = acum_b = 0.0
    for r in regs:
        bons, maus, q = r.pop("bons"), r.pop("maus"), r["qtd"]
        pb = (bons + s) / tb if tb else 0.0
        pm = (maus + s) / tm if tm else 0.0
        woe = math.log(pb / pm) if pb > 0 and pm > 0 else None
        acum_m += maus
        acum_b += bons
        maus_acum = round(100 * acum_m / tot_maus, 2) if tot_maus else None
        bons_acum = round(100 * acum_b / tot_bons, 2) if tot_bons else None
        r.update(
            {
                "qtd_bons": bons,
                "qtd_maus": maus,
                "pct_qtd": round(100 * q / total, 2) if total else None,
                "pct_bons": round(100 * pb, 2),
                "pct_maus": round(100 * pm, 2),
                "bad_rate": round(100 * maus / q, 4) if q else None,
                "woe": round(woe, 6) if woe is not None else None,
                "iv_faixa": round((pb - pm) * woe, 6) if woe is not None else None,
                "lift": round((maus / q) / br_geral, 4) if (br_geral and q) else None,
                "maus_acum": maus_acum,
                "bons_acum": bons_acum,
                "ks_faixa": round(abs(maus_acum - bons_acum), 2) if (maus_acum is not None and bons_acum is not None) else None,
                "suavizado": suavizar,
            }
        )


def _metricas_continuas(regs: list[dict], total: float, spec: BinSpec) -> None:
    media_geral = sum(r.get("soma") or 0.0 for r in regs) / total if total else None
    if spec.tipo == "categorica":
        regs.sort(key=lambda r: ((r.get("soma") or 0.0) / r["qtd"]) if r["qtd"] else 0.0)
        for i, r in enumerate(regs):
            r["ordem_faixa"] = i
    else:
        regs.sort(key=lambda r: r["ordem_faixa"])
    for r in regs:
        q = r["qtd"]
        media = (r.get("soma") or 0.0) / q if q else None
        var = ((r.get("soma_quad") or 0.0) / q - media**2) if (q and media is not None) else None
        r.update(
            {
                "pct_qtd": round(100 * q / total, 2) if total else None,
                "media_target": round(media, 6) if media is not None else None,
                "mediana_target": round(r["mediana"], 6) if r.get("mediana") is not None else None,
                "desvio_target": round(math.sqrt(max(var, 0.0)), 6) if var is not None else None,
                "lift": round(media / media_geral, 4) if (media is not None and media_geral) else None,
            }
        )


COLUNAS_FAIXAS_BIN = {
    "variavel": pl.Utf8, "faixa": pl.Utf8, "codigo": pl.Utf8, "ordem_faixa": pl.Int64, "limite_inf": pl.Float64, "limite_sup": pl.Float64,
    "qtd": pl.Float64, "pct_qtd": pl.Float64, "qtd_bons": pl.Float64, "qtd_maus": pl.Float64, "pct_bons": pl.Float64, "pct_maus": pl.Float64,
    "bad_rate": pl.Float64, "woe": pl.Float64, "iv_faixa": pl.Float64, "lift": pl.Float64, "maus_acum": pl.Float64, "bons_acum": pl.Float64,
    "ks_faixa": pl.Float64, "suavizado": pl.Boolean,
}
COLUNAS_FAIXAS_CONT = {
    "variavel": pl.Utf8, "faixa": pl.Utf8, "codigo": pl.Utf8, "ordem_faixa": pl.Int64, "limite_inf": pl.Float64, "limite_sup": pl.Float64,
    "qtd": pl.Float64, "pct_qtd": pl.Float64, "media_target": pl.Float64, "mediana_target": pl.Float64, "desvio_target": pl.Float64, "lift": pl.Float64,
}
