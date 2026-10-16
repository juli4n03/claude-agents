# Referência da API usada pela skill (Yggdrasil 0.0.22)

Assinaturas extraídas do código da tag `0.0.22` e conferidas num smoke test. Em versão
nova, confira de novo com `inspect.signature`.

## `yggdrasil.credit_risk.model.ModelSegmenter`

```python
ModelSegmenter(df, target="target", task_type="classification", sample_col=None,
               ref_sample="DES", feature_labels=None, problem_label=None, features=None,
               date_col=None, verbose=True, score_scale=1000.0, random_state=42,
               score_invertido=True, n_jobs=1)
```

| método | uso | retorno |
|---|---|---|
| `variable_iv(features=None, sample=None, max_n_bins=5, min_bin_size=0.05, with_psi=True)` | IV e PSI univariados | DataFrame |
| `select_features(steps=None, apply=True, **params)` | etapas: `missing, constante, categoricas, iv, psi, monotonia, correlacao` (default) + `vif`, `backward` | `seg.selection_` (`SelectionResult`) |
| `fit(algorithm, hyperparams=None, features=None, transform="raw", class_balance=False, monotone=None)` | `transform="woe"` = scorecard | modelo em `seg` |
| `tune_optuna(algorithm, n_trials=30, transform="raw", cv=None, time_aware=False, stability_penalty=None, monotone=None, pruner=None, log_mlflow=False, ...)` | `time_aware=True` exige `cv` e `date_col`; objetivo penaliza PSI > 0,10 e gap treino-validação > 0,05 | refita o melhor |
| `build_ratings(method="quantil", n_ratings=10, monotonic_fusion=True, alpha=0.05, cuts=None, percentiles=None)` | métodos: `decis, quantil, arvore, optbin, manual_score, manual_percentil`; `n_ratings="auto"` | ratings em `seg` |
| `metrics()` | uma linha por amostra (`amostra`, `n`, `auc`, `gini`, `ks`, `ks_cutoff`, `brier`, `logloss`, ...) | DataFrame |
| `backtest(time_col=None, sample=None, tol=None)` | `safra, n, previsto_medio, realizado_medio, gap, ic_low, ic_high, p_valor, status` | DataFrame |
| `hosmer_lemeshow(sample=None, n_groups=10)` | `statistic, p_value, df, n_groups, table` | dict |
| `rating_table()`, `adjacent_rating_tests()`, `rating_psi_by_safra()` | régua, teste entre vizinhos, PSI por safra | DataFrame |
| `scorecard_table(feature)` | pontos por faixa da variável | DataFrame |
| `predict(X, col_score="score", col_rating="rating", col_value=None, col_reasons=None, reasons_top_n=3)` | pontua dados novos (holdout, produção) | DataFrame `score, rating, motivo_*` |
| `to_sql(table, score_col="score", col_rating="rating", ...)`, `logit_sql(table, ...)` | régua / logit como SQL | str |
| `apply_spark(sdf, col_score="score", col_rating="rating", ...)` | pontua Spark DataFrame | Spark DataFrame |
| `save(path)`, `load(path, df=None)` | JSON + `.model.joblib` (unpickle) | — |
| `log_to_mlflow(experiment=None, run_name=None, registered_model_name=None, ...)` | registra run | — |

Algoritmos (`ALGORITHMS`): `logistica, linear, random_forest, extra_trees,
gradient_boosting, hist_gradient_boosting, lightgbm, xgboost, catboost` (catboost exige
o extra `[catboost]`, fora do venv padrão).

## `yggdrasil.credit_risk.tree.TreeSegmenter`

```python
TreeSegmenter(df, target="target", task_type="classification", sample_col=None,
              ref_sample="DES", min_leaf_rows=50, ...)
.fit_auto(features=None, max_depth=3, min_iv=0.02, max_n_bins=2, min_bin_size=0.05, ...)
.metrics(cutoff=None)            # amostra, n, taxa_default, KS, AUC, Gini, Acuracia, F1
.psi_by_safra(time_col=None, eps=1e-06)
.to_sql(table, col_seg="segmento", col_nota="folha", col_valor="valor_previsto")
.to_pyspark(func_name="aplicar_regua"); .apply_spark(sdf, ...)
.save(path); TreeSegmenter.load(path, df)
```

## `yggdrasil.metrics` (esperam valor alto = mais risco: passe `-score`)

```python
classification_metrics(y_true, y_score, cutoff=None)   # auc, gini, accuracy_ratio, ks, ks_cutoff, accuracy, f1, precision, recall, brier, logloss
ks_statistic(y_true, y_score); lift_table(y_true, y_score, n_bins=10)
reliability_table(y_true, y_pred, n_bins=10, alpha=0.05, method="jeffreys")
calibration_slope_intercept(y_true, y_pred)
bootstrap_metrics_ci(y_true, y_score, metrics=("auc",), n_boot=200, alpha=0.05, stratified=True, seed=None)
    # lista de {valor, ic_low, ic_high, se}, na ordem de `metrics`
binomial_ci(k, n, alpha=0.05, method="jeffreys")
metric_shifts(metrics_ref, metrics_cmp, flag_atencao=0.1, flag_degradado=0.2)
shift_significance(y_ref, score_ref, y_cmp, score_cmp, metric="auc", n_boot=200, alpha=0.05, seed=None)
```

## `yggdrasil.monitoring`

```python
psi(expected, actual, bins=10, eps=1e-06)
psi_rating_over_time(df, rating_col, cfg, baseline=None)          # cfg = yggdrasil.ColumnConfig
rating_migration_matrix(df, rating_col, cfg, period_a, period_b, labels=None)
metric_over_time(df, cfg, problem_type, freq="M", min_n=200)
```
