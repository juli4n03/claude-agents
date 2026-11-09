---
name: estilo-analise
description: Meu padrão para escrever notebooks de análise no Databricks - template de 5 seções, PySpark até o recorte e Polars depois, extração isolada, joins com cardinalidade validada, asserts e nomenclatura. Use ao criar um notebook de análise novo.
---

# Estilo de análise (Databricks)

## Template de notebook (5 células de código)

Cada célula de código é precedida por uma célula markdown curta com o título da seção.

```python
# 1. Imports
from datetime import date

import polars as pl
from pyspark.sql import DataFrame, functions as F
```

```python
# 2. Constantes e extração (só SELECT + filtro de partição)
DT_INI = date(2026, 1, 1)
DT_FIM = date(2026, 3, 31)
TB_BASE = "catalogo.schema.tabela"
TB_DIM = "catalogo.schema.dim_cliente"

sdf_base = (
    spark.table(TB_BASE)
    .where(F.col("dt_ref").between(F.lit(DT_INI), F.lit(DT_FIM)))   # sempre filtro de partição
    .select("id_contrato", "id_cliente", "renda", "dt_ref")
)
sdf_dim = spark.table(TB_DIM).select("id_cliente", "safra")
```

```python
# 3. Funções auxiliares (puras, sem I/O)
def valida_chave_unica(sdf: DataFrame, chave: list[str]) -> None:
    dup = sdf.groupBy(*chave).count().where("count > 1").limit(1).count()
    assert dup == 0, f"chave {chave} duplicada"


def join_validado(esq: DataFrame, dir: DataFrame, on: list[str], how: str, cardinalidade: str) -> DataFrame:
    """Join com cardinalidade declarada ('1:1', 'm:1', '1:m'), como o validate= do Polars."""
    if cardinalidade in ("1:1", "m:1"):
        valida_chave_unica(dir, on)
    if cardinalidade in ("1:1", "1:m"):
        valida_chave_unica(esq, on)
    return esq.join(dir, on=on, how=how)  # check_nb: ignora validate


def trata_sentinelas(sdf: DataFrame, cols: list[str], sentinelas: list[int]) -> DataFrame:
    for c in cols:
        sdf = sdf.withColumn(c, F.when(F.col(c).isin(sentinelas), None).otherwise(F.col(c)))
    return sdf
```

```python
# 4. Transformação (PySpark até o recorte; Polars só se o resultado couber no driver)
sdf_resultado = (
    join_validado(trata_sentinelas(sdf_base, ["renda"], [-9999]), sdf_dim,
                  on=["id_cliente"], how="left", cardinalidade="m:1")
    .groupBy("safra")
    .agg(F.count("*").alias("qtd"), F.avg("renda").alias("renda_media"))
)
df_resultado = pl.from_pandas(sdf_resultado.toPandas())   # já agregado: cabe no driver
```

```python
# 5. Validações (asserts que falham, não prints)
n_base = sdf_base.count()
valida_chave_unica(sdf_base, ["id_contrato"])
assert df_resultado.height > 0, "resultado vazio"
assert df_resultado["qtd"].sum() == n_base, "join mudou a contagem"
```

## Regras

- **Extração isolada**: a célula 2 só faz `spark.table` + filtro + `select`. Lógica fica
  nas células 3 e 4.
- **Todo join com cardinalidade declarada**: em PySpark via `join_validado`; em Polars com
  `validate=` (`"1:1"`, `"m:1"`, `"1:m"`).
- **Sentinelas viram nulo** antes de qualquer agregação.
- **Validações são `assert`**, com mensagem que diz o que quebrou.
- **Nada volta inteiro para o driver**: `toPandas()`, `collect()` e Polars só depois de
  filtrar ou agregar. Em Polars, um único `.collect()` por cadeia lazy.
- **Reprodutibilidade**: para análise que vai para comitê, anote a versão da tabela
  (`DESCRIBE HISTORY`) ou leia com `VERSION AS OF`.
- **Notebook entregue sem outputs.**
- Nomes em `snake_case` português; Spark DataFrame com prefixo `sdf_`, Polars `df_`,
  LazyFrame `lf_`.

## Armadilhas do PySpark

- **Nulos não casam em join**: chave nula some num `inner` sem aviso. Conte nulos na chave
  antes do join. `eqNullSafe` só se nulo = nulo for regra de negócio.
- **`countDistinct` ignora nulos**: para validar chave use `groupBy().count()`.
- **`display()` mostra uma amostra**, não o total: nunca tire conclusão de contagem dele.
- **Avaliação preguiçosa**: cada `count()` relê a base. Em base grande, `cache()` antes
  de várias ações e `unpersist()` depois.
- **Fuso**: `spark.sql.session.timeZone` muda `current_date()` e conversões de timestamp.
- **`BETWEEN` com timestamp** corta o último dia: use `< fim + 1 dia`.
- **Média de taxa por grupo ≠ taxa do total**: pondere (`soma_maus / soma_qtd`).
- **Agregado sem quebra por faixa de risco** pode inverter o sinal de todas as faixas (mix).

## Armadilhas do Polars (depois do recorte)

- **NaN ≠ null**: NaN passa em `>= 0`, não é pego por `is_null()` e deixa `mean()` em NaN.
  `toPandas()` costuma trazer nulos numéricos como NaN: use `.fill_nan(None)`.
- **`pl.lit(1.0).sum()` devolve 1**, não o número de linhas: use `pl.len()`.
- **Filtro `!=` / `is_in` com nulos** descarta os nulos dos dois lados.
- **Data como string** compara lexicograficamente: use `date` nas constantes.

## SQL → PySpark

| SQL | PySpark |
|---|---|
| `CASE WHEN x = -9999 THEN NULL ELSE x END` | `F.when(F.col("x") == -9999, None).otherwise(F.col("x"))` |
| `COUNT(DISTINCT id)` | `F.countDistinct("id")` (ignora nulos) |
| `SUM(CASE WHEN alvo = 1 THEN 1 ELSE 0 END)` | `F.sum((F.col("alvo") == 1).cast("int"))` |
| `ROW_NUMBER() OVER (PARTITION BY id ORDER BY dt DESC) = 1` | `Window.partitionBy("id").orderBy(F.desc("dt"))` + `F.row_number()` + filtro `== 1` |
| `LAG(x) OVER (PARTITION BY id ORDER BY dt)` | `F.lag("x").over(Window.partitionBy("id").orderBy("dt"))` |
| `DATE_TRUNC('month', dt)` | `F.date_trunc("month", "dt")` |
