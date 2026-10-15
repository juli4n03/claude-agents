#!/usr/bin/env bash
# Liga o kit ao ~/.claude por symlinks. Idempotente: pode rodar de novo a qualquer hora,
# inclusive depois de um reset do ambiente.
#
#   ./install.sh                       # parte privada em ~/.claude-local
#   CLAUDE_LOCAL_DIR=~/shared/claude-local ./install.sh --memoria
#
# CLAUDE_LOCAL_DIR  onde mora a parte privada (empresa.md, config, cache, memória).
#                   Use um disco que sobrevive a resets (EFS, volume persistente).
# --memoria         também guarda a memória automática do Claude Code em CLAUDE_LOCAL_DIR,
#                   para a pasta de trabalho atual.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE="$HOME/.claude"
LOCAL_DIR="${CLAUDE_LOCAL_DIR:-$HOME/.claude-local}"
LOCAL_DIR="${LOCAL_DIR/#\~/$HOME}"
BACKUP="$CLAUDE/backup-$(date +%Y%m%d-%H%M%S)"
MEMORIA=0
[[ "${1:-}" == "--memoria" ]] && MEMORIA=1

mkdir -p "$CLAUDE"

liga() {  # liga <origem> <destino>
  local origem="$1" destino="$2"
  if [[ -e "$destino" && ! -L "$destino" ]]; then
    mkdir -p "$BACKUP"
    mv "$destino" "$BACKUP/"
    echo "  backup: $(basename "$destino") -> $BACKUP/"
  fi
  ln -sfn "$origem" "$destino"
  echo "  $destino -> $origem"
}

echo "Kit: $REPO"
for item in CLAUDE.md settings.json agents skills hooks bin; do
  liga "$REPO/claude/$item" "$CLAUDE/$item"
done
liga "$REPO/local.example" "$CLAUDE/local.example"

echo "Parte privada: $LOCAL_DIR"
if [[ ! -d "$LOCAL_DIR" ]]; then
  mkdir -p "$LOCAL_DIR/cache"
  echo "  criada (rode /setup-empresa no Claude para preencher)"
fi
mkdir -p "$LOCAL_DIR/cache"
liga "$LOCAL_DIR" "$CLAUDE/local"
[[ -f "$LOCAL_DIR/settings.local.json" ]] || echo '{}' > "$LOCAL_DIR/settings.local.json"
liga "$LOCAL_DIR/settings.local.json" "$CLAUDE/settings.local.json"

if (( MEMORIA )); then
  chave="$(pwd | sed 's#[/.]#-#g')"
  origem="$CLAUDE/projects/$chave/memory"
  destino="$LOCAL_DIR/memoria/$chave"
  mkdir -p "$destino" "$(dirname "$origem")"
  if [[ -d "$origem" && ! -L "$origem" ]]; then
    for f in "$origem"/*; do
      [[ -e "$f" && ! -e "$destino/$(basename "$f")" ]] && cp -R "$f" "$destino/"
    done
    rm -rf "$origem"
    echo "  memória existente copiada para $destino"
  fi
  liga "$destino" "$origem"
fi

python3 -c "import yaml, nbformat" 2>/dev/null \
  || echo "Aviso: instale as dependências dos scripts com: pip install pyyaml nbformat"

echo
echo "Pronto. Reinicie o claude (CLAUDE.md, agents e settings são lidos no início da sessão)."
[[ -f "$LOCAL_DIR/empresa.md" ]] || echo "Empresa ainda não configurada: rode /setup-empresa."
