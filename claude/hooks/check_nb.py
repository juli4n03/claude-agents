#!/usr/bin/env python3
"""Checklist mecânico de notebooks.

Uso:
  - como hook UserPromptSubmit com `--marca`: grava a hora de início do turno;
  - como hook Stop: quando o Claude vai encerrar o turno, checa os .ipynb da pasta do
    projeto modificados desde o início do turno (sem marca, nos últimos 30 minutos).
    Assim um notebook que eu editei no Jupyter não bloqueia o turno do Claude;
  - na linha de comando: python check_nb.py caminho/do/notebook.ipynb [...]

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

PADRAO_NOME = re.compile(r"^\d{8}_[a-z0-9]+(?:_[a-z0-9]+)*(?:_v\d+)?\.ipynb$")
JOIN = re.compile(r"\.join\(")
COLLECT = re.compile(r"\.collect\(")


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
    nb = json.loads(caminho.read_text(encoding="utf-8"))
    codigo = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
    problemas: list[str] = []

    if not PADRAO_NOME.match(caminho.name):
        problemas.append(f"nome '{caminho.name}' fora do padrão YYYYMMDD_nome_snake_case[_vN].ipynb")

    com_output = [i for i, c in enumerate(codigo) if c.get("outputs")]
    if com_output:
        problemas.append(f"células de código com output salvo: {com_output} (entregar sem outputs)")

    n_collect = 0
    for i, c in enumerate(codigo):
        src = "".join(c.get("source", []))
        if src.lstrip().startswith(("%", "!")):
            continue
        codigo_limpo = _sem_comentarios(src)
        if not _ignora(src, "validate"):
            for chamada in _chamadas_join(codigo_limpo):
                if "validate=" not in chamada:
                    problemas.append(f"célula {i}: join sem validate= -> {chamada[:80]}")
        if not _ignora(src, "collect"):
            n_collect += len(COLLECT.findall(codigo_limpo))

    if n_collect > 1:
        problemas.append(f"{n_collect} chamadas de .collect() (o padrão é um único .collect())")

    if not any("assert" in "".join(c.get("source", [])) for c in codigo):
        problemas.append("nenhum assert de validação no notebook")

    return problemas


def _arquivo_marca(sessao: str) -> Path:
    return DIR_MARCAS / f"{re.sub(r'[^A-Za-z0-9_-]', '_', sessao or 'sem_sessao')}.txt"


def _marca_turno() -> int:
    """Hook UserPromptSubmit: registra o início do turno desta sessão."""
    try:
        dados = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    DIR_MARCAS.mkdir(exist_ok=True)
    _arquivo_marca(dados.get("session_id", "")).write_text(str(time.time()))
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
            if nome.endswith(".ipynb"):
                p = Path(pasta) / nome
                try:
                    if p.stat().st_mtime >= desde:
                        achados.append(p)
                except OSError:
                    continue
    return achados


def _caminhos_do_hook() -> list[Path]:
    """Hook Stop: checa os notebooks da pasta do projeto modificados neste turno."""
    try:
        dados = json.load(sys.stdin)
    except json.JSONDecodeError:
        return []
    if dados.get("stop_hook_active"):
        return []  # já bloqueou uma vez neste turno; não entra em loop
    raiz = Path(dados.get("cwd") or ".")
    return _notebooks(raiz, _inicio_turno(dados.get("session_id", "")))


def main() -> int:
    if sys.argv[1:] == ["--marca"]:
        return _marca_turno()
    caminhos = [Path(p) for p in sys.argv[1:]] or _caminhos_do_hook()
    falhas = []
    for p in caminhos:
        if p.exists():
            falhas += [f"{p.name}: {msg}" for msg in checa(p)]
    if falhas:
        print("check_nb encontrou problemas:\n- " + "\n- ".join(falhas), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
