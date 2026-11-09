## Empresa

<!-- Preenchido pela skill setup-empresa. Mantenha curto: é carregado em toda conversa.
Não coloque senha, token nem dado de cliente aqui. -->

### Área e domínio
- Time / área: <...>
- O que analisamos: <crédito, marketing, operações…>
- Targets e métricas usuais: <...>

### Databricks
- Catálogo e schema padrão: <catalogo.schema>
- Compute usado em análise: <cluster compartilhado / serverless>
- Extração padrão (célula 2 do template):

```python
sdf_base = spark.table("<catalogo.schema.tabela>").where("dt_ref BETWEEN '<ini>' AND '<fim>'")
```

### Tabelas centrais
| tabela | grão | partição | observação |
|---|---|---|---|
| <catalogo.schema.tabela> | <1 linha por …> | <dt_ref> | <...> |

### Sentinelas e armadilhas conhecidas
- <coluna>: <-9999> significa ausente
- Fuso das datas: <UTC / America/Sao_Paulo>

### Código de produção e convenções
- Onde vive o código de produção: <Git folder / repositório>
- Regras de nome, revisão, publicação: <...>

### Proibições específicas
- <ações que sempre pedem confirmação, além das padrão>
