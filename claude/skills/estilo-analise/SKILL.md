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
SELECT col_a, col_b, dt_ref
FROM schema.tabela
WHERE dt_ref BETWEEN '{DT_INI}' AND '{DT_FIM}'   -- sempre filtro de partição
"""
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
df_base = extrai(SQL_BASE)          # única ida ao warehouse (função do empresa.md)

resultado = (
    df_base.lazy()
    .pipe(trata_sentinelas, ["renda"], {-9999})
    .join(dim.lazy(), on="id_cliente", how="left", validate="m:1")
    .group_by("safra")
    .agg(pl.len().alias("qtd"))
    .collect()                       # um único .collect()
)
```

```python
# 5. Validações (asserts que falham, não prints)
assert df_base["id_contrato"].is_unique().all(), "grão quebrado na base"
assert resultado.height > 0, "resultado vazio"
assert resultado["qtd"].sum() == df_base.height, "join mudou a contagem"
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

## Exemplos canônicos

<!-- TODO: trazer de contexto_juliano.md os notebooks exemplares, a tabela de tradução
SQL -> Polars e os anti-padrões pessoais. -->
