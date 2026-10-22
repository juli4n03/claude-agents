---
name: data-science
description: Pipeline de modelagem de risco de crédito ponta a ponta - desenho amostral (split por cliente, validação temporal, OOT intocado, maturação por alvo), montagem da base com joins validados e point-in-time, scorecard (logística/WoE) ou LightGBM com tuning Optuna temporal e poda por SHAP, ratings, calibração, comparação com scores incumbentes, valor incremental de fonte nova, swap, contrato do modelo e exportação ONNX/SQL/PySpark. Use para montar base de modelagem, treinar, validar, comparar, recalibrar ou exportar um modelo; a triagem inicial de variáveis na base inteira é da skill data-profiler.
---

# Data science: da base de modelagem ao modelo em produção

Dois motores, um contrato:
- **Yggdrasil 0.0.22** (MIT, cópia privada `juli4n03/yggdrasil-project`, tag `0.0.22`):
  scorecard, ratings, calibração, backtest, SQL/PySpark. API em [referencia.md](referencia.md).
- **`kit_ds.py`** (nesta skill, testado): o que o Yggdrasil não cobre: desenho amostral,
  joins validados e PIT, LightGBM com validação temporal, comparação pareada de scores,
  swap, contrato do modelo e ONNX. Cada função existe porque o erro já aconteceu.

As regras abaixo vêm de pipelines reais de crédito e dos bugs encontrados neles.

## Ambiente

- Venv `~/.claude/local/venvs/data-science` (criado por `install.sh --data-science`),
  kernel Jupyter **`data-science`**. Versões travadas em [restricoes.txt](restricoes.txt)
  (`scikit-learn<1.8`, `pandas<3`, `mlflow<3`: com elas 527 testes do Yggdrasil passam).
- Confira antes de escrever:
  `~/.claude/local/venvs/data-science/bin/python -c "import yggdrasil, data_profiler, lightgbm, onnxruntime"`.
  Falhou: peça `install.sh --data-science`; não faça `pip install` avulso.
- No notebook:
  ```python
  import sys; sys.path.insert(0, str(Path.home() / ".claude/skills/data-science"))
  import kit_ds as k
  ```
- `SEED = 42` fixo em tudo (split, Optuna, LightGBM). Nunca `int(time.time())`: o split
  muda a cada execução e o "teste" de um notebook vira treino do outro.

## 1. Desenho amostral

| amostra | o que é | usado para |
|---|---|---|
| `DES` | safras de desenvolvimento, fatia de **clientes** sorteada | treino (OPT no Optuna) |
| `VAL` | últimas safras do desenvolvimento (janela temporal) | early stopping, escolha de trial, corte de poda, ratings, calibração |
| `TESTE` (opcional) | os demais clientes das safras de DES | estabilidade in-time; não decide nada |
| `OOT` | safras seguintes, nunca vistas | **um** número honesto no fim |

- **Split por cliente, não por linha**: `k.split_por_entidade(df, "id_cliente", 0.3, seed=SEED)`.
  O mesmo cliente em várias safras não pode estar dos dois lados.
- **Grave a coluna de amostra** no parquet da base: o split é parte do dado, não do código.
- **Maturação por alvo**: `tab = k.tabela_maturacao(df, "safra", ["ever60_m3", "ever30_m3", "fpd30"])`
  e `k.ultima_safra_madura(tab, alvo)` define o fim do OOT **de cada alvo**. Alvo nulo =
  imaturo: sai da amostra daquele alvo, nunca vira bom.
- **Alvos alternativos** só são avaliados (no OOT de cada um), não otimizados.
- **Quebra de público** (mudança de política, canal novo): a VAL deve ficar depois dela.
- **OOT é para um número.** Escolha de variante, corte de SHAP, ranges do Optuna e limiares
  saem da VAL. Registre cada variante que olhou o OOT em `k.registra_run(...)`: com 15
  variantes comparadas no OOT, ele virou validação e você precisa de outro holdout.

## 2. Montagem da base

- **Todo join** passa por `k.left_join_validado(esq, dir, chave, "fonte", col_safra="safra", ordem_dedup="dt_atualizacao")`:
  deduplica a direita com ordem determinística, falha se a contagem mudar, cria
  `match_fonte` **antes** de qualquer `fillna` e devolve a cobertura por safra. Safra com
  cobertura destoante é fonte que não existia no período: exclua a variável ou o período.
- **Dado com data própria** (bureau, folha, cadastro SCD-2) entra com
  `k.join_pit(esq, dir, "id", "dt_ref", "dt_bureau", "bureau", lag_dias=N)`: só o que já
  estava disponível na data de referência, considerando a defasagem de entrega.
