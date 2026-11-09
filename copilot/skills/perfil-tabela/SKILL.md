---
name: perfil-tabela
description: Perfil de tabelas do Unity Catalog antes de uma análise nova - schema real, sentinelas, grão, cardinalidade de joins, escala e maturidade. Somente leitura. Use quando a tabela é nova na conversa e a escala ou os joins são incertos.
---

# Perfil de tabela

Você perfila tabelas para uma análise. Você **só lê**: nunca cria, altera ou apaga nada
no catálogo, em volumes ou em notebooks de outras pessoas.

## Antes de começar

1. Leia a seção **Empresa** das instruções: catálogos, tabelas centrais e sentinelas já
   conhecidas.
2. Se já existir um perfil recente desta tabela (arquivo `perfis/perfil_<tabela>.md` ou
   célula de perfil na conversa), reutilize e complete só o que a tarefa pede de novo.

## O que levantar, por tabela

1. **Schema**: `DESCRIBE TABLE EXTENDED <tabela>` (tipos e comentários das colunas).
   Tamanho e partições: `DESCRIBE DETAIL <tabela>` (`numFiles`, `sizeInBytes`,
   `partitionColumns`). Última escrita: `DESCRIBE HISTORY <tabela> LIMIT 5`.
2. **Sentinelas**: confira as da seção Empresa e procure outras nas colunas que a tarefa
   usa: mínimo/máximo absurdos (-9999, 9999999), datas 1900-01-01, strings vazias,
   `'NULL'`, `'N/A'`, `'-'`. Uma consulta agregada por coluna, não amostra de linhas.
3. **Grão e cardinalidade**: `count(*)` vs. contagem de chaves distintas (com
   `groupBy(chave).count()`, porque `count(distinct)` ignora nulos) e, para cada join
   previsto, `1:1`, `m:1`, `1:m` ou `m:m` com a contagem que comprova. Conte chaves sem
   par dos dois lados (o join vai perder linhas?) e chaves nulas.
4. **Escala**: linhas e partições **no recorte da tarefa**. Feche num sinal:
   `cabe no driver (Polars)` / `PySpark` / `PySpark com cuidado (shuffle grande)`.
5. **Maturidade** (se houver target com janela, ex. atraso até o mês N): última safra com
   janela completa.

Consultas de perfil sempre com filtro de partição e `LIMIT` ou agregação. Nunca
`SELECT *` sem filtro, nunca `display()` da tabela inteira.

## Saída

Registre as consultas usadas (para reprodução) e devolva **no máximo 15 linhas**:

```
perfil: <tabela> (<novo | reutilizado de AAAA-MM-DD>)
grão: <chave> — <único | duplicado: N chaves repetidas>
escala: <N linhas, P partições no recorte> → <sinal>
sentinelas: <coluna=valor, ...>
joins: <tabela_a × tabela_b on chave: m:1, X% sem par, Y chaves nulas>
maturidade: <última safra completa | n/a>
surpresas: <o que muda a análise, ou "nenhuma">
```

Você não escreve a análise final nem escolhe engine. Se a tarefa for de seleção de
variáveis ou modelagem, diga em `surpresas` que o notebook deve usar a skill
`data-profiler`.
