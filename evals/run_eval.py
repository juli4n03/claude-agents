#!/usr/bin/env python3
"""Roda os casos de evals/casos.yaml no Claude Code e mede tempo, custo e qualidade.

Uso:
  python3 evals/run_eval.py --pasta ~/analises/eval_sandbox [--casos a1_ajuste_filtro,b1_contagem_safra]
  python3 evals/run_eval.py --pasta ... --julgar          # também dá nota a cada caso
  python3 evals/run_eval.py --pasta ... --repeticoes 3    # mede a variância

Cada caso roda com `claude -p` numa **cópia descartável** de --pasta, para um caso não
alterar a pasta do seguinte. Do stream de eventos saem checagens determinísticas: a rota
declarada na primeira linha, os subagentes e skills chamados e o check_nb nos notebooks
criados ou alterados. O juiz (opcional) recebe essa evidência, não só o texto final.

Os resultados vão para ~/.claude/local/evals/<data-hora>.csv. Compare duas rodadas
(antes e depois de mudar o kit) para saber se a mudança ajudou.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

RAIZ = Path(__file__).parent
LOCAL = Path(os.environ.get("CLAUDE_LOCAL_DIR", Path.home() / ".claude" / "local")).expanduser()
CHECK_NB = Path.home() / ".claude" / "hooks" / "check_nb.py"
ROTA = re.compile(r"ROTA\s+([0ABC])\b", re.I)

RUBRICA = """Você avalia o trabalho de um agente de análise de dados. Responda SOMENTE com um
objeto JSON, sem texto antes ou depois, sem rota, sem markdown.

Rota esperada: {rota}. Critério: {criterio}

Evidência coletada automaticamente:
- rota declarada: {rota_detectada}
- subagentes chamados: {subagentes}
- skills carregadas: {skills}
- ferramentas usadas: {ferramentas}
- notebooks criados/alterados: {notebooks}
- check_nb: {check_nb}

