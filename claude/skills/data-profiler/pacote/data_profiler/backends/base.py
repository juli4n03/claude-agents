"""Contrato dos backends (padrão *Strategy*).

Princípio de projeto: o backend só executa as **agregações pesadas** sobre a
base e devolve DataFrames **Polars pequenos** (uma linha por variável, por
faixa, por safra...). Toda a lógica de negócio — flags, WoE/IV, KS a partir de
faixas, classificação, recomendação — é aplicada depois, em código comum,
sobre esses resultados. Assim o schema e as regras de saída são idênticos
independentemente do backend; as únicas divergências possíveis são numéricas
(percentis/distintos aproximados no Spark) e estão documentadas em
``docs/METRICAS.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Sequence

import polars as pl

from .._types import BinSpec, TipoLogico


class BaseBackend(ABC):
    """Interface comum a ``PolarsBackend`` e ``SparkBackend``."""

    nome: str = "base"
    exato: bool = True
    """``True`` se percentis, distintos e KS/AUC brutos são exatos."""

    # ------------------------------------------------------------------ geral
    @abstractmethod
    def n_linhas(self) -> int: ...

    @abstractmethod
    def schema(self) -> dict[str, TipoLogico]:
        """Mapeia cada coluna para sua família de tipo lógico."""

    @abstractmethod
    def persistir(self) -> None: ...

    @abstractmethod
    def liberar(self) -> None: ...

    @abstractmethod
    def filtrar_nao_nulo(self, coluna: str) -> "BaseBackend":
        """Novo backend sobre as linhas em que ``coluna`` não é nula."""

    @abstractmethod
    def preparar_target(self, target: str, mapa: dict | None, binario: bool) -> "BaseBackend":
        """Novo backend com o target recodificado (``mapa``) e convertido para Float64.

        Para binário, valores fora de {0, 1} viram null (contabilizados como indeterminados).
        """

    @abstractmethod
    def filtrar_safra(self, coluna_safra: str, safra: str) -> "BaseBackend":
        """Novo backend apenas com as linhas da safra ``yyyy-MM`` informada."""

    @abstractmethod
    def amostra_pandas(
        self,
        colunas: Sequence[str],
        n: int,
        seed: int,
        estratificar_por: str | None = None,
    ) -> Any:
        """Devolve um ``pandas.DataFrame`` com até ``n`` linhas das colunas pedidas."""

    @abstractmethod
    def converter_colunas(self, conversoes: dict[str, str], formato_data: str | None) -> "BaseBackend":
        """Novo backend com colunas convertidas (``"numerica"`` ou ``"data"``)."""

    # -------------------------------------------------------------- profiling
    @abstractmethod
    def estatisticas_numericas(self, colunas: Sequence[str], percentis: Sequence[float]) -> pl.DataFrame:
        """Uma linha por coluna: contagem, nulos, nan_count, inf_count, media, desvio_padrao,
        variancia, minimo, maximo, distintos, zeros, negativos, nao_inteiros, assimetria, curtose e um
        campo ``p_<pct>`` para cada percentil (ex.: ``p_25``). NaN/±inf são excluídos."""

    @abstractmethod
    def contar_outliers(self, limites: dict[str, tuple[float, float]]) -> pl.DataFrame:
        """``variavel, qtd_outliers_inf, qtd_outliers_sup`` dado ``{col: (lim_inf, lim_sup)}``."""

    @abstractmethod
    def estatisticas_categoricas(self, colunas: Sequence[str], valores_suspeitos: Sequence[str]) -> pl.DataFrame:
        """``variavel, contagem, nulos, distintos, vazios, tamanho_min, tamanho_max, tamanho_medio,
        qtd_suspeitos``. Colunas não textuais são convertidas para string."""

    @abstractmethod
    def frequencias(self, coluna: str, limite: int) -> pl.DataFrame:
        """``valor (str), qtd`` das ``limite`` categorias mais frequentes, nulos excluídos,
        ordenado por ``qtd`` desc e ``valor`` asc (desempate determinístico)."""

    @abstractmethod
    def estatisticas_datas(self, colunas: Sequence[str], data_minima: str) -> pl.DataFrame:
        """``variavel, contagem, nulos, data_min, data_max, datas_futuras, datas_invalidas``."""

    @abstractmethod
    def contagem_por_safra(self, coluna: str) -> pl.DataFrame:
        """``safra (yyyy-MM), contagem`` para uma coluna de data."""

    @abstractmethod
    def conversibilidade_strings(
        self, colunas: Sequence[str], formato_data: str | None, n_amostra: int
    ) -> pl.DataFrame:
        """``variavel, pct_numerico, pct_data``: % de valores não nulos conversíveis."""

    # ------------------------------------------------------------ estabilidade
    @abstractmethod
    def preenchimento_por_safra(
        self, coluna_safra: str, numericas: Sequence[str], categoricas: Sequence[str]
    ) -> pl.DataFrame:
        """Tabela longa ``safra, variavel, contagem, pct_nulos, media`` (media só para numéricas)."""

    @abstractmethod
    def moda_por_safra(self, coluna_safra: str, coluna: str) -> pl.DataFrame:
        """``safra, moda, pct_moda`` para uma categórica."""

    @abstractmethod
    def media_target_por_safra(self, coluna_safra: str, target: str, peso: str | None) -> pl.DataFrame:
        """``safra, contagem, media_target`` (ponderado se ``peso``)."""

    # --------------------------------------------------------- poder preditivo
    @abstractmethod
    def quantis(self, colunas: Sequence[str], probabilidades: Sequence[float]) -> dict[str, list[float]]:
        """Quantis de várias colunas numa única passada."""

    @abstractmethod
    def top_categorias(self, coluna: str, k: int) -> list[str]: ...

    @abstractmethod
    def tabela_faixas_longa(
        self,
        specs: Sequence[BinSpec],
        target: str | None,
        tipo_target: str | None,
        coluna_safra: str | None,
        coluna_peso: str | None,
    ) -> pl.DataFrame:
        """Tabela longa ``variavel, faixa (código), safra, qtd, maus | soma, soma_quad, mediana``.

        ``qtd``/``maus`` são ponderados por ``coluna_peso`` quando informada. Para
        ``target=None`` só ``qtd`` é calculada (usado no PSI). Implementada com um
        único ``group_by`` sobre a base em formato longo.
        """

    @abstractmethod
    def ks_auc_bruto(self, colunas: Sequence[str], target: str, coluna_peso: str | None) -> pl.DataFrame:
        """``variavel, ks_bruto, auc_bruto, ponto_bisserial, n_obs`` sobre o valor bruto.

        Polars: exato (ordenação). Spark: aproximado via ``n_faixas_finas`` quantis.
        ``auc_bruto`` é a AUC de ``valor ↑ → target=1`` (pode ser < 0.5).
        """

    @abstractmethod
    def correlacoes_com_target(self, colunas: Sequence[str], target: str) -> pl.DataFrame:
        """``variavel, pearson, spearman, n_obs`` entre cada numérica e um target contínuo."""

    @abstractmethod
    def aplicar_faixas(self, specs: Sequence[BinSpec], mapa_woe: dict[str, dict[str, float]], sufixo: str) -> Any:
        """Devolve o DataFrame nativo com colunas ``<var><sufixo>`` contendo o WoE."""

    # ------------------------------------------------------------- correlação
    @abstractmethod
    def correlacao_numerica(
        self, colunas: Sequence[str], metodo: str, pairwise: bool, amostra: float | int | None, seed: int
    ) -> tuple[pl.DataFrame, bool]:
        """Tabela longa ``var_1, var_2, valor, n_obs`` para pares (i<j).

        Retorna também ``amostral`` (``True`` se usou amostra).
        """

    @abstractmethod
    def contingencia(self, spec_a: BinSpec, spec_b: BinSpec) -> pl.DataFrame:
        """``faixa_a, faixa_b, qtd`` entre duas variáveis discretizadas (nulos excluídos)."""

    @abstractmethod
    def estatisticas_por_grupo(self, numerica: str, categorica: str, k: int) -> pl.DataFrame:
        """``grupo, n, media, variancia`` da numérica por categoria (top-k + OUTROS)."""
