---
name: data-profiler
description: Profiling de variáveis, poder preditivo (WoE/IV, KS, AUC/Gini, PSI, estabilidade por safra, suspeita de vazamento) e associação entre variáveis (Pearson, Spearman, V de Cramér, VIF) em PySpark ou Polars com o mesmo schema de saída. Use ao montar ou auditar uma base de modelagem, selecionar variáveis para scorecard, investigar drift entre safras ou aplicar WoE numa base nova.
---

# data_profiler

Pacote próprio, que acompanha esta skill em `pacote/` (e como wheel em `pacotes/` do kit).
Não reimplemente IV, KS, PSI ou binning à mão: chame o pacote, que já tem testes de
paridade Polars × Spark e trata NaN, inf, strings vazias e sentinelas.

Esta skill faz a **triagem** na base inteira. Treinar, gerar ratings, calibrar ou
exportar o modelo é com a skill `data-science`, que recebe as variáveis eleitas aqui.

## Disponibilizar o pacote no Databricks

Na primeira célula do notebook (instalação com escopo de notebook):

```python
%pip install /Workspace/Users/<seu_email>/kit_ia/pacotes/data_profiler-1.0.0-py3-none-any.whl
```

Se a instalação estiver bloqueada, use o código-fonte direto:

```python
import sys; sys.path.insert(0, "/Workspace/Users/<seu_email>/kit_ia/skills/data-profiler/pacote")
from data_profiler import profile, poder_preditivo, correlate
```

Obrigatórias: `polars numpy scipy` (numpy e scipy já vêm no runtime; confira o `polars`
com `%pip show polars`). Opcionais degradam com warning: `optbinning scikit-learn`
(binning ótimo), `openpyxl plotly` (Excel/HTML), `lightgbm phik`.

## Fluxo padrão (respeita o template da skill `estilo-analise`)

```python
# 4. Profiling na base inteira (Spark: só agregações voltam ao driver)
prof = profile(sdf_base, coluna_safra="dt_ref", target="target", ignorar=["id_cliente"])

pp = poder_preditivo(
    sdf_base, "target",
    coluna_safra="dt_ref", safra_referencia="2025-01",
    df_validacao=sdf_oot,         # out-of-time: os cortes do treino são reaplicados
    profile_result=prof,          # reaproveita o status "Eleita"
)
candidatas = pp.resumo.filter(pl.col("recomendacao").is_in(["Selecionar", "Revisar"]))["variavel"].to_list()
corr = correlate(sdf_base, colunas=candidatas, profile_result=prof)
```

```python
# 5. Validações
assert pp.resumo.filter(pl.col("suspeita_vazamento")).is_empty(), "variável com suspeita de vazamento: investigar antes de seguir"
assert pp.resumo["gap_gini"].max() < 10, "binning com overfitting no OOT"
```

Saídas: `prof.to_excel()`, `pp.to_excel()`, `pp.to_html()`, `pp.cortes_json()`. Grave em
`/Workspace/Users/<seu_email>/...` ou num volume do Unity Catalog, e guarde os cortes junto
do notebook para reaplicar com `pp.aplicar_woe(df_novo)`.

## Escolha de backend

- `backend="auto"` decide pelo tipo da entrada: Spark DataFrame → backend Spark;
  Polars → backend Polars (acima de `max_linhas_polars`, 50 mi, ele avisa para ir de Spark).
- No Databricks, o padrão é Spark. Em base larga, calibre
  `ProfileConfig(spark_lote_variaveis=...)`.
- Spark é **aproximado** em percentis, distintos, KS/AUC bruto (200 quantis finos, erro
  < 0,01) e Spearman (listwise, em amostra). Se o número vai para comitê, diga qual backend
  gerou.

## Como ler (limites padrão, todos em `config.py`)

| Métrica | Leitura |
|---|---|
| IV | < 0,02 sem poder · 0,02–0,10 fraco · 0,10–0,30 médio · 0,30–0,50 forte · **> 0,50 suspeito** |
| PSI | < 0,10 estável · 0,10–0,25 atenção · > 0,25 instável |
| `cv_iv` | > 0,30 → IV instável entre safras |
| `gap_gini` | gini treino − gini OOT em p.p.; gap grande = binning sobreajustado |
| `suspeita_vazamento` | IV > 0,5, AUC > 0,85 ou nome com `atraso`, `default`, `ever`, `pagamento`… |
| `redundante_com` | a de menor IV aponta para a mais forte (|Spearman| > 0,7 ou V > 0,5) |
| VIF | > 5 moderada · > 10 alta |

Convenção: `woe = ln(%bons / %maus)`, então WoE **positivo concentra bons**. Percentuais
na escala 0–100. Fórmulas completas e divergências Polars × Spark em
[pacote/docs/METRICAS.md](pacote/docs/METRICAS.md): leia só a seção que precisar.

## Armadilhas que o pacote já resolve (não "conserte" por fora)

- Sentinelas numéricas: passe em `PoderPreditivoConfig(valores_especiais=(-9999,))` para
  ganharem faixa própria, em vez de substituí-las antes.
- NaN ≠ null; o pacote separa `nan_count` e `nulos`.
- Target precisa de pelo menos `min_maus` (30) maus **e** bons, senão aborta: isso é
  sinal de recorte errado, não de parâmetro a baixar.
- Safra imatura entra na bad rate como bom. Filtre safras sem janela de performance
  completa antes de chamar `poder_preditivo`.

Exemplos completos: `pacote/examples/exemplo_spark.py` e `pacote/examples/exemplo_polars.py`.
