# Como trabalhar comigo

Sou analista de dados. Respondo em português. O entregável padrão é código de análise
(notebook), não texto sobre código.

O contexto da empresa atual (warehouse, dicionários, regras de negócio) está em:
@~/.claude/local/empresa.md

Se esse arquivo não existir, avise uma vez e sugira rodar `/setup-empresa`.

## Triagem — decida na primeira linha da resposta

**ROTA A — AJUSTE.** Mudança em código ou notebook que já está na conversa ou num arquivo
existente. Edite direto. Nenhum subagente, nenhum plano.

**ROTA B — ANÁLISE NOVA.** Notebook ou consulta nova sobre tabelas.
1. Escreva a linha de engine (abaixo). Ela é o briefing.
2. Schema: `python ~/.claude/bin/schema.py <tabela>` para cada tabela.
3. Chame `@dados` só se a tabela for nova para você nesta sessão **e** a escala ou a
   cardinalidade de algum join for incerta. Tabelas independentes vão em paralelo.
4. Carregue a skill `estilo-analise` e escreva.

**ROTA C — PIPELINE / TABELA DE PRODUÇÃO / ESCALA INCERTA.** Rota B com `@dados`
obrigatório e `@revisor` no fim, recebendo só o artefato e o critério de pronto.

Subtarefa que depende do contexto atual: `/subtask` (fork), não subagente novo.

Pergunte antes de agir só quando assumir errado torna o entregável inútil ou a ação
insegura. No resto, declare a premissa numa linha e siga.

## Linha de engine (obrigatória nas rotas B e C)

> `~X linhas, dado [materializado/no lake], saída [local/warehouse] → [engine] porque [motivo]. | artefato: [notebook .ipynb / script .py]`

- **Polars é o default.** PySpark para dado grande particionado no lake ou saída que volta
  ao warehouse. SQL só como extração fina (`SELECT` + filtro de partição); lógica em SQL ou
  DuckDB só se eu pedir.
- **Notebook é o default de artefato**, independente da engine. Script `.py` só a pedido,
  ou quando o arquivo `.py` já existe.

## Notebooks

- Notebook novo nasce via `nbformat` (nunca JSON cru). Notebook existente se edita com
  `NotebookEdit`, uma célula por chamada.
- Nome: `YYYYMMDD_nome_snake_case[_vN].ipynb`, caminho confirmado com Glob.
- Antes de entregar, rode `python ~/.claude/hooks/check_nb.py <arquivo>`. O mesmo checklist
  roda como hook no fim do turno e devolve o que falhou. Corrija antes de entregar, não
  entregue com ressalva.

## Verificação

Antes de dizer que terminou, mostre evidência: a saída do comando que rodou, não "validei".
Quando der, execute o notebook numa amostra pequena (uma partição, `LIMIT`).

## Ações que sempre pedem confirmação

`DROP` / `DELETE` / `TRUNCATE` / `INSERT OVERWRITE`, escrita no warehouse, remoção no lake,
sobrescrita de arquivo existente, `git push`. Leitura, inspeção, extract e arquivo novo
rodam direto.

## Sessão

- Tarefas sem relação: sugira `/clear`.
- Depois de duas correções no mesmo ponto, proponha recomeçar limpo com um prompt melhor.
- Ao compactar, preserve: arquivos modificados, linha de engine, tabelas e premissas.
