"""Fluxo completo com PySpark (EMR/Glue): leitura de Parquet ou Iceberg no S3 -> profiling ->
poder preditivo -> correlação -> WoE em base nova.

Nunca é feito ``toPandas()``/``collect()`` da base inteira: só agregados e amostras limitadas
por configuração chegam ao driver.
"""

from __future__ import annotations

import logging

import polars as pl
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from data_profiler import CorrelacaoConfig, PoderPreditivoConfig, ProfileConfig, correlate, poder_preditivo, profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def sessao() -> SparkSession:
    return (
        SparkSession.builder.appName("data_profiler")
        # Iceberg via Glue Catalog (ajuste para o seu catálogo/warehouse):
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.glue", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.glue.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
        .config("spark.sql.catalog.glue.warehouse", "s3://meu-bucket/warehouse/")
        .config("spark.sql.catalog.glue.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
        .getOrCreate()
    )


def main() -> None:
    spark = sessao()

    # Fonte A: Parquet particionado no S3
    df = spark.read.parquet("s3://meu-bucket/credito/base_modelagem/").filter(F.col("dt_ref") >= "2024-01-01")
    # Fonte B: tabela Iceberg (mesmo fluxo)
    # df = spark.table("glue.credito.base_modelagem").filter("dt_ref >= DATE '2024-01-01'")
    nova = spark.read.parquet("s3://meu-bucket/credito/base_oot/")

    cfg_prof = ProfileConfig(
        spark_accuracy_percentil=10_000,  # maior = percentis mais precisos e mais lentos
        spark_distintos_exatos=False,  # approx_count_distinct (rsd 1%); True força countDistinct
        spark_lote_variaveis=12,  # variáveis por groupBy no formato longo
        persistir=True,  # cache() durante as múltiplas passadas, unpersist() ao final
    )

    # 1) Profiling ----------------------------------------------------------------------
    prof = profile(df, coluna_safra="dt_ref", target="target", ignorar=["id_cliente"], config=cfg_prof)
    print(prof.resumo())
    prof.to_parquet("/tmp/profiling")  # saídas são Polars pequenos: gravar local/S3 é barato

    # 2) Poder preditivo ---------------------------------------------------------------
    pp = poder_preditivo(
        df,
        "target",
        coluna_safra="dt_ref",
        safra_referencia="2024-01",
        df_validacao=nova,
        profile_result=prof,
        config=PoderPreditivoConfig(
            metodo_binning="otimo",  # optbinning ajustado em amostra estratificada (sampleBy) de n_amostra_binning
            n_amostra_binning=200_000,
            monotonicidade="auto",
            valores_especiais=(-1, 9999),
            n_faixas_finas=200,  # KS/AUC brutos aproximados por 200 quantis (sem ordenar a base)
            importancia_multivariada=False,  # True: LightGBM em amostra (opcional)
        ),
        profile_config=cfg_prof,
    )
    print(pp.resumo.select("ranking", "variavel", "recomendacao", "iv", "ks", "gini_woe", "gap_gini", "obs"))
    pp.to_excel("/tmp/poder_preditivo.xlsx")
    with open("/tmp/cortes.json", "w", encoding="utf-8") as f:
        f.write(pp.cortes_json())

    # 3) Correlação (Spearman no Spark é listwise e amostral) -------------------------------
    selecionadas = pp.resumo.filter(pl.col("recomendacao") == "Selecionar")["variavel"].to_list()
    corr = correlate(df, colunas=selecionadas, profile_result=prof, amostra=500_000, config=CorrelacaoConfig(seed=42))
    print(corr.pares_altos, corr.metadados["amostral"], corr.metadados["obs"])

    # 4) WoE em base nova: devolve um DataFrame Spark com colunas <var>_woe ----------------
    nova_woe = pp.aplicar_woe(nova, colunas=selecionadas)
    nova_woe.write.mode("overwrite").parquet("s3://meu-bucket/credito/base_oot_woe/")


if __name__ == "__main__":
    main()
