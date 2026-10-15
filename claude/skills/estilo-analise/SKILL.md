---
name: estilo-analise
description: Meu padrão para escrever notebooks e scripts de análise de dados - template de 5 células, Polars lazy, extração isolada, validações e nomenclatura. Use ao criar um notebook ou script de análise novo.
---

# Estilo de análise

## Template de notebook (5 células de código)

Cada célula de código é precedida por uma célula markdown curta com o título da seção.

```python
# 1. Imports
from datetime import date
from pathlib import Path

import polars as pl
```

```python
# 2. Constantes e SQL de extração
DT_INI = date(2026, 1, 1)
DT_FIM = date(2026, 3, 31)
DIR_SAIDA = Path("saida")

SQL_BASE = f"""
SELECT id_contrato, id_cliente, renda, dt_ref
FROM schema.tabela
WHERE dt_ref BETWEEN '{DT_INI}' AND '{DT_FIM}'   -- sempre filtro de partição
"""
SQL_DIM = "SELECT id_cliente, safra FROM schema.dim_cliente"
```

```python
# 3. Funções auxiliares (puras, sem I/O)
def trata_sentinelas(lf: pl.LazyFrame, cols: list[str], sentinelas: set[int]) -> pl.LazyFrame:
    return lf.with_columns(
        pl.when(pl.col(c).is_in(list(sentinelas))).then(None).otherwise(pl.col(c)).alias(c)
        for c in cols
    )
```

```python
# 4. Extract isolado + cadeia lazy
df_base = extrai(SQL_BASE)          # idas ao warehouse só aqui (função do empresa.md)
df_dim = extrai(SQL_DIM)

df_resultado = (
    df_base.lazy()
    .pipe(trata_sentinelas, ["renda"], {-9999})
    .join(df_dim.lazy(), on="id_cliente", how="left", validate="m:1")
    .group_by("safra")
    .agg(pl.len().alias("qtd"), pl.col("renda").mean().alias("renda_media"))
    .collect()                       # um único .collect()
)
```

```python
# 5. Validações (asserts que falham, não prints)
assert df_base["id_contrato"].is_unique().all(), "grão quebrado na base"
assert df_dim["id_cliente"].is_unique().all(), "dimensão com chave duplicada"
assert df_resultado.height > 0, "resultado vazio"
assert df_resultado["qtd"].sum() == df_base.height, "join mudou a contagem"
```

## Regras

- **Extração isolada**: o SQL só faz `SELECT` + filtro. Toda lógica vai para Polars.
- **Um único `.collect()`** por cadeia. Nada de `.collect()` intermediário para "olhar".
- **Todo join com `validate=`** (`"1:1"`, `"m:1"`, `"1:m"`). Join sem cardinalidade
  declarada é bug esperando acontecer.
- **Sentinelas viram nulo** antes de qualquer agregação.
- **Validações são `assert`**, com mensagem que diz o que quebrou.
- **Notebook entregue sem outputs.**
- Nomes de colunas e variáveis em `snake_case` português; DataFrames com prefixo `df_`,
  LazyFrames com `lf_`.

## Criar notebook novo

```python
import nbformat as nbf

nb = nbf.v4.new_notebook()
nb.cells = [
    nbf.v4.new_markdown_cell("## Imports"),
    nbf.v4.new_code_cell("import polars as pl"),
    # ...
]
nbf.write(nb, "20260115_nome_da_analise.ipynb")
```

## SQL → Polars (o que eu escreveria em SQL)

| SQL | Polars |
|---|---|
| `CASE WHEN x = -9999 THEN NULL ELSE x END` | `pl.when(pl.col("x") == -9999).then(None).otherwise(pl.col("x"))` |
| `COUNT(DISTINCT id)` | `pl.col("id").n_unique()` |
| `SUM(CASE WHEN alvo = 1 THEN 1 ELSE 0 END)` | `(pl.col("alvo") == 1).sum()` |
| `AVG(x) FILTER (WHERE cond)` | `pl.col("x").filter(cond).mean()` |
| `ROW_NUMBER() OVER (PARTITION BY id ORDER BY dt DESC) = 1` | `.sort("dt", descending=True).unique("id", keep="first", maintain_order=True)` |
| `LAG(x) OVER (PARTITION BY id ORDER BY dt)` | `pl.col("x").shift(1).over("id", order_by="dt")` |
| `DATE_TRUNC('month', dt)` | `pl.col("dt").dt.truncate("1mo")` |
| `HAVING COUNT(*) > 100` | `.filter(pl.col("qtd") > 100)` depois do `agg` |

## Armadilhas do Polars (já custaram caro)

- **Nulos não casam em join** (`nulls_equal=False`): chave nula some num `inner` sem
  aviso. Conte nulos na chave antes do join.
- **NaN ≠ null**: NaN passa em `>= 0`, não é pego por `is_null()` e deixa `mean()` em NaN.
  Converta com `.fill_nan(None)` antes de filtrar ou agregar.
- **`pl.lit(1.0).sum()` devolve 1**, não o número de linhas. Para contar ou somar pesos
  default use `pl.len()` ou uma coluna de peso explícita.
- **Média de taxa por grupo ≠ taxa do total**: pondere (`soma_maus / soma_qtd`), não tire
  a média das taxas.
- **Agregado sem `by=` a faixa de risco** pode inverter o sinal de todas as faixas (mix).
- **Filtro `!=` / `is_in` com nulos** descarta os nulos dos dois lados.
- **Data de partição como string** compara lexicograficamente: use `date` nas constantes.
