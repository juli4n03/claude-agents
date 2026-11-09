---
name: setup-empresa
description: Configura o kit para a empresa - descobre catálogos e tabelas no Databricks, faz uma entrevista curta e preenche a seção Empresa das instruções. Rode só quando eu pedir explicitamente.
disable-model-invocation: true
---

# Setup da empresa

Rode **só quando eu pedir**. Objetivo: em uma conversa curta, preencher a seção
**Empresa** das instruções (modelo em `instrucoes/empresa.modelo.md` do kit). Nada de
senha, token ou dado de cliente vai para esse texto.

## 1. Descobrir sozinho antes de perguntar

Consultas de leitura, sem pedir permissão:

- `SELECT current_user(), current_catalog(), current_schema()`
- `SHOW CATALOGS` e, nos catálogos de análise, `SHOW SCHEMAS IN <catalogo>`
- Tabelas mais usadas por mim, se a tabela de sistema estiver liberada:
  `SELECT source_table_full_name, count(*) FROM system.access.table_lineage WHERE created_by = current_user() GROUP BY 1 ORDER BY 2 DESC LIMIT 20`
  (se der erro de permissão, pule).
- Comentários das tabelas centrais: `DESCRIBE TABLE EXTENDED <tabela>`.

## 2. Entrevista (no máximo 2 rodadas de perguntas)

Pergunte só o que a descoberta não respondeu:

1. **Catálogo e compute**: catálogo/schema padrão e o cluster ou serverless usado.
2. **Tabelas centrais**: grão, partição e o que cada uma representa.
3. **Sentinelas e armadilhas**: valores que significam ausente, fusos, grãos que
   costumam colapsar.
4. **Domínio**: o que a área faz, targets e métricas usuais.
5. **Convenções**: onde vive o código de produção, regras de nome e revisão.
6. **Proibições**: ações que nunca devem acontecer sem confirmação além das padrão.

## 3. Escrever

- Preencha a seção **Empresa** (até ~150 linhas) e me mostre o texto final para eu colar
  no arquivo de instruções (Genie Code: `.assistant_instructions.md`; GitHub Copilot:
  `copilot-instructions.md` ou `AGENTS.md`).
- Se a área trabalha com crédito: preencha também `credito.modelo.md` (parâmetros
  canônicos com status "confirmado / a confirmar", direção da escala de risco, target e
  janela de maturação).
- O arquivo de instruções do Genie Code tem limite de 20.000 caracteres: confira o
  tamanho final.

## 4. Verificar

1. `DESCRIBE TABLE EXTENDED` de uma tabela central devolve colunas.
2. A extração padrão da seção Empresa roda com `.limit(5)`.
3. Mostre a saída dos dois e o resumo do que foi preenchido.
