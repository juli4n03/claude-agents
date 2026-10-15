# Empresa: <nome>

<!-- Gerado por /setup-empresa. Fica em ~/.claude/local/, nunca vai para o git.
Mantenha curto: é carregado em toda sessão. -->

## Área e domínio
- Time / área: <...>
- O que analisamos: <crédito, marketing, operações…>
- Targets e métricas usuais: <...>

## Warehouse e extração
- Engine: <Athena / BigQuery / Snowflake / Databricks / Postgres>
- Função de extração (colar no notebook, célula 3):

```python
def extrai(sql: str) -> pl.DataFrame:
    ...
```

## Tabelas centrais
| tabela | grão | partição | observação |
|---|---|---|---|
| <schema.tabela> | <1 linha por …> | <dt_ref> | <...> |

## Sentinelas e armadilhas conhecidas
- <coluna>: <-9999> significa ausente
- Fuso das datas: <UTC / America/Sao_Paulo>

## Código de produção e convenções
- Repositórios: <...>
- Regras de nome, PR, revisão: <...>

## Proibições específicas
- <ações que sempre pedem confirmação, além das padrão>
