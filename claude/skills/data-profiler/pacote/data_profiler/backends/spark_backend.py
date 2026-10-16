"""Backend PySpark.

Estratégia de performance:
    * todas as métricas agregáveis saem de **um único** ``agg()`` com expressões
      geradas dinamicamente (uma passada pela base);
    * percentis via ``percentile_approx`` para todas as colunas na mesma passada;
    * contagem de outliers numa segunda passada única;
    * poder preditivo em formato longo (``explode`` de structs) com um único ``groupBy``;
    * ``cache()`` enquanto houver múltiplas passadas, ``unpersist()`` ao final.

Aproximações (documentadas em ``docs/METRICAS.md``): percentis
(``percentile_approx``), distintos (``approx_count_distinct``, configurável),
KS/AUC sobre valor bruto (faixas finas) e mediana por faixa (target contínuo).
Nunca é feito ``collect()``/``toPandas()`` da base inteira — apenas de amostras
limitadas por configuração.
"""

from __future__ import annotations

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
from .polars_backend import _reformatar

_INF = float("inf")


def _strftime_para_java(fmt: str) -> str:
    """Converte um padrão strftime (``%Y-%m-%d``) para o padrão Java do Spark."""
    mapa = {"%Y": "yyyy", "%m": "MM", "%d": "dd", "%H": "HH", "%M": "mm", "%S": "ss", "%y": "yy"}
    saida = fmt
    for k, v in mapa.items():
        saida = saida.replace(k, v)
    return saida.replace("T", "'T'")


