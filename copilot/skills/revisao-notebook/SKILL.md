---
name: revisao-notebook
description: Revisão de correção de um notebook de dados (PySpark, Polars ou SQL) feita sem o contexto de quem escreveu. Use no fim da rota C ou quando eu pedir revisão, recebendo só o caminho do notebook e o critério de pronto.
---

# Revisão de notebook

Você revisa um artefato de análise sem ter visto como ele foi construído. Se esta
conversa foi a que escreveu o notebook, diga isso e sugira repetir a revisão num chat
novo, passando só o caminho e o critério de pronto.

Reconstrua o contexto lendo o código. Leia também a seção **Empresa** das instruções
(sentinelas, fusos, grãos) e, se o tema for crédito, os parâmetros de crédito.

Você não edita nada e nunca executa célula que escreva em tabela. Pode rodar consultas
de leitura com filtro e `LIMIT` para confirmar uma suspeita.

## Procure só o que afeta correção

**Joins e grão**
- Join que multiplica ou perde linhas: cardinalidade, chave errada, join sem
  `join_validado` (PySpark) ou sem `validate=` (Polars).
- Nulos na chave: não casam em join (PySpark e Polars), e linhas somem num `inner`.
- Mudança de grão sem agregação explícita; `dropDuplicates()` / `unique()` escondendo
  duplicidade real.

**Valores**
- Sentinela ou nulo tratado como valor real (médias, filtros, targets).
- NaN ≠ null em Polars e depois de `toPandas()`.
- `isin` / `!=` com nulos: a linha nula sai do filtro.
- `countDistinct` usado para validar chave (ignora nulos).
- Divisão por zero ou denominador errado (taxa sobre a base errada, % sobre total vs
  sobre não nulos).
- Média de médias ou de taxas sem ponderar; agregado dominado por mix (Simpson).

**Tempo**
- Filtro de data ou partição que corta ou duplica o período (`BETWEEN` com timestamp,
  fim de mês, fuso da sessão Spark).
- Safra sem janela de maturação completa entrando como "bom" no target.
- Vazamento do futuro em features ou targets.

**Escala e escrita**
- `toPandas()`, `collect()` ou Polars sobre a base inteira.
- Escrita com `overwrite`, `MERGE`, `DELETE` ou `saveAsTable` sem confirmação explícita.

**Entrega**
- Critério de pronto não atendido.
- Assert que não testa nada (sempre verdadeiro) ou que testa o lugar errado.

## Não reporte

Estilo, nomes, preferências de formatação ou melhorias "seria bom". Só reporte o que você
consegue apontar no código: "talvez" sem célula não entra.

## Saída

```
VEREDITO: <aprovado | corrigir>
1. [muda o número | pode mudar] célula N — <problema>
   por quê: <como altera o resultado>
   correção: <o que mudar, em uma linha de código quando der>
check_nb: <saída, ou "não disponível">
```

Sem problema de correção: só `VEREDITO: aprovado` e a linha do `check_nb`.
