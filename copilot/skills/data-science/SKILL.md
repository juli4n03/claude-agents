---
name: data-science
description: Modelagem de risco de crédito ponta a ponta com o Yggdrasil - seleção de variáveis, modelo (logística/WoE scorecard, LightGBM, XGBoost com monotonicidade), tuning Optuna por safra, ratings/grupos homogêneos, calibração, backtest, monitoramento (PSI de rating, migração) e exportação da régua em SQL ou PySpark. Use para treinar, validar, recalibrar ou exportar um modelo ou segmentação; a triagem inicial de variáveis na base inteira é da skill data-profiler.
---

# Data science: do lote de variáveis ao modelo em produção

Motor: **Yggdrasil 0.0.22** (licença MIT, richardguilhermeds). O wheel acompanha o kit em
`pacotes/`. Assinaturas das funções em [referencia.md](referencia.md).

## Ambiente no Databricks

O Yggdrasil traz mlflow, shap, lightgbm, xgboost, optuna e optbinning, e só foi validado com
`scikit-learn<1.8`, `pandas<3` e `mlflow<3` ([restricoes.txt](restricoes.txt)). Instale
com escopo de notebook, na primeira célula:

```python
%pip install /Workspace/Users/<seu_email>/kit_ia/pacotes/yggdrasil_project-0.0.22-py3-none-any.whl /Workspace/Users/<seu_email>/kit_ia/pacotes/data_profiler-1.0.0-py3-none-any.whl -c /Workspace/Users/<seu_email>/kit_ia/skills/data-science/restricoes.txt
dbutils.library.restartPython()
```

- As dependências vêm do PyPI (ou do espelho da empresa). Se o `%pip` falhar por rede,
  pare e me avise: não contorne instalando versões soltas.
- Prefira um cluster com runtime **ML** (já traz lightgbm, xgboost, mlflow). Se o mlflow do
  runtime for 3.x, as restrições fazem downgrade só neste notebook; confira com
  `import mlflow; mlflow.__version__`.
- MLflow no Databricks já registra no workspace: use `mlflow.set_experiment("/Users/<seu_email>/<nome>")`.

## Contrato das amostras (leia antes de montar a base)

O Yggdrasil é pandas: treino roda no driver. Leve só as variáveis já triadas e uma
amostra que caiba (o `data-profiler` faz a triagem na base inteira, em Spark):
`pd_base = sdf_modelagem.select(VARIAVEIS + ["target", "amostra", "dt_ref"]).toPandas()`.

| amostra | papel | entra no `ModelSegmenter`? |
|---|---|---|
| `DES` | treino e cortes de binning | sim (`ref_sample="DES"`) |
| `OOT` | validação: tuning do Optuna, **fusão dos ratings**, métricas de shift | sim |
| `HOLDOUT` | teste final, nunca visto | **não**: pontue com `seg.predict(df_holdout)` no fim |

Duas armadilhas do código que este contrato neutraliza:
- O "OOT" interno é **a primeira amostra diferente da DES na ordem das linhas**
  (`_oot_sample`). Com três amostras no mesmo DataFrame, uma ordenação diferente troca
  o OOT em silêncio. Por isso o `HOLDOUT` fica fora.
- `build_ratings(monotonic_fusion=True)` funde grupos olhando o alvo do OOT e o
  `tune_optuna` maximiza a métrica no OOT: as métricas de OOT ficam otimistas. O número
  que vai para comitê é o do `HOLDOUT`.

Safra imatura (sem janela de performance completa) não entra em nenhuma amostra.

## Fluxo (um notebook, template da `estilo-analise`)

```python
# 4. Modelo
from yggdrasil.credit_risk.model import ModelSegmenter

df_treino = pd_base[pd_base["amostra"].isin(["DES", "OOT"])].sort_values("amostra")  # DES antes de OOT
df_holdout = pd_base[pd_base["amostra"] == "HOLDOUT"]

seg = ModelSegmenter(df_treino, target="target", task_type="classification",
                     sample_col="amostra", ref_sample="DES", date_col="dt_ref",
                     features=VARIAVEIS, random_state=42)
seg.select_features()                       # missing → constante → categóricas → iv → psi → monotonia → correlação
print(seg.selection_)                        # trilha: selecionada / revisar / excluida e o motivo

seg.fit(algorithm="logistica", transform="woe")          # scorecard, interpretável
# ou: seg.tune_optuna(algorithm="lightgbm", n_trials=40, cv=4, time_aware=True,
#                     stability_penalty=1.0, monotone=MONOTONIA)
seg.build_ratings(method="quantil", n_ratings="auto")
```

