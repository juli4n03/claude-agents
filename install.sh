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
#                   para a pasta de trabalho atual (ou a raiz do repositório git dela).
# --sem-plugins     não instala os plugins listados em PLUGINS.
# CREDIT_ANALYTICS_REPO  repositório privado do credit_analytics (padrão: juli4n03/credit-analytics),
#                   clonado em CLAUDE_LOCAL_DIR/pacotes/ quando a máquina tem acesso.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE="$HOME/.claude"
LOCAL_DIR="${CLAUDE_LOCAL_DIR:-$HOME/.claude-local}"
LOCAL_DIR="${LOCAL_DIR/#\~/$HOME}"
BACKUP="$CLAUDE/backup-$(date +%Y%m%d-%H%M%S)"
MEMORIA=0
PLUGINS_ON=1
for arg in "$@"; do
  case "$arg" in
    --memoria) MEMORIA=1 ;;
    --sem-plugins) PLUGINS_ON=0 ;;
    *) echo "Opção desconhecida: $arg" >&2; exit 1 ;;
  esac
done

# Os mesmos de "enabledPlugins" em claude/settings.json.
PLUGINS=(
  context7@claude-plugins-official
  claude-md-management@claude-plugins-official
  claude-code-setup@claude-plugins-official
)

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
mkdir -p "$LOCAL_DIR/cache" "$LOCAL_DIR/pacotes"
liga "$LOCAL_DIR" "$CLAUDE/local"
[[ -f "$LOCAL_DIR/settings.local.json" ]] || echo '{}' > "$LOCAL_DIR/settings.local.json"
liga "$LOCAL_DIR/settings.local.json" "$CLAUDE/settings.local.json"

if (( MEMORIA )); then
  # O Claude Code usa a raiz do repositório git (ou a pasta, fora do git) e troca todo
  # caractere não alfanumérico por "-": espaço, "_" e "." inclusive.
  projeto="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  chave="$(printf '%s' "$projeto" | sed 's#[^a-zA-Z0-9]#-#g')"
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

echo "Pacotes das skills"
if python3 -m pip install -q -e "$REPO/claude/skills/data-profiler/pacote" 2>/dev/null; then
  echo "  data_profiler instalado (editável)"
else
  echo "  Aviso: data_profiler não instalado; a skill usa sys.path como alternativa."
fi

# credit_analytics mora num repositório privado: só entra se esta máquina tiver acesso.
CREDIT="$LOCAL_DIR/pacotes/credit_analytics"
CREDIT_REPO="${CREDIT_ANALYTICS_REPO:-https://github.com/juli4n03/credit-analytics.git}"
if [[ -d "$CREDIT/.git" ]]; then
  git -C "$CREDIT" pull -q --ff-only 2>/dev/null && echo "  credit_analytics atualizado" \
    || echo "  Aviso: não consegui atualizar $CREDIT (seguindo com a versão local)"
elif GIT_TERMINAL_PROMPT=0 git clone -q "$CREDIT_REPO" "$CREDIT" 2>/dev/null; then
  echo "  credit_analytics clonado do repositório privado"
else
  echo "  credit_analytics não instalado (sem acesso a $CREDIT_REPO; faça 'gh auth login' e rode de novo)"
fi
if [[ -d "$CREDIT" ]]; then
  python3 -m pip install -q -e "$CREDIT" 2>/dev/null && echo "  credit_analytics instalado (editável)" \
    || echo "  Aviso: pip falhou; a skill usa sys.path como alternativa."
fi

if (( PLUGINS_ON )); then
  echo "Plugins"
  if command -v claude >/dev/null 2>&1; then
    # Em máquina nova, sessões não interativas não registram o marketplace oficial sozinhas.
    claude plugin marketplace add anthropics/claude-plugins-official >/dev/null 2>&1 || true
    for p in "${PLUGINS[@]}"; do
      if claude plugin install "$p" --scope user >/dev/null 2>&1; then
        echo "  $p"
      else
        echo "  Aviso: falhou $p (instale com /plugin install $p)"
      fi
    done
  else
    echo "  claude não está no PATH; instale depois com /plugin install <nome>"
  fi
fi

echo
echo "Pronto. Reinicie o claude (CLAUDE.md, agents e settings são lidos no início da sessão)."
[[ -f "$LOCAL_DIR/empresa.md" ]] || echo "Empresa ainda não configurada: rode /setup-empresa."
