"""Configurações do módulo ``data_profiler``.

Todos os limites e regras de negócio ficam concentrados nestas dataclasses,
com valores padrão documentados. Nenhum número mágico deve existir fora daqui.

Convenções globais:
    * Percentuais são expressos na escala **0–100**, arredondados a 2 casas.
    * ``woe = ln(pct_bons / pct_maus)``: faixas com WoE **positivo** concentram
      bons; WoE negativo concentra maus. IV é sempre não negativo.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

Backend = Literal["auto", "polars", "spark"]
TipoTarget = Literal["auto", "binario", "continuo"]
MetodoBinning = Literal["quantil", "otimo", "arvore"]
Monotonicidade = Literal["auto", "ascending", "descending", "none"]


@dataclass
class ProfileConfig:
    """Parâmetros do profiling (seções 2 a 6 da especificação)."""

    # --- seleção de backend ------------------------------------------------
    max_linhas_polars: int = 50_000_000
    """Acima disso, entradas pandas/Polars geram warning sugerindo Spark."""

    # --- classificação de tipos -------------------------------------------
    max_distintos_num_como_cat: int = 10
    """Numérica com até este nº de distintos também é tratada como categórica."""
    pct_identificador: float = 95.0
    """Distintos / não nulos (%) acima disso → ``identificador``."""
    min_linhas_identificador: int = 100
    """Tabelas menores que isso nunca marcam identificadores (evita falso positivo)."""
    pct_conversivel: float = 100.0
    """% de strings conversíveis para número/data para sugerir ``tipo_sugerido``."""
    formato_data: str | None = None
    """Formato (strftime) para strings de data. ``None`` → inferência."""

    # --- numéricas ----------------------------------------------------------
    percentis: tuple[float, ...] = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
    fator_iqr: float = 1.5
    limite_coef_variacao: float = 0.10
    epsilon_media: float = 1e-9
    """Se ``|media| < epsilon``, o coeficiente de variação é indefinido; usa critério alternativo."""
    pct_quase_constante: float = 95.0
    """``pct_moda`` acima disso → ``quase_constante``."""

    # --- categóricas --------------------------------------------------------
    top_n: int = 10
    pct_categoria_rara: float = 1.0
    max_distintos_alta_cardinalidade: int = 50
    max_categorias_distribuicao: int = 1_000
    """Nº máximo de categorias coletadas para entropia/raras (acima disso a entropia é aproximada)."""
    valores_suspeitos: tuple[str, ...] = (
        "NULL", "null", "Null", "N/A", "n/a", "NA", "na", "NaN", "nan", "-", "0", "None", "none", "#N/A",
    )

    # --- datas --------------------------------------------------------------
    data_minima_valida: str = "1900-01-01"

    # --- regras de eleição --------------------------------------------------
    pct_nulos_max_eleita: float = 5.0
    """``flag_pct_nulo = 1`` e não eleita quando ``pct_nulos`` ultrapassa este valor."""

    # --- estabilidade (seção 6) --------------------------------------------
    n_decis_psi: int = 10
    psi_atencao: float = 0.10
    psi_instavel: float = 0.25
    estabilidade_moda_categoricas: bool = True
    """Calcular moda por safra para categóricas (um ``group_by`` por coluna)."""

    # --- performance / Spark ---------------------------------------------
    spark_accuracy_percentil: int = 10_000
    """``accuracy`` do ``percentile_approx`` (maior = mais preciso e mais lento)."""
    spark_distintos_exatos: bool = False
    """``False`` usa ``approx_count_distinct`` (rsd 1%); ``True`` usa ``countDistinct``."""
    spark_rsd_distintos: float = 0.01
    calcular_top_n: bool = True
    """Permite pular o ``group_by`` por coluna categórica em bases muito largas."""
    persistir: bool = True
    """``cache()`` do DataFrame Spark durante as múltiplas passadas."""
    spark_lote_variaveis: int = 12
    """Nº de variáveis por ``groupBy`` no formato longo (evita código Java grande demais no codegen)."""
    amostra_conversibilidade: int = 100_000
    """Linhas usadas para testar se strings são conversíveis em número/data."""

    def to_dict(self) -> dict:
        """Serializa a configuração (para ``metadados``)."""
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


@dataclass
class PoderPreditivoConfig:
    """Parâmetros do módulo de poder preditivo (seção 7)."""

    # --- target -------------------------------------------------------------
    mapa_target: dict | None = None
    """Ex.: ``{"mau": 1, "bom": 0}``. Aplicado antes da validação {0, 1}."""
    min_maus: int = 30
    """Mínimo de maus **e** de bons; abaixo disso a execução aborta."""

    # --- binning ------------------------------------------------------------
    metodo_binning: MetodoBinning = "quantil"
    n_faixas: int = 10
    min_pct_faixa: float = 5.0
    """Cada faixa precisa de pelo menos esta % da base (faixas menores são fundidas)."""
    monotonicidade: Monotonicidade = "auto"
    """Restrição de monotonicidade para ``optbinning`` (``metodo_binning="otimo"``)."""
    min_samples_leaf_pct: float = 5.0
    """Para ``metodo_binning="arvore"``: % mínima de observações por folha."""
    valores_especiais: tuple[float, ...] = ()
    """Valores numéricos que recebem faixa própria (ex.: ``(-1, 9999)``)."""
    max_categorias: int = 50
    """Categóricas: mantém as top-K categorias; o resto vai para ``OUTROS``."""
    agrupamento_otimo_categorias: bool = False
    """Agrupar categorias com bad rate semelhante via ``optbinning``."""
    suavizacao_woe: float = 0.5
    """Somado a bons e maus quando alguma faixa tem zero em um dos lados."""
    n_amostra_binning: int = 200_000
    """Tamanho da amostra (Spark) usada para ajustar binning ótimo/árvore."""
    seed: int = 42

    # --- métricas -----------------------------------------------------------
    n_faixas_finas: int = 200
    """Quantis usados no Spark para aproximar KS/AUC sobre o valor bruto."""
    classes_iv: tuple[tuple[float, str], ...] = (
        (0.02, "sem poder"),
        (0.10, "fraco"),
        (0.30, "medio"),
        (0.50, "forte"),
        (float("inf"), "suspeito"),
    )
    """Limites superiores (exclusivos) e rótulo de cada classe de IV."""
    max_inversoes_monotonica: int = 1
    """Numérica com mais inversões que isso → ``nao_monotonica``."""

    # --- vazamento ----------------------------------------------------------
    iv_suspeita_vazamento: float = 0.5
    auc_suspeita_vazamento: float = 0.85
    padroes_vazamento: tuple[str, ...] = (
        "atraso", "default", "inadimpl", "bad", "target", "dt_pagamento", "pagamento", "ever", "over",
    )

    # --- estabilidade -------------------------------------------------------
    cv_iv_max: float = 0.30
    spearman_estabilidade_ordem: float = 0.8
    pct_min_estabilidade_ordem: float = 70.0
    psi_instavel: float = 0.25

    # --- ranking e seleção --------------------------------------------------
    iv_min_selecionar: float = 0.02
    spearman_redundancia: float = 0.7
    cramer_v_redundancia: float = 0.5
    importancia_multivariada: bool = False
    n_amostra_lightgbm: int = 300_000

    def to_dict(self) -> dict:
        return asdict(self)

    def classificar_iv(self, iv: float | None) -> str | None:
        """Retorna o rótulo da classe de IV conforme ``classes_iv``."""
        if iv is None:
            return None
        for limite, rotulo in self.classes_iv:
            if iv < limite:
                return rotulo
        return self.classes_iv[-1][1]


@dataclass
class CorrelacaoConfig:
    """Parâmetros da função ``correlate`` (seção 8)."""

    tratamento_nulos: Literal["pairwise", "listwise"] = "pairwise"
    amostra: float | int | None = None
    """Fração (0–1) ou nº de linhas para amostrar antes de métricas caras (Spearman no Spark)."""
    seed: int = 42
    max_categorias: int = 30
    """Categóricas de alta cardinalidade: mantém top-K e agrupa o resto em ``OUTROS``."""
    pct_celulas_esperado_baixo: float = 20.0
    """Alerta do qui-quadrado quando mais de X% das células têm esperado < ``min_esperado``."""
    min_esperado: float = 5.0
    limite_associacao_alta_num: float = 0.7
    """|Pearson| ou |Spearman| acima disso → par de associação alta."""
    limite_associacao_alta_cat: float = 0.5
    """V de Cramér acima disso → par de associação alta."""
    n_faixas_mi: int = 10
    """Decis usados para discretizar numéricas no cálculo da informação mútua."""
    calcular_vif: bool = True
    calcular_phik: bool = False
    calcular_informacao_mutua: bool = False
    n_amostra_phik: int = 100_000

    def to_dict(self) -> dict:
        return asdict(self)
