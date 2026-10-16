# Métricas do `data_profiler`

Convenções globais:

- **Percentuais** (`pct_*`, `bad_rate`, `ks_faixa`, `estabilidade_ordem`, `poder_do_nulo`, `queda_gini`, `gap_gini`, `maus_acum`, `bons_acum`) estão na escala **0–100**, 2 casas (bad rate com 4).
- **Estatísticas** (`iv`, `ks`, `auc`, `gini`, `psi`, `woe`, correlações, `entropia`) são números puros (KS/AUC/Gini em 0–1).
- **WoE** `= ln(pct_bons / pct_maus)`: WoE **positivo** concentra bons (menor risco); negativo concentra maus. Essa convenção torna o WoE monotônico decrescente em relação à bad rate.
- Nomes de colunas em português, `snake_case`, sem acentos; o schema é **idêntico** nos backends Polars e Spark.

## 1. Profiling — numéricas (`ProfileResult.numericas`)

| Métrica | Fórmula / definição | Interpretação |
|---|---|---|
| `contagem` | nº de valores não nulos e finitos | NaN e ±inf são contados à parte e excluídos de todas as estatísticas |
| `nulos`, `pct_nulos` | nulos; `100·nulos / total_linhas` | `flag_pct_nulo = 1` se > `pct_nulos_max_eleita` (5%) |
| `nan_count`, `inf_count` | ocorrências de NaN e ±inf | em float, NaN ≠ null; ambos indicam problema de origem |
| `media`, `desvio_padrao`, `variancia` | média amostral; desvio e variância com `ddof=1` | — |
| `coef_variacao` | `desvio_padrao / |media|` | `variabilidade = True` se > 0,10. Se `|media| < epsilon`, usa `desvio_padrao > 0 e distintos > 1` (registrado em `obs`) |
| `minimo`, `maximo`, `p_1 … p_99` | extremos e percentis (`p_1`/`p_99` para capping) | Polars exato (interpolação linear); Spark `percentile_approx` |
| `iqr` | `p_75 − p_25` | — |
| `lim_inf_outlier`, `lim_sup_outlier` | `Q1 − 1,5·IQR`, `Q3 + 1,5·IQR` | fator configurável (`fator_iqr`) |
| `qtd_outliers_inf/sup`, `pct_outliers_inf/sup` | valores abaixo/acima dos limites; % sobre `contagem` | — |
| `distintos` | nº de valores distintos não nulos | Polars exato; Spark `approx_count_distinct` (rsd 1%) por padrão |
| `zeros`, `pct_zeros`, `negativos`, `pct_negativos` | contagens; % sobre `contagem` | — |
| `assimetria` | skewness (momento, viesada = população) | > 0 cauda à direita |
| `curtose` | excess kurtosis (normal = 0) | > 0 caudas pesadas |
| `moda`, `pct_moda` | valor mais frequente; % sobre `contagem` | `quase_constante = True` se `pct_moda > 95%` |
| `tipo_detectado` | `numerica` ou `identificador` | identificador: valores **inteiros** com `distintos / contagem > 95%` em tabelas com ≥ 100 linhas. Contínuas com casas decimais nunca são identificador |
| `tratada_como_categorica` | `distintos ≤ max_distintos_num_como_cat` (10) | a variável também aparece em `categoricas` e é tratada como categórica no poder preditivo |
| `variavel_eleita` | "Eleita" se `variabilidade` **e** `pct_nulos ≤ 5%` **e** não `quase_constante` **e** não identificador | `motivo_nao_eleita` lista todos os motivos ("sem variabilidade; nulos > 5%") |

## 2. Profiling — categóricas (`ProfileResult.categoricas`)

