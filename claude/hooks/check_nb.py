#!/usr/bin/env python3
"""Checklist mecânico de notebooks.

Uso:
  - como hook UserPromptSubmit com `--marca`: grava a hora de início do turno;
  - como hook Stop: quando o Claude vai encerrar o turno, checa os .ipynb da pasta do
    projeto modificados desde o início do turno (sem marca, nos últimos 30 minutos).
    Assim um notebook que eu editei no Jupyter não bloqueia o turno do Claude;
  - na linha de comando: python check_nb.py caminho/do/notebook.ipynb [...]
  - no GitHub Copilot, com `--copilot` (hooks userPromptSubmitted e agentStop): mesma
    checagem, mas o bloqueio sai como JSON {"decision": "block", "reason": ...}.

Aceita .ipynb e notebooks Databricks exportados como fonte (.py que começa com
"# Databricks notebook source"). Join PySpark não tem validate=: nele vale usar
join_validado/valida_chave_unica (skill estilo-analise) ou um assert de contagem.

Sai com código 2 e lista os problemas no stderr quando algo falha. No hook Stop isso
impede o turno de terminar e devolve a lista ao modelo, uma vez por turno.
Para pular uma regra numa célula, inclua o comentário `# check_nb: ignora <regra>`
(regras: validate, collect).
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import time
import tokenize
from pathlib import Path

JANELA_SEG = 30 * 60
PROFUNDIDADE_MAX = 4
PASTAS_IGNORADAS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ipynb_checkpoints", ".claude"}
DIR_MARCAS = Path(tempfile.gettempdir()) / "claude_check_nb"

PADRAO_NOME = re.compile(r"^\d{8}_[a-z0-9]+(?:_[a-z0-9]+)*(?:_v\d+)?\.(ipynb|py)$")
JOIN = re.compile(r"\.join\(")
COLLECT = re.compile(r"\.collect\(")
CABECALHO_DATABRICKS = "# Databricks notebook source"
SEPARADOR_DATABRICKS = re.compile(r"^# COMMAND -+\s*$", re.M)
USA_SPARK = re.compile(r"\bpyspark\b|\bspark\.(table|sql|read|createDataFrame)\b")
VALIDACAO_SPARK = re.compile(r"join_validado\(|valida_chave_unica\(|assert\b[^\n]*count")


def eh_notebook_databricks(caminho: Path) -> bool:
    try:
        with caminho.open(encoding="utf-8") as f:
            return f.readline().startswith(CABECALHO_DATABRICKS)
    except (OSError, UnicodeDecodeError):
        return False


def _celulas(caminho: Path) -> list[dict]:
    """Células no formato {"tipo": "code"|"markdown", "src": str, "outputs": bool}."""
    if caminho.suffix == ".py":
        corpo = caminho.read_text(encoding="utf-8").split("\n", 1)[-1]
        celulas = []
        for bloco in SEPARADOR_DATABRICKS.split(corpo):
            src = bloco.strip("\n")
            if not src.strip():
                continue
            magica = src.lstrip().startswith("# MAGIC")
            md = magica and src.lstrip().startswith("# MAGIC %md")
            # Célula mágica (%sql, %pip...) vira "%..." para as regras de código a pularem.
            if magica and not md:
                src = "%" + src
            celulas.append({"tipo": "markdown" if md else "code", "src": src, "outputs": False})
        return celulas
    nb = json.loads(caminho.read_text(encoding="utf-8"))
    return [
        {"tipo": c.get("cell_type"), "src": "".join(c.get("source", [])), "outputs": bool(c.get("outputs"))}
        for c in nb.get("cells", [])
    ]


def _sem_comentarios(src: str) -> str:
    """Remove comentários, para `# um único .collect()` não contar como chamada."""
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type != tokenize.COMMENT]
        return tokenize.untokenize(toks)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return "\n".join(linha.split("#", 1)[0] for linha in src.splitlines())


def _ignora(src: str, regra: str) -> bool:
    return f"check_nb: ignora {regra}" in src


def _chamadas_join(src: str) -> list[str]:
    """Devolve o texto de cada .join(...) de DataFrame, com parênteses balanceados.

    Ignora str.join ("x".join) e os.path.join.
    """
    chamadas = []
    for m in JOIN.finditer(src):
        antes = src[: m.start()].rstrip()
        if antes.endswith(("'", '"', "path", "sep")):
            continue
        i, nivel = m.end(), 1
        while i < len(src) and nivel:
            nivel += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        chamadas.append(src[m.start():i])
    return chamadas


def checa(caminho: Path) -> list[str]:
    celulas = [c for c in _celulas(caminho) if c["tipo"] == "code"]
    codigo = [c["src"] for c in celulas]
    saidas = [c["outputs"] for c in celulas]
    problemas: list[str] = []

    if not PADRAO_NOME.match(caminho.name):
        problemas.append(f"nome '{caminho.name}' fora do padrão YYYYMMDD_nome_snake_case[_vN].ipynb (ou .py)")

    com_output = [i for i, tem in enumerate(saidas) if tem]
    if com_output:
        problemas.append(f"células de código com output salvo: {com_output} (entregar sem outputs)")

    usa_spark = any(USA_SPARK.search(_sem_comentarios(src)) for src in codigo)
    n_collect = 0
    joins_spark = 0
    for i, src in enumerate(codigo):
        if src.lstrip().startswith(("%", "!")):
            continue
        codigo_limpo = _sem_comentarios(src)
        if not _ignora(src, "validate"):
            # Em notebook PySpark, célula sem Polars (`pl.`) tem join Spark: sem validate=.
            celula_spark = usa_spark and "pl." not in codigo_limpo
            for chamada in _chamadas_join(codigo_limpo):
                if celula_spark:
                    joins_spark += 1
                elif "validate=" not in chamada:
                    problemas.append(f"célula {i}: join sem validate= -> {chamada[:80]}")
        if not _ignora(src, "collect"):
            n_collect += len(COLLECT.findall(codigo_limpo))

    if n_collect > 1:
        problemas.append(f"{n_collect} chamadas de .collect() (o padrão é um único .collect())")

    if joins_spark and not any(VALIDACAO_SPARK.search(_sem_comentarios(src)) for src in codigo):
        problemas.append(
            f"{joins_spark} join(s) PySpark sem cardinalidade validada "
            "(use join_validado/valida_chave_unica ou assert de contagem)"
        )

    if not any("assert" in src for src in codigo):
        problemas.append("nenhum assert de validação no notebook")

    return problemas


def _arquivo_marca(sessao: str) -> Path:
    return DIR_MARCAS / f"{re.sub(r'[^A-Za-z0-9_-]', '_', sessao or 'sem_sessao')}.txt"


def _sessao(dados: dict) -> str:
    return str(dados.get("session_id") or dados.get("sessionId") or "")


def _marca_turno() -> int:
    """Hook UserPromptSubmit: registra o início do turno desta sessão."""
    try:
        dados = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    DIR_MARCAS.mkdir(exist_ok=True)
    _arquivo_marca(_sessao(dados)).write_text(str(time.time()))
    return 0


def _inicio_turno(sessao: str) -> float:
    try:
        return float(_arquivo_marca(sessao).read_text())
    except (OSError, ValueError):
        return time.time() - JANELA_SEG


def _notebooks(raiz: Path, desde: float) -> list[Path]:
    """Notebooks até PROFUNDIDADE_MAX níveis abaixo da raiz, modificados desde `desde`.

    Poda as pastas ignoradas e ocultas antes de descer, para o hook não varrer um
    ambiente virtual ou o home inteiro a cada turno.
    """
    achados = []
    base = len(raiz.parts)
    for pasta, subpastas, arquivos in os.walk(raiz):
        nivel = len(Path(pasta).parts) - base
        subpastas[:] = [] if nivel >= PROFUNDIDADE_MAX - 1 else [
            s for s in subpastas if s not in PASTAS_IGNORADAS and not s.startswith(".")
        ]
        for nome in arquivos:
            if nome.endswith((".ipynb", ".py")):
                p = Path(pasta) / nome
                try:
                    if p.stat().st_mtime < desde:
                        continue
                except OSError:
                    continue
                if nome.endswith(".ipynb") or eh_notebook_databricks(p):
                    achados.append(p)
    return achados


def _caminhos_do_hook() -> list[Path]:
    """Hook Stop: checa os notebooks da pasta do projeto modificados neste turno."""
    try:
        dados = json.load(sys.stdin)
    except json.JSONDecodeError:
        return []
    if not isinstance(dados, dict) or dados.get("stop_hook_active") or dados.get("stopHookActive"):
        return []  # já bloqueou uma vez neste turno; não entra em loop
    raiz = Path(dados.get("cwd") or ".")
    return _notebooks(raiz, _inicio_turno(_sessao(dados)))


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--copilot"]
    copilot = len(args) < len(sys.argv[1:])
    if args == ["--marca"]:
        return _marca_turno()
    caminhos = [Path(p) for p in args] or _caminhos_do_hook()
    falhas = []
    for p in caminhos:
        if p.exists():
            falhas += [f"{p.name}: {msg}" for msg in checa(p)]
    if falhas:
        mensagem = "check_nb encontrou problemas:\n- " + "\n- ".join(falhas)
        if copilot and not args:
            # Copilot (agentStop): o bloqueio é por JSON; exit 2 seria só um aviso.
            print(json.dumps({"decision": "block", "reason": mensagem}, ensure_ascii=False))
            return 0
        print(mensagem, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
