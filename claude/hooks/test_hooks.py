#!/usr/bin/env python3
"""Testes dos hooks guard_bash.py e check_nb.py (formatos Claude Code e Copilot).

Rode: python3 claude/hooks/test_hooks.py   (sem dependências além da biblioteca padrão)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent.parent
GUARD = AQUI / "guard_bash.py"
CHECK = AQUI / "check_nb.py"
SKILL_ESTILO = RAIZ / "copilot" / "skills" / "estilo-analise" / "SKILL.md"


def roda(script: Path, entrada: dict | None, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=json.dumps(entrada) if entrada is not None else "",
        capture_output=True, text=True, timeout=30,
    )


def test_guard() -> None:
    # Claude Code: formato antigo continua igual.
    r = roda(GUARD, {"tool_input": {"command": "rm -rf /tmp/x"}})
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask", r.stdout
    assert roda(GUARD, {"tool_input": {"command": "echo ok"}}).stdout == ""
    assert roda(GUARD, {"tool_input": {"command": "git commit -m 'DELETE FROM nada'"}}).stdout == ""

    # Copilot: toolArgs como dict e como string JSON; saída no nível de cima.
    casos_ask = [
        {"toolName": "bash", "toolArgs": {"command": "databricks fs rm dbfs:/tmp/x"}},
        {"toolName": "bash", "toolArgs": json.dumps({"command": "python -c \"spark.sql('VACUUM cat.s.t')\""})},
        {"toolName": "powershell", "toolArgs": {"command": "Remove-Item C:\\dados -Recurse"}},
        {"toolName": "bash", "toolArgs": {"command": "databricks bundle deploy -t prod"}},
        {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
    ]
    for caso in casos_ask:
        r = roda(GUARD, caso, "--copilot")
        saida = json.loads(r.stdout)
        assert saida["permissionDecision"] == "ask", (caso, r.stdout)
        assert "hookSpecificOutput" not in saida
    # Ferramenta que não é shell não é checada.
    assert roda(GUARD, {"toolName": "edit", "toolArgs": {"command": "rm -rf /"}}, "--copilot").stdout == ""
    assert roda(GUARD, {"toolName": "bash", "toolArgs": {"command": "ls -la"}}, "--copilot").stdout == ""
    # Entrada inválida não derruba o hook (preToolUse do Copilot falha fechado).
    r = subprocess.run([sys.executable, str(GUARD), "--copilot"], input="não é json",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == ""


def _notebook_databricks(celulas: list[str]) -> str:
    return "# Databricks notebook source\n" + "\n\n# COMMAND ----------\n\n".join(celulas) + "\n"


def _blocos_template() -> list[str]:
    texto = SKILL_ESTILO.read_text(encoding="utf-8")
    blocos = re.findall(r"```python\n(.*?)```", texto, re.S)[:5]
    assert len(blocos) == 5 and blocos[0].startswith("# 1. Imports")
    return blocos


def test_check_nb() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # Template da skill estilo-analise (PySpark) passa limpo.
        ok = d / "20261109_template_skill.py"
        ok.write_text(_notebook_databricks(["# MAGIC %md\n# MAGIC ## Imports", *_blocos_template()]))
        r = roda(CHECK, None, str(ok))
        assert r.returncode == 0, r.stderr

        # Join PySpark sem validação de cardinalidade é apontado.
        ruim = d / "20261109_join_spark.py"
        ruim.write_text(_notebook_databricks([
            "from pyspark.sql import functions as F",
            "sdf = spark.table('a.b.c').join(spark.table('a.b.d'), on='id', how='left')",
            "assert sdf.limit(1) is not None",
        ]))
        r = roda(CHECK, None, str(ruim))
        assert r.returncode == 2 and "PySpark sem cardinalidade" in r.stderr, r.stderr

        # Célula mágica (%sql) não é tratada como Python.
        sql = d / "20261109_magica.py"
        sql.write_text(_notebook_databricks([
            "# MAGIC %sql\n# MAGIC SELECT a.x FROM a JOIN b ON a.id = b.id",
            "assert True",
        ]))
        assert roda(CHECK, None, str(sql)).returncode == 0

        # Polars (.ipynb) continua exigindo validate= e nome no padrão.
        nb = d / "Analise Teste.ipynb"
        nb.write_text(json.dumps({"cells": [
            {"cell_type": "code", "source": ["import polars as pl\n", "df = a.join(b, on='id')"], "outputs": []},
        ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
        r = roda(CHECK, None, str(nb))
        assert r.returncode == 2 and "validate=" in r.stderr and "fora do padrão" in r.stderr, r.stderr

        # .py comum (sem cabeçalho Databricks) é ignorado pelo hook.
        (d / "util.py").write_text("x = a.join(b)\n")

        # Copilot agentStop: bloqueia via JSON e sai com 0.
        sessao = f"teste-{os.getpid()}"
        roda(CHECK, {"sessionId": sessao}, "--marca")
        time.sleep(0.05)
        ruim.touch()
        r = roda(CHECK, {"sessionId": sessao, "cwd": str(d)}, "--copilot")
        saida = json.loads(r.stdout)
        assert r.returncode == 0 and saida["decision"] == "block" and "join_spark" in saida["reason"], r.stdout
        assert "util.py" not in saida["reason"]
        # Já bloqueou uma vez: não entra em loop.
        r = roda(CHECK, {"sessionId": sessao, "cwd": str(d), "stop_hook_active": True}, "--copilot")
        assert r.returncode == 0 and r.stdout == ""
        # Claude Code (Stop): exit 2 com a lista no stderr.
        r = roda(CHECK, {"session_id": sessao, "cwd": str(d)})
        assert r.returncode == 2 and "join_spark" in r.stderr


if __name__ == "__main__":
    test_guard()
    test_check_nb()
    print("hooks: todos os testes passaram")
