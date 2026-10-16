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


def _casa_exato(nome: str, alvo: str, ancestrais: tuple[str, ...]) -> bool:
    """Nome completo igual, ou nome sem schema dentro de um nó cujo ancestral é o schema."""
    nome, alvo = nome.lower(), alvo.lower()
    if nome == alvo:
        return True
    if "." in alvo and "." not in nome:
        schema, tabela = alvo.rsplit(".", 1)
        return nome == tabela and schema in (a.lower() for a in ancestrais)
    return False


def _procura(no, alvo: str, exato: bool, ancestrais: tuple[str, ...] = ()):
    """Procura recursivamente uma chave ou um campo de nome igual ao alvo."""
    casa = (lambda n: _casa_exato(n, alvo, ancestrais)) if exato else (lambda n: _casa(n, alvo))
    if isinstance(no, dict):
        for k, v in no.items():
            if isinstance(k, str) and casa(k) and isinstance(v, (dict, list)):
                return {k: v}
        for campo in CAMPOS_NOME:
            if isinstance(no.get(campo), str) and casa(no[campo]):
                return no
        filhos = [(v, ancestrais + (k,) if isinstance(k, str) else ancestrais) for k, v in no.items()]
    elif isinstance(no, list):
        filhos = [(v, ancestrais) for v in no]
    else:
        return None
    for filho, anc in filhos:
        achado = _procura(filho, alvo, exato, anc)
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

    lidos: dict[Path, object] = {}

    def carrega(arq: Path):
        if arq not in lidos:
            try:
                lidos[arq] = yaml.safe_load(arq.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                lidos[arq] = None
        return lidos[arq]

    def busca(alvo: str, exato: bool):
        for arq in arquivos:
            achado = _procura(carrega(arq), alvo, exato)
            if achado is not None:
                return arq, achado
        return None

    codigo = 0
    for alvo in sys.argv[1:]:
        # Primeiro o nome completo em todos os dicionários; só depois só o nome da tabela,
        # para `schema_a.clientes` não devolver `schema_b.clientes` de um arquivo mais novo.
        resultado = busca(alvo, exato=True)
        if resultado is None and (resultado := busca(alvo, exato=False)) is not None and "." in alvo:
            print(f"# aviso: {alvo} casou só pelo nome da tabela; confira se o schema é o mesmo.",
                  file=sys.stderr)
        if resultado is not None:
            arq, achado = resultado
            data = datetime.fromtimestamp(arq.stat().st_mtime).strftime("%Y-%m-%d")
            print(f"# {alvo} — fonte: {arq} (modificado em {data})")
            print(yaml.safe_dump(achado, allow_unicode=True, sort_keys=False))
        else:
            print(f"# {alvo}: não encontrado em {len(arquivos)} dicionário(s). Use o fallback do warehouse.",
                  file=sys.stderr)
            codigo = 1
    return codigo


if __name__ == "__main__":
    sys.exit(main())
