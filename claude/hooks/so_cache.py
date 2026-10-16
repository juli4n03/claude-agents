#!/usr/bin/env python3
"""Hook PreToolUse do @dados: só deixa Write/Edit dentro de ~/.claude/local/cache/.

O agent é somente leitura; a única escrita permitida é o perfil da tabela no cache.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CACHE = (Path(os.environ.get("CLAUDE_LOCAL_DIR", Path.home() / ".claude" / "local")).expanduser() / "cache").resolve()


def main() -> int:
    try:
        entrada = json.load(sys.stdin).get("tool_input", {})
    except json.JSONDecodeError:
        return 0
    caminho = entrada.get("file_path") or entrada.get("notebook_path") or ""
    if not caminho:
        return 0
    alvo = Path(caminho).expanduser().resolve()
    if alvo == CACHE or CACHE in alvo.parents:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"@dados só grava em {CACHE}; {alvo} está fora.",
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
