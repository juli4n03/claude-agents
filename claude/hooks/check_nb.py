#!/usr/bin/env python3
"""Checklist mecânico de notebooks.

Uso:
  - como hook Stop: quando o Claude vai encerrar o turno, checa os .ipynb da pasta do
    projeto modificados nos últimos 30 minutos;
  - na linha de comando: python check_nb.py caminho/do/notebook.ipynb [...]

Sai com código 2 e lista os problemas no stderr quando algo falha. No hook Stop isso
impede o turno de terminar e devolve a lista ao modelo, uma vez por turno.
Para pular uma regra numa célula, inclua o comentário `# check_nb: ignora <regra>`
(regras: validate, collect).
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import tokenize
from pathlib import Path

JANELA_SEG = 30 * 60

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


def _caminhos_do_hook() -> list[Path]:
    """Hook Stop: checa os notebooks da pasta do projeto modificados nos últimos 30 min."""
    try:
        dados = json.load(sys.stdin)
    except json.JSONDecodeError:
        return []
    if dados.get("stop_hook_active"):
        return []  # já bloqueou uma vez neste turno; não entra em loop
    raiz = Path(dados.get("cwd") or ".")
    limite = time.time() - JANELA_SEG
    return [
        p for p in raiz.glob("**/*.ipynb")
        if ".ipynb_checkpoints" not in p.parts and len(p.relative_to(raiz).parts) <= 4
        and p.stat().st_mtime >= limite
    ]


def main() -> int:
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
