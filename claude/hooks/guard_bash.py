#!/usr/bin/env python3
"""Hook PreToolUse (Bash): pede confirmação antes de comandos destrutivos.

Não bloqueia: devolve permissionDecision "ask", então a ação só roda se eu aprovar,
mesmo em auto mode. Cobre comandos de shell (inclusive `python -c` com SQL inline);
SQL executado de dentro de um notebook não passa por aqui e depende das regras do
CLAUDE.md.

Uso:
  python3 guard_bash.py             # Claude Code (saída em hookSpecificOutput)
  python3 guard_bash.py --copilot   # GitHub Copilot (preToolUse, saída no nível de cima)
"""
from __future__ import annotations

import json
import re
import sys

# Padrões de SQL só valem quando o comando executa SQL (evita falso positivo em
# mensagens de commit, grep etc.).
CONTEXTO_SQL = re.compile(
    r"athena|\bbq\b|psql|snowsql|spark-sql|beeline|mysql|sqlite3|duckdb|trino|presto"
    r"|databricks|--query|\.sql\b|awswrangler|\bwr\.|read_sql|execute\(|spark\.sql\(",
    re.I,
)
PERIGOSOS_SQL = [
    (r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA|VIEW|CATALOG|VOLUME)\b", "DDL destrutivo"),
    (r"\bDELETE\s+FROM\b", "DELETE"),
    (r"\bINSERT\s+OVERWRITE\b", "INSERT OVERWRITE"),
    (r"\bALTER\s+TABLE\b.*\b(DROP|RENAME)\b", "ALTER TABLE destrutivo"),
    (r"\bINSERT\s+INTO\b", "INSERT no warehouse"),
    (r"\bUPDATE\s+[\w.`\"]+\s+SET\b", "UPDATE"),
    (r"\bMERGE\s+INTO\b", "MERGE"),
    (r"\b(CREATE\s+(OR\s+REPLACE\s+)?|REPLACE\s+)TABLE\b", "CREATE/REPLACE TABLE"),
    (r"\bVACUUM\b", "VACUUM (apaga histórico Delta)"),
    (r"\bRESTORE\s+TABLE\b", "RESTORE TABLE"),
]
PERIGOSOS = [
    (r"\baws\s+s3\s+(rm|rb)\b", "remoção no S3"),
    (r"\baws\s+s3\s+(mv|sync)\b.*--delete\b", "sync/mv com --delete no S3"),
    (r"\bgsutil\s+(-m\s+)?rm\b|\bgcloud\s+storage\s+rm\b", "remoção no GCS"),
    (r"\bdbutils\.fs\.rm\(|\bhdfs\s+dfs\s+-rm\b", "remoção no lake"),
    (r"\bdatabricks\s+(fs\s+rm|workspace\s+delete|bundle\s+destroy"
     r"|(jobs|pipelines|clusters|warehouses|catalogs|schemas|tables|volumes)\s+(delete|permanent-delete))\b",
     "remoção no Databricks"),
    (r"\bdatabricks\s+(bundle\s+deploy|jobs\s+run-now)\b", "deploy/execução de job no Databricks"),
    # rm com recursivo e força em qualquer ordem: -rf, -fr, -r -f, --recursive --force
    (r"\brm\b(?=[^;&|\n]*\s(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b)(?=[^;&|\n]*\s(-[a-zA-Z]*f[a-zA-Z]*|--force)\b)",
     "rm -rf"),
    # PowerShell: Remove-Item com -Recurse (o -Force é opcional no Windows)
    (r"\b(Remove-Item|rmdir|rd)\b(?=[^;&|\n]*\s-Recurse\b)|\b(rmdir|rd)\s+/s\b", "remoção recursiva (Windows)"),
    (r"\.mode\(\s*[\"']overwrite|\.\w+\(.*\bmode\s*=\s*[\"']overwrite", "escrita com overwrite"),
    (r"\.saveAsTable\(|\.writeTo\(.*\.(replace|createOrReplace|overwrite)\(", "escrita em tabela"),
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "git clean -f"),
]
# Ferramentas de shell do Copilot: toolName contém um destes trechos (bash, powershell,
# run_in_terminal...). As demais (edit, view, grep...) não executam comando.
SHELL_COPILOT = ("bash", "shell", "terminal", "command")


def _comando(dados: dict, copilot: bool) -> str:
    """Extrai o comando de shell da entrada do hook (formato Claude Code ou Copilot)."""
    entrada = dados.get("tool_input")
    if entrada is None and copilot:
        nome = str(dados.get("toolName") or dados.get("tool_name") or "").lower()
        if nome and not any(trecho in nome for trecho in SHELL_COPILOT):
            return ""
        entrada = dados.get("toolArgs")
        if isinstance(entrada, str):
            try:
                entrada = json.loads(entrada)
            except json.JSONDecodeError:
                return entrada  # argumento cru: checa o texto todo
    if isinstance(entrada, dict):
        return str(entrada.get("command") or entrada.get("cmd") or entrada.get("script") or "")
    return ""


def motivos_de(comando: str) -> list[str]:
    regras = PERIGOSOS + (PERIGOSOS_SQL if CONTEXTO_SQL.search(comando) else [])
    return [nome for padrao, nome in regras if re.search(padrao, comando, re.I | re.S)]


def main() -> int:
    copilot = "--copilot" in sys.argv[1:]
    try:
        dados = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(dados, dict):
        return 0
    motivos = motivos_de(_comando(dados, copilot))
    if motivos:
        razao = "Ação irreversível: " + ", ".join(motivos)
        if copilot:
            saida = {"permissionDecision": "ask", "permissionDecisionReason": razao}
        else:
            saida = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": razao,
            }}
        print(json.dumps(saida, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
