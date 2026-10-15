#!/usr/bin/env python3
"""Roda os casos de evals/casos.yaml no Claude Code e mede tempo, custo e qualidade.

Uso:
  python evals/run_eval.py --pasta ~/analises/eval_sandbox [--casos a1_ajuste_filtro,b1_contagem_safra]
  python evals/run_eval.py --pasta ... --julgar        # também dá nota a cada caso

Cada caso roda com `claude -p` dentro de --pasta (uma pasta de trabalho descartável).
Os resultados vão para ~/.claude/local/evals/<data-hora>.csv. Compare duas rodadas
(antes e depois de mudar o kit) para saber se a mudança ajudou.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

RAIZ = Path(__file__).parent
LOCAL = Path(os.environ.get("CLAUDE_LOCAL_DIR", Path.home() / ".claude" / "local")).expanduser()

RUBRICA = """Você avalia a resposta de um agente de análise de dados.
Rota esperada: {rota}. Critério: {criterio}

Resposta do agente:
---
{resposta}
---
Responda só com JSON: {{"nota": <0.0 a 1.0>, "passou": <true|false>, "motivo": "<uma frase>"}}"""


def claude(prompt: str, pasta: Path, timeout: int = 1800) -> dict:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json"],
        cwd=pasta, capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"result": proc.stdout or proc.stderr, "is_error": True}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pasta", required=True, type=Path)
    ap.add_argument("--casos", default="")
    ap.add_argument("--julgar", action="store_true")
    args = ap.parse_args()

    variaveis = yaml.safe_load((LOCAL / "eval_vars.yaml").read_text(encoding="utf-8"))
    casos = yaml.safe_load((RAIZ / "casos.yaml").read_text(encoding="utf-8"))
    if args.casos:
        casos = [c for c in casos if c["id"] in args.casos.split(",")]

    saida = LOCAL / "evals" / f"{datetime.now():%Y%m%d_%H%M}.csv"
    saida.parent.mkdir(parents=True, exist_ok=True)
    pasta = args.pasta.expanduser()

    with saida.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "rota", "segundos", "turnos", "custo_usd", "erro", "nota", "passou", "motivo"])
        for caso in casos:
            r = claude(caso["prompt"].format(**variaveis), pasta)
            linha = [caso["id"], caso["rota"], round(r.get("duration_ms", 0) / 1000, 1),
                     r.get("num_turns"), r.get("total_cost_usd"), r.get("is_error", False)]
            if args.julgar:
                j = claude(RUBRICA.format(rota=caso["rota"], criterio=caso["criterio"],
                                          resposta=str(r.get("result", ""))[:20000]), pasta)
                try:
                    nota = json.loads(str(j.get("result", "")).strip().strip("`").removeprefix("json"))
                except json.JSONDecodeError:
                    nota = {}
                linha += [nota.get("nota"), nota.get("passou"), nota.get("motivo")]
            w.writerow(linha)
            print(*linha, sep=" | ", flush=True)
    print(f"\nResultados em {saida}")


if __name__ == "__main__":
    main()
