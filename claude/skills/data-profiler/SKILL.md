---
name: data-profiler
description: Profiling de variáveis, poder preditivo (WoE/IV, KS, AUC/Gini, PSI, estabilidade por safra, suspeita de vazamento) e associação entre variáveis (Pearson, Spearman, V de Cramér, VIF) em Polars ou PySpark com o mesmo schema de saída. Use ao montar ou auditar uma base de modelagem, selecionar variáveis para scorecard, investigar drift entre safras ou aplicar WoE numa base nova.
---

# data_profiler

Pacote próprio, versionado dentro desta skill em `${CLAUDE_SKILL_DIR}/pacote/`.
Não reimplemente IV, KS, PSI ou binning à mão: chame o pacote, que já tem testes de
paridade Polars × Spark e trata NaN, inf, strings vazias e sentinelas.

Esta skill faz a **triagem** na base inteira. Treinar, gerar ratings, calibrar ou
exportar o modelo é com a skill `data-science`, que recebe as variáveis eleitas aqui.

## Disponibilizar o pacote

Preferência: instalado no ambiente (`pip install -e ${CLAUDE_SKILL_DIR}/pacote`, rodado
pelo `install.sh`). Confira com `pip show data-profiler`. Se não estiver instalado, use na
célula de imports:

```python
import sys; sys.path.insert(0, str(Path.home() / ".claude/skills/data-profiler/pacote"))
from data_profiler import profile, poder_preditivo, correlate
```

Obrigatórias: `polars numpy scipy`. Opcionais degradam com warning: `pyspark` (backend
Spark), `optbinning scikit-learn` (binning ótimo), `openpyxl plotly` (Excel/HTML),
`lightgbm phik`.

## Fluxo padrão (respeita o template da skill `estilo-analise`)

```python
# 4. Extract + profiling
prof = profile(lf_base, coluna_safra="dt_ref", target="target", ignorar=["id_cliente"])

pp = poder_preditivo(
    lf_base, "target",
    coluna_safra="dt_ref", safra_referencia="2025-01",
    df_validacao=lf_oot,          # out-of-time: os cortes do treino são reaplicados
    profile_result=prof,          # reaproveita o status "Eleita"
)
candidatas = pp.resumo.filter(pl.col("recomendacao").is_in(["Selecionar", "Revisar"]))["variavel"].to_list()
corr = correlate(lf_base, colunas=candidatas, profile_result=prof)
```

```python
# 5. Validações
assert pp.resumo.filter(pl.col("suspeita_vazamento")).is_empty(), "variável com suspeita de vazamento: investigar antes de seguir"
assert pp.resumo["gap_gini"].max() < 10, "binning com overfitting no OOT"
```

Saídas: `prof.to_excel()`, `pp.to_excel()`, `pp.to_html()`, `pp.cortes_json()` (guarde os
cortes junto do notebook para reaplicar com `pp.aplicar_woe(df_novo)`).

## Escolha de backend

- `backend="auto"` decide pelo tipo da entrada. Polars aceita `LazyFrame` direto (use
  `pl.scan_parquet`); acima de `max_linhas_polars` (50 mi) ele avisa para ir de Spark.
- Spark nunca coleta a base inteira: só agregações voltam para o driver. Em base larga,
  calibre `ProfileConfig(spark_lote_variaveis=...)`.
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

## Feature store larga (centenas a milhares de colunas)

- **Só no DES.** Triagem, redundância e cortes usam a amostra de desenvolvimento; VAL e
  OOT entram só para estabilidade (`df_validacao=`). Filtro calculado com o OOT junto já
  escolheu variável olhando o teste.
- **Por grupo de variáveis** (book/fonte): `profile` e `poder_preditivo` com `colunas=` de
  um grupo por vez sobre o mesmo `LazyFrame`/Spark DataFrame; nada de coletar a base
  inteira para pandas. Junte os `resumo` no fim.
- **Primeiro sem alvo, depois com alvo**: `profile` corta quase-constantes
  (`pct_moda > 95%`) e identificadores; `poder_preditivo` corta sem poder e marca
  vazamento.
- **Redundância em duas rodadas**: `correlate` dentro de cada grupo e depois entre os
  sobreviventes (com `CorrelacaoConfig(amostra=...)` em base grande). O desempate é pelo
  IV (`redundante_com`), **nunca pela variância**, que depende da escala da variável.
- Sentinelas viram faixa própria, não "todo valor negativo é missing": cada código
  ("sem presença na fonte", "sem evento na janela") carrega risco diferente.
- Registre os limiares usados ao lado da lista aprovada (JSON/CSV com versão): o
  notebook de modelagem lê essa lista, não refaz a triagem.

## Armadilhas que o pacote já resolve (não "conserte" por fora)

- Sentinelas numéricas: passe em `PoderPreditivoConfig(valores_especiais=(-9999,))` para
  ganharem faixa própria, em vez de substituí-las antes.
- NaN ≠ null em Polars (NaN passa em `>=`); o pacote separa `nan_count` e `nulos`.
- Target precisa de pelo menos `min_maus` (30) maus **e** bons, senão aborta: isso é
  sinal de recorte errado, não de parâmetro a baixar.
- Safra imatura entra na bad rate como bom. Filtre safras sem janela de performance
  completa antes de chamar `poder_preditivo`.

## Testar o pacote

`python -m pytest ${CLAUDE_SKILL_DIR}/pacote/tests -q -m "not spark"` (~1 min, 34 testes).
Exemplo completo: `pacote/examples/exemplo_polars.py`; Spark no EMR/Glue:
`pacote/examples/exemplo_spark.py`.
