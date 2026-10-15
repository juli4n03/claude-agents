"""Seleção automática de backend."""

from __future__ import annotations

import warnings
from typing import Any

import polars as pl

from ..config import Backend, ProfileConfig
from .base import BaseBackend


def _eh_spark(df: Any) -> bool:
    try:
        from pyspark.sql import DataFrame as SparkDF

        if isinstance(df, SparkDF):
            return True
    except ImportError:
        pass
    mod = type(df).__module__ or ""
    return mod.startswith("pyspark.sql")


def _eh_pandas(df: Any) -> bool:
    return (type(df).__module__ or "").startswith("pandas")


def selecionar_backend(
    df: Any,
    backend: Backend = "auto",
    config: ProfileConfig | None = None,
    n_faixas_finas: int = 200,
) -> BaseBackend:
    """Cria o backend adequado à entrada.

    Regras:
        * ``pyspark.sql.DataFrame`` → ``SparkBackend`` (nunca coleta a base inteira).
        * ``pandas``/``polars`` → ``PolarsBackend`` (pandas é convertido para Polars).
        * ``backend="spark"`` com entrada pandas/Polars exige uma ``SparkSession`` ativa.
        * ``backend="polars"`` com entrada Spark é rejeitado (exigiria ``toPandas``).
    """
    from .polars_backend import PolarsBackend

    cfg = config or ProfileConfig()
    spark_entrada = _eh_spark(df)

    if backend == "polars" and spark_entrada:
        raise ValueError("backend='polars' com DataFrame Spark exigiria coletar a base inteira; use backend='auto' ou 'spark'.")

    if spark_entrada or backend == "spark":
        from .spark_backend import SparkBackend

        if not spark_entrada:
            from pyspark.sql import SparkSession

            spark = SparkSession.getActiveSession()
            if spark is None:
                raise ValueError("backend='spark' exige uma SparkSession ativa para converter a entrada.")
            pdf = df.to_pandas() if isinstance(df, (pl.DataFrame, pl.LazyFrame)) and not isinstance(df, pl.LazyFrame) else (df.collect().to_pandas() if isinstance(df, pl.LazyFrame) else df)
            df = spark.createDataFrame(pdf)
        return SparkBackend(
            df,
            accuracy_percentil=cfg.spark_accuracy_percentil,
            distintos_exatos=cfg.spark_distintos_exatos,
            rsd_distintos=cfg.spark_rsd_distintos,
            n_faixas_finas=n_faixas_finas,
            usar_cache=cfg.persistir,
            lote_variaveis=cfg.spark_lote_variaveis,
        )

    origem_pandas = False
    if _eh_pandas(df):
        df = pl.from_pandas(df)
        origem_pandas = True
    if not isinstance(df, (pl.DataFrame, pl.LazyFrame)):
        raise TypeError(f"Tipo de entrada não suportado: {type(df)!r}. Use pandas, Polars ou PySpark.")

    be = PolarsBackend(df, origem_pandas=origem_pandas)
    n = be.n_linhas()
    if n > cfg.max_linhas_polars:
        warnings.warn(
            f"A base tem {n:,} linhas (> max_linhas_polars={cfg.max_linhas_polars:,}). Considere usar PySpark.",
            RuntimeWarning,
            stacklevel=3,
        )
    return be


__all__ = ["BaseBackend", "selecionar_backend"]
