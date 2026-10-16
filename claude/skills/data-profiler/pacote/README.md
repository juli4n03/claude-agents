# data_profiler

Profiling de variáveis, poder preditivo (variável × target) e associação entre variáveis para
modelagem de risco de crédito — em **Polars** (pandas/Polars) ou **PySpark**, com o **mesmo schema de
saída** independentemente do backend. Testado com Polars 2.0, PySpark 4.2 e Python 3.12.

```python
from data_profiler import profile, poder_preditivo, correlate

prof = profile(df, coluna_safra="dt_ref", target="target")          # ProfileResult
pp   = poder_preditivo(df, "target", coluna_safra="dt_ref",
                       df_validacao=df_oot, profile_result=prof)     # PoderPreditivoResult
corr = correlate(df, colunas=pp.resumo.filter(pl.col("recomendacao") == "Selecionar")["variavel"].to_list())
df_oot_woe = pp.aplicar_woe(df_oot)                                   # mesmo tipo de entrada na saída
```

`df` pode ser `pandas.DataFrame`, `polars.DataFrame`/`LazyFrame` ou `pyspark.sql.DataFrame`; o backend é
escolhido automaticamente (`backend="auto"|"polars"|"spark"`). Entradas Spark nunca são coletadas inteiras.

## Arquitetura

```
data_profiler/
├── config.py              ProfileConfig, PoderPreditivoConfig, CorrelacaoConfig (todos os limites de negócio)
├── _types.py              BinSpec e códigos de faixa compartilhados
├── _utils.py              KS/AUC a partir de distribuição, PSI, entropia, cronômetro, dependências opcionais
├── backends/
│   ├── base.py            BaseBackend (Strategy): contrato de agregações
│   ├── polars_backend.py  LazyFrame + expressões vetorizadas (exato)
│   ├── spark_backend.py   agg() único, percentile_approx, formato longo em lotes (aproximado onde documentado)
│   └── __init__.py        selecionar_backend()
├── profiling.py           profile() e ProfileResult
├── binning.py             cortes (quantil/otimo/arvore), fusão de faixas, remapeamento, tabela de faixas
├── stability.py           preenchimento, PSI e bad rate por safra
├── predictive_power.py    poder_preditivo() e PoderPreditivoResult (aplicar_woe, cortes JSON)
├── correlation.py         correlate() e CorrelacaoResult (Pearson, Spearman, Cramér-Bergsma, η, φK, MI, VIF)
└── report.py              Excel (formatação condicional), Parquet, HTML/plotly
```

Princípio: o backend só executa **agregações** e devolve Polars pequenos; flags, WoE/IV, KS por faixa,
classificação e recomendação são calculados em código comum. Veja `docs/METRICAS.md` para fórmulas,
interpretação e a lista exata de onde Polars (exato) e Spark (aproximado) divergem.

## Instalação

```bash
pip install polars numpy scipy            # obrigatórias
pip install pyspark                       # backend Spark (requer Java)
pip install optbinning lightgbm phik openpyxl plotly scikit-learn   # opcionais (degradam com warning)
```

## Testes

```bash
pytest tests -q            # 41 testes; os de Spark são pulados se pyspark/Java não existirem
pytest tests -q -m "not spark"
```

A suíte gera uma base sintética (`tests/sintetico.py`) com nulos, NaN/inf, strings vazias, coluna
constante, identificador, datas futuras/inválidas, uma variável forte e monotônica, uma aleatória
(IV ≈ 0) e uma "vazada" (verificada em `suspeita_vazamento`), e compara Polars × Spark com tolerância
apenas nas métricas aproximadas.

## Exemplos

- `examples/exemplo_polars.py` — fluxo completo em Polars.
- `examples/exemplo_spark.py` — Parquet/Iceberg no S3 (EMR/Glue), fluxo completo em Spark.
