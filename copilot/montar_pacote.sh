#!/usr/bin/env bash
# Monta o Kit IA para Copilot / Genie Code a partir do repositório, num zip para enviar
# por email. Fontes únicas: copilot/ (instruções, skills, adaptadores), claude/hooks/
# (guard_bash.py, check_nb.py) e o pacote data_profiler da skill do Claude.
#
#   copilot/montar_pacote.sh            # gera dist/kit_ia_copilot_AAAAMMDD{,.zip}
#   DATA=20261109 copilot/montar_pacote.sh
#
# YGGDRASIL_DIR  clone do Yggdrasil na tag fixada (padrão: ~/.claude-local/pacotes/yggdrasil-project)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${DATA:-$(date +%Y%m%d)}"
NOME="kit_ia_copilot_$DATA"
DIST="${DIST:-$REPO/dist}"
OUT="$DIST/$NOME"
YGG="${YGGDRASIL_DIR:-${CLAUDE_LOCAL_DIR:-$HOME/.claude-local}/pacotes/yggdrasil-project}"
YGG_REF="${YGGDRASIL_REF:-0.0.22}"
LIMITE_GENIE=20000

C="$REPO/copilot"
[[ -d "$OUT" ]] && rm -r "$OUT"
rm -f "$DIST/$NOME.zip"
mkdir -p "$OUT"/{instrucoes,skills,genie-code,github-copilot/agents,github-copilot/hooks,github-copilot/kit_ia/hooks,m365-copilot,pacotes}

cp "$C/LEIA-ME.md" "$C/email_sugestao.txt" "$OUT/"

# Instruções e modelos da parte da empresa
cp "$C/instrucoes/INSTRUCOES.md" "$C/instrucoes/empresa.modelo.md" "$OUT/instrucoes/"
sed 's#Lido pela skill analise-credito. Fica em ~/.claude/local/, nunca vai para o git.#Lido pela skill analise-credito. Preencha na empresa; não vai para repositório público.#' \
  "$REPO/local.example/credito.md" > "$OUT/instrucoes/credito.modelo.md"
INSTRUCOES_COMPLETAS="$(cat "$C/instrucoes/INSTRUCOES.md"; printf '\n'; cat "$C/instrucoes/empresa.modelo.md")"

# Skills (versões Databricks) + arquivos de apoio que vivem nas skills do Claude
cp -R "$C/skills/." "$OUT/skills/"
rsync -a --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' --exclude 'build' \
  "$REPO/claude/skills/data-profiler/pacote" "$OUT/skills/data-profiler/"
cp "$REPO/claude/skills/data-science/referencia.md" "$REPO/claude/skills/data-science/restricoes.txt" \
  "$OUT/skills/data-science/"

# Genie Code: um arquivo de instruções (limite de 20.000 caracteres)
printf '%s\n' "$INSTRUCOES_COMPLETAS" > "$OUT/genie-code/assistant_instructions.md"
n_chars=$(python3 -c "import sys; print(len(open(sys.argv[1], encoding='utf-8').read()))" "$OUT/genie-code/assistant_instructions.md")
if (( n_chars > LIMITE_GENIE )); then
  echo "Erro: instruções do Genie Code com $n_chars caracteres (limite $LIMITE_GENIE)" >&2
  exit 1
fi

# GitHub Copilot: instruções, agentes gerados das skills, hooks e scripts
printf '%s\n' "$INSTRUCOES_COMPLETAS" > "$OUT/github-copilot/copilot-instructions.md"
gera_agente() {  # gera_agente <nome> <skill>
  local nome="$1" skill="$C/skills/$2/SKILL.md"
  local descricao
  descricao="$(grep -m1 '^description:' "$skill" | sed 's/^description: *//')"
  {
    printf -- '---\nname: %s\ndescription: %s\n---\n' "$nome" "$descricao"
    awk 'f>=2 {print} /^---$/ && f<2 {f++}' "$skill"
  } > "$OUT/github-copilot/agents/$nome.agent.md"
}
gera_agente dados perfil-tabela
gera_agente revisor revisao-notebook
cp "$C/github-copilot/hooks/kit_ia.json" "$OUT/github-copilot/hooks/"
cp "$REPO/claude/hooks/guard_bash.py" "$REPO/claude/hooks/check_nb.py" "$OUT/github-copilot/kit_ia/hooks/"

# Microsoft 365 Copilot
cp "$C/m365-copilot/"*.md "$OUT/m365-copilot/"

# Wheels (sem dependências; o %pip baixa o resto). Constrói numa cópia temporária para
# não deixar build/ e egg-info nas fontes.
TMP_BUILD="$(mktemp -d)"
trap 'rm -r "$TMP_BUILD"' EXIT
wheel_de() {  # wheel_de <pasta-fonte> <nome>
  rsync -a --exclude '.git' --exclude '__pycache__' --exclude '*.egg-info' --exclude 'build' \
    --exclude 'notebooks' --exclude 'benchmarks' "$1/" "$TMP_BUILD/$2/"
  python3 -m pip wheel -q --no-deps -w "$OUT/pacotes" "$TMP_BUILD/$2"
}
wheel_de "$REPO/claude/skills/data-profiler/pacote" data_profiler
if [[ -d "$YGG/.git" ]] && [[ "$(git -C "$YGG" describe --tags --exact-match 2>/dev/null)" == "$YGG_REF" ]]; then
  wheel_de "$YGG" yggdrasil
  cp "$YGG/LICENSE" "$OUT/pacotes/LICENSE_yggdrasil.txt"
else
  echo "Aviso: Yggdrasil fora da tag $YGG_REF em $YGG; wheel não incluído." >&2
fi

# Nada de caminho do Claude Code fora das menções explicativas
if grep -rn --include='*.md' --include='*.json' '~/\.claude' "$OUT"; then
  echo "Erro: referência a ~/.claude no pacote" >&2
  exit 1
fi

find "$OUT" -name '.DS_Store' -delete
(cd "$DIST" && zip -rq "$NOME.zip" "$NOME")

# Email fora do zip, com o tamanho do anexo
tamanho_kb=$(( $(wc -c < "$DIST/$NOME.zip") / 1024 ))
{ cat "$C/email_sugestao.txt"; printf -- '- Tamanho do anexo: %s KB.\n' "$tamanho_kb"; } > "$DIST/email_sugestao_$DATA.txt"

echo "Pacote: $DIST/$NOME.zip ($tamanho_kb KB, $(cd "$OUT" && find . -type f | wc -l | tr -d ' ') arquivos)"
echo "Instruções do Genie Code: $n_chars de $LIMITE_GENIE caracteres"
echo "Email: $DIST/email_sugestao_$DATA.txt"
