#!/usr/bin/env python3
"""Busca o schema de uma tabela nos dicionários YAML da empresa.

Uso: python ~/.claude/bin/schema.py <tabela> [<tabela> ...]

Lê `dicionarios_dir` (e opcionalmente `padrao`) de ~/.claude/local/config.yaml.
Percorre os YAML do mais recente para o mais antigo e imprime a primeira entrada que
casar com o nome, dizendo de qual arquivo veio. Aceita `schema.tabela` ou só `tabela`.
Sai com código 1 se não achar, para quem chamou cair no fallback do warehouse.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("schema.py precisa de pyyaml: pip install pyyaml")

CONFIG = Path(os.environ.get("CLAUDE_LOCAL_DIR", Path.home() / ".claude" / "local")) / "config.yaml"
CAMPOS_NOME = ("name", "nome", "table", "tabela", "table_name")


def _config() -> tuple[Path, str]:
    if not CONFIG.exists():
        sys.exit(f"Sem {CONFIG}. Rode /setup-empresa ou crie o arquivo com 'dicionarios_dir'.")
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    if not cfg.get("dicionarios_dir"):
        sys.exit(f"{CONFIG} não define 'dicionarios_dir'.")
    return Path(os.path.expanduser(cfg["dicionarios_dir"])), cfg.get("padrao", "**/*.y*ml")


def _casa(chave: str, alvo: str) -> bool:
    chave, alvo = chave.lower(), alvo.lower()
    return chave == alvo or chave.split(".")[-1] == alvo.split(".")[-1]


def _procura(no, alvo: str):
    """Procura recursivamente uma chave ou um campo de nome igual ao alvo."""
    if isinstance(no, dict):
        for k, v in no.items():
            if isinstance(k, str) and _casa(k, alvo) and isinstance(v, (dict, list)):
                return {k: v}
        for campo in CAMPOS_NOME:
            if isinstance(no.get(campo), str) and _casa(no[campo], alvo):
                return no
        filhos = no.values()
    elif isinstance(no, list):
        filhos = no
    else:
        return None
    for filho in filhos:
        achado = _procura(filho, alvo)
        if achado is not None:
            return achado
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    pasta, padrao = _config()
    arquivos = sorted(pasta.glob(padrao), key=lambda p: p.stat().st_mtime, reverse=True)
    if not arquivos:
        sys.exit(f"Nenhum dicionário em {pasta} com o padrão {padrao}.")

    codigo = 0
    for alvo in sys.argv[1:]:
        for arq in arquivos:
            try:
                dados = yaml.safe_load(arq.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            achado = _procura(dados, alvo)
            if achado is not None:
                data = datetime.fromtimestamp(arq.stat().st_mtime).strftime("%Y-%m-%d")
                print(f"# {alvo} — fonte: {arq} (modificado em {data})")
                print(yaml.safe_dump(achado, allow_unicode=True, sort_keys=False))
                break
        else:
            print(f"# {alvo}: não encontrado em {len(arquivos)} dicionário(s). Use o fallback do warehouse.",
                  file=sys.stderr)
            codigo = 1
    return codigo


if __name__ == "__main__":
    sys.exit(main())