class SparkBackend(BaseBackend):
    """Backend sobre ``pyspark.sql.DataFrame``."""

    nome = "spark"
    exato = False

    def __init__(
        self,
        df: Any,
        accuracy_percentil: int = 10_000,
        distintos_exatos: bool = False,
        rsd_distintos: float = 0.01,
        n_faixas_finas: int = 200,
        usar_cache: bool = True,
        lote_variaveis: int = 12,
    ):
        from pyspark.sql import functions as F  # noqa: N812

        self.F = F
        self.df = df
        self.accuracy = accuracy_percentil
        self.distintos_exatos = distintos_exatos
        self.rsd = rsd_distintos
        self.n_faixas_finas = n_faixas_finas
        self.usar_cache = usar_cache
        self.lote_variaveis = lote_variaveis
        self._n: int | None = None
        self._cacheado = False

    # ------------------------------------------------------------------ geral
    def n_linhas(self) -> int:
        if self._n is None:
            self._n = int(self.df.count())
        return self._n

    def schema(self) -> dict[str, TipoLogico]:
        from pyspark.sql import types as T  # noqa: N812

        out: dict[str, TipoLogico] = {}
        for campo in self.df.schema.fields:
            t = campo.dataType
            if isinstance(t, T.BooleanType):
                out[campo.name] = "booleana"
            elif isinstance(t, (T.ByteType, T.ShortType, T.IntegerType, T.LongType, T.FloatType, T.DoubleType, T.DecimalType)):
                out[campo.name] = "numerica"
            elif isinstance(t, T.StringType):
                out[campo.name] = "string"
            elif isinstance(t, (T.DateType, T.TimestampType)) or type(t).__name__ == "TimestampNTZType":
                out[campo.name] = "data"
            else:
                out[campo.name] = "nao_suportado"
        return out

    def persistir(self) -> None:
        if self.usar_cache and not self._cacheado:
            self.df = self.df.cache()
            self._cacheado = True

    def liberar(self) -> None:
        if self._cacheado:
            self.df.unpersist()
            self._cacheado = False

    def filtrar_nao_nulo(self, coluna):
        return self._novo(self.df.filter(self.F.col(coluna).isNotNull()))

    def _novo(self, df):
        return SparkBackend(df, self.accuracy, self.distintos_exatos, self.rsd, self.n_faixas_finas, self.usar_cache, self.lote_variaveis)

    def preparar_target(self, target, mapa, binario):
        F = self.F
        t = F.col(target)
        if mapa:
            m = F.create_map(*[F.lit(v) for k, val in mapa.items() for v in (str(k), float(val))])
            t = F.try_element_at(m, t.cast("string")).cast("double")
        else:
            t = self._try_double(t.cast("string"))
        if binario:
            t = F.when(t.isin([0.0, 1.0]), t)
        return self._novo(self.df.withColumn(target, t))

    def filtrar_safra(self, coluna_safra, safra):
        return self._novo(self.df.filter(self._safra(coluna_safra) == safra))

    def amostra_pandas(self, colunas, n, seed, estratificar_por=None):
        F = self.F
        cols = list(dict.fromkeys([*colunas, *([estratificar_por] if estratificar_por else [])]))
        total = self.n_linhas()
        fracao = min(1.0, 1.2 * n / max(total, 1))
        base = self.df.select([F.col(c) for c in cols])
        if fracao < 1.0:
            if estratificar_por:
                valores = [r[0] for r in base.select(estratificar_por).distinct().collect()]
                base = base.sampleBy(estratificar_por, {v: fracao for v in valores}, seed=seed)
            else:
                base = base.sample(fraction=fracao, seed=seed)
        return base.limit(n).toPandas()

    def converter_colunas(self, conversoes, formato_data):
        df = self.df
        for c, tipo in conversoes.items():
            if tipo == "numerica":
                df = df.withColumn(c, self._try_double(self.F.trim(self.F.col(c).cast("string"))))
            elif tipo == "data":
                df = df.withColumn(c, self._para_data(c, formato_data))
        return self._novo(df)

    # ------------------------------------------------------- expressões base
    def _num(self, c: str):
        x = self.F.col(c).cast("double")
        return self.F.when((~self.F.isnan(x)) & (self.F.abs(x) != self.F.lit(_INF)), x)

    def _try_double(self, col):
        try:
            return col.try_cast("double")
        except AttributeError:  # pragma: no cover - Spark < 4
            return self.F.expr(f"try_cast({col._jc.toString()} as double)")

    def _safra(self, c: str):
        F = self.F
        s = F.col(c).cast("string")
        return F.coalesce(F.date_format(F.try_to_timestamp(s), "yyyy-MM"), F.substring(s, 1, 7)).alias("safra")

    def _para_data(self, c: str, formato: str | None):
        F = self.F
        s = F.trim(F.col(c).cast("string"))
        formatos = [formato] if formato else list(FORMATOS_DATA_PADRAO)
        return F.coalesce(*[F.try_to_timestamp(s, F.lit(_strftime_para_java(f))) for f in formatos]).cast("date")

    def _distintos(self, col):
        return self.F.countDistinct(col) if self.distintos_exatos else self.F.approx_count_distinct(col, self.rsd)

    def expressao_faixa(self, spec: BinSpec):
        """Expressão Spark que devolve o código de faixa (mesmos códigos do Polars)."""
        F = self.F
        if spec.tipo == "numerica":
            x = self._num(spec.variavel)
            if spec.cortes:
                # nº de cortes <= x, via filtro em array literal: gera código compacto no codegen
                # mesmo com centenas de cortes (um ``when`` por corte estoura o limite do Janino).
                cortes = F.array(*[F.lit(float(c)) for c in spec.cortes])
                codigo = F.size(F.filter(cortes, lambda c: x >= c))
            else:
                codigo = F.lit(0)
            # format_string em vez de lpad: lpad trunca strings maiores que o tamanho ("100" -> "10").
            codigo_str = F.concat(F.lit(PREFIXO_BIN), F.format_string("%02d", codigo.cast("int")))
            expr = F.when(x.isNull(), F.lit(COD_NULO))
            for esp in spec.valores_especiais:
                expr = expr.when(x == F.lit(float(esp)), F.lit(codigo_especial(esp)))
            return expr.otherwise(codigo_str)
        s = F.col(spec.variavel).cast("string")
        cond_cat = s.isin(list(spec.categorias)) if spec.categorias else F.lit(False)
        return (
            F.when(s.isNull(), F.lit(COD_NULO))
            .when(F.trim(s) == "", F.lit(COD_VAZIO))
            .when(cond_cat, F.concat(F.lit(PREFIXO_CAT), s))
            .otherwise(F.lit(COD_OUTROS))
        )

    def _agg_linha(self, df, exprs) -> dict[str, Any]:
        return df.agg(*exprs).collect()[0].asDict()

    # -------------------------------------------------------------- profiling
    def estatisticas_numericas(self, colunas, percentis):
        F = self.F
        if not colunas:
            return pl.DataFrame()
        exprs = []
        probs = [float(p) for p in percentis]
        for c in colunas:
            raw = F.col(c).cast("double")
            x = self._num(c)
            exprs += [
                F.count(x).alias(f"{c}||contagem"),
                F.sum(raw.isNull().cast("long")).alias(f"{c}||nulos"),
                F.sum(F.isnan(raw).cast("long")).alias(f"{c}||nan_count"),
                F.sum((F.abs(raw) == F.lit(_INF)).cast("long")).alias(f"{c}||inf_count"),
                F.mean(x).alias(f"{c}||media"),
                F.stddev_samp(x).alias(f"{c}||desvio_padrao"),
                F.var_samp(x).alias(f"{c}||variancia"),
                F.min(x).alias(f"{c}||minimo"),
                F.max(x).alias(f"{c}||maximo"),
                self._distintos(x).alias(f"{c}||distintos"),
                F.sum((x == 0).cast("long")).alias(f"{c}||zeros"),
                F.sum((x < 0).cast("long")).alias(f"{c}||negativos"),
                F.sum((x != F.floor(x)).cast("long")).alias(f"{c}||nao_inteiros"),
                F.skewness(x).alias(f"{c}||assimetria"),
                F.kurtosis(x).alias(f"{c}||curtose"),
                F.percentile_approx(x, F.array(*[F.lit(p) for p in probs]), self.accuracy).alias(f"{c}||__pcts"),
            ]
        linha = self._agg_linha(self.df, exprs)
        for c in colunas:
            pcts = linha.pop(f"{c}||__pcts") or [None] * len(probs)
            for p, v in zip(percentis, pcts):
                linha[f"{c}||{nome_percentil(p)}"] = v
        return _reformatar(linha, colunas)

    def contar_outliers(self, limites):
        F = self.F
        if not limites:
            return pl.DataFrame({"variavel": [], "qtd_outliers_inf": [], "qtd_outliers_sup": []})
        exprs = []
        for c, (li, ls) in limites.items():
            x = self._num(c)
            exprs += [
                F.sum((x < F.lit(float(li))).cast("long")).alias(f"{c}||qtd_outliers_inf"),
                F.sum((x > F.lit(float(ls))).cast("long")).alias(f"{c}||qtd_outliers_sup"),
            ]
        return _reformatar(self._agg_linha(self.df, exprs), list(limites))

    def estatisticas_categoricas(self, colunas, valores_suspeitos):
        F = self.F
        if not colunas:
            return pl.DataFrame()
        exprs = []
        for c in colunas:
            s = F.col(c).cast("string")
            t = F.trim(s)
            exprs += [
                F.count(s).alias(f"{c}||contagem"),
                F.sum(s.isNull().cast("long")).alias(f"{c}||nulos"),
                self._distintos(s).alias(f"{c}||distintos"),
                F.sum((t == "").cast("long")).alias(f"{c}||vazios"),
                F.min(F.length(s)).alias(f"{c}||tamanho_min"),
                F.max(F.length(s)).alias(f"{c}||tamanho_max"),
                F.mean(F.length(s)).alias(f"{c}||tamanho_medio"),
                F.sum(t.isin(list(valores_suspeitos)).cast("long")).alias(f"{c}||qtd_suspeitos"),
            ]
        return _reformatar(self._agg_linha(self.df, exprs), colunas)

    def frequencias(self, coluna, limite):
        F = self.F
        base = self.df
        if self.schema().get(coluna) == "numerica":
            base = base.filter(self._num(coluna).isNotNull())  # exclui NaN/±inf mantendo o dtype original
        linhas = (
            base.select(F.col(coluna).cast("string").alias("valor"))
            .filter(F.col("valor").isNotNull())
            .groupBy("valor")
            .agg(F.count("*").alias("qtd"))
            .orderBy(F.desc("qtd"), F.asc("valor"))
            .limit(limite)
            .collect()
        )
        return pl.DataFrame({"valor": [r["valor"] for r in linhas], "qtd": [float(r["qtd"]) for r in linhas]}, schema={"valor": pl.Utf8, "qtd": pl.Float64})

    def estatisticas_datas(self, colunas, data_minima):
        F = self.F
        if not colunas:
            return pl.DataFrame()
        exprs = []
        for c in colunas:
            d = F.col(c).cast("date")
            exprs += [
                F.count(d).alias(f"{c}||contagem"),
                F.sum(d.isNull().cast("long")).alias(f"{c}||nulos"),
                F.min(d).alias(f"{c}||data_min"),
                F.max(d).alias(f"{c}||data_max"),
                F.sum((d > F.current_date()).cast("long")).alias(f"{c}||datas_futuras"),
                F.sum((d < F.to_date(F.lit(data_minima))).cast("long")).alias(f"{c}||datas_invalidas"),
            ]
        return _reformatar(self._agg_linha(self.df, exprs), colunas)

    def contagem_por_safra(self, coluna):
        F = self.F
        linhas = (
            self.df.select(self._safra(coluna)).filter(F.col("safra").isNotNull()).groupBy("safra").agg(F.count("*").alias("contagem")).orderBy("safra").collect()
        )
        return pl.DataFrame({"safra": [r["safra"] for r in linhas], "contagem": [float(r["contagem"]) for r in linhas]}, schema={"safra": pl.Utf8, "contagem": pl.Float64})

    def conversibilidade_strings(self, colunas, formato_data, n_amostra):
        F = self.F
        if not colunas:
            return pl.DataFrame({"variavel": [], "pct_numerico": [], "pct_data": []})
        exprs = []
        for c in colunas:
            s = F.trim(F.col(c).cast("string"))
            exprs += [
                (F.count(self._try_double(s)) / F.count(s) * 100).alias(f"{c}||pct_numerico"),
                (F.count(self._para_data(c, formato_data)) / F.count(s) * 100).alias(f"{c}||pct_data"),
            ]
        return _reformatar(self._agg_linha(self.df.limit(n_amostra), exprs), colunas)

    # ------------------------------------------------------------ estabilidade
    def preenchimento_por_safra(self, coluna_safra, numericas, categoricas):
        F = self.F
        exprs = [F.count("*").alias("__n")]
        for c in numericas:
            exprs += [F.sum(F.col(c).isNull().cast("long")).alias(f"{c}||nulos"), F.mean(self._num(c)).alias(f"{c}||media")]
        for c in categoricas:
            exprs.append(F.sum(F.col(c).isNull().cast("long")).alias(f"{c}||nulos"))
        linhas = self.df.groupBy(self._safra(coluna_safra)).agg(*exprs).filter(F.col("safra").isNotNull()).orderBy("safra").collect()
        registros = []
        for r in linhas:
            d, n = r.asDict(), r["__n"]
            for c in [*numericas, *categoricas]:
                registros.append(
                    {"safra": d["safra"], "variavel": c, "contagem": float(n), "pct_nulos": round(100.0 * d[f"{c}||nulos"] / n, 2) if n else None, "media": d.get(f"{c}||media")}
                )
        return pl.DataFrame(registros, schema={"safra": pl.Utf8, "variavel": pl.Utf8, "contagem": pl.Float64, "pct_nulos": pl.Float64, "media": pl.Float64})

    def moda_por_safra(self, coluna_safra, coluna):
        F = self.F
        from pyspark.sql import Window

        base = self.df.select(self._safra(coluna_safra), F.col(coluna).cast("string").alias("valor")).filter(F.col("safra").isNotNull() & F.col("valor").isNotNull())
        freq = base.groupBy("safra", "valor").agg(F.count("*").alias("qtd"))
        w = Window.partitionBy("safra").orderBy(F.desc("qtd"), F.asc("valor"))
        tot = base.groupBy("safra").agg(F.count("*").alias("total"))
        linhas = (
            freq.withColumn("rk", F.row_number().over(w)).filter(F.col("rk") == 1).join(tot, "safra")
            .select("safra", F.col("valor").alias("moda"), F.round(F.col("qtd") / F.col("total") * 100, 2).alias("pct_moda")).orderBy("safra").collect()
        )
        return pl.DataFrame({"safra": [r["safra"] for r in linhas], "moda": [r["moda"] for r in linhas], "pct_moda": [r["pct_moda"] for r in linhas]}, schema={"safra": pl.Utf8, "moda": pl.Utf8, "pct_moda": pl.Float64})

    def media_target_por_safra(self, coluna_safra, target, peso):
        F = self.F
        w = F.col(peso).cast("double") if peso else F.lit(1.0)
        y = F.col(target).cast("double")
        linhas = (
            self.df.filter(F.col(target).isNotNull()).groupBy(self._safra(coluna_safra))
            .agg(F.sum(w).alias("contagem"), F.try_divide(F.sum(w * y), F.sum(w)).alias("media_target"))
            .filter(F.col("safra").isNotNull()).orderBy("safra").collect()
        )
        return pl.DataFrame({"safra": [r["safra"] for r in linhas], "contagem": [float(r["contagem"]) for r in linhas], "media_target": [r["media_target"] for r in linhas]}, schema={"safra": pl.Utf8, "contagem": pl.Float64, "media_target": pl.Float64})

    # --------------------------------------------------------- poder preditivo
    def quantis(self, colunas, probabilidades):
        F = self.F
        if not colunas:
            return {}
        probs = F.array(*[F.lit(float(p)) for p in probabilidades])
        linha = self._agg_linha(self.df, [F.percentile_approx(self._num(c), probs, self.accuracy).alias(c) for c in colunas])
        return {c: list(linha[c]) if linha[c] is not None else [None] * len(probabilidades) for c in colunas}

    def top_categorias(self, coluna, k):
        return self.frequencias(coluna, k)["valor"].to_list()

    def tabela_faixas_longa(self, specs, target, tipo_target, coluna_safra, coluna_peso):
        """Executa em lotes de ``lote_variaveis`` specs: o ``explode`` de structs com ``when``
        encadeados gera código Java grande demais para o whole-stage codegen quando há
        muitas variáveis (cada lote = um ``groupBy``)."""
        if not specs:
            return pl.DataFrame()
        partes = [self._tabela_faixas_lote(list(specs[i : i + self.lote_variaveis]), target, tipo_target, coluna_safra, coluna_peso) for i in range(0, len(specs), self.lote_variaveis)]
        return pl.concat(partes).sort(["variavel", "safra", "faixa"])

    def _tabela_faixas_lote(self, specs, target, tipo_target, coluna_safra, coluna_peso):
        F = self.F
        w = (F.col(coluna_peso).cast("double") if coluna_peso else F.lit(1.0)).alias("w")
        extras = [w]
        base = self.df
        aggs = [F.sum("w").alias("qtd")]
        if target is not None:
            base = base.filter(F.col(target).isNotNull())
            extras.append(F.col(target).cast("double").alias("y"))
            if tipo_target == "binario":
                aggs.append(F.sum(F.col("w") * F.col("y")).alias("maus"))
            else:
                aggs += [
                    F.sum(F.col("w") * F.col("y")).alias("soma"),
                    F.sum(F.col("w") * F.col("y") * F.col("y")).alias("soma_quad"),
                    F.percentile_approx("y", 0.5, self.accuracy).alias("mediana"),
                ]
        if coluna_safra:
            extras.append(self._safra(coluna_safra))
        structs = [F.struct(F.lit(s.variavel).alias("variavel"), self.expressao_faixa(s).alias("faixa")) for s in specs]
        longo = base.select(F.explode(F.array(*structs)).alias("vf"), *extras)
        chaves = ["vf.variavel", "vf.faixa"] + (["safra"] if coluna_safra else [])
        res = longo.groupBy(*[F.col(k).alias(k.split(".")[-1]) for k in chaves]).agg(*aggs).collect()
        nomes_agg = ["qtd"] + (["maus"] if tipo_target == "binario" and target else []) + (["soma", "soma_quad", "mediana"] if target and tipo_target != "binario" else [])
        dados = {"variavel": [], "faixa": [], "safra": []}
        for n in nomes_agg:
            dados[n] = []
        for r in res:
            d = r.asDict()
            dados["variavel"].append(d["variavel"])
            dados["faixa"].append(d["faixa"])
            dados["safra"].append(d.get("safra"))
            for n in nomes_agg:
                dados[n].append(None if d[n] is None else float(d[n]))
        schema = {"variavel": pl.Utf8, "faixa": pl.Utf8, "safra": pl.Utf8, **{n: pl.Float64 for n in nomes_agg}}
        return pl.DataFrame(dados, schema=schema)

    def ks_auc_bruto(self, colunas, target, coluna_peso):
        F = self.F
        if not colunas:
            return pl.DataFrame({"variavel": [], "ks_bruto": [], "auc_bruto": [], "ponto_bisserial": [], "n_obs": []})
        # 1) cortes finos de todas as colunas numa passada
        probs = [i / self.n_faixas_finas for i in range(1, self.n_faixas_finas)]
        cortes = self.quantis(colunas, probs)
        specs = []
        for c in colunas:
            cs = sorted({float(v) for v in cortes[c] if v is not None})
            specs.append(BinSpec(c, "numerica", cortes=cs))
        # 2) distribuição por faixa fina (um groupBy) + correlação ponto-bisserial (um agg)
        base = self.df.filter(F.col(target).isNotNull())
        dist = self.tabela_faixas_longa_sobre(base, specs, target, coluna_peso)
        y = F.col(target).cast("double")
        corr_exprs = []
        for c in colunas:
            x = self._num(c)
            corr_exprs += [F.corr(x, y).alias(f"{c}||r"), F.count(F.when(x.isNotNull() & y.isNotNull(), 1)).alias(f"{c}||n")]
        corr = self._agg_linha(base, corr_exprs)
        linhas = []
        for c in colunas:
            d = (
                dist.filter((pl.col("variavel") == c) & (pl.col("faixa") != COD_NULO))
                .with_columns(pl.col("faixa").str.slice(1).cast(pl.Int64).alias("__ord"))
                .sort("__ord")
            )
            tab = d.select((pl.col("maus")).alias("w1"), (pl.col("qtd") - pl.col("maus")).alias("w0"))
            ks, auc = ks_auc_de_distribuicao(tab)
            linhas.append({"variavel": c, "ks_bruto": ks, "auc_bruto": auc, "ponto_bisserial": corr[f"{c}||r"], "n_obs": float(corr[f"{c}||n"])})
        return pl.DataFrame(linhas, schema={"variavel": pl.Utf8, "ks_bruto": pl.Float64, "auc_bruto": pl.Float64, "ponto_bisserial": pl.Float64, "n_obs": pl.Float64})

    def tabela_faixas_longa_sobre(self, base, specs, target, coluna_peso):
        """Variante interna de ``tabela_faixas_longa`` sobre um DataFrame arbitrário (binário)."""
        return self._novo(base).tabela_faixas_longa(specs, target, "binario", None, coluna_peso)

    def correlacoes_com_target(self, colunas, target):
        F = self.F
        if not colunas:
            return pl.DataFrame({"variavel": [], "pearson": [], "spearman": [], "n_obs": []})
        y = F.col(target).cast("double")
        exprs = []
        for c in colunas:
            x = self._num(c)
            exprs += [F.corr(x, y).alias(f"{c}||pearson"), F.count(F.when(x.isNotNull() & y.isNotNull(), 1)).cast("double").alias(f"{c}||n_obs")]
        linha = self._agg_linha(self.df, exprs)
        # Spearman via ml.stat.Correlation (listwise entre cada x e y) — uma passada por coluna
        from pyspark.ml.feature import VectorAssembler
        from pyspark.ml.stat import Correlation

        for c in colunas:
            df2 = self.df.select(self._num(c).alias("x"), y.alias("y")).dropna()
            if df2.limit(3).count() < 3:
                linha[f"{c}||spearman"] = None
                continue
            vec = VectorAssembler(inputCols=["x", "y"], outputCol="v").transform(df2).select("v")
            m = Correlation.corr(vec, "v", "spearman").head()[0].toArray()
            linha[f"{c}||spearman"] = float(m[0, 1])
        return _reformatar(linha, colunas)

    def aplicar_faixas(self, specs, mapa_woe, sufixo):
        F = self.F
        df = self.df
        for spec in specs:
            mapa = mapa_woe.get(spec.variavel, {})
            if not mapa:
                df = df.withColumn(f"{spec.variavel}{sufixo}", F.lit(None).cast("double"))
                continue
            m = F.create_map(*[F.lit(v) for kv in mapa.items() for v in kv])
            df = df.withColumn(f"{spec.variavel}{sufixo}", F.try_element_at(m, self.expressao_faixa(spec)).cast("double"))
        return df

    # ------------------------------------------------------------- correlação
    def correlacao_numerica(self, colunas, metodo, pairwise, amostra, seed):
        F = self.F
        pares = [(a, b) for i, a in enumerate(colunas) for b in colunas[i + 1 :]]
        vazio = pl.DataFrame({"var_1": [], "var_2": [], "valor": [], "n_obs": []}, schema={"var_1": pl.Utf8, "var_2": pl.Utf8, "valor": pl.Float64, "n_obs": pl.Float64})
        if not pares:
            return vazio, False
        base = self.df.select([self._num(c).alias(c) for c in colunas])
        amostral = False
        if amostra is not None:
            total = self.n_linhas()
            fracao = float(amostra) if amostra < 1 else min(1.0, float(amostra) / max(total, 1))
            if fracao < 1.0:
                base = base.sample(fraction=fracao, seed=seed)
                amostral = True
        if metodo == "pearson" and pairwise:
            exprs = []
            for a, b in pares:
                exprs += [F.corr(F.col(a), F.col(b)).alias(f"{a}|{b}|v"), F.count(F.when(F.col(a).isNotNull() & F.col(b).isNotNull(), 1)).cast("double").alias(f"{a}|{b}|n")]
            linha = self._agg_linha(base, exprs)
            return (
                pl.DataFrame({"var_1": [a for a, _ in pares], "var_2": [b for _, b in pares], "valor": [linha[f"{a}|{b}|v"] for a, b in pares], "n_obs": [linha[f"{a}|{b}|n"] for a, b in pares]}, schema=vazio.schema),
                amostral,
            )
        # Spearman (ou listwise): pyspark.ml.stat.Correlation sobre linhas completas
        from pyspark.ml.feature import VectorAssembler
        from pyspark.ml.stat import Correlation

        completo = base.dropna()
        n = completo.count()
        if n < 3:
            return vazio, amostral
        vec = VectorAssembler(inputCols=list(colunas), outputCol="v").transform(completo).select("v")
        m = Correlation.corr(vec, "v", metodo).head()[0].toArray()
        idx = {c: i for i, c in enumerate(colunas)}
        return (
            pl.DataFrame({"var_1": [a for a, _ in pares], "var_2": [b for _, b in pares], "valor": [float(m[idx[a], idx[b]]) for a, b in pares], "n_obs": [float(n)] * len(pares)}, schema=vazio.schema),
            amostral,
        )

    def contingencia(self, spec_a, spec_b):
        F = self.F
        linhas = self.df.groupBy(self.expressao_faixa(spec_a).alias("faixa_a"), self.expressao_faixa(spec_b).alias("faixa_b")).agg(F.count("*").alias("qtd")).collect()
        return pl.DataFrame({"faixa_a": [r["faixa_a"] for r in linhas], "faixa_b": [r["faixa_b"] for r in linhas], "qtd": [float(r["qtd"]) for r in linhas]}, schema={"faixa_a": pl.Utf8, "faixa_b": pl.Utf8, "qtd": pl.Float64}).sort(["faixa_a", "faixa_b"])

    def estatisticas_por_grupo(self, numerica, categorica, k):
        F = self.F
        spec = BinSpec(categorica, "categorica", categorias=self.top_categorias(categorica, k))
        x = self._num(numerica)
        linhas = self.df.filter(x.isNotNull()).groupBy(self.expressao_faixa(spec).alias("grupo")).agg(F.count("*").alias("n"), F.mean(x).alias("media"), F.var_samp(x).alias("variancia")).orderBy("grupo").collect()
        return pl.DataFrame({"grupo": [r["grupo"] for r in linhas], "n": [float(r["n"]) for r in linhas], "media": [r["media"] for r in linhas], "variancia": [r["variancia"] for r in linhas]}, schema={"grupo": pl.Utf8, "n": pl.Float64, "media": pl.Float64, "variancia": pl.Float64})
