"""Fluxo completo com Polars: profiling -> poder preditivo -> correlação -> WoE em base nova.

Execute a partir da raiz do repositório:  python examples/exemplo_polars.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from data_profiler import CorrelacaoConfig, PoderPreditivoConfig, ProfileConfig, correlate, poder_preditivo, profile  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    # Em produção: pl.scan_parquet("s3://bucket/caminho/*.parquet") devolve um LazyFrame e
    # funciona direto (storage_options para credenciais). Aqui usamos a base sintética.
    from sintetico import gerar_base

    treino = gerar_base(50_000, seed=1)
    nova = gerar_base(10_000, seed=2)
    saida = Path("saida_polars")
    saida.mkdir(exist_ok=True)

    # 1) Profiling com estabilidade por safra ---------------------------------------
    prof = profile(
        treino,
        coluna_safra="dt_ref",
        target="target",
        ignorar=["id_cliente"],
        tipos={"str_numero": "numerica"},
        config=ProfileConfig(pct_nulos_max_eleita=5.0, top_n=10),
    )
    print(prof.resumo())
    print(prof.numericas.select("variavel", "pct_nulos", "coef_variacao", "variavel_eleita", "motivo_nao_eleita"))
    prof.to_excel(str(saida / "profiling.xlsx"))
    prof.to_parquet(str(saida / "profiling"))

    # 2) Poder preditivo reaproveitando o status "Eleita" ------------------------------
    pp = poder_preditivo(
        treino,
        "target",
        coluna_safra="dt_ref",
        safra_referencia="2024-01",
        df_validacao=nova,
        coluna_peso="peso",
        profile_result=prof,
        config=PoderPreditivoConfig(metodo_binning="quantil", n_faixas=10, min_pct_faixa=5.0),
    )
    print(pp.resumo.select("ranking", "variavel", "recomendacao", "iv", "ks", "gini_woe", "cv_iv", "gap_gini", "redundante_com", "obs"))
    print(pp.faixas.filter(pl.col("variavel") == "score_bureau").select("faixa", "pct_qtd", "bad_rate", "woe", "iv_faixa"))
    pp.to_excel(str(saida / "poder_preditivo.xlsx"))
    pp.to_html(str(saida / "poder_preditivo.html"))  # requer plotly (degrada com warning)
    (saida / "cortes.json").write_text(pp.cortes_json(), encoding="utf-8")

    # 3) Correlação entre as variáveis selecionadas/revisadas ----------------------------
    candidatas = pp.resumo.filter(pl.col("recomendacao").is_in(["Selecionar", "Revisar"]))["variavel"].to_list()
    corr = correlate(treino, colunas=candidatas, profile_result=prof, config=CorrelacaoConfig(calcular_vif=True))
    print(corr.pares_altos)
    print(corr.vif)

    # 4) Aplicar WoE em uma base nova (cortes salvos) ----------------------------------
    nova_woe = pp.aplicar_woe(nova, colunas=candidatas)
    print(nova_woe.select([c for c in nova_woe.columns if c.endswith("_woe")]).head())
    nova_woe.write_parquet(saida / "nova_woe.parquet")

    # Em outro processo, sem reexecutar nada:
    # from data_profiler import PoderPreditivoResult
    # pp2 = PoderPreditivoResult.de_cortes(Path("saida_polars/cortes.json").read_text())
    # pp2.aplicar_woe(pl.scan_parquet("s3://.../producao/*.parquet").collect())


if __name__ == "__main__":
    main()
