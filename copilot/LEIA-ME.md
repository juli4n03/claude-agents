# Kit IA — análise de dados no Databricks com Copilot

Meu jeito de trabalhar com IA em análise de dados, empacotado para levar a qualquer
empresa: regras de trabalho, skills (instruções especializadas), agentes, checagens
automáticas e pacotes Python. **Não contém dado, credencial nem regra de negócio de
nenhuma empresa**: a parte da empresa é preenchida no primeiro uso (skill `setup-empresa`).

## O que tem aqui

| Pasta | O que é | Usado por |
|---|---|---|
| `instrucoes/` | Regras de trabalho (`INSTRUCOES.md`) e modelos da parte da empresa | todos |
| `skills/` | 7 skills no formato `SKILL.md`: estilo-analise, perfil-tabela, revisao-notebook, setup-empresa, data-profiler (com o pacote), data-science, analise-credito | Genie Code, GitHub Copilot, M365 (como conhecimento) |
| `genie-code/` | Arquivo de instruções pronto para o Genie Code (assistente do Databricks) | Genie Code |
| `github-copilot/` | Instruções, agentes (`dados`, `revisor`), hooks de segurança e checklist | GitHub Copilot (CLI/IDE) |
| `m365-copilot/` | Roteiro para criar um agente no Microsoft 365 Copilot | M365 Copilot |
| `pacotes/` | Wheels Python: `data_profiler` (meu) e `yggdrasil_project` 0.0.22 (MIT) | notebooks no Databricks |

## Qual usar

- **Dentro do Databricks** (o mais útil): Genie Code. Ele roda o código no notebook, lê
  o Unity Catalog e aceita as mesmas skills. Não tem hooks: a segurança fica na
  aprovação de ações (ligue a aprovação manual) e nas suas permissões do Unity Catalog.
- **GitHub Copilot** (CLI ou IDE) no notebook da empresa: única opção com checagens
  automáticas (hooks) que pedem confirmação antes de comando destrutivo e bloqueiam
  notebook fora do padrão.
- **Microsoft 365 Copilot**: só conversa. Escreve e revisa código para você colar.

## Instalação

Antes de tudo: extraia o zip numa pasta, por exemplo `Documentos/kit_ia`.

### A. Genie Code (Databricks)

1. No workspace, crie a pasta `/Workspace/Users/<seu_email>/kit_ia` e importe nela o
   conteúdo de `skills/` e `pacotes/` (Workspace › Importar; se a importação de pasta não
   estiver disponível, importe arquivo por arquivo).
2. **Instruções**: abra o painel do Genie Code › configurações › instruções do usuário
   (cria `/Users/<seu_email>/.assistant_instructions.md`) e cole o conteúdo de
   `genie-code/assistant_instructions.md`. Limite: 20.000 caracteres.
3. **Skills**: copie cada pasta de `skills/` para `/Users/<seu_email>/.assistant/skills/`
   (cada skill numa pasta com o seu `SKILL.md`), ou registre `/Workspace/Users/<seu_email>/kit_ia/skills`
   como pasta adicional de skills nas configurações do Genie Code. Comece um chat novo
   depois de mudar skills.
4. **Segurança**: nas configurações do Genie Code, ative a aprovação de cada ação
   ("Approve tool actions"). Por padrão um classificador aprova sozinho.
5. Peça no chat: "rode a skill setup-empresa" e cole o resultado na seção Empresa das
   instruções.

Documentação: docs.databricks.com/aws/en/genie-code/instructions e
docs.databricks.com/aws/en/genie-code/skills

### B. GitHub Copilot (CLI ou IDE)

Pré-requisito: Python 3.10+ no PATH. Confira com `python --version` (Windows) ou
`python3 --version`. **Importante:** o hook `preToolUse` falha fechado. Se o Python não
rodar, o Copilot passa a negar todos os comandos; nesse caso apague
`~/.copilot/hooks/kit_ia.json`.

1. Copie `github-copilot/hooks/kit_ia.json` para `~/.copilot/hooks/` e a pasta
   `github-copilot/kit_ia/` para `~/.copilot/kit_ia/` (contém `hooks/guard_bash.py` e
   `hooks/check_nb.py`).
2. Copie as pastas de `skills/` para `~/.copilot/skills/`.
3. Copie `github-copilot/agents/*.agent.md` para `~/.copilot/agents/`.
4. Instruções: em cada repositório/pasta de análise, copie
   `github-copilot/copilot-instructions.md` para `.github/copilot-instructions.md` (ou
   como `AGENTS.md` na raiz). Preencha a seção Empresa.
5. Teste: peça ao Copilot para rodar `rm -rf pasta_teste`. Ele deve pedir confirmação.
6. Teste do checklist: `python ~/.copilot/kit_ia/hooks/check_nb.py <notebook>` num
   notebook seu (`.ipynb` ou `.py` exportado do Databricks).

No Windows, troque `~` por `%USERPROFILE%`. Se `COPILOT_HOME` estiver definido, use essa
pasta no lugar de `~/.copilot`.

### C. Microsoft 365 Copilot

Siga `m365-copilot/agente-analista-dados.md`.

## Pacotes Python no Databricks

```python
%pip install /Workspace/Users/<seu_email>/kit_ia/pacotes/data_profiler-1.0.0-py3-none-any.whl
```

`yggdrasil_project` (skill data-science) instala com as restrições de
`skills/data-science/restricoes.txt` e baixa dependências do PyPI ou do espelho da
empresa. Se a rede bloquear, peça à TI a liberação do pacote; não contorne.

## O que ficou de fora (de propósito)

- **Pacote de métricas de crédito (`credit_analytics`)**: tem regras e parâmetros de
  outro contexto. A skill `analise-credito` funciona sem ele, declarando premissas.
- **Avaliação automática do kit (`evals/`)**: depende do Claude Code.
- **Plugins do Claude Code**: não existem no Copilot. Para skills oficiais da Databricks,
  a CLI tem `databricks aitools install`.

## Segurança e privacidade

- Não cole dado de cliente, senha ou token nas instruções, skills ou chats.
- As regras de confirmação nas instruções são texto: só o GitHub Copilot com os hooks
  impõe a confirmação de fato. No Genie Code, ative a aprovação manual.
- Os scripts `.py` deste kit só leem a entrada do hook e os notebooks da pasta; não
  acessam rede. Código-fonte aberto para auditoria em `github-copilot/kit_ia/hooks/`.
