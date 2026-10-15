---
name: setup-empresa
description: Configura o kit para uma empresa nova - entrevista sobre warehouse, dicionários, sentinelas e regras, e gera ~/.claude/local/empresa.md e config.yaml.
disable-model-invocation: true
---

# Setup de empresa nova

Objetivo: em uma conversa curta, gerar a parte privada do kit em `~/.claude/local/`.
Nada do que for escrito aqui vai para o repositório público.

## 1. Descobrir sozinho antes de perguntar

Rode e anote o que encontrar, sem pedir permissão:

- `aws sts get-caller-identity`, `gcloud config list`, `databricks auth describe`,
  `snowsql --version` (o que existir indica o warehouse)
- `pip list 2>/dev/null | grep -iE "awswrangler|pyathena|google-cloud-bigquery|snowflake|databricks|psycopg|sqlalchemy|polars|pyspark"`
- `find ~ -maxdepth 4 \( -iname "*dicionario*" -o -iname "*dictionary*" -o -iname "*schema*" \) -name "*.y*ml" 2>/dev/null | head`
- `ls ~` e repositórios git em `~` (`find ~ -maxdepth 3 -name .git -type d`)

## 2. Entrevista (AskUserQuestion, no máximo 2 rodadas)

Pergunte só o que a descoberta não respondeu:

1. **Warehouse e extração**: qual engine (Athena, BigQuery, Snowflake, Databricks,
   Postgres…) e o boilerplate de extração que a equipe usa (cole um exemplo).
2. **Dicionários de dados**: onde ficam e em que formato. Se não houver, `schema.py` usa
   só o fallback do warehouse.
3. **Sentinelas e armadilhas**: valores que significam ausente, fusos, grãos que costumam
   colapsar.
4. **Domínio**: o que a área faz, nomes de tabelas centrais, targets e métricas usuais.
5. **Repositórios e convenções**: onde o código de produção vive e regras de nome ou PR.
6. **Proibições**: ações que nunca devem acontecer sem confirmação além das padrão.

## 3. Escrever os arquivos

Copie os modelos de `~/.claude/local.example/` e preencha:

- `~/.claude/local/empresa.md` — curto (até ~150 linhas): warehouse, função `extrai()`
  pronta para colar, sentinelas, tabelas centrais, convenções, proibições.
- `~/.claude/local/config.yaml` — `dicionarios_dir` e o padrão de arquivo dos dicionários.
- Se a área trabalha com crédito: `~/.claude/local/credito.md` a partir do modelo
  (parâmetros canônicos com status "confirmado / a confirmar", direção da escala de risco,
  target e janela de maturação). Se houver pacote privado de métricas (ex.
  `credit_analytics`), copie para `~/.claude/local/pacotes/` e registre o caminho em
  `pacotes:` no `config.yaml`.
- Se a equipe usa comandos de leitura que pedem aprovação toda hora, proponha regras
  `allow` para `~/.claude/settings.local.json` e mostre antes de gravar.

## 4. Verificar

1. `python3 ~/.claude/bin/schema.py <uma tabela central>` precisa devolver colunas.
2. Uma extração de teste com `LIMIT 5` usando a `extrai()` do `empresa.md`.
3. Mostre ao usuário a saída dos dois e o resumo do que foi gravado.