- **Mesma fonte em todas as amostras.** Se o DES usa o retroativo e o OOT a produção,
  rode `k.reconcilia_fontes(df, "score_retro", "score_prod", "safra")` no overlap; sem
  bater, a variável muda de definição entre treino e teste.
- **Chave sem par não vira categoria**: cliente sem código não é "novo" por `fillna`;
  é desconhecido e precisa da própria categoria (ou sair).
- **Vazamento por categoria**, com assert antes de treinar:
  `k.checa_vazamento(features, {"alvo": ["ever\\d+", "fpd"], "chave": ["^id_", "cpf"], "pos_decisao": ["atraso", "pagto"], "benchmark": ["^score_"]})`.
- Sentinelas com significado ("sem presença na fonte", "sem evento na janela") ficam
  como valor próprio no boosting e como faixa especial no binning (nunca "todo negativo
  é missing").

## 3. Modelo

**Trilha A, scorecard (comitê, regulador, política):** Yggdrasil.

```python
from yggdrasil.credit_risk.model import ModelSegmenter
df_mod = df[df["amostra"].isin(["DES", "VAL"])].sort_values("amostra")   # DES antes de VAL
seg = ModelSegmenter(df_mod, target=ALVO, sample_col="amostra", ref_sample="DES",
                     date_col="dt_ref", features=FEATURES, random_state=SEED)
seg.select_features()
seg.fit(algorithm="logistica", transform="woe")
seg.build_ratings(method="quantil", n_ratings="auto")
```

**Trilha B, LightGBM (máximo de ordenação):** `kit_ds` + Yggdrasil para ratings.

```python
des, val = df[df.amostra == "DES"], df[df.amostra == "VAL"]
dv = pd.concat([des, val])
mono = lambda fs: [MONO.get(f, 0) for f in fs]          # MONO = {feature: +1 | -1}, direção conhecida

def tunar(fs, n_trials=80):
    return k.tune_lgbm_temporal(des[fs], des[ALVO], val[fs], val[ALVO], n_trials=n_trials,
                                gap_max=0.05, monotone=mono(fs), seed=SEED)   # gap é filtro, não penalidade

res = tunar(FEATURES)
imp = k.importancia_acumulada(k.refit_final(res, des[FEATURES], des[ALVO], monotone=mono(FEATURES)),
                              des[FEATURES].sample(30_000, random_state=SEED))
candidatos = {100: (FEATURES, res)}
for pct in (97, 95, 90):                                 # poda decidida na VAL, nunca no OOT
    fs = k.corta_por_importancia(imp, pct)
    candidatos[pct] = (fs, tunar(fs, n_trials=40))
melhor = max(c[1].auc_val for c in candidatos.values())
pct, (feats, res_f) = min(((p, c) for p, c in candidatos.items() if c[1].auc_val >= melhor - 0.002),
                          key=lambda pc: len(pc[1][0]))  # o menor conjunto empatado com o melhor
modelo = k.refit_final(res_f, dv[feats], dv[ALVO], monotone=mono(feats))   # DES ∪ VAL, best_iter fixo

seg = ModelSegmenter(dv.sort_values("amostra"), target=ALVO, sample_col="amostra",
                     ref_sample="DES", date_col="dt_ref", features=feats, random_state=SEED)
seg.set_model(modelo, features=feats)                    # ratings, backtest, HL e SQL do LightGBM
seg.build_ratings(method="quantil", n_ratings="auto")
```

- `res.auc_val` é **otimista** (a VAL escolheu iteração e trial). Rotule assim.
- `refit_final` usa `n_estimators=best_iter` sem early stopping: parar no próprio treino
  não regulariza e faz o "AUC de validação" virar in-sample.
- Warm start (`trials_iniciais`) só com parâmetros do **mesmo** espaço e das mesmas features.
- **Monotonicidade**: declare `MONO` (+1 / −1 / 0 por feature) quando a direção é
  conhecida. Sem ela, confira `k.shap_por_faixa(modelo, X, feature, sentinelas)`: os
  quartis devem andar na direção esperada e cada sentinela empurrar para um lado que faça
  sentido.
- Peso por recência só se a quebra de público justificar, e com a VAL decidindo λ.

## 4. Avaliação

**Direção fixada antes, nunca pelo resultado.** `k.orienta(score, maior_eh_risco=...)`
pela definição do score (PD: alto = risco; score de bureau e do Yggdrasil: alto = bom).
`max(auc, 1-auc)` é proibido, e aplicado ao KS transforma 0,3 em 0,7.

| pergunta | função | regra |
|---|---|---|
| quanto ordena? | `k.metricas(y, risco)` | AUC < 0,5 é aviso, não inversão |
| contra os incumbentes? | `k.compara_scores(df_oot, ALVO, {"novo": ("pd", True), "bureau": ("score", False)}, referencia="bureau")` | **mesmas linhas** (interseção) e cobertura de cada um ao lado |
| a fonte nova agrega? | `k.delta_pareado(y, risco_base, risco_base_mais_fonte)` | modelos aninhados nas mesmas linhas; ganho com IC pareado, não dois números soltos |
| é estável? | `k.metricas_por_grupo(df, ALVO, "risco", "safra")`, idem por segmento | grupo sem `n_min` maus/bons sai nulo |
| decis | `cortes = k.cortes_decis(risco_des)` e `k.aplica_decis(risco, cortes)` em todas as amostras | um só jogo de cortes, do treino; faixa 0 = sem score |
| deriva | `k.estabilidade_decis(df, "safra", "faixa", ALVO)` e `k.psi(des, oot, especiais=SENT)` | nulos e cada sentinela com faixa própria |
| calibrado? | `seg.backtest()`, `seg.hosmer_lemeshow()` | descalibrado: recalibre na VAL (Platt/isotônica), confira no OOT |

- AUC de modelo só em amostras que ele não viu. Comparar o modelo em safras de treino
  com score externo é injusto.
- Benchmark com índice: depois de `reset_index` nos splits, alinhe por coluna de chave,
  nunca por `df.loc[X.index]`.
- Score de benchmark com coeficientes copiados para o código precisa da janela em que foi
  treinado; se ela cruza o OOT, não é benchmark.

## 5. Swap e população inteira

`k.swap(df, ALVO, "risco_atual", "risco_novo", taxa_aprovacao=0.6)`: mesma taxa de
aprovação nos dois scores; mantidos, swap_in, swap_out e reprovados com bad rate e IC.

- Bad rate só onde há alvo. `n_sem_alvo` (reprovados no passado, imaturos) é
  **desconhecido**, não bom: aplicar a bad rate do decil do DES (parceling) subestima o
  risco dos reprovados. Rotule como estimativa e diga o viés.
- Pontuar a base inteira ("through the door") exige recriar as features engenheiradas com
  **a mesma função** do treino e `k.valida_entrada(df, contrato)` antes: feature ausente
  preenchida com sentinela em silêncio é pontuar outro cliente.

## 6. Produção

```python
contrato = k.contrato_modelo(modelo, des, feats, alvo=ALVO, sentinelas=SENT,
                             janelas=JANELAS, metricas_=METRICAS_OOT, commit=COMMIT)
k.salva_contrato(contrato, "modelo/contrato.json")
k.exporta_onnx(modelo, val[feats].sample(min(20_000, len(val)), random_state=SEED), "modelo/modelo.onnx",
               tol_prob=1e-4, cortes=CORTES_DE_POLITICA)
modelo.booster_.save_model("modelo/modelo.txt")            # formato nativo, não pickle
```

- O contrato traz ordem, dtype, % nulo, % por sentinela e percentis do treino (baseline
  de drift), hash do **conteúdo** do treino, versões das bibliotecas e commit.
- ONNX só vale com diferença de probabilidade ≤ `tol_prob` **e** as mesmas decisões nos
  cortes; Spearman alto não basta.
- Scorecard ou régua: `seg.to_sql(...)` / `seg.apply_spark(sdf)`; compare o SQL com
  `seg.predict` numa partição.
- Nada de `pickle.load` de caminho compartilhado nem "pickle mais recente da pasta":
  carregue pelo caminho registrado no ledger.

## Não faça

- Escolher direção, variante, corte de poda ou range de busca olhando o OOT.
- Early stopping na mesma amostra que entra no treino final.
- Comparar modelos rodados em populações diferentes (um só com score não nulo, outro na
  base inteira).
- Duas definições de decil, ou dois limites de PSI, no mesmo relatório.
- `SHAP` com a amostra editada (trocar extremos por NaN muda o ramo das árvores).
- `pip install` dentro do notebook; interfaces interativas (`*UI`) em notebook entregue.
- Reimplementar KS, AUC, PSI, decis, swap ou WoE à mão: use `kit_ds`, Yggdrasil ou o
  `data_profiler`.

## Atualizar o Yggdrasil

Versão nova só depois de: `git -C ~/.claude/local/pacotes/yggdrasil-project fetch upstream`,
ler o changelog, rodar os testes do pacote e os de `tests/test_kit_ds.py` no venv da
skill, criar a tag nova na cópia privada e mudar `YGGDRASIL_REF` no `install.sh`.