Resposta final do agente:
---
{resposta}
---
Formato: {{"nota": <0.0 a 1.0>, "passou": <true|false>, "motivo": "<uma frase>"}}"""


def claude(prompt: str, pasta: Path, timeout: int = 1800, stream: bool = True) -> dict:
    """Roda `claude -p` e devolve o evento final mais a lista de chamadas de ferramenta."""
    fmt = ["--output-format", "stream-json", "--verbose"] if stream else ["--output-format", "json"]
    try:
        proc = subprocess.run(["claude", "-p", prompt, *fmt], cwd=pasta, capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return {"result": "timeout", "is_error": True, "ferramentas": [], "primeiro_texto": ""}
    if not stream:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"result": proc.stdout or proc.stderr, "is_error": True}

    final: dict = {"result": proc.stderr, "is_error": True}
    ferramentas: list[dict] = []
    primeiro_texto = ""
    for linha in proc.stdout.splitlines():
        try:
            ev = json.loads(linha)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for bloco in ev.get("message", {}).get("content", []):
                if bloco.get("type") == "tool_use":
                    ferramentas.append({"nome": bloco.get("name"), "input": bloco.get("input", {})})
                elif bloco.get("type") == "text" and not primeiro_texto and bloco.get("text", "").strip():
                    primeiro_texto = bloco["text"]
        elif ev.get("type") == "result":
            final = ev
    final["ferramentas"] = ferramentas
    final["primeiro_texto"] = primeiro_texto
    return final


def _notebooks_alterados(antes: dict[Path, float], pasta: Path) -> list[Path]:
    return [p for p in pasta.rglob("*.ipynb")
            if ".ipynb_checkpoints" not in p.parts and antes.get(p) != p.stat().st_mtime]


def _check_nb(notebooks: list[Path]) -> tuple[bool | None, str]:
    if not notebooks:
        return None, "sem notebooks"
    proc = subprocess.run([sys.executable, str(CHECK_NB), *map(str, notebooks)],
                          capture_output=True, text=True, encoding="utf-8")
    return proc.returncode == 0, (proc.stderr.strip() or "passou")[:1000]


def _json_do_juiz(texto: str) -> dict:
    m = re.search(r"\{.*\}", texto or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def roda_caso(caso: dict, variaveis: dict, sandbox: Path, julgar: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"eval_{caso['id']}_") as tmp:
        pasta = Path(tmp) / "trabalho"
        shutil.copytree(sandbox, pasta)
        antes = {p: p.stat().st_mtime for p in pasta.rglob("*.ipynb")}

        r = claude(caso["prompt"].format(**variaveis), pasta)

        usos = r.get("ferramentas", [])
        subagentes = sorted({u["input"].get("subagent_type", "?") for u in usos if u["nome"] in ("Agent", "Task")})
        skills = sorted({u["input"].get("skill", "?") for u in usos if u["nome"] == "Skill"})
        rota = ROTA.search(r.get("primeiro_texto", "").splitlines()[0] if r.get("primeiro_texto") else "")
        rota_detectada = rota.group(1).upper() if rota else ""
        notebooks = _notebooks_alterados(antes, pasta)
        nb_ok, nb_saida = _check_nb(notebooks)

        esperados = caso.get("subagentes")          # lista; [] = nenhum subagente
        sub_ok = None if esperados is None else set(esperados) == set(subagentes)
        skills_ok = None if not caso.get("skills") else set(caso["skills"]) <= set(skills)

        linha = {
            "id": caso["id"], "rota": caso["rota"], "rota_detectada": rota_detectada,
            "rota_ok": rota_detectada == str(caso["rota"]),
            "subagentes": ",".join(subagentes), "subagentes_ok": sub_ok,
            "skills": ",".join(skills), "skills_ok": skills_ok,
            "notebooks": ",".join(p.name for p in notebooks), "check_nb_ok": nb_ok,
            "segundos": round(r.get("duration_ms", 0) / 1000, 1), "turnos": r.get("num_turns"),
            "custo_usd": r.get("total_cost_usd"), "erro": r.get("is_error", False),
            "nota": None, "passou": None, "motivo": None,
        }
        if julgar:
            j = claude(RUBRICA.format(
                rota=caso["rota"], criterio=caso["criterio"], rota_detectada=rota_detectada or "nenhuma",
                subagentes=linha["subagentes"] or "nenhum", skills=linha["skills"] or "nenhuma",
                ferramentas=", ".join(sorted({u["nome"] for u in usos})) or "nenhuma",
                notebooks=linha["notebooks"] or "nenhum", check_nb=nb_saida,
                resposta=str(r.get("result", ""))[:20000],
            ), Path(tmp), stream=False)
            nota = _json_do_juiz(str(j.get("result", "")))
            linha.update(nota=nota.get("nota"), passou=nota.get("passou"), motivo=nota.get("motivo"))
        return linha


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pasta", required=True, type=Path, help="pasta sandbox (copiada a cada caso)")
    ap.add_argument("--casos", default="")
    ap.add_argument("--julgar", action="store_true")
    ap.add_argument("--repeticoes", type=int, default=1)
    args = ap.parse_args()

    variaveis = yaml.safe_load((LOCAL / "eval_vars.yaml").read_text(encoding="utf-8"))
    casos = yaml.safe_load((RAIZ / "casos.yaml").read_text(encoding="utf-8"))
    if args.casos:
        casos = [c for c in casos if c["id"] in args.casos.split(",")]
    sandbox = args.pasta.expanduser()

    saida = LOCAL / "evals" / f"{datetime.now():%Y%m%d_%H%M}.csv"
    saida.parent.mkdir(parents=True, exist_ok=True)
    with saida.open("w", newline="", encoding="utf-8") as f:
        w = None
        for rep in range(1, args.repeticoes + 1):
            for caso in casos:
                linha = {"repeticao": rep, **roda_caso(caso, variaveis, sandbox, args.julgar)}
                if w is None:
                    w = csv.DictWriter(f, fieldnames=list(linha))
                    w.writeheader()
                w.writerow(linha)
                f.flush()
                print(*linha.values(), sep=" | ", flush=True)
    print(f"\nResultados em {saida}")


if __name__ == "__main__":
    main()