| Métrica | Fórmula / definição | Interpretação |
|---|---|---|
| `contagem`, `nulos`, `pct_nulos`, `distintos` | como acima | — |
| `vazios`, `pct_vazios` | `""` e strings só com espaços (após `trim`); % sobre `contagem` | vazio ≠ nulo, mas costuma significar o mesmo |
| `tamanho_min/max/medio` | comprimento em caracteres | detecta códigos de tamanho fixo e lixo |
| `moda`, `freq_moda`, `pct_moda` | categoria mais frequente | desempate determinístico (ordem alfabética) |
| `top_n` | JSON com as N categorias mais frequentes (`valor`, `qtd`, `pct`) | — |
| `qtd_categorias_raras` | categorias com frequência < `pct_categoria_rara` (1%) | exato mesmo com cap: no máximo 100 categorias têm ≥ 1% |
| `entropia`, `entropia_normalizada` | `H = −Σ p·ln p`; `H / ln(k)` ∈ [0, 1] | 0 = constante; 1 = uniforme. Acima de `max_categorias_distribuicao` (1 000) categorias, aproximada (top-K + "resto") e sinalizada em `obs` |
| `alta_cardinalidade` | `distintos > 50` | candidatas a agrupamento/target encoding |
| `constante`, `quase_constante` | `distintos == 1`; `pct_moda > 95%` | — |
| `pct_valores_suspeitos` | % de valores em `valores_suspeitos` ("NULL", "N/A", "-", "0"…) | nulos disfarçados de texto |
| `tipo_sugerido` | `numerica`/`data` se 100% das strings não nulas convertem | use `tipos={col: tipo}` para forçar a conversão |
| `tipo_detectado` | `categorica`, `booleana`, `identificador` ou `numerica` (baixa cardinalidade) | — |
| `variavel_eleita` | "Eleita" se `pct_nulos ≤ 5%`, não constante, não quase constante, não identificador | — |

## 3. Profiling — datas

| Tabela | Métrica | Definição |
|---|---|---|
| `datas_resumo` | `data_min`, `data_max`, `qtd_safras` | — |
| | `datas_futuras` | datas > hoje |
| | `datas_invalidas` | datas < `data_minima_valida` (1900-01-01) |
| | `safras_faltantes`, `qtd_safras_faltantes` | meses sem registro entre `data_min` e `data_max` (JSON) |
| `datas_safra` | `contagem`, `pct_total` | registros por safra `yyyy-MM`; % do total |
| | `variacao_mom` | `100·(n_t − n_{t−1}) / n_{t−1}` |

## 4. Estabilidade por safra (`coluna_safra`)

| Tabela | Métrica | Definição / interpretação |
|---|---|---|
| `estabilidade` | `pct_nulos`, `media` (numéricas), `moda`, `pct_moda` (categóricas) por safra | drift de preenchimento |
| `psi` | `PSI = Σ (p_atual − p_ref)·ln(p_atual / p_ref)` | numéricas em decis da **safra de referência**; categóricas por top-K categorias. `< 0,10` estável, `0,10–0,25` atenção, `> 0,25` instável. Frequências zero truncadas em 1e-6 |
| `bad_rate_safra` | `100·média(target)` por safra | — |

## 5. Poder preditivo — tabela de faixas (`faixas`)

| Coluna | Fórmula |
|---|---|
| `faixa`, `codigo`, `ordem_faixa` | rótulo legível (`[a, b)`, `NULO`, `ESPECIAL_<v>`, `OUTROS`, `VAZIO`), código interno e ordem (numéricas: ordem natural; categóricas: bad rate crescente; NULO/ESPECIAL ao final) |
| `limite_inf`, `limite_sup` | bordas da faixa numérica (`[inf, sup)`); null nas demais |
| `qtd`, `pct_qtd` | observações (ponderadas por `coluna_peso`); % da variável |
| `qtd_bons`, `qtd_maus`, `pct_bons`, `pct_maus` | contagens; % sobre o total de bons/maus. Se alguma faixa tem zero bons **ou** maus, `suavizacao_woe` (0,5) é somado a bons e maus de **todas** as faixas (`suavizado = True`) |
| `bad_rate` | `100·maus / qtd` |
| `woe` | `ln(pct_bons / pct_maus)` |
| `iv_faixa` | `(pct_bons − pct_maus)·woe` (em proporções) |
| `lift` | `bad_rate_faixa / bad_rate_geral` |
| `maus_acum`, `bons_acum`, `ks_faixa` | acumulados (%) na `ordem_faixa`; `|maus_acum − bons_acum|` |
| `media_target`, `mediana_target`, `desvio_target` | target contínuo: média e desvio **ponderados** por faixa; mediana não ponderada (Spark: `percentile_approx`) |

