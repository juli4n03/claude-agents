"""Backend Polars (``LazyFrame``, expressões vetorizadas, resultados exatos)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

import polars as pl

from .._types import (
    COD_NULO,
    COD_OUTROS,
    COD_VAZIO,
    PREFIXO_BIN,
    PREFIXO_CAT,
    BinSpec,
    TipoLogico,
)
from .._utils import FORMATOS_DATA_PADRAO, codigo_especial, ks_auc_de_distribuicao, nome_percentil
from .base import BaseBackend

_NUMERICOS = (
    pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.Int128,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
    pl.Float32, pl.Float64,
)


def _familia(dtype: pl.DataType) -> TipoLogico:
    if dtype == pl.Boolean:
        return "booleana"
    if dtype in _NUMERICOS or dtype.is_numeric():
        return "numerica"
    if dtype in (pl.Utf8, pl.String, pl.Categorical) or isinstance(dtype, (pl.Categorical, pl.Enum)):
        return "string"
    if dtype == pl.Date or isinstance(dtype, pl.Datetime):
        return "data"
    return "nao_suportado"


def _num(c: str) -> pl.Expr:
    """Coluna numérica como Float64 com NaN/±inf removidos (viram null)."""
    x = pl.col(c).cast(pl.Float64)
    return pl.when(x.is_finite()).then(x).otherwise(None)


def _data(c: str) -> pl.Expr:
    return pl.col(c).cast(pl.Date, strict=False)


def _safra(c: str) -> pl.Expr:
    """Safra ``yyyy-MM`` a partir de data/timestamp ou string."""
    return pl.coalesce(
        pl.col(c).cast(pl.Date, strict=False).dt.strftime("%Y-%m"),
        pl.col(c).cast(pl.Utf8).str.slice(0, 7),
    ).alias("safra")


def _para_data(c: str, formato: str | None) -> pl.Expr:
    s = pl.col(c).cast(pl.Utf8).str.strip_chars()
    if formato:
        return s.str.to_date(formato, strict=False)
    return pl.coalesce(
        *[
            s.str.to_datetime(f, strict=False).cast(pl.Date) if "%H" in f else s.str.to_date(f, strict=False)
            for f in FORMATOS_DATA_PADRAO
        ]
    )


def expressao_faixa(spec: BinSpec) -> pl.Expr:
    """Expressão Polars que devolve o **código** de faixa de uma variável."""
    if spec.tipo == "numerica":
        x = _num(spec.variavel)
        if spec.cortes:
            codigo = pl.sum_horizontal([(x >= c).cast(pl.Int32) for c in spec.cortes])
        else:
            codigo = pl.lit(0, dtype=pl.Int32)
        codigo_str = pl.concat_str([pl.lit(PREFIXO_BIN), codigo.cast(pl.Utf8).str.zfill(2)])
        expr = pl.when(x.is_null()).then(pl.lit(COD_NULO))
        for esp in spec.valores_especiais:
            expr = expr.when(x == float(esp)).then(pl.lit(codigo_especial(esp)))
        return expr.otherwise(codigo_str)
    s = pl.col(spec.variavel).cast(pl.Utf8)
    return (
        pl.when(s.is_null())
        .then(pl.lit(COD_NULO))
        .when(s.str.strip_chars() == "")
        .then(pl.lit(COD_VAZIO))
        .when(s.is_in(list(spec.categorias)) if spec.categorias else pl.lit(False))
        .then(pl.concat_str([pl.lit(PREFIXO_CAT), s]))
        .otherwise(pl.lit(COD_OUTROS))
    )


class PolarsBackend(BaseBackend):
    """Backend sobre ``polars.LazyFrame``."""

    nome = "polars"
    exato = True

    def __init__(self, df: pl.DataFrame | pl.LazyFrame, origem_pandas: bool = False):
        self.lf: pl.LazyFrame = df.lazy() if isinstance(df, pl.DataFrame) else df
        self.origem_pandas = origem_pandas
        self._n: int | None = None
        self._schema = self.lf.collect_schema()

    # ------------------------------------------------------------------ geral
    def n_linhas(self) -> int:
        if self._n is None:
            self._n = int(self.lf.select(pl.len()).collect().item())
        return self._n

    def schema(self) -> dict[str, TipoLogico]:
        return {c: _familia(t) for c, t in self._schema.items()}

    def persistir(self) -> None:  # Polars resolve o plano a cada collect; cache explícito
        self.lf = self.lf.collect().lazy()

    def liberar(self) -> None:
        pass

    def filtrar_nao_nulo(self, coluna: str) -> "PolarsBackend":
        return PolarsBackend(self.lf.filter(pl.col(coluna).is_not_null()), self.origem_pandas)

    def preparar_target(self, target, mapa, binario):
        t = pl.col(target)
        if mapa:
            t = t.cast(pl.Utf8).replace_strict({str(k): float(v) for k, v in mapa.items()}, default=None, return_dtype=pl.Float64)
        else:
            t = t.cast(pl.Float64, strict=False)
        if binario:
            t = pl.when(t.is_in([0.0, 1.0])).then(t).otherwise(None)
        return PolarsBackend(self.lf.with_columns(t.alias(target)), self.origem_pandas)

    def filtrar_safra(self, coluna_safra, safra):
        return PolarsBackend(self.lf.filter(_safra(coluna_safra) == safra), self.origem_pandas)

    def amostra_pandas(self, colunas, n, seed, estratificar_por=None):
        df = self.lf.select(list(dict.fromkeys([*colunas, *([estratificar_por] if estratificar_por else [])]))).collect()
        if df.height > n:
            df = df.sample(n=n, seed=seed, shuffle=True)
        return df.to_pandas()

    def converter_colunas(self, conversoes, formato_data):
        exprs = []
        for c, tipo in conversoes.items():
            if tipo == "numerica":
                exprs.append(pl.col(c).cast(pl.Utf8).str.strip_chars().cast(pl.Float64, strict=False).alias(c))
            elif tipo == "data":
                exprs.append(_para_data(c, formato_data).alias(c))
        return PolarsBackend(self.lf.with_columns(exprs) if exprs else self.lf, self.origem_pandas)

    # -------------------------------------------------------------- profiling
    def estatisticas_numericas(self, colunas, percentis):
        if not colunas:
            return pl.DataFrame()
        exprs: list[pl.Expr] = []
        for c in colunas:
            raw = pl.col(c).cast(pl.Float64)
            x = _num(c)
            exprs += [
                x.count().alias(f"{c}||contagem"),
                raw.null_count().alias(f"{c}||nulos"),
                raw.is_nan().sum().alias(f"{c}||nan_count"),
                raw.is_infinite().sum().alias(f"{c}||inf_count"),
                x.mean().alias(f"{c}||media"),
                x.std(ddof=1).alias(f"{c}||desvio_padrao"),
                x.var(ddof=1).alias(f"{c}||variancia"),
                x.min().alias(f"{c}||minimo"),
                x.max().alias(f"{c}||maximo"),
                x.drop_nulls().n_unique().alias(f"{c}||distintos"),
                (x == 0).sum().alias(f"{c}||zeros"),
                (x < 0).sum().alias(f"{c}||negativos"),
                (x != x.floor()).sum().alias(f"{c}||nao_inteiros"),
                x.skew().alias(f"{c}||assimetria"),
                x.kurtosis(fisher=True).alias(f"{c}||curtose"),
            ]
            exprs += [x.quantile(p, interpolation="linear").alias(f"{c}||{nome_percentil(p)}") for p in percentis]
        linha = self.lf.select(exprs).collect().row(0, named=True)
        return _reformatar(linha, colunas)

    def contar_outliers(self, limites):
        if not limites:
            return pl.DataFrame({"variavel": [], "qtd_outliers_inf": [], "qtd_outliers_sup": []})
        exprs = []
        for c, (li, ls) in limites.items():
            x = _num(c)
            exprs += [(x < li).sum().alias(f"{c}||qtd_outliers_inf"), (x > ls).sum().alias(f"{c}||qtd_outliers_sup")]
        return _reformatar(self.lf.select(exprs).collect().row(0, named=True), list(limites))

    def estatisticas_categoricas(self, colunas, valores_suspeitos):
        if not colunas:
            return pl.DataFrame()
        exprs = []
        for c in colunas:
            s = pl.col(c).cast(pl.Utf8)
            t = s.str.strip_chars()
            exprs += [
                s.count().alias(f"{c}||contagem"),
                s.null_count().alias(f"{c}||nulos"),
                s.drop_nulls().n_unique().alias(f"{c}||distintos"),
                (t == "").sum().alias(f"{c}||vazios"),
                s.str.len_chars().min().alias(f"{c}||tamanho_min"),
                s.str.len_chars().max().alias(f"{c}||tamanho_max"),
                s.str.len_chars().mean().alias(f"{c}||tamanho_medio"),
                t.is_in(list(valores_suspeitos)).sum().alias(f"{c}||qtd_suspeitos"),
            ]
        return _reformatar(self.lf.select(exprs).collect().row(0, named=True), colunas)

    def frequencias(self, coluna, limite):
        base = self.lf.select(pl.col(coluna))
        if _familia(self._schema[coluna]) == "numerica":
            # exclui NaN/±inf mantendo o dtype original (int 1 -> "1", nunca "1.0")
            base = base.filter(pl.col(coluna).cast(pl.Float64).is_finite())
        return (
            base.select(pl.col(coluna).cast(pl.Utf8).alias("valor"))
            .filter(pl.col("valor").is_not_null())
            .group_by("valor")
            .agg(pl.len().alias("qtd"))
            .sort(["qtd", "valor"], descending=[True, False])
            .head(limite)
            .collect()
            .with_columns(pl.col("qtd").cast(pl.Float64))
        )

    def estatisticas_datas(self, colunas, data_minima):
        if not colunas:
            return pl.DataFrame()
        hoje = dt.date.today()
        minima = dt.date.fromisoformat(data_minima)
        exprs = []
        for c in colunas:
            d = _data(c)
            exprs += [
                d.count().alias(f"{c}||contagem"),
                d.null_count().alias(f"{c}||nulos"),
                d.min().alias(f"{c}||data_min"),
                d.max().alias(f"{c}||data_max"),
                (d > pl.lit(hoje)).sum().alias(f"{c}||datas_futuras"),
                (d < pl.lit(minima)).sum().alias(f"{c}||datas_invalidas"),
            ]
        return _reformatar(self.lf.select(exprs).collect().row(0, named=True), colunas)

    def contagem_por_safra(self, coluna):
        return (
            self.lf.select(_safra(coluna))
            .filter(pl.col("safra").is_not_null())
            .group_by("safra")
            .agg(pl.len().cast(pl.Float64).alias("contagem"))
            .sort("safra")
            .collect()
        )

    def conversibilidade_strings(self, colunas, formato_data, n_amostra):
        if not colunas:
            return pl.DataFrame({"variavel": [], "pct_numerico": [], "pct_data": []})
        exprs = []
        for c in colunas:
            s = pl.col(c).cast(pl.Utf8).str.strip_chars().drop_nulls()
            exprs += [
                (s.cast(pl.Float64, strict=False).is_not_null().mean() * 100).alias(f"{c}||pct_numerico"),
                (_para_data(c, formato_data).drop_nulls().count() / pl.col(c).count() * 100).alias(f"{c}||pct_data"),
            ]
        linha = self.lf.head(n_amostra).select(exprs).collect().row(0, named=True)
        return _reformatar(linha, colunas)

    # ------------------------------------------------------------ estabilidade
    def preenchimento_por_safra(self, coluna_safra, numericas, categoricas):
        exprs = [pl.len().alias("__n")]
        for c in numericas:
            exprs += [pl.col(c).null_count().alias(f"{c}||nulos"), _num(c).mean().alias(f"{c}||media")]
        for c in categoricas:
            exprs += [pl.col(c).null_count().alias(f"{c}||nulos")]
        wide = self.lf.group_by(_safra(coluna_safra)).agg(exprs).filter(pl.col("safra").is_not_null()).sort("safra").collect()
        linhas = []
        for r in wide.iter_rows(named=True):
            n = r["__n"]
            for c in [*numericas, *categoricas]:
                linhas.append(
                    {
                        "safra": r["safra"],
                        "variavel": c,
                        "contagem": float(n),
                        "pct_nulos": round(100.0 * r[f"{c}||nulos"] / n, 2) if n else None,
                        "media": r.get(f"{c}||media"),
                    }
                )
        return pl.DataFrame(linhas, schema={"safra": pl.Utf8, "variavel": pl.Utf8, "contagem": pl.Float64, "pct_nulos": pl.Float64, "media": pl.Float64})

    def moda_por_safra(self, coluna_safra, coluna):
        base = self.lf.select(_safra(coluna_safra), pl.col(coluna).cast(pl.Utf8).alias("valor")).filter(
            pl.col("safra").is_not_null() & pl.col("valor").is_not_null()
        )
        freq = base.group_by(["safra", "valor"]).agg(pl.len().alias("qtd"))
        tot = base.group_by("safra").agg(pl.len().alias("total"))
        return (
            freq.sort(["safra", "qtd", "valor"], descending=[False, True, False])
            .group_by("safra", maintain_order=True)
            .first()
            .join(tot, on="safra")
            .select("safra", pl.col("valor").alias("moda"), (pl.col("qtd") / pl.col("total") * 100).round(2).alias("pct_moda"))
            .sort("safra")
            .collect()
        )

    def media_target_por_safra(self, coluna_safra, target, peso):
        y = pl.col(target).cast(pl.Float64)
        if peso:
            w = pl.col(peso).cast(pl.Float64)
            aggs = [w.sum().alias("contagem"), ((w * y).sum() / w.sum()).alias("media_target")]
        else:
            aggs = [pl.len().cast(pl.Float64).alias("contagem"), y.mean().alias("media_target")]
        return (
            self.lf.filter(pl.col(target).is_not_null())
            .group_by(_safra(coluna_safra))
            .agg(aggs)
            .filter(pl.col("safra").is_not_null())
            .sort("safra")
            .collect()
        )

    # --------------------------------------------------------- poder preditivo
    def quantis(self, colunas, probabilidades):
        if not colunas:
            return {}
        exprs = [_num(c).quantile(p, interpolation="linear").alias(f"{c}||{i}") for c in colunas for i, p in enumerate(probabilidades)]
        linha = self.lf.select(exprs).collect().row(0, named=True)
        return {c: [linha[f"{c}||{i}"] for i in range(len(probabilidades))] for c in colunas}

    def top_categorias(self, coluna, k):
        return self.frequencias(coluna, k)["valor"].to_list()

    def tabela_faixas_longa(self, specs, target, tipo_target, coluna_safra, coluna_peso):
        w = pl.col(coluna_peso).cast(pl.Float64) if coluna_peso else pl.lit(1.0)
        aggs = [(w.sum() if coluna_peso else pl.len().cast(pl.Float64)).alias("qtd")]
        base = self.lf
        if target is not None:
            y = pl.col(target).cast(pl.Float64)
            base = base.filter(pl.col(target).is_not_null())
            if tipo_target == "binario":
                aggs.append((w * y).sum().alias("maus"))
            else:
                aggs += [(w * y).sum().alias("soma"), (w * y * y).sum().alias("soma_quad"), y.median().alias("mediana")]
        chaves = [expressao_faixa(s).alias("faixa") for s in specs]
        planos = []
        for spec, chave in zip(specs, chaves):
            grupo = [chave] + ([_safra(coluna_safra)] if coluna_safra else [])
            plano = base.group_by(grupo).agg(aggs).with_columns(pl.lit(spec.variavel).alias("variavel"))
            if not coluna_safra:
                plano = plano.with_columns(pl.lit(None, dtype=pl.Utf8).alias("safra"))
            planos.append(plano)
        if not planos:
            return pl.DataFrame()
        partes = pl.collect_all(planos)
        cols = ["variavel", "faixa", "safra", *[a.meta.output_name() for a in aggs]]
        return pl.concat([p.select(cols) for p in partes]).sort(["variavel", "safra", "faixa"])

    def ks_auc_bruto(self, colunas, target, coluna_peso):
        w = pl.col(coluna_peso).cast(pl.Float64) if coluna_peso else pl.lit(1.0)
        y = pl.col(target).cast(pl.Float64)
        planos = []
        for c in colunas:
            x = _num(c)
            d = self.lf.filter(x.is_not_null() & pl.col(target).is_not_null()).select(x.alias("x"), y.alias("y"), w.alias("w"))
            planos.append(d.group_by("x").agg((pl.col("w") * pl.col("y")).sum().alias("w1"), (pl.col("w") * (1 - pl.col("y"))).sum().alias("w0"), pl.len().alias("n")).sort("x"))
            planos.append(d.select(pl.corr(pl.col("x"), pl.col("y")).alias("r")))
        resultados = pl.collect_all(planos) if planos else []
        linhas = []
        for i, c in enumerate(colunas):
            dist, corr = resultados[2 * i], resultados[2 * i + 1]
            ks, auc = ks_auc_de_distribuicao(dist)
            r = corr["r"][0] if corr.height else None
            linhas.append({"variavel": c, "ks_bruto": ks, "auc_bruto": auc, "ponto_bisserial": r, "n_obs": float(dist["n"].sum()) if dist.height else 0.0})
        return pl.DataFrame(linhas, schema={"variavel": pl.Utf8, "ks_bruto": pl.Float64, "auc_bruto": pl.Float64, "ponto_bisserial": pl.Float64, "n_obs": pl.Float64})

    def correlacoes_com_target(self, colunas, target):
        y = pl.col(target).cast(pl.Float64)
        exprs = []
        for c in colunas:
            x = _num(c)
            m = x.is_not_null() & y.is_not_null()
            exprs += [
                pl.corr(x.filter(m), y.filter(m)).alias(f"{c}||pearson"),
                pl.corr(x.filter(m), y.filter(m), method="spearman").alias(f"{c}||spearman"),
                m.sum().cast(pl.Float64).alias(f"{c}||n_obs"),
            ]
        if not exprs:
            return pl.DataFrame({"variavel": [], "pearson": [], "spearman": [], "n_obs": []})
        return _reformatar(self.lf.select(exprs).collect().row(0, named=True), colunas)

    def aplicar_faixas(self, specs, mapa_woe, sufixo):
        exprs = []
        for spec in specs:
            mapa = mapa_woe.get(spec.variavel, {})
            exprs.append(
                expressao_faixa(spec).replace_strict(mapa, default=None, return_dtype=pl.Float64).alias(f"{spec.variavel}{sufixo}")
            )
        out = self.lf.with_columns(exprs)
        if self.origem_pandas:
            return out.collect().to_pandas()
        return out.collect()

    # ------------------------------------------------------------- correlação
    def correlacao_numerica(self, colunas, metodo, pairwise, amostra, seed):
        lf = self.lf.select([_num(c).alias(c) for c in colunas])
        amostral = False
        if amostra is not None:
            df = lf.collect()
            n = int(amostra) if amostra >= 1 else int(df.height * amostra)
            if n < df.height:
                df = df.sample(n=n, seed=seed, shuffle=True)
                amostral = True
            lf = df.lazy()
        if not pairwise:
            lf = lf.drop_nulls()
        exprs, pares = [], []
        for i, a in enumerate(colunas):
            for b in colunas[i + 1 :]:
                xa, xb = pl.col(a), pl.col(b)
                m = xa.is_not_null() & xb.is_not_null()
                exprs += [pl.corr(xa.filter(m), xb.filter(m), method=metodo).alias(f"{a}|{b}|v"), m.sum().cast(pl.Float64).alias(f"{a}|{b}|n")]
                pares.append((a, b))
        if not pares:
            return pl.DataFrame({"var_1": [], "var_2": [], "valor": [], "n_obs": []}, schema={"var_1": pl.Utf8, "var_2": pl.Utf8, "valor": pl.Float64, "n_obs": pl.Float64}), amostral
        linha = lf.select(exprs).collect().row(0, named=True)
        return (
            pl.DataFrame(
                {
                    "var_1": [a for a, _ in pares],
                    "var_2": [b for _, b in pares],
                    "valor": [linha[f"{a}|{b}|v"] for a, b in pares],
                    "n_obs": [linha[f"{a}|{b}|n"] for a, b in pares],
                },
                schema={"var_1": pl.Utf8, "var_2": pl.Utf8, "valor": pl.Float64, "n_obs": pl.Float64},
            ),
            amostral,
        )

    def contingencia(self, spec_a, spec_b):
        return (
            self.lf.group_by(expressao_faixa(spec_a).alias("faixa_a"), expressao_faixa(spec_b).alias("faixa_b"))
            .agg(pl.len().cast(pl.Float64).alias("qtd"))
            .sort(["faixa_a", "faixa_b"])
            .collect()
        )

    def estatisticas_por_grupo(self, numerica, categorica, k):
        spec = BinSpec(categorica, "categorica", categorias=self.top_categorias(categorica, k))
        x = _num(numerica)
        return (
            self.lf.filter(x.is_not_null())
            .group_by(expressao_faixa(spec).alias("grupo"))
            .agg(pl.len().cast(pl.Float64).alias("n"), x.mean().alias("media"), x.var(ddof=1).alias("variancia"))
            .sort("grupo")
            .collect()
        )


def _reformatar(linha: dict[str, Any], colunas: Sequence[str]) -> pl.DataFrame:
    """Transforma uma linha ``{"col||metrica": valor}`` em uma tabela por variável."""
    registros = []
    for c in colunas:
        pref = f"{c}||"
        reg = {"variavel": c}
        reg.update({k[len(pref):]: v for k, v in linha.items() if k.startswith(pref)})
        registros.append(reg)
    return pl.DataFrame(registros, infer_schema_length=None)
