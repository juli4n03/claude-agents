# Referência da API usada pela skill

## `kit_ds` (nesta skill; `tests/test_kit_ds.py`, 17 testes)

Convenção: "risco" = valor alto é mais risco. `SEED = 42`.

| área | função | devolve |
|---|---|---|
| amostra | `split_por_entidade(df, col_entidade, frac_teste=0.3, seed, col_estrato=None)` | Series `treino`/`teste` por linha, sorteando entidades |
| amostra | `tabela_maturacao(df, col_safra, alvos)` · `ultima_safra_madura(tab, alvo, pct_min=99)` | safra × alvo: n, % maduro, bad rate · fim do OOT do alvo |
| base | `left_join_validado(esq, dir, on, nome, col_safra=None, ordem_dedup=None)` | `(df, cobertura_por_safra)`; cria `match_<nome>`; falha se a contagem mudar |
| base | `join_pit(esq, dir, chave, data_esq, data_dir, nome, lag_dias=0)` | df com `pit_<nome>` e `defasagem_<nome>_dias` |
| base | `reconcilia_fontes(df, col_a, col_b, col_safra=None, tol=0)` | n no overlap, % igual, diferença média e máxima |
| base | `checa_vazamento(features, {categoria: [regex]})` | `AssertionError` listando as proibidas |
| métrica | `orienta(score, maior_eh_risco)` · `ks(y, risco)` · `metricas(y, risco)` | n, bad_rate, auc, gini, ks (sem inverter) |
| métrica | `metricas_por_grupo(df, col_y, col_risco, col_grupo, n_min=50)` | por safra/segmento; grupo pequeno sai nulo |
| métrica | `delta_pareado(y, risco_a, risco_b, metricas_=("auc","ks"), n_boot=500, seed)` | delta B − A com IC pareado e `p_delta_le_0` |
| métrica | `compara_scores(df, col_y, {rotulo: (coluna, maior_eh_risco)}, referencia=None)` | `(métricas na interseção + cobertura, deltas vs referência)` |
| faixas | `cortes_decis(risco_treino, n=10)` · `aplica_decis(risco, cortes)` | cortes do treino · faixa 1 = menor risco, 0 = sem score |
| faixas | `estabilidade_decis(df, col_safra, col_faixa, col_y=None)` | `{"composicao_pct", "bad_rate_pct"}` safra × faixa |
| faixas | `psi(esperado, atual, n_bins=10, especiais=(), eps=1e-6)` | PSI com faixa de nulo e de cada sentinela |
| swap | `swap(df, col_y, risco_atual, risco_novo, taxa_aprovacao)` | quadrantes + carteiras, bad rate com IC Jeffreys, `n_sem_alvo` |
| lgbm | `tune_lgbm_temporal(X_opt, y_opt, X_val, y_val, n_trials=80, gap_max=0.05, seed, espaco=None, monotone=None, peso_opt=None, max_arvores=4000, paciencia=100, trials_iniciais=())` | `ResultadoTuning(params, best_iter, auc_opt, auc_val, gap, trials, aviso, estudo)` |
| lgbm | `ESPACO_PADRAO` | dict `{param: (tipo, lo, hi)}`; tipos `int`, `log_int`, `float`, `log`; `num_leaves_frac` vira `num_leaves` |
| lgbm | `refit_final(res, X, y, seed, monotone=None, peso=None)` | `LGBMClassifier` com `n_estimators=best_iter` |
| lgbm | `importancia_acumulada(modelo, X_amostra=None, metodo="shap"\|"gain")` · `corta_por_importancia(tab, pct)` | ranking com `acumulado_pct` · lista de features |
| lgbm | `shap_por_faixa(modelo, X_amostra, feature, sentinelas=(), n_quartis=4)` | SHAP médio por quartil válido, nulo e sentinela |
| produção | `contrato_modelo(modelo, X_treino, features, alvo, sentinelas=(), janelas=None, metricas_=None, commit=None, extras=None)` · `salva_contrato(c, caminho)` | dict JSON-serializável |
| produção | `valida_entrada(df, contrato)` | colunas na ordem do contrato; falha se faltar ou não for numérica |
| produção | `exporta_onnx(modelo, X_amostra, caminho, tol_prob=1e-4, cortes=())` | relatório; falha se divergir; grava `<caminho>.io.json` |
| produção | `registra_run(ledger_csv, **campos)` | ledger acumulado |

# Yggdrasil 0.0.22

Assinaturas extraídas do código da tag `0.0.22` e conferidas num smoke test. Em versão
nova, confira de novo com `inspect.signature`. `ModelSegmenter.set_model(model, features=None)`
recebe um modelo já ajustado (ex.: o LightGBM do `kit_ds`) e libera ratings, backtest,
Hosmer-Lemeshow e `to_sql` para ele.

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