## 6. Poder preditivo — resumo por variável (`resumo`)

| Métrica | Fórmula / definição | Interpretação |
|---|---|---|
| `iv` | `Σ iv_faixa` | `< 0,02` sem poder; `0,02–0,10` fraco; `0,10–0,30` médio; `0,30–0,50` forte; `> 0,50` suspeito (`classificacao_iv`) |
| `ks_faixas` | `max(ks_faixa) / 100` | KS calculado sobre as faixas finais (comparável entre tipos) |
| `ks_bruto` | KS sobre o valor bruto (numéricas) | Polars exato; Spark aproximado por `n_faixas_finas` (200) quantis |
| `ks` | `ks_bruto` para numéricas, `ks_faixas` para categóricas | — |
| `auc_bruto`, `gini_bruto`, `direcao` | AUC do valor bruto como score de mau, orientada (≥ 0,5); `gini = 2·AUC − 1`; direção "valor maior → mais/menos risco" | — |
| `auc_woe`, `gini_woe` | AUC/Gini usando `−woe` como score | comparável entre numéricas e categóricas |
| `monotonicidade` | Spearman entre `ordem_faixa` e `bad_rate` (faixas regulares) | ±1 = perfeitamente monotônica |
| `qtd_inversoes` | nº de mudanças de direção da bad rate entre faixas adjacentes | `nao_monotonica` se > `max_inversoes_monotonica` (1) |
| `poder_do_nulo`, `bad_rate_nulo`, `pct_nulo` | `bad_rate(NULO) − bad_rate(demais)` em p.p. | ausência informativa (positivo = nulo é mais arriscado) |
| `informacao_mutua` | `MI(faixa, target) / min(H(faixa), H(target))` | 0 = independente; 1 = determinística |
| `qui_quadrado`, `p_valor_qui_quadrado`, `cramer_v` | teste de independência faixa × target (categóricas); `V = √(χ² / n)` | p pequeno = associação significativa |
| `p_valor_mann_whitney` | numéricas; derivado da AUC: `U = AUC·n₁·n₀`, aproximação normal **sem** correção de empates | — |
| `ponto_bisserial` | Pearson(valor, target) | sinal indica a direção |
| `pearson`, `spearman`, `eta`, `r2_faixas` | target contínuo; `η² = SS_entre / SS_total` sobre as faixas; `r2_faixas = η²` (R² do modelo "média da faixa") | — |
| `qtd_faixas`, `menor_pct_faixa`, `metodo_binning`, `faixa_pequena` | — | `faixa_pequena`: alguma faixa (ex.: NULO/ESPECIAL, que nunca são fundidas) < `min_pct_faixa` |
| `iv_referencia`, `gini_referencia` | métricas na safra de referência | — |
| `cv_iv` | `desvio(IV_safras) / média(IV_safras)` (exclui a referência) | `instavel` se > 0,30 |
| `estabilidade_ordem` | % de safras com Spearman(bad rate por faixa vs. referência) ≥ 0,8 | `instavel` se < 70% |
| `psi_faixas_max` | maior PSI da distribuição das faixas vs. referência | `instavel` se > 0,25 |
| `queda_gini` | `100·(gini_última_safra − gini_referência)` em p.p. | negativo = perda de poder |
| `iv_validacao`, `ks_validacao`, `gini_validacao`, `gap_gini` | métricas em `df_validacao` com os cortes do treino; `gap_gini = 100·(gini_treino − gini_validação)` | gap grande = overfitting do binning |
| `suspeita_vazamento` | `iv > 0,5` **ou** `auc_bruto`/`auc_woe > 0,85` **ou** nome casa com `padroes_vazamento` | investigar antes de usar |
| `recomendacao`, `motivo_recomendacao` | Selecionar: eleita no profiling, `iv ≥ 0,02`, sem alertas. Revisar: poder relevante com alerta (instável, não monotônica, vazamento). Descartar: sem poder ou não eleita | — |
| `redundante_com` | entre pares "Selecionar" com `|Spearman| > 0,7` ou `V de Cramér > 0,5`, a de menor IV aponta para a outra | — |
| `importancia` (opcional) | LightGBM em amostra: ganho e importância por permutação (AUC) | compara univariado × multivariado |