```python
# 5. Validações
from yggdrasil.metrics import classification_metrics, bootstrap_metrics_ci

df_metricas = seg.metrics().set_index("amostra")          # linhas DES e OOT; colunas auc, gini, ks, brier...
df_backtest = seg.backtest()                               # por safra: previsto x realizado, ic, status
hl = seg.hosmer_lemeshow()                                 # statistic, p_value, df, n_groups, table
df_h = seg.predict(df_holdout, col_reasons="motivo")       # score, rating, motivo_1..3

risco_h = -df_h["score"]                                   # score do Yggdrasil: maior = MENOS risco
met_h = classification_metrics(df_holdout["target"], risco_h)
ic_h = bootstrap_metrics_ci(df_holdout["target"], risco_h, metrics=("auc", "ks"), seed=42)

assert met_h["auc"] > 0.5, "AUC < 0,5: direção do score trocada"
assert met_h["ks"] > KS_MIN, "KS do holdout abaixo do mínimo"
assert df_metricas.loc["OOT", "gini"] - met_h["gini"] < 0.05, "gini do OOT otimista frente ao holdout"
assert hl["p_value"] > 0.05, "modelo descalibrado (Hosmer-Lemeshow)"
```

**Direção do score.** `ModelSegmenter` usa `score_invertido=True` e `score_scale=1000`:
score alto = cliente bom. As funções de `yggdrasil.metrics` esperam o contrário (valor
alto = mais risco). Passe `-score` (ou a probabilidade) a elas: com o score cru o mesmo
modelo sai com AUC 0,24 e KS 0 em vez de 0,76 e 0,40. `seg.metrics()` já corrige
sozinho; só as chamadas avulsas precisam do sinal.

Escolha do algoritmo: **logística com WoE** quando o modelo vai para comitê, política
ou regulador (coeficientes, `scorecard_table`, `logit_sql`); boosting com `monotone=`
declarado quando o ganho de KS no **holdout** paga a perda de interpretabilidade.
Sem `monotone`, boosting pode inverter a relação de uma variável com o risco.

## Ratings, calibração e monitoramento

- `build_ratings`: `quantil` (default, fusão por teste de inversão) ou `arvore`/`optbin`.
  Depois `seg.rating_table()` e `seg.adjacent_rating_tests()`: grupo vizinho sem
  diferença significativa é para fundir.
- Calibração por safra: `seg.backtest()` e `seg.hosmer_lemeshow()`. Com probabilidades na
  mão: `reliability_table(y, p)` (IC de Jeffreys por faixa) e
  `calibration_slope_intercept(y, p)` (ideal: slope ≈ 1, intercept ≈ 0).
- Incerteza: `bootstrap_metrics_ci(y, -score, metrics=("auc", "ks"))`; diferença entre
  amostras com `shift_significance(...)` em vez de comparar números soltos.
- Monitoramento: `seg.rating_psi_by_safra()`; fora do segmentador,
  `yggdrasil.monitoring.psi_rating_over_time` e `rating_migration_matrix`.

## Segmentação por árvore

`TreeSegmenter(df_treino, target, sample_col="amostra", min_leaf_rows=...)` →
`fit_auto(features=..., max_depth=3, min_iv=0.02)` → `metrics()` e `psi_by_safra()` →
`to_pyspark()` para aplicar no Databricks. Atenção: as colunas de `metrics()` da árvore
são `KS`, `AUC`, `Gini`, `taxa_default` (maiúsculas), diferente do `ModelSegmenter`.

## Exportar para produção

- **PySpark (preferido no Databricks)**: `seg.apply_spark(sdf)` pontua sem coletar a base.
- SQL: `seg.to_sql(table=...)` e `seg.logit_sql(...)` foram escritos para Athena. Antes de
  usar no Databricks SQL, rode numa partição e compare com `seg.predict` nas mesmas
  linhas: diferença acima de arredondamento é bug de exportação ou de dialeto.
- Persistência: `seg.save(caminho)` grava JSON + `.model.joblib` (use um volume do Unity
  Catalog). `ModelSegmenter.load` faz unpickle: **só carregue arquivo que você mesmo gerou**.
- MLflow (`seg.log_to_mlflow(...)`): confira no run que o artefato do modelo existe; o
  logger do Yggdrasil engole erro de serialização e só deixa a tag `model_log_error`.

## Não faça

- Interfaces interativas (`ModelSegmenterUI`, `TreeSegmenterUI`): notebook entregue
  precisa reproduzir sem cliques.
- `MLPipeline` sem `problem_type="classification"` explícito (o default é regressão).
- Reimplementar IV, KS, PSI, ratings ou WoE à mão: use o pacote.
