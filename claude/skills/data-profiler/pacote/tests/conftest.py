"""Fixtures compartilhadas."""

from __future__ import annotations

import logging

import pytest

from .sintetico import gerar_base

logging.getLogger("data_profiler").setLevel(logging.WARNING)


@pytest.fixture(scope="session")
def base():
    return gerar_base(20_000, seed=7)


@pytest.fixture(scope="session")
def base_validacao():
    return gerar_base(5_000, seed=99)


@pytest.fixture(scope="session")
def spark():
    """SparkSession local; testes que dependem dela são pulados se pyspark/Java não estiverem disponíveis."""
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        sessao = (
            SparkSession.builder.master("local[2]")
            .appName("data_profiler_tests")
            .config("spark.sql.shuffle.partitions", "4")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
        sessao.sparkContext.setLogLevel("ERROR")
    except Exception as exc:  # pragma: no cover - ambiente sem Java
        pytest.skip(f"Spark indisponível: {exc}")
    yield sessao
    sessao.stop()


@pytest.fixture(scope="session")
def base_spark(spark, base):
    return spark.createDataFrame(base.to_pandas())