## 7. Correlação / associação (`CorrelacaoResult.pares`)

| `metodo` | Par | Definição | Associação alta |
|---|---|---|---|
| `pearson` | num × num | correlação linear; p-valor por teste t | `|r| > 0,7` |
| `spearman` | num × num | correlação de postos; p-valor aproximado (teste t) | `|ρ| > 0,7` |
| `cramer_v` | cat × cat | V de Cramér **com correção de viés (Bergsma, 2013)**: `φ²c = max(0, χ²/n − (k−1)(r−1)/(n−1))`, `kc = k − (k−1)²/(n−1)`, `rc = r − (r−1)²/(n−1)`, `V = √(φ²c / min(kc−1, rc−1))`. `obs` alerta quando > 20% das células têm esperado < 5 | `V > 0,5` |
| `qui_quadrado` | cat × cat | estatística χ², `gl` em `obs`, p-valor | — |
| `eta` | num × cat | razão de correlação `η = √(SS_entre / SS_total)` | `η > 0,7` |
| `ponto_bisserial` | num × cat binária | `(m₁ − m₀)/s · √(n₀n₁/n²)` | — |
| `phik` (opcional) | qualquer | coeficiente φK (biblioteca `phik`, em amostra) | `> 0,5` |
| `informacao_mutua` (opcional) | qualquer | MI normalizada por `min(H)`; numéricas discretizadas em decis | `> 0,5` |
| `vif` | numéricas | `VIF_i = diag(R⁻¹)` via pseudo-inversa da matriz de Pearson | `> 5` moderada, `> 10` alta |

Categóricas de alta cardinalidade: top-`max_categorias` (30) categorias; o resto vira `OUTROS` antes do teste. Nulos: *pairwise deletion* por padrão (`tratamento_nulos="listwise"` opcional).

## 8. Onde Polars (exato) e Spark (aproximado) divergem

| Item | Polars | Spark |
|---|---|---|
| Percentis (`p_*`, cortes por quantil, decis do PSI) | exatos, interpolação linear | `percentile_approx` (erro ≤ 1/`accuracy`, padrão 1/10 000 em posição) |
| `distintos` | exato | `approx_count_distinct` (rsd 1%); exato com `spark_distintos_exatos=True` |
| `ks_bruto`, `auc_bruto`, `gini_bruto`, `p_valor_mann_whitney` | exatos (agrupamento pelo valor) | aproximados por `n_faixas_finas` quantis (padrão 200): erro observado < 0,01 em KS/Gini |
| `mediana_target` por faixa (target contínuo) | exata | `percentile_approx` |
| Spearman em `correlate` | pairwise por par | **listwise** via `ml.stat.Correlation`, opcionalmente amostral (`amostra`); registrado em `metadados.obs` |
| Binning ótimo/árvore | ajustado em amostra (`n_amostra_binning`) | ajustado em amostra estratificada por `sampleBy` |
| Moda numérica, categorias, WoE, IV, KS por faixa, PSI de categóricas, correlações Pearson/η/V | exatos | **exatos** (agregações por chave; sem aproximação) |

Nota de dados: a conversão pandas → Spark transforma `NaN` em `null`, então `nulos` e `nan_count` podem trocar de lugar entre backends quando a origem é pandas; a soma é idêntica.

## 9. Referências

- Siddiqi, N. *Credit Risk Scorecards*. Wiley, 2006 (IV, WoE, KS, faixas de IV).
- Bergsma, W. (2013). "A bias-correction for Cramér's V and Tschuprow's T". *Journal of the Korean Statistical Society*, 42(3).
- Baak, M. et al. (2020). "A new correlation coefficient between categorical, ordinal and interval variables with Pearson characteristics" (φK).
- Navas-Palencia, G. *optbinning* — optimal binning (documentação da biblioteca).
