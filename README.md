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
│   ├── CLAUDE.md     fluxo e regras       ├── credito.md          parâmetros de crédito
│   ├── settings.json permissões, hooks,   ├── config.yaml         dicionários e pacotes
│   │                 plugins              ├── settings.local.json overrides da máquina
│   ├── agents/       dados, revisor       ├── eval_vars.yaml      nomes reais para o eval
│   ├── skills/       estilo, setup,       ├── pacotes/            clone do repo privado credit_analytics
│   │                 data-profiler,       ├── cache/              perfis de tabela
│   │                 analise-credito      ├── evals/              resultados das rodadas
│   ├── hooks/        guard_bash, check_nb,└── memoria/            memória automática
│   │                 so_cache
│   └── bin/          schema.py
├── local.example/  modelos da parte privada
└── evals/          casos e runner
```

## Fluxo

O `CLAUDE.md` faz a triagem na primeira linha de cada resposta:

| Rota | Quando | O que roda |
|---|---|---|
| **0 — pergunta** | conceito ou leitura de número, sem artefato | resposta direta, sem notebook |
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
- **`data-profiler`**: profiling, WoE/IV, KS, PSI, estabilidade por safra, vazamento e
  correlação em Polars ou PySpark com o mesmo schema. O pacote (41 testes) vem dentro da
  skill e o `install.sh` o instala em modo editável.
- **`analise-credito`**: método PD → perda em R$ → margem em NPV → corte e swap. Só o
  método é público. O pacote `credit_analytics` fica no repositório **privado**
  `juli4n03/credit-analytics`, que o `install.sh` clona em `pacotes/` quando a máquina
  tem acesso (`gh auth login`); os parâmetros da empresa ficam em `credito.md`.
- **`guard_bash.py`** (hook antes de cada comando): pede confirmação para DROP, DELETE,
  INSERT/UPDATE/MERGE/CREATE TABLE, escrita com overwrite, remoção no lake, `rm -rf` em
  qualquer ordem de flags e `git push`, mesmo em auto mode.
- **`check_nb.py`** (hook no fim do turno e CLI): nome do arquivo, outputs, `validate=` em
  joins, um único `.collect()`, presença de asserts. Só olha notebooks alterados no turno
  atual (marca gravada no `UserPromptSubmit`), então não bloqueia por notebook que eu
  editei no Jupyter.
- **`so_cache.py`** (hook só do `@dados`): o agent só grava em `~/.claude/local/cache/`.
- **`schema.py`**: schema de uma tabela a partir dos dicionários YAML, em segundos.

## Plugins

Declarados em `claude/settings.json` (`enabledPlugins`) e instalados pelo `install.sh`
(`--sem-plugins` pula). Critério: nada que mande código ou dado da empresa para terceiros
nem concorra com as rotas do `CLAUDE.md`.

| Plugin | Status | Por quê |
|---|---|---|
| `context7` | ligado | docs atuais de Polars, PySpark, boto3: menos API inventada. Manda só o nome da lib e a pergunta |
| `claude-md-management` | ligado | `/revise-claude-md` incorpora o que a sessão aprendeu. Edita o repo (symlink): revise o diff |
| `claude-code-setup` | ligado | recomenda automações para um projeto; leitura, uso pontual |
| `superpowers` | opcional | método forte (plano, TDD, debug), mas o prompt impositivo compete com as rotas A/B/C e TDD rende pouco em notebook |
| `feature-dev`, `code-review` | opcional | úteis em repositório de aplicação; em análise duplicam `@revisor` e o `/code-review` embutido |
| `ponytail` | opcional | "escreva menos código"; injeta contexto em toda sessão e em todo subagente |
| `claude-mega-brain` | opcional | só vale se a documentação seguir o formato OKF; sobrepõe `empresa.md` |
| `claude-mem` | evitar com dado sensível | captura toda saída de ferramenta e comprime com LLM (padrão: serviço hospedado), telemetria ligada, hook síncrono em todo Bash |
| `codex` (OpenAI) | evitar com dado sensível | manda código e diffs para a OpenAI; exige conta própria |

Para ligar um opcional numa máquina: `/plugin install superpowers@claude-plugins-official`
(ou `claude plugin marketplace add <owner/repo>` antes, para os de terceiros).

## Evoluir sem achismo

```bash
python3 evals/run_eval.py --pasta ~/eval_sandbox --julgar --repeticoes 3
```

Roda os casos de `evals/casos.yaml` com `claude -p`, cada um numa cópia descartável da
pasta, e mede tempo, turnos e custo. Do stream de eventos saem checagens sem juiz: rota da
primeira linha, subagentes e skills chamados e `check_nb` nos notebooks gerados. O juiz
(opcional) recebe essa evidência junto com a resposta. Rode antes e depois de mudar o kit
e compare os CSVs.

## Requisitos

Claude Code recente (o agent `@dados` usa `omitClaudeMd`, de 2.1.271, e hooks no
frontmatter), Python 3.10+, `pyyaml` e `nbformat`. Para as skills: `polars numpy scipy`
(`data-profiler`) e `matplotlib duckdb` (`credit_analytics`).
