#!/usr/bin/env python3
"""Hook PreToolUse (Bash): pede confirmação antes de comandos destrutivos.

Não bloqueia: devolve permissionDecision "ask", então a ação só roda se eu aprovar,
mesmo em auto mode. Cobre comandos de shell (inclusive `python -c` com SQL inline);
SQL executado de dentro de um notebook não passa por aqui e depende das regras do
CLAUDE.md.
"""
from __future__ import annotations

import json
import re
import sys

# Padrões de SQL só valem quando o comando executa SQL (evita falso positivo em
# mensagens de commit, grep etc.).
CONTEXTO_SQL = re.compile(
    r"athena|\bbq\b|psql|snowsql|spark-sql|beeline|mysql|sqlite3|duckdb|trino|presto"
    r"|databricks|--query|\.sql\b|awswrangler|read_sql|execute\(",
    re.I,
)
PERIGOSOS_SQL = [
    (r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA|VIEW)\b", "DDL destrutivo"),
    (r"\bDELETE\s+FROM\b", "DELETE"),
    (r"\bINSERT\s+OVERWRITE\b", "INSERT OVERWRITE"),
    (r"\bALTER\s+TABLE\b.*\b(DROP|RENAME)\b", "ALTER TABLE destrutivo"),
]
PERIGOSOS = [
    (r"\baws\s+s3\s+(rm|rb)\b", "remoção no S3"),
    (r"\baws\s+s3\s+(mv|sync)\b.*--delete\b", "sync/mv com --delete no S3"),
    (r"\bgsutil\s+(-m\s+)?rm\b|\bgcloud\s+storage\s+rm\b", "remoção no GCS"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r", "rm -rf"),
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "git clean -f"),
]


def main() -> int:
    try:
        comando = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except json.JSONDecodeError:
        return 0
    regras = PERIGOSOS + (PERIGOSOS_SQL if CONTEXTO_SQL.search(comando) else [])
    motivos = [nome for padrao, nome in regras if re.search(padrao, comando, re.I | re.S)]
    if motivos:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "Ação irreversível: " + ", ".join(motivos),
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
