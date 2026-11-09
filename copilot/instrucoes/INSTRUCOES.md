# Como trabalhar comigo

Sou analista de dados. Responda em português. O entregável padrão é código de análise
(notebook Databricks), não texto sobre código.

Ambiente: Databricks. Dados no Unity Catalog, código em notebooks, computação Spark.
O contexto da empresa (catálogos, tabelas, sentinelas, regras de negócio) está na seção
**Empresa** no fim destas instruções. Se ela ainda estiver com `<...>`, avise uma vez e
sugira rodar a skill `setup-empresa`.

## Triagem: decida na primeira linha da resposta

**ROTA 0 — PERGUNTA.** Conceito, interpretação de número ou dúvida sem artefato a
produzir. Responda direto, curto, com exemplo numérico quando ajudar. Sem notebook.

**ROTA A — AJUSTE.** Mudança em código ou notebook que já existe. Edite direto, sem plano.

**ROTA B — ANÁLISE NOVA.** Notebook ou consulta nova sobre tabelas.
1. Escreva a linha de engine (abaixo). Ela é o briefing.
2. Schema: `DESCRIBE TABLE EXTENDED <catalogo.schema.tabela>` (colunas, tipos e
   comentários do Unity Catalog).
3. Use a skill `perfil-tabela` só se a tabela for nova na conversa **e** a escala ou a
   cardinalidade de algum join for incerta.
4. Siga a skill `estilo-analise`. Some as skills de domínio quando o tema pedir:
   `data-profiler` (profiling, IV/KS/PSI, triagem de variáveis), `data-science` (treinar,
   validar, calibrar, ratings, exportar modelo) e `analise-credito` (NPV, margem, perda em
   R$, corte e swap de política).

**ROTA C — PIPELINE / TABELA DE PRODUÇÃO / ESCALA INCERTA.** Rota B com `perfil-tabela`
obrigatória e revisão no fim pela skill `revisao-notebook`, de preferência num chat novo
(ou no agente `revisor`), recebendo só o caminho do notebook e o critério de pronto.

Pergunte antes de agir só quando assumir errado torna o entregável inútil ou a ação
insegura. No resto, declare a premissa numa linha e siga.

## Linha de engine (obrigatória nas rotas B e C)

> `~X linhas, dado [Unity Catalog / arquivo em volume], saída [notebook / tabela Delta] → [engine] porque [motivo]. | artefato: notebook`

- **PySpark até o recorte.** Dado no Unity Catalog é filtrado e agregado em PySpark: o dado
  não sai do cluster. Saída que vira tabela também é PySpark.
- **Polars depois do recorte**, quando o resultado já filtrado cabe no driver (ordem de
  poucos milhões de linhas): `pl.from_pandas(sdf.toPandas())`.
- **SQL só como extração fina** (`SELECT` + filtro de partição via `spark.sql`). Lógica de
  negócio em SQL só se eu pedir.
- Nunca `toPandas()`, `collect()` ou `display()` sobre a base inteira.

## Notebooks

- Nome: `YYYYMMDD_nome_snake_case[_vN]`.
- Template e regras da skill `estilo-analise`: 5 seções, extração isolada, joins com
  cardinalidade validada, sentinelas viram nulo, validações com `assert`.
- Entregue sem outputs (Limpar estado e resultados) quando o notebook for exportado ou
  versionado.
- Se o checklist `check_nb.py` estiver disponível, rode-o antes de entregar e corrija o que
  falhar. Não entregue com ressalva.

## Verificação

Antes de dizer que terminou, mostre evidência: a saída da célula que rodou, não
"validei". Rode primeiro numa amostra pequena (uma partição, `.limit()`).

Rastreabilidade de números:
- Toda cifra citada tem origem (notebook, célula ou tabela). Sintético é marcado como tal.
- Conta derivada por você (anualizar taxa, mudar base) é marcada como "cálculo meu".
- Documentação e código divergem: o código manda; aponte a divergência.
- Use as funções dos pacotes das skills em vez de reescrever métricas à mão.

## Ações que sempre pedem minha confirmação explícita

Antes de executar, mostre o comando e espere eu aprovar:
`DROP`, `DELETE`, `TRUNCATE`, `INSERT OVERWRITE`, `INSERT INTO`, `UPDATE`, `MERGE`,
`CREATE OR REPLACE`, `REPLACE TABLE`, `ALTER TABLE ... DROP/RENAME`, `VACUUM`,
`RESTORE TABLE`, `.write` com `mode("overwrite")`, `saveAsTable`, `dbutils.fs.rm`,
deploy ou exclusão de job/pipeline, `git push`. Leitura, inspeção e criação de notebook
novo rodam direto.

## Conversa

- Tarefas sem relação: sugira começar um chat novo.
- Depois de duas correções no mesmo ponto, proponha recomeçar limpo com um pedido melhor.
