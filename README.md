# claude-agents

Meu kit pessoal de Claude Code para análise de dados. Em qualquer empresa nova, dois passos
deixam tudo configurado:

```bash
git clone https://github.com/juli4n03/claude-agents ~/claude-agents
CLAUDE_LOCAL_DIR=~/<disco-persistente>/claude-local ~/claude-agents/install.sh --memoria
```

e, dentro do `claude`, `/setup-empresa`.

## Ideia

O repositório guarda **como eu trabalho**: fluxo, agents, estilo, checagens. O que é
**de cada empresa** (warehouse, dicionários, tabelas, regras de negócio, memórias) é gerado
por uma entrevista e fica numa pasta local que nunca entra no git.

```
claude-agents/  (público)                  ~/.claude-local/  (privado, por empresa)
├── claude/      → ligado em ~/.claude     ├── empresa.md          contexto da empresa
│   ├── CLAUDE.md     fluxo e regras       ├── config.yaml         onde estão os dicionários
│   ├── settings.json permissões e hooks   ├── settings.local.json overrides da máquina
│   ├── agents/       dados, revisor       ├── eval_vars.yaml      nomes reais para o eval
│   ├── skills/       estilo, setup        ├── cache/              perfis de tabela
│   ├── hooks/        guard_bash, check_nb ├── evals/              resultados das rodadas
│   └── bin/          schema.py            └── memoria/            memória automática
├── local.example/  modelos da parte privada
└── evals/          casos e runner
```

## Fluxo

O `CLAUDE.md` faz a triagem na primeira linha de cada resposta:

| Rota | Quando | O que roda |
|---|---|---|
| **A — ajuste** | mudança em código que já existe | edição direta, nenhum subagente |
| **B — análise nova** | notebook ou consulta nova | linha de engine → `schema.py` → `@dados` só se a escala for incerta → skill de estilo |
| **C — pipeline** | tabela de produção, escala incerta | B + `@dados` obrigatório + `@revisor` com contexto limpo |

Subtarefas que dependem do contexto atual vão com `/subtask` (fork, reaproveita o cache).

## Peças

- **`@dados`** (Sonnet, somente leitura): schema, sentinelas, cardinalidade de joins e
  sinal de escala. Grava o perfil em `cache/` e devolve no máximo 15 linhas.
- **`@revisor`** (Opus, contexto limpo): só problemas que mudam o resultado.
- **`estilo-analise`**: template de 5 células, Polars lazy, extração isolada, asserts.
- **`setup-empresa`**: descobre o ambiente, entrevista e gera a parte privada.
- **`guard_bash.py`** (hook antes de cada comando): pede confirmação para DROP, DELETE,
  remoção no lake, `rm -rf`, `git push`, mesmo em auto mode.
- **`check_nb.py`** (hook no fim do turno e CLI): nome do arquivo, outputs, `validate=` em
  joins, um único `.collect()`, presença de asserts.
- **`schema.py`**: schema de uma tabela a partir dos dicionários YAML, em segundos.

## Evoluir sem achismo

```bash
python evals/run_eval.py --pasta ~/eval_sandbox --julgar
```

Roda os casos de `evals/casos.yaml` com `claude -p`, mede tempo, turnos e custo e dá nota
com um juiz. Rode antes e depois de mudar o kit e compare os dois CSVs.

## Requisitos

Claude Code recente (o agent `@dados` usa `omitClaudeMd`), Python 3.10+, `pyyaml` e
`nbformat`.
