"""Profiling de variáveis (seções 2 a 6 e 10 da especificação)."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from ._types import TipoDetectado
from ._utils import arredondar, cronometro, entropia, logger, meses_entre, pct
from .backends import BaseBackend, selecionar_backend
from .config import Backend, ProfileConfig


@dataclass
class ProfileResult:
    """Resultado do profiling.

    Attributes:
        numericas: uma linha por variável numérica.
        categoricas: uma linha por variável categórica (inclui booleanas,
            identificadores e numéricas de baixa cardinalidade).
        datas_resumo / datas_safra: resumo por coluna de data e contagem por safra.
        estabilidade / psi: preenchidos quando ``coluna_safra`` é informada.
        metadados: backend, linhas, tempos, config, colunas ignoradas, tipos detectados.
    """

    numericas: pl.DataFrame
    categoricas: pl.DataFrame
    datas_resumo: pl.DataFrame
    datas_safra: pl.DataFrame
    estabilidade: pl.DataFrame | None = None
    psi: pl.DataFrame | None = None
    bad_rate_safra: pl.DataFrame | None = None
    metadados: dict[str, Any] = field(default_factory=dict)

    @property
    def tipos(self) -> dict[str, TipoDetectado]:
        return dict(self.metadados.get("tipos_detectados", {}))

    def eleitas(self) -> list[str]:
        """Variáveis com ``variavel_eleita == "Eleita"`` (numéricas e categóricas)."""
        out: list[str] = []
        for tab in (self.numericas, self.categoricas):
            if tab.height:
                out += tab.filter(pl.col("variavel_eleita") == "Eleita")["variavel"].to_list()
        return list(dict.fromkeys(out))

    def resumo(self) -> pl.DataFrame:
        """Quantidade de variáveis eleitas/não eleitas por tipo e motivo."""
        partes = []
        for nome, tab in (("numerica", self.numericas), ("categorica", self.categoricas)):
            if tab.height:
                partes.append(
                    tab.select(pl.lit(nome).alias("tipo"), "variavel_eleita", pl.col("motivo_nao_eleita").fill_null("").alias("motivo"))
                    .group_by(["tipo", "variavel_eleita", "motivo"])
                    .agg(pl.len().alias("qtd"))
                )
        if not partes:
            return pl.DataFrame(schema={"tipo": pl.Utf8, "variavel_eleita": pl.Utf8, "motivo": pl.Utf8, "qtd": pl.UInt32})
        return pl.concat(partes).sort(["tipo", "variavel_eleita", "qtd"], descending=[False, False, True])

    def to_excel(self, path: str) -> str:
        from .report import profile_para_excel

        return profile_para_excel(self, path)

    def to_parquet(self, diretorio: str) -> list[str]:
        from .report import profile_para_parquet

        return profile_para_parquet(self, diretorio)


# --------------------------------------------------------------------------- #
def profile(
    df: Any,
    *,
    colunas: list[str] | None = None,
    ignorar: list[str] | None = None,
    tipos: dict[str, str] | None = None,
    coluna_safra: str | None = None,
    safra_referencia: str | None = None,
    target: str | None = None,
    backend: Backend = "auto",
    config: ProfileConfig | None = None,
) -> ProfileResult:
    """Gera o relatório de profiling de uma tabela.

    Args:
        df: ``pandas``, ``polars`` (eager ou lazy) ou ``pyspark.sql.DataFrame``.
        colunas: subconjunto de colunas a analisar (padrão: todas).
        ignorar: colunas excluídas da análise.
        tipos: forçar tipo por coluna: ``"numerica"``, ``"categorica"``, ``"data"``,
            ``"booleana"`` ou ``"identificador"`` (strings convertíveis são convertidas).
        coluna_safra: ativa a seção de estabilidade (preenchimento, PSI, bad rate).
        safra_referencia: safra ``yyyy-MM`` de referência do PSI (padrão: a primeira).
        target: target binário para bad rate por safra.
        backend: ``"auto"`` seleciona pelo tipo de entrada.
        config: :class:`ProfileConfig`.

    Returns:
        :class:`ProfileResult`.
    """
    cfg = config or ProfileConfig()
    inicio = time.perf_counter()
    tempos: dict[str, float] = {}
    be = selecionar_backend(df, backend, cfg)
    be.persistir()
    try:
        return _profile_backend(be, colunas, ignorar, tipos, coluna_safra, safra_referencia, target, cfg, tempos, inicio)
    finally:
        be.liberar()


def _profile_backend(be: BaseBackend, colunas, ignorar, tipos, coluna_safra, safra_referencia, target, cfg: ProfileConfig, tempos, inicio) -> ProfileResult:
    ignorar = set(ignorar or [])
    tipos = dict(tipos or {})
    schema = be.schema()
    total = be.n_linhas()
    logger.info("Backend %s | %s linhas | %s colunas", be.nome, f"{total:,}", len(schema))

    cols = [c for c in (colunas or list(schema)) if c not in ignorar]
    ignoradas: dict[str, str] = {c: "ignorada pelo usuario" for c in ignorar if c in schema}
    for c in cols:
        if c not in schema:
            raise KeyError(f"Coluna '{c}' não existe na tabela.")

    # --- família lógica e conversões forçadas ---------------------------------
    familias = {c: schema[c] for c in cols}
    conversoes: dict[str, str] = {}
    for c, t in tipos.items():
        if c in familias and familias[c] == "string" and t in ("numerica", "data"):
            conversoes[c] = t
    if conversoes:
        be = be.converter_colunas(conversoes, cfg.formato_data)
        for c, t in conversoes.items():
            familias[c] = t  # type: ignore[assignment]

    nao_sup = [c for c in cols if familias[c] == "nao_suportado"]
    for c in nao_sup:
        ignoradas[c] = "tipo nao suportado"
    cols = [c for c in cols if c not in nao_sup]
    if total == 0:
        logger.warning("Tabela vazia: nenhuma métrica calculada.")
        return _resultado_vazio(be, cols, ignoradas, cfg, tempos, inicio)

    numericas = [c for c in cols if familias[c] == "numerica" and tipos.get(c) not in ("categorica", "identificador")]
    strings = [c for c in cols if familias[c] == "string" or tipos.get(c) in ("categorica", "identificador")]
    booleanas = [c for c in cols if familias[c] == "booleana" and c not in strings]
    datas = [c for c in cols if familias[c] == "data"]

    # --- numéricas ----------------------------------------------------------
    with cronometro("estatisticas_numericas", tempos):
        est_num = be.estatisticas_numericas(numericas, cfg.percentis)
    limites: dict[str, tuple[float, float]] = {}
    linhas_num = {r["variavel"]: r for r in est_num.iter_rows(named=True)} if est_num.height else {}
    for c, r in linhas_num.items():
        q1, q3 = r.get("p_25"), r.get("p_75")
        if q1 is not None and q3 is not None:
            iqr = q3 - q1
            limites[c] = (q1 - cfg.fator_iqr * iqr, q3 + cfg.fator_iqr * iqr)
    with cronometro("outliers", tempos):
        out = be.contar_outliers(limites)
    outliers = {r["variavel"]: r for r in out.iter_rows(named=True)} if out.height else {}
    modas_num: dict[str, pl.DataFrame] = {}
    if cfg.calcular_top_n:
        with cronometro("moda_numericas", tempos):
            for c in numericas:
                modas_num[c] = be.frequencias(c, 1)

    tipos_detectados: dict[str, TipoDetectado] = {}
    reg_num: list[dict] = []
    tratadas_como_cat: list[str] = []
    for c in numericas:
        r = linhas_num[c]
        tipo = _tipo_numerico(r, total, cfg, tipos.get(c))
        tipos_detectados[c] = tipo
        if tipo != "identificador" and (r["distintos"] or 0) <= cfg.max_distintos_num_como_cat:
            tratadas_como_cat.append(c)
        reg_num.append(_linha_numerica(c, r, outliers.get(c), modas_num.get(c), total, tipo, c in tratadas_como_cat, cfg))

    # --- categóricas (strings, booleanas, numéricas de baixa cardinalidade) -------
    cat_cols = [*strings, *booleanas, *tratadas_como_cat]
    with cronometro("estatisticas_categoricas", tempos):
        est_cat = be.estatisticas_categoricas(cat_cols, cfg.valores_suspeitos)
    with cronometro("conversibilidade_strings", tempos):
        conv = be.conversibilidade_strings([c for c in strings if c not in conversoes], cfg.formato_data, cfg.amostra_conversibilidade)
    conversiveis = {r["variavel"]: r for r in conv.iter_rows(named=True)} if conv.height else {}
    reg_cat: list[dict] = []
    linhas_cat = {r["variavel"]: r for r in est_cat.iter_rows(named=True)} if est_cat.height else {}
    with cronometro("frequencias_categoricas", tempos):
        for c in cat_cols:
            r = linhas_cat[c]
            if c in tratadas_como_cat:
                tipo: TipoDetectado = "numerica"
            elif c in booleanas:
                tipo = "booleana"
            else:
                tipo = _tipo_categorico(r, total, cfg, tipos.get(c))
            if c not in tratadas_como_cat:
                tipos_detectados[c] = tipo
            freq = be.frequencias(c, cfg.max_categorias_distribuicao) if cfg.calcular_top_n else pl.DataFrame({"valor": [], "qtd": []})
            reg_cat.append(_linha_categorica(c, r, freq, total, tipo, conversiveis.get(c), c in tratadas_como_cat, cfg))

    # --- datas --------------------------------------------------------------
    with cronometro("datas", tempos):
        est_dat = be.estatisticas_datas(datas, cfg.data_minima_valida)
        reg_dat, reg_safra = [], []
        for r in (est_dat.iter_rows(named=True) if est_dat.height else []):
            c = r["variavel"]
            tipos_detectados[c] = "data"
            safras = be.contagem_por_safra(c)
            reg_dat.append(_linha_data(c, r, safras, total))
            reg_safra += _linhas_safra(c, safras)

    # --- estabilidade -------------------------------------------------------
    estab = psi_df = badrate = None
    if coluna_safra:
        from .stability import estabilidade_por_safra

        with cronometro("estabilidade", tempos):
            estab, psi_df, badrate = estabilidade_por_safra(
                be, coluna_safra, safra_referencia, target,
                numericas=[c for c in numericas if tipos_detectados[c] == "numerica"],
                categoricas=[c for c in cat_cols if tipos_detectados.get(c) != "identificador"],
                config=cfg,
            )

    metadados = {
        "backend": be.nome,
        "exato": be.exato,
        "total_linhas": total,
        "total_colunas": len(schema),
        "colunas_analisadas": cols,
        "colunas_ignoradas": ignoradas,
        "tipos_detectados": tipos_detectados,
        "conversoes_aplicadas": conversoes,
        "coluna_safra": coluna_safra,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "tempos": tempos,
        "tempo_total_s": round(time.perf_counter() - inicio, 3),
        "config": cfg.to_dict(),
    }
    return ProfileResult(
        numericas=pl.DataFrame(reg_num, schema=_SCHEMA_NUM(cfg)) if reg_num else pl.DataFrame(schema=_SCHEMA_NUM(cfg)),
        categoricas=pl.DataFrame(reg_cat, schema=_SCHEMA_CAT) if reg_cat else pl.DataFrame(schema=_SCHEMA_CAT),
        datas_resumo=pl.DataFrame(reg_dat, schema=_SCHEMA_DATA) if reg_dat else pl.DataFrame(schema=_SCHEMA_DATA),
        datas_safra=pl.DataFrame(reg_safra, schema=_SCHEMA_SAFRA) if reg_safra else pl.DataFrame(schema=_SCHEMA_SAFRA),
        estabilidade=estab,
        psi=psi_df,
        bad_rate_safra=badrate,
        metadados=metadados,
    )


# --------------------------------------------------------------------------- #
def _tipo_numerico(r: dict, total: int, cfg: ProfileConfig, forcado: str | None) -> TipoDetectado:
    if forcado in ("identificador", "numerica"):
        return forcado  # type: ignore[return-value]
    cont = r["contagem"] or 0
    # Numéricas com casas decimais nunca são identificadores (contínuas têm ~100% de distintos).
    inteira = (r.get("nao_inteiros") or 0) == 0
    if inteira and total >= cfg.min_linhas_identificador and cont > 0 and 100.0 * (r["distintos"] or 0) / cont > cfg.pct_identificador:
        return "identificador"
    return "numerica"


def _tipo_categorico(r: dict, total: int, cfg: ProfileConfig, forcado: str | None) -> TipoDetectado:
    if forcado in ("identificador", "categorica"):
        return forcado  # type: ignore[return-value]
    cont = r["contagem"] or 0
    if total >= cfg.min_linhas_identificador and cont > 0 and 100.0 * (r["distintos"] or 0) / cont > cfg.pct_identificador:
        return "identificador"
    return "categorica"


def _linha_numerica(c, r, outl, moda_df, total, tipo, tratada_cat, cfg: ProfileConfig) -> dict:
    cont = r["contagem"] or 0
    media, dp = r["media"], r["desvio_padrao"]
    obs: list[str] = []
    cv = None
    if media is not None and dp is not None:
        if abs(media) < cfg.epsilon_media:
            variabilidade = bool(dp > 0 and (r["distintos"] or 0) > 1)
            obs.append("media ~ 0: variabilidade avaliada por desvio_padrao > 0 e distintos > 1")
        else:
            cv = dp / abs(media)
            variabilidade = bool(cv > cfg.limite_coef_variacao)
    else:
        variabilidade = False
    q1, q3 = r.get("p_25"), r.get("p_75")
    iqr = (q3 - q1) if (q1 is not None and q3 is not None) else None
    moda = pct_moda = None
    if moda_df is not None and moda_df.height:
        moda = float(moda_df["valor"][0]) if _eh_float(moda_df["valor"][0]) else None
        pct_moda = pct(moda_df["qtd"][0], cont)
    quase_const = bool(pct_moda is not None and pct_moda > cfg.pct_quase_constante)
    pct_nulos = pct(r["nulos"], total)
    flag_nulo = int(pct_nulos is not None and pct_nulos > cfg.pct_nulos_max_eleita)
    motivos = []
    if tipo == "identificador":
        motivos.append("identificador")
    if not variabilidade:
        motivos.append("sem variabilidade")
    if flag_nulo:
        motivos.append(f"nulos > {cfg.pct_nulos_max_eleita:g}%")
    if quase_const:
        motivos.append("quase constante")
    if cont == 0:
        motivos = ["totalmente nula"]
        obs.append("coluna totalmente nula")
    if (r["distintos"] or 0) == 1:
        obs.append("valor unico")
    if (r["nan_count"] or 0) or (r["inf_count"] or 0):
        obs.append("NaN/inf excluidos das estatisticas")
    if tratada_cat:
        obs.append("baixa cardinalidade: tambem tratada como categorica")
    eleita = "Eleita" if not motivos else "Nao Eleita"
    linha = {
        "variavel": c,
        "tipo_detectado": tipo,
        "tratada_como_categorica": tratada_cat,
        "contagem": int(cont),
        "nulos": int(r["nulos"] or 0),
        "pct_nulos": pct_nulos,
        "nan_count": int(r["nan_count"] or 0),
        "inf_count": int(r["inf_count"] or 0),
        "media": arredondar(media),
        "desvio_padrao": arredondar(dp),
        "variancia": arredondar(r["variancia"]),
        "coef_variacao": arredondar(cv),
        "minimo": arredondar(r["minimo"]),
        "maximo": arredondar(r["maximo"]),
    }
    for p in cfg.percentis:
        from ._utils import nome_percentil

        linha[nome_percentil(p)] = arredondar(r.get(nome_percentil(p)))
    linha.update(
        {
            "iqr": arredondar(iqr),
            "lim_inf_outlier": arredondar(q1 - cfg.fator_iqr * iqr) if iqr is not None else None,
            "lim_sup_outlier": arredondar(q3 + cfg.fator_iqr * iqr) if iqr is not None else None,
            "qtd_outliers_inf": int(outl["qtd_outliers_inf"]) if outl else 0,
            "qtd_outliers_sup": int(outl["qtd_outliers_sup"]) if outl else 0,
            "pct_outliers_inf": pct(outl["qtd_outliers_inf"], cont) if outl else None,
            "pct_outliers_sup": pct(outl["qtd_outliers_sup"], cont) if outl else None,
            "distintos": int(r["distintos"] or 0),
            "zeros": int(r["zeros"] or 0),
            "pct_zeros": pct(r["zeros"], cont),
            "negativos": int(r["negativos"] or 0),
            "pct_negativos": pct(r["negativos"], cont),
            "assimetria": arredondar(r["assimetria"]),
            "curtose": arredondar(r["curtose"]),
            "moda": moda,
            "pct_moda": pct_moda,
            "quase_constante": quase_const,
            "variabilidade": variabilidade,
            "flag_pct_nulo": flag_nulo,
            "variavel_eleita": eleita,
            "motivo_nao_eleita": "; ".join(motivos) if motivos else None,
            "obs": "; ".join(obs) if obs else None,
        }
    )
    return linha


def _eh_float(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _linha_categorica(c, r, freq: pl.DataFrame, total, tipo, conv, tratada_cat, cfg: ProfileConfig) -> dict:
    cont = r["contagem"] or 0
    distintos = int(r["distintos"] or 0)
    pct_nulos = pct(r["nulos"], total)
    flag_nulo = int(pct_nulos is not None and pct_nulos > cfg.pct_nulos_max_eleita)
    moda = freq_moda = pct_moda = None
    top_n_json = None
    raras = ent = ent_norm = None
    obs: list[str] = []
    if freq.height:
        moda = str(freq["valor"][0])
        freq_moda = float(freq["qtd"][0])
        pct_moda = pct(freq_moda, cont)
        top = freq.head(cfg.top_n)
        top_n_json = json.dumps(
            [{"valor": v, "qtd": q, "pct": pct(q, cont)} for v, q in zip(top["valor"].to_list(), top["qtd"].to_list())],
            ensure_ascii=False,
        )
        qtds = freq["qtd"].to_list()
        nao_raras = sum(1 for q in qtds if cont and 100.0 * q / cont >= cfg.pct_categoria_rara)
        raras = max(distintos - nao_raras, 0)
        if distintos > freq.height:
            resto = max(cont - sum(qtds), 0)
            ent, ent_norm = entropia([*qtds, resto])
            obs.append(f"entropia aproximada (top {freq.height} categorias + resto)")
        else:
            ent, ent_norm = entropia(qtds)
    constante = distintos == 1
    quase_const = bool(pct_moda is not None and pct_moda > cfg.pct_quase_constante)
    tipo_sugerido = None
    if conv:
        if (conv.get("pct_numerico") or 0) >= cfg.pct_conversivel:
            tipo_sugerido = "numerica"
        elif (conv.get("pct_data") or 0) >= cfg.pct_conversivel:
            tipo_sugerido = "data"
    motivos = []
    if tipo == "identificador":
        motivos.append("identificador")
    if flag_nulo:
        motivos.append(f"nulos > {cfg.pct_nulos_max_eleita:g}%")
    if constante:
        motivos.append("constante")
    if quase_const and not constante:
        motivos.append("quase constante")
    if cont == 0:
        motivos = ["totalmente nula"]
        obs.append("coluna totalmente nula")
    if tratada_cat:
        obs.append("numerica de baixa cardinalidade")
    if tipo_sugerido:
        obs.append(f"100% conversivel para {tipo_sugerido}")
    return {
        "variavel": c,
        "tipo_detectado": tipo,
        "tipo_sugerido": tipo_sugerido,
        "contagem": int(cont),
        "nulos": int(r["nulos"] or 0),
        "pct_nulos": pct_nulos,
        "distintos": distintos,
        "vazios": int(r["vazios"] or 0),
        "pct_vazios": pct(r["vazios"], cont),
        "tamanho_min": r["tamanho_min"],
        "tamanho_max": r["tamanho_max"],
        "tamanho_medio": arredondar(r["tamanho_medio"], 2),
        "moda": moda,
        "freq_moda": freq_moda,
        "pct_moda": pct_moda,
        "top_n": top_n_json,
        "qtd_categorias_raras": raras,
        "entropia": ent,
        "entropia_normalizada": ent_norm,
        "alta_cardinalidade": distintos > cfg.max_distintos_alta_cardinalidade,
        "constante": constante,
        "quase_constante": quase_const,
        "pct_valores_suspeitos": pct(r["qtd_suspeitos"], cont),
        "flag_pct_nulo": flag_nulo,
        "variavel_eleita": "Eleita" if not motivos else "Nao Eleita",
        "motivo_nao_eleita": "; ".join(motivos) if motivos else None,
        "obs": "; ".join(obs) if obs else None,
    }


def _linha_data(c, r, safras: pl.DataFrame, total) -> dict:
    lista = safras["safra"].to_list() if safras.height else []
    faltantes = [m for m in meses_entre(lista[0], lista[-1])] if lista else []
    faltantes = [m for m in faltantes if m not in set(lista)]
    return {
        "variavel": c,
        "contagem": int(r["contagem"] or 0),
        "nulos": int(r["nulos"] or 0),
        "pct_nulos": pct(r["nulos"], total),
        "data_min": r["data_min"],
        "data_max": r["data_max"],
        "qtd_safras": len(lista),
        "datas_futuras": int(r["datas_futuras"] or 0),
        "datas_invalidas": int(r["datas_invalidas"] or 0),
        "qtd_safras_faltantes": len(faltantes),
        "safras_faltantes": json.dumps(faltantes),
    }


def _linhas_safra(c, safras: pl.DataFrame) -> list[dict]:
    if not safras.height:
        return []
    total = float(safras["contagem"].sum())
    out, anterior = [], None
    for s, n in zip(safras["safra"].to_list(), safras["contagem"].to_list()):
        var = round(100.0 * (n - anterior) / anterior, 2) if anterior else None
        out.append({"variavel": c, "safra": s, "contagem": float(n), "pct_total": pct(n, total), "variacao_mom": var})
        anterior = n
    return out


def _resultado_vazio(be, cols, ignoradas, cfg, tempos, inicio) -> ProfileResult:
    return ProfileResult(
        numericas=pl.DataFrame(schema=_SCHEMA_NUM(cfg)),
        categoricas=pl.DataFrame(schema=_SCHEMA_CAT),
        datas_resumo=pl.DataFrame(schema=_SCHEMA_DATA),
        datas_safra=pl.DataFrame(schema=_SCHEMA_SAFRA),
        metadados={"backend": be.nome, "total_linhas": 0, "colunas_analisadas": cols, "colunas_ignoradas": ignoradas, "tipos_detectados": {}, "tempos": tempos, "tempo_total_s": round(time.perf_counter() - inicio, 3), "config": cfg.to_dict(), "timestamp": dt.datetime.now().isoformat(timespec="seconds")},
    )


def _SCHEMA_NUM(cfg: ProfileConfig) -> dict:  # noqa: N802
    from ._utils import nome_percentil

    base = {
        "variavel": pl.Utf8, "tipo_detectado": pl.Utf8, "tratada_como_categorica": pl.Boolean, "contagem": pl.Int64, "nulos": pl.Int64,
        "pct_nulos": pl.Float64, "nan_count": pl.Int64, "inf_count": pl.Int64, "media": pl.Float64, "desvio_padrao": pl.Float64,
        "variancia": pl.Float64, "coef_variacao": pl.Float64, "minimo": pl.Float64, "maximo": pl.Float64,
    }
    base.update({nome_percentil(p): pl.Float64 for p in cfg.percentis})
    base.update(
        {
            "iqr": pl.Float64, "lim_inf_outlier": pl.Float64, "lim_sup_outlier": pl.Float64, "qtd_outliers_inf": pl.Int64, "qtd_outliers_sup": pl.Int64,
            "pct_outliers_inf": pl.Float64, "pct_outliers_sup": pl.Float64, "distintos": pl.Int64, "zeros": pl.Int64, "pct_zeros": pl.Float64,
            "negativos": pl.Int64, "pct_negativos": pl.Float64, "assimetria": pl.Float64, "curtose": pl.Float64, "moda": pl.Float64, "pct_moda": pl.Float64,
            "quase_constante": pl.Boolean, "variabilidade": pl.Boolean, "flag_pct_nulo": pl.Int64, "variavel_eleita": pl.Utf8,
            "motivo_nao_eleita": pl.Utf8, "obs": pl.Utf8,
        }
    )
    return base


_SCHEMA_CAT = {
    "variavel": pl.Utf8, "tipo_detectado": pl.Utf8, "tipo_sugerido": pl.Utf8, "contagem": pl.Int64, "nulos": pl.Int64, "pct_nulos": pl.Float64,
    "distintos": pl.Int64, "vazios": pl.Int64, "pct_vazios": pl.Float64, "tamanho_min": pl.Int64, "tamanho_max": pl.Int64, "tamanho_medio": pl.Float64,
    "moda": pl.Utf8, "freq_moda": pl.Float64, "pct_moda": pl.Float64, "top_n": pl.Utf8, "qtd_categorias_raras": pl.Int64, "entropia": pl.Float64,
    "entropia_normalizada": pl.Float64, "alta_cardinalidade": pl.Boolean, "constante": pl.Boolean, "quase_constante": pl.Boolean,
    "pct_valores_suspeitos": pl.Float64, "flag_pct_nulo": pl.Int64, "variavel_eleita": pl.Utf8, "motivo_nao_eleita": pl.Utf8, "obs": pl.Utf8,
}
_SCHEMA_DATA = {
    "variavel": pl.Utf8, "contagem": pl.Int64, "nulos": pl.Int64, "pct_nulos": pl.Float64, "data_min": pl.Date, "data_max": pl.Date,
    "qtd_safras": pl.Int64, "datas_futuras": pl.Int64, "datas_invalidas": pl.Int64, "qtd_safras_faltantes": pl.Int64, "safras_faltantes": pl.Utf8,
}
_SCHEMA_SAFRA = {"variavel": pl.Utf8, "safra": pl.Utf8, "contagem": pl.Float64, "pct_total": pl.Float64, "variacao_mom": pl.Float64}
