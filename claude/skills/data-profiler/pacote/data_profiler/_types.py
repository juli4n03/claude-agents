"""Tipos compartilhados entre backends e lógica comum."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TipoLogico = Literal["numerica", "string", "booleana", "data", "nao_suportado"]
"""Família de tipo física da coluna, informada pelo backend."""

TipoDetectado = Literal["numerica", "categorica", "data", "booleana", "identificador"]

# Códigos de faixa emitidos pelos backends (iguais em Polars e Spark).
COD_NULO = "NULO"
COD_VAZIO = "VAZIO"
COD_OUTROS = "OUTROS"
PREFIXO_ESPECIAL = "ESP:"
PREFIXO_BIN = "B"
PREFIXO_CAT = "CAT:"


@dataclass
class BinSpec:
    """Especificação de discretização de uma variável.

    Para numéricas, ``cortes`` são os pontos internos ``c_1 < ... < c_{k-1}``;
    a faixa ``j`` cobre ``[c_j, c_{j+1})``, com ``c_0 = -inf`` e ``c_k = +inf``.
    O código emitido é ``B{j:02d}``; nulos/NaN viram ``NULO`` e valores especiais
    viram ``ESP:<valor>``.

    Para categóricas, ``categorias`` é a lista mantida (código ``CAT:<valor>``);
    o resto vira ``OUTROS``; nulos ``NULO``; vazios/espaços ``VAZIO``.
    ``grupos`` (opcional) mapeia categoria → rótulo de grupo, para agrupamento ótimo.
    """

    variavel: str
    tipo: Literal["numerica", "categorica"]
    cortes: list[float] = field(default_factory=list)
    valores_especiais: list[float] = field(default_factory=list)
    categorias: list[str] = field(default_factory=list)
    grupos: dict[str, str] | None = None
    metodo: str = "quantil"

    def to_dict(self) -> dict:
        return {
            "variavel": self.variavel,
            "tipo": self.tipo,
            "cortes": list(self.cortes),
            "valores_especiais": list(self.valores_especiais),
            "categorias": list(self.categorias),
            "grupos": self.grupos,
            "metodo": self.metodo,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BinSpec":
        return cls(**d)

    @property
    def n_faixas_regulares(self) -> int:
        return len(self.cortes) + 1 if self.tipo == "numerica" else len(self.categorias)
