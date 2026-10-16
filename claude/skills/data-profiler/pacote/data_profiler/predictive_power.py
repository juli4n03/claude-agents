"""Poder preditivo variável × target (seção 7)."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import polars as pl

from ._types import COD_NULO, COD_OUTROS, COD_VAZIO, PREFIXO_ESPECIAL, BinSpec
from ._utils import arredondar, cronometro, garantir_dependencia, ks_auc_de_distribuicao, logger, psi, spearman
from .backends import BaseBackend, selecionar_backend
from .binning import ajustar_specs, construir_specs, remapear, tabela_faixas
from .config import Backend, PoderPreditivoConfig, ProfileConfig, TipoTarget
from .profiling import ProfileResult, _profile_backend

_CODIGOS_NAO_REGULARES = (COD_NULO, COD_VAZIO, COD_OUTROS)


@dataclass
class PoderPreditivoResult:
    """Resultado de :func:`poder_preditivo`.

    Attributes:
        resumo: uma linha por variável (métricas, flags e recomendação), ordenada por IV.
        faixas: tabela de faixas (uma linha por variável × faixa).
        por_safra: métricas por variável × safra (vazia se ``coluna_safra`` não informada).
        cortes: dicionário serializável com specs de binning e mapa código → WoE por variável.
        metadados: target, bad rate, linhas válidas, backend, tempos, config.
    """

    resumo: pl.DataFrame
    faixas: pl.DataFrame
    por_safra: pl.DataFrame
    cortes: dict[str, Any]
    metadados: dict[str, Any] = field(default_factory=dict)
    importancia: pl.DataFrame | None = None
    _specs: list[BinSpec] = field(default_factory=list, repr=False)
    _mapa_woe: dict[str, dict[str, float]] = field(default_factory=dict, repr=False)

    def aplicar_woe(self, df: Any, backend: Backend = "auto", sufixo: str = "_woe", colunas: Sequence[str] | None = None) -> Any:
        """Transforma uma base nova em WoE usando os cortes salvos (mesmo tipo de entrada na saída)."""
        be = selecionar_backend(df, backend)
        specs = [s for s in self._specs if colunas is None or s.variavel in colunas]
        return be.aplicar_faixas(specs, self._mapa_woe, sufixo)

    def to_excel(self, path: str) -> str:
        from .report import poder_preditivo_para_excel

        return poder_preditivo_para_excel(self, path)

    def to_html(self, path: str, variaveis: Sequence[str] | None = None) -> str | None:
        from .report import poder_preditivo_para_html

        return poder_preditivo_para_html(self, path, variaveis)

    def graficos(self, variavel: str):
        from .report import grafico_variavel

        return grafico_variavel(self, variavel)

    def cortes_json(self) -> str:
        return json.dumps(self.cortes, ensure_ascii=False, indent=2, default=str)

    @classmethod
    def de_cortes(cls, cortes: dict[str, Any] | str) -> "PoderPreditivoResult":
        """Reconstrói um resultado "só para aplicação de WoE" a partir de ``cortes`` (dict ou JSON)."""
        if isinstance(cortes, str):
            cortes = json.loads(cortes)
        specs = [BinSpec.from_dict(v["spec"]) for v in cortes.values()]
        mapa = {k: {c: float(w) for c, w in v["woe"].items()} for k, v in cortes.items()}
        vazio = pl.DataFrame()
        return cls(vazio, vazio, vazio, cortes, {}, None, specs, mapa)


# --------------------------------------------------------------------------- #
def poder_preditivo(
    df: Any,
    target: str,
    *,
    colunas: Sequence[str] | None = None,
    tipo_target: TipoTarget = "auto",
    coluna_safra: str | None = None,
    safra_referencia: str | None = None,
    df_validacao: Any | None = None,
    coluna_peso: str | None = None,
    profile_result: ProfileResult | None = None,
    backend: Backend = "auto",
    config: PoderPreditivoConfig | None = None,
    profile_config: ProfileConfig | None = None,
) -> PoderPreditivoResult:
    """Mede, para cada variável, o quanto ela separa o target e se isso é estável e confiável.

    Args:
        df: base (pandas, Polars ou Spark).
        target: nome do target. Binário (1 = mau) ou contínuo.
        colunas: subconjunto de variáveis (padrão: todas as eleitas/avaliáveis).
        tipo_target: ``"auto"`` → binário se houver exatamente 2 valores distintos.
        coluna_safra: ativa estabilidade temporal (7.6); cortes vêm de ``safra_referencia``.
        safra_referencia: ``yyyy-MM``; padrão: primeira safra.
        df_validacao: base out-of-time; cortes da referência são reaplicados.
        coluna_peso: pesos amostrais; todas as contagens passam a ser ponderadas.
        profile_result: resultado de :func:`profile` para reaproveitar o status "Eleita".
        config: :class:`PoderPreditivoConfig`.
    """
    cfg = config or PoderPreditivoConfig()
    pcfg = profile_config or ProfileConfig()
    inicio = time.perf_counter()
    tempos: dict[str, float] = {}
    be = selecionar_backend(df, backend, pcfg, n_faixas_finas=cfg.n_faixas_finas)
    be.persistir()
    try:
        return _executar(be, target, colunas, tipo_target, coluna_safra, safra_referencia, df_validacao, coluna_peso, profile_result, backend, cfg, pcfg, tempos, inicio)
    finally:
        be.liberar()


def _executar(be: BaseBackend, target, colunas, tipo_target, coluna_safra, safra_referencia, df_validacao, coluna_peso, profile_result, backend, cfg, pcfg, tempos, inicio) -> PoderPreditivoResult:
    schema = be.schema()
    if target not in schema:
        raise KeyError(f"Target '{target}' não existe na tabela.")

    # --- 7.1 target --------------------------------------------------------------
    with cronometro("validacao_target", tempos):
        tipo = _detectar_tipo_target(be, target, tipo_target)
        binario = tipo == "binario"
        total_bruto = be.n_linhas()
        be = be.preparar_target(target, cfg.mapa_target, binario)
        be_t = be.filtrar_nao_nulo(target)
        total_validas = be_t.n_linhas()
        meta_target: dict[str, Any] = {"target": target, "tipo_target": tipo, "linhas_total": total_bruto, "linhas_validas": total_validas, "linhas_excluidas_target": total_bruto - total_validas}
        if binario:
            freq = be_t.frequencias(target, 3)
            contagens = {str(float(v)): q for v, q in zip(freq["valor"].to_list(), freq["qtd"].to_list())}
            maus, bons = contagens.get("1.0", 0.0), contagens.get("0.0", 0.0)
            if coluna_peso:
                pond = be_t.tabela_faixas_longa([BinSpec(target, "numerica", cortes=[0.5])], target, "binario", None, coluna_peso)
                maus_p = float(pond["maus"].sum())
                bons_p = float(pond["qtd"].sum()) - maus_p
            else:
                maus_p, bons_p = maus, bons
            if maus < cfg.min_maus or bons < cfg.min_maus:
                raise ValueError(f"Target '{target}' tem {int(maus)} maus e {int(bons)} bons; mínimo exigido: {cfg.min_maus} de cada.")
            meta_target.update({"qtd_maus": maus, "qtd_bons": bons, "qtd_maus_ponderado": maus_p, "qtd_bons_ponderado": bons_p, "bad_rate_geral": round(100 * maus_p / (maus_p + bons_p), 4)})
            if coluna_safra:
                meta_target["bad_rate_por_safra"] = be_t.media_target_por_safra(coluna_safra, target, coluna_peso).with_columns((pl.col("media_target") * 100).round(4).alias("bad_rate")).drop("media_target").to_dicts()
        else:
            if total_validas < cfg.min_maus:
                raise ValueError(f"Target contínuo com apenas {total_validas} linhas válidas.")

    # --- profiling / seleção de colunas --------------------------------------------
    excluir = {target, *([coluna_safra] if coluna_safra else []), *([coluna_peso] if coluna_peso else [])}
    with cronometro("profiling", tempos):
        if profile_result is None:
            profile_result = _profile_backend(be_t, None, list(excluir), None, None, None, None, pcfg, {}, time.perf_counter())
    tipos = profile_result.tipos
    tratadas_cat = set(profile_result.numericas.filter(pl.col("tratada_como_categorica"))["variavel"].to_list()) if profile_result.numericas.height else set()
    eleitas = set(profile_result.eleitas())
    motivos_prof = {}
    for tab in (profile_result.numericas, profile_result.categoricas):
        if tab.height:
            motivos_prof.update({r["variavel"]: r["motivo_nao_eleita"] for r in tab.select("variavel", "motivo_nao_eleita").iter_rows(named=True)})

    candidatas = [c for c in (colunas or list(tipos)) if c in schema and c not in excluir and tipos.get(c) not in ("data", "identificador", None)]
    numericas = [c for c in candidatas if tipos[c] == "numerica" and c not in tratadas_cat]
    categoricas = [c for c in candidatas if c not in numericas]
    logger.info("Poder preditivo: %d numéricas, %d categóricas, target %s (%s)", len(numericas), len(categoricas), target, tipo)
    if not candidatas:
        raise ValueError("Nenhuma variável avaliável (todas são datas, identificadores ou foram excluídas).")

    # --- 7.2 binning na referência ---------------------------------------------------
    with cronometro("binning", tempos):
        safras = None
        ref = None
        if coluna_safra:
            safras_df = be_t.contagem_por_safra(coluna_safra) if tipos.get(coluna_safra) == "data" or schema[coluna_safra] == "data" else be_t.frequencias(coluna_safra, 10_000).rename({"valor": "safra", "qtd": "contagem"})
            safras = sorted(s[:7] for s in safras_df["safra"].to_list() if s)
            ref = safra_referencia or (safras[0] if safras else None)
            be_ref = be_t.filtrar_safra(coluna_safra, ref) if ref else be_t
        else:
            be_ref = be_t
        specs_ini = construir_specs(be_ref, numericas, categoricas, cfg, target, tipo, coluna_peso)
        longa = be_t.tabela_faixas_longa(specs_ini, target, tipo, coluna_safra, coluna_peso)
        longa_ref = longa.filter(pl.col("safra") == ref) if (coluna_safra and ref) else longa
        longa_ref = longa_ref.group_by(["variavel", "faixa"]).agg([pl.col(c).sum() if c != "mediana" else pl.col(c).mean() for c in longa.columns if c not in ("variavel", "faixa", "safra")]).with_columns(pl.lit(None, dtype=pl.Utf8).alias("safra"))
        specs, faixa_pequena = ajustar_specs(longa_ref, specs_ini, cfg, binario)
        longa = remapear(longa, specs, specs_ini)

    # --- 7.3 tabela de faixas --------------------------------------------------------
    with cronometro("tabela_faixas", tempos):
        faixas = tabela_faixas(longa, specs, cfg, tipo)

    # --- 7.4 / 7.5 métricas por variável -----------------------------------------------
    with cronometro("metricas_variavel", tempos):
        if binario:
            bruto = be_t.ks_auc_bruto(numericas, target, coluna_peso)
            brutos = {r["variavel"]: r for r in bruto.iter_rows(named=True)}
            resumo = [_metricas_binario(v, faixas, specs_por_var(specs)[v], brutos.get(v), meta_target, cfg, faixa_pequena.get(v, False)) for v in [*numericas, *categoricas]]
        else:
            corr = be_t.correlacoes_com_target(numericas, target)
            corrs = {r["variavel"]: r for r in corr.iter_rows(named=True)}
            spec_target = _spec_target_continuo(be_t, target, cfg)
            resumo = [_metricas_continuo(v, faixas, specs_por_var(specs)[v], corrs.get(v), be_t, spec_target, cfg, faixa_pequena.get(v, False)) for v in [*numericas, *categoricas]]

    # --- 7.6 estabilidade -----------------------------------------------------------
    por_safra = pl.DataFrame(schema=_SCHEMA_SAFRA)
    with cronometro("estabilidade", tempos):
        if coluna_safra and binario and safras:
            por_safra, estab = _estabilidade(longa, specs, cfg, ref, safras)
            for r in resumo:
                r.update(estab.get(r["variavel"], {}))
        if df_validacao is not None and binario:
            be_val = selecionar_backend(df_validacao, backend, pcfg, n_faixas_finas=cfg.n_faixas_finas).preparar_target(target, cfg.mapa_target, True).filtrar_nao_nulo(target)
            longa_val = be_val.tabela_faixas_longa(specs, target, tipo, None, coluna_peso)
            faixas_val = tabela_faixas(longa_val, specs, cfg, tipo)
            for r in resumo:
                m = _iv_ks_gini(faixas_val.filter(pl.col("variavel") == r["variavel"]))
                r.update({"iv_validacao": m["iv"], "ks_validacao": m["ks"], "gini_validacao": m["gini"]})
                r["gap_gini"] = arredondar(100 * (r["gini_woe"] - m["gini"]), 2) if (r.get("gini_woe") is not None and m["gini"] is not None) else None

    # --- 7.7 flags + 7.8 ranking -------------------------------------------------------
    with cronometro("ranking", tempos):
        for r in resumo:
            _flags(r, cfg, binario)
            r["eleita_profiling"] = r["variavel"] in eleitas
            r["motivo_nao_eleita_profiling"] = motivos_prof.get(r["variavel"])
            _recomendar(r, cfg, binario)
        chave_ord = "iv" if binario else "r2_faixas"
        resumo.sort(key=lambda r: (-(r.get(chave_ord) or 0.0), -(r.get("gini_woe") or 0.0) if binario else 0.0))
        for i, r in enumerate(resumo):
            r["ranking"] = i + 1
        _redundancia(resumo, be_t, numericas, categoricas, tipos, cfg)

    df_resumo = pl.DataFrame([{k: r.get(k) for k in _SCHEMA_RESUMO} for r in resumo], schema=_SCHEMA_RESUMO)

    # --- opcional: importância multivariada ----------------------------------------------
    importancia = None
    if cfg.importancia_multivariada and binario:
        with cronometro("lightgbm", tempos):
            importancia = _importancia_multivariada(be_t, df_resumo, target, coluna_peso, cfg)

    mapa_woe = _mapa_woe(faixas) if binario else {}
    cortes = {s.variavel: {"spec": s.to_dict(), "woe": mapa_woe.get(s.variavel, {})} for s in specs}
    metadados = {**meta_target, "backend": be.nome, "exato": be.exato, "coluna_safra": coluna_safra, "safra_referencia": ref, "coluna_peso": coluna_peso, "metodo_binning": cfg.metodo_binning, "timestamp": dt.datetime.now().isoformat(timespec="seconds"), "tempos": tempos, "tempo_total_s": round(time.perf_counter() - inicio, 3), "config": cfg.to_dict()}
    return PoderPreditivoResult(df_resumo, faixas, por_safra, cortes, metadados, importancia, specs, mapa_woe)


# --------------------------------------------------------------------------- #
def specs_por_var(specs: Sequence[BinSpec]) -> dict[str, BinSpec]:
    return {s.variavel: s for s in specs}


def _detectar_tipo_target(be: BaseBackend, target: str, tipo_target: str) -> str:
    if tipo_target in ("binario", "continuo"):
        return tipo_target
    fam = be.schema()[target]
    if fam == "booleana":
        return "binario"
    freq = be.frequencias(target, 3)
    return "binario" if freq.height == 2 else "continuo"


def _mapa_woe(faixas: pl.DataFrame) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in faixas.select("variavel", "codigo", "woe").iter_rows(named=True):
        if r["woe"] is not None:
            out.setdefault(r["variavel"], {})[r["codigo"]] = float(r["woe"])
    return out


def _iv_ks_gini(f: pl.DataFrame) -> dict[str, float | None]:
    if not f.height:
        return {"iv": None, "ks": None, "gini": None, "auc": None}
    iv = float(f["iv_faixa"].fill_null(0.0).sum())
    ks = float(f["ks_faixa"].max()) / 100 if f["ks_faixa"].null_count() < f.height else None
    # AUC sobre o WoE: score = -woe (maior = mais risco)
    g = f.filter(pl.col("woe").is_not_null()).sort("woe", descending=True)
    _, auc = ks_auc_de_distribuicao(g.select(pl.col("qtd_maus").alias("w1"), pl.col("qtd_bons").alias("w0")))
    return {"iv": round(iv, 6), "ks": arredondar(ks), "gini": arredondar(2 * auc - 1) if auc is not None else None, "auc": arredondar(auc)}


def _mann_whitney_p(auc: float | None, n1: float, n0: float) -> float | None:
    """p-valor bicaudal do teste de Mann-Whitney via aproximação normal (sem correção de empates)."""
    if auc is None or n1 <= 0 or n0 <= 0:
        return None
    from scipy.stats import norm

    u = auc * n1 * n0
    mu = n1 * n0 / 2
    sigma = math.sqrt(n1 * n0 * (n1 + n0 + 1) / 12)
    if sigma == 0:
        return None
    z = (u - mu) / sigma
    return float(2 * (1 - norm.cdf(abs(z))))


def _mi_normalizada(tabela: np.ndarray) -> float | None:
    """Informação mútua normalizada por ``min(H(X), H(Y))`` a partir de uma tabela de contingência."""
    n = tabela.sum()
    if n <= 0:
        return None
    p = tabela / n
    px, py = p.sum(axis=1, keepdims=True), p.sum(axis=0, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        termo = np.where(p > 0, p * np.log(p / (px @ py)), 0.0)
    mi = float(termo.sum())
    hx = float(-(px[px > 0] * np.log(px[px > 0])).sum())
    hy = float(-(py[py > 0] * np.log(py[py > 0])).sum())
    den = min(hx, hy)
    return round(mi / den, 6) if den > 0 else 0.0


def _metricas_binario(var, faixas: pl.DataFrame, spec: BinSpec, bruto, meta, cfg: PoderPreditivoConfig, faixa_pequena: bool) -> dict:
    f = faixas.filter(pl.col("variavel") == var)
    reg = f.filter(~pl.col("codigo").is_in(_CODIGOS_NAO_REGULARES) & ~pl.col("codigo").str.starts_with(PREFIXO_ESPECIAL)).sort("ordem_faixa")
    m = _iv_ks_gini(f)
    r: dict[str, Any] = {"variavel": var, "tipo": spec.tipo, "metodo_binning": spec.metodo, "qtd_faixas": f.height, "menor_pct_faixa": float(f["pct_qtd"].min()) if f.height else None, "faixa_pequena": faixa_pequena}
    r.update({"iv": m["iv"], "classificacao_iv": cfg.classificar_iv(m["iv"]), "ks_faixas": m["ks"], "auc_woe": m["auc"], "gini_woe": m["gini"]})
    # bruto (numéricas)
    if bruto and bruto.get("auc_bruto") is not None:
        auc = bruto["auc_bruto"]
        r["direcao"] = "valor maior -> mais risco" if auc >= 0.5 else "valor maior -> menos risco"
        r["auc_bruto"] = arredondar(max(auc, 1 - auc))
        r["gini_bruto"] = arredondar(2 * r["auc_bruto"] - 1)
        r["ks_bruto"] = arredondar(bruto["ks_bruto"])
        r["ks"] = r["ks_bruto"]
        n1 = float(f["qtd_maus"].sum()) if f.height else 0.0
        n0 = float(f["qtd_bons"].sum()) if f.height else 0.0
        r["p_valor_mann_whitney"] = _mann_whitney_p(auc, n1, n0)
        r["ponto_bisserial"] = arredondar(bruto.get("ponto_bisserial"))
    else:
        r["ks"] = m["ks"]
    # monotonicidade (numéricas, faixas regulares)
    if spec.tipo == "numerica" and reg.height >= 2:
        br = reg["bad_rate"].fill_null(0.0).to_numpy()
        difs = np.sign(np.diff(br))
        difs = difs[difs != 0]
        r["qtd_inversoes"] = int((np.diff(difs) != 0).sum()) if difs.size > 1 else 0
        r["monotonicidade"] = arredondar(spearman(reg["ordem_faixa"].to_list(), br.tolist()), 4) if reg.height >= 3 else (1.0 if difs.size and difs[0] > 0 else -1.0 if difs.size else None)
    # poder do nulo
    nulo = f.filter(pl.col("codigo") == COD_NULO)
    if nulo.height and nulo["bad_rate"][0] is not None:
        resto = f.filter(pl.col("codigo") != COD_NULO)
        br_resto = 100 * float(resto["qtd_maus"].sum()) / float(resto["qtd"].sum()) if resto.height and float(resto["qtd"].sum()) else None
        r["bad_rate_nulo"] = nulo["bad_rate"][0]
        r["pct_nulo"] = nulo["pct_qtd"][0]
        r["poder_do_nulo"] = arredondar(nulo["bad_rate"][0] - br_resto, 4) if br_resto is not None else None
    # informação mútua e testes categóricos (faixa × target)
    if f.height:
        tab = np.column_stack([f["qtd_bons"].to_numpy(), f["qtd_maus"].to_numpy()]).astype(float)
        r["informacao_mutua"] = _mi_normalizada(tab)
        if spec.tipo == "categorica" and tab.shape[0] >= 2:
            from scipy.stats import chi2_contingency

            tab_ok = tab[tab.sum(axis=1) > 0]
            try:
                chi2, p, dof, _ = chi2_contingency(tab_ok, correction=False)
                n = tab_ok.sum()
                r["qui_quadrado"] = arredondar(chi2, 4)
                r["p_valor_qui_quadrado"] = float(p)
                r["cramer_v"] = arredondar(math.sqrt(chi2 / (n * (min(tab_ok.shape) - 1))), 6)
            except ValueError:
                pass
    return r


def _spec_target_continuo(be: BaseBackend, target: str, cfg: PoderPreditivoConfig) -> BinSpec:
    probs = [i / cfg.n_faixas for i in range(1, cfg.n_faixas)]
    q = be.quantis([target], probs)[target]
    return BinSpec(target, "numerica", cortes=sorted({float(v) for v in q if v is not None}))


def _metricas_continuo(var, faixas, spec: BinSpec, corr, be: BaseBackend, spec_target: BinSpec, cfg, faixa_pequena) -> dict:
    f = faixas.filter(pl.col("variavel") == var)
    r: dict[str, Any] = {"variavel": var, "tipo": spec.tipo, "metodo_binning": spec.metodo, "qtd_faixas": f.height, "menor_pct_faixa": float(f["pct_qtd"].min()) if f.height else None, "faixa_pequena": faixa_pequena}
    if corr:
        r["pearson"] = arredondar(corr.get("pearson"))
        r["spearman"] = arredondar(corr.get("spearman"))
    if f.height:
        q = f["qtd"].to_numpy()
        m = f["media_target"].fill_null(0.0).to_numpy()
        s = f["desvio_target"].fill_null(0.0).to_numpy()
        media = (q * m).sum() / q.sum()
        ss_between = float((q * (m - media) ** 2).sum())
        ss_total = float((q * (s**2 + (m - media) ** 2)).sum())
        eta2 = ss_between / ss_total if ss_total > 0 else None
        r["eta"] = arredondar(math.sqrt(eta2)) if eta2 is not None else None
        r["r2_faixas"] = arredondar(eta2)
    cont = be.contingencia(spec, spec_target).filter(~pl.col("faixa_a").is_in([COD_NULO]) & ~pl.col("faixa_b").is_in([COD_NULO]))
    if cont.height:
        piv = cont.pivot(on="faixa_b", index="faixa_a", values="qtd", aggregate_function="sum").fill_null(0.0)
        r["informacao_mutua"] = _mi_normalizada(piv.drop("faixa_a").to_numpy().astype(float))
    return r


def _estabilidade(longa: pl.DataFrame, specs, cfg: PoderPreditivoConfig, ref: str, safras: list[str]) -> tuple[pl.DataFrame, dict[str, dict]]:
    linhas: list[dict] = []
    por_var: dict[str, dict] = {}
    spv = specs_por_var(specs)
    for var, spec in spv.items():
        sub = longa.filter(pl.col("variavel") == var)
        f_ref = tabela_faixas(sub.filter(pl.col("safra") == ref), [spec], cfg, "binario")
        m_ref = _iv_ks_gini(f_ref)
        dist_ref = {r["codigo"]: r["qtd"] for r in f_ref.select("codigo", "qtd").iter_rows(named=True)}
        br_ref = {r["codigo"]: r["bad_rate"] for r in f_ref.select("codigo", "bad_rate").iter_rows(named=True)}
        ivs, estaveis, psis, ginis = [], [], [], {}
        for safra in safras:
            f_s = tabela_faixas(sub.filter(pl.col("safra") == safra), [spec], cfg, "binario")
            if not f_s.height:
                continue
            m = _iv_ks_gini(f_s)
            codigos = sorted(set(dist_ref) | set(f_s["codigo"].to_list()))
            dist_s = {r["codigo"]: r["qtd"] for r in f_s.select("codigo", "qtd").iter_rows(named=True)}
            tot_r, tot_s = sum(dist_ref.values()) or 1.0, sum(dist_s.values()) or 1.0
            p = round(psi([dist_ref.get(c, 0.0) / tot_r for c in codigos], [dist_s.get(c, 0.0) / tot_s for c in codigos]), 6)
            br_s = {r["codigo"]: r["bad_rate"] for r in f_s.select("codigo", "bad_rate").iter_rows(named=True)}
            comuns = [c for c in codigos if br_ref.get(c) is not None and br_s.get(c) is not None]
            sp = spearman([br_ref[c] for c in comuns], [br_s[c] for c in comuns]) if len(comuns) >= 3 else (1.0 if len(comuns) == 2 and np.sign(br_ref[comuns[1]] - br_ref[comuns[0]]) == np.sign(br_s[comuns[1]] - br_s[comuns[0]]) else None)
            ordem_ok = bool(sp is not None and sp >= cfg.spearman_estabilidade_ordem)
            qtd = float(f_s["qtd"].sum())
            br = 100 * float(f_s["qtd_maus"].sum()) / qtd if qtd else None
            linhas.append({"variavel": var, "safra": safra, "referencia": safra == ref, "qtd": qtd, "bad_rate": arredondar(br, 4), "iv": m["iv"], "ks": m["ks"], "gini": m["gini"], "psi_faixas": p, "spearman_ordem_vs_ref": arredondar(sp, 4), "ordem_estavel": ordem_ok})
            ginis[safra] = m["gini"]
            if safra != ref:
                ivs.append(m["iv"] or 0.0)
                estaveis.append(ordem_ok)
                psis.append(p)
        ultima = safras[-1]
        por_var[var] = {
            "iv_referencia": m_ref["iv"],
            "gini_referencia": m_ref["gini"],
            "cv_iv": arredondar(float(np.std(ivs, ddof=0) / np.mean(ivs)), 4) if ivs and np.mean(ivs) > 0 else None,
            "estabilidade_ordem": round(100 * sum(estaveis) / len(estaveis), 2) if estaveis else None,
            "psi_faixas_max": max(psis) if psis else None,
            "queda_gini": arredondar(100 * (ginis.get(ultima) - m_ref["gini"]), 2) if (ginis.get(ultima) is not None and m_ref["gini"] is not None and ultima != ref) else None,
        }
    return pl.DataFrame(linhas, schema=_SCHEMA_SAFRA) if linhas else pl.DataFrame(schema=_SCHEMA_SAFRA), por_var


def _flags(r: dict, cfg: PoderPreditivoConfig, binario: bool) -> None:
    obs: list[str] = []
    nome = r["variavel"].lower()
    padrao = any(re.search(p, nome) for p in cfg.padroes_vazamento)
    if binario:
        iv, auc_b, auc_w = r.get("iv") or 0.0, r.get("auc_bruto") or 0.0, r.get("auc_woe") or 0.0
        r["suspeita_vazamento"] = bool(iv > cfg.iv_suspeita_vazamento or auc_b > cfg.auc_suspeita_vazamento or auc_w > cfg.auc_suspeita_vazamento or padrao)
        if r["suspeita_vazamento"]:
            obs.append("suspeita de vazamento (" + "; ".join(filter(None, [f"IV={iv:.3f}" if iv > cfg.iv_suspeita_vazamento else None, f"AUC={max(auc_b, auc_w):.3f}" if max(auc_b, auc_w) > cfg.auc_suspeita_vazamento else None, "nome" if padrao else None])) + ")")
    else:
        r["suspeita_vazamento"] = bool(padrao or (r.get("r2_faixas") or 0.0) > 0.9)
        if r["suspeita_vazamento"]:
            obs.append("suspeita de vazamento")
    r["nao_monotonica"] = bool(r.get("tipo") == "numerica" and (r.get("qtd_inversoes") or 0) > cfg.max_inversoes_monotonica)
    if r["nao_monotonica"]:
        obs.append(f"nao monotonica ({r['qtd_inversoes']} inversoes)")
    instavel = False
    if r.get("cv_iv") is not None and r["cv_iv"] > cfg.cv_iv_max:
        instavel = True
        obs.append(f"cv_iv={r['cv_iv']:.2f}")
    if r.get("estabilidade_ordem") is not None and r["estabilidade_ordem"] < cfg.pct_min_estabilidade_ordem:
        instavel = True
        obs.append(f"ordem instavel ({r['estabilidade_ordem']:.0f}% das safras)")
    if r.get("psi_faixas_max") is not None and r["psi_faixas_max"] > cfg.psi_instavel:
        instavel = True
        obs.append(f"PSI={r['psi_faixas_max']:.2f}")
    r["instavel"] = instavel
    if r.get("faixa_pequena"):
        obs.append("faixa abaixo do minimo")
    r["obs"] = "; ".join(obs) if obs else None


def _recomendar(r: dict, cfg: PoderPreditivoConfig, binario: bool) -> None:
    poder = (r.get("iv") or 0.0) >= cfg.iv_min_selecionar if binario else (r.get("r2_faixas") or 0.0) >= 0.01
    alertas = [a for a, v in (("instavel", r["instavel"]), ("nao monotonica", r["nao_monotonica"]), ("suspeita de vazamento", r["suspeita_vazamento"])) if v]
    if not poder:
        r["recomendacao"], r["motivo_recomendacao"] = "Descartar", "sem poder preditivo" + (" (IV < %.2f)" % cfg.iv_min_selecionar if binario else "")
    elif not r["eleita_profiling"]:
        r["recomendacao"], r["motivo_recomendacao"] = "Descartar", f"nao eleita no profiling: {r.get('motivo_nao_eleita_profiling')}"
    elif alertas:
        r["recomendacao"], r["motivo_recomendacao"] = "Revisar", "; ".join(alertas)
    else:
        r["recomendacao"], r["motivo_recomendacao"] = "Selecionar", "poder preditivo relevante, estavel, sem alertas"


def _redundancia(resumo: list[dict], be: BaseBackend, numericas, categoricas, tipos, cfg: PoderPreditivoConfig) -> None:
    from .config import CorrelacaoConfig
    from .correlation import correlate_backend

    sel = [r["variavel"] for r in resumo if r["recomendacao"] == "Selecionar"]
    for r in resumo:
        r["redundante_com"] = None
    if len(sel) < 2:
        return
    iv_de = {r["variavel"]: (r.get("iv") or r.get("r2_faixas") or 0.0) for r in resumo}
    ccfg = CorrelacaoConfig(limite_associacao_alta_num=cfg.spearman_redundancia, limite_associacao_alta_cat=cfg.cramer_v_redundancia, calcular_vif=False)
    res = correlate_backend(be, sel, ("spearman", "cramer_v"), tipos, ccfg)
    for par in res.pares_altos.iter_rows(named=True):
        a, b = par["var_1"], par["var_2"]
        menor = a if iv_de[a] < iv_de[b] else b
        maior = b if menor == a else a
        for r in resumo:
            if r["variavel"] == menor and r["redundante_com"] is None:
                r["redundante_com"] = maior


def _importancia_multivariada(be: BaseBackend, resumo: pl.DataFrame, target: str, coluna_peso: str | None, cfg: PoderPreditivoConfig) -> pl.DataFrame | None:
    lgb = garantir_dependencia("lightgbm", "importancia multivariada")
    if lgb is None:
        return None
    sel = resumo.filter(pl.col("recomendacao").is_in(["Selecionar", "Revisar"]))["variavel"].to_list()
    if not sel:
        return None
    pdf = be.amostra_pandas([*sel, target, *([coluna_peso] if coluna_peso else [])], cfg.n_amostra_lightgbm, cfg.seed, estratificar_por=target)
    X = pdf[sel].copy()
    for c in X.columns:
        if X[c].dtype == object or str(X[c].dtype) in ("string", "bool", "boolean"):
            X[c] = X[c].astype("category")
    y = pdf[target].astype(float)
    w = pdf[coluna_peso].astype(float) if coluna_peso else None
    modelo = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, verbose=-1, random_state=cfg.seed)
    modelo.fit(X, y, sample_weight=w)
    ganho = modelo.booster_.feature_importance(importance_type="gain")
    out = {"variavel": sel, "importancia_ganho": [float(g) for g in ganho]}
    sk = garantir_dependencia("sklearn.inspection", "importancia por permutacao")
    if sk is not None:
        try:
            perm = sk.permutation_importance(modelo, X, y, scoring="roc_auc", n_repeats=5, random_state=cfg.seed)
            out["importancia_permutacao"] = [float(v) for v in perm.importances_mean]
        except Exception as exc:  # pragma: no cover
            logger.warning("Permutation importance falhou: %s", exc)
    return pl.DataFrame(out).sort("importancia_ganho", descending=True)


_SCHEMA_RESUMO = {
    "ranking": pl.Int64, "variavel": pl.Utf8, "tipo": pl.Utf8, "recomendacao": pl.Utf8, "motivo_recomendacao": pl.Utf8, "redundante_com": pl.Utf8,
    "iv": pl.Float64, "classificacao_iv": pl.Utf8, "ks": pl.Float64, "ks_faixas": pl.Float64, "ks_bruto": pl.Float64, "auc_bruto": pl.Float64, "gini_bruto": pl.Float64,
    "direcao": pl.Utf8, "auc_woe": pl.Float64, "gini_woe": pl.Float64, "monotonicidade": pl.Float64, "qtd_inversoes": pl.Int64, "poder_do_nulo": pl.Float64,
    "bad_rate_nulo": pl.Float64, "pct_nulo": pl.Float64, "informacao_mutua": pl.Float64, "qui_quadrado": pl.Float64, "p_valor_qui_quadrado": pl.Float64,
    "cramer_v": pl.Float64, "p_valor_mann_whitney": pl.Float64, "ponto_bisserial": pl.Float64, "pearson": pl.Float64, "spearman": pl.Float64, "eta": pl.Float64,
    "r2_faixas": pl.Float64, "qtd_faixas": pl.Int64, "menor_pct_faixa": pl.Float64, "metodo_binning": pl.Utf8, "faixa_pequena": pl.Boolean,
    "iv_referencia": pl.Float64, "gini_referencia": pl.Float64, "cv_iv": pl.Float64, "estabilidade_ordem": pl.Float64, "psi_faixas_max": pl.Float64,
    "queda_gini": pl.Float64, "iv_validacao": pl.Float64, "ks_validacao": pl.Float64, "gini_validacao": pl.Float64, "gap_gini": pl.Float64,
    "suspeita_vazamento": pl.Boolean, "instavel": pl.Boolean, "nao_monotonica": pl.Boolean, "eleita_profiling": pl.Boolean,
    "motivo_nao_eleita_profiling": pl.Utf8, "obs": pl.Utf8,
}
_SCHEMA_SAFRA = {
    "variavel": pl.Utf8, "safra": pl.Utf8, "referencia": pl.Boolean, "qtd": pl.Float64, "bad_rate": pl.Float64, "iv": pl.Float64, "ks": pl.Float64,
    "gini": pl.Float64, "psi_faixas": pl.Float64, "spearman_ordem_vs_ref": pl.Float64, "ordem_estavel": pl.Boolean,
}
