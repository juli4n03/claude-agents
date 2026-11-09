---
name: analise-credito
description: Economia de produtos de crédito parcelado - NPV e margem unitária de contrato, perda esperada em R$, corte de política por faixa de risco, swap set entre políticas, hazard/EAD/curva de safra, accrual e stop accrual no P&L, funil e conversão. Use em qualquer pergunta ou notebook sobre viabilidade, unit economics, política de crédito ou leitura de safras.
---

# Análise de crédito: PD → R$ → margem em NPV → corte

## Onde estão os números

Nenhum parâmetro de empresa mora nesta skill. Antes de calcular:

1. Leia os parâmetros de crédito da empresa (seção **Crédito** das instruções ou o arquivo
   `credito.md` preenchido a partir de `credito.modelo.md`): taxas canônicas, direção da
   escala de risco, nome do target, tabelas e pendências em aberto.
2. Se a empresa tiver um pacote aprovado de métricas de crédito, **use as funções dele**
   em vez de reescrever a conta. Este kit não traz pacote de crédito.
3. Sem parâmetros confirmados: siga o método abaixo e declare cada parâmetro como
   premissa explícita no topo do notebook, numa célula de constantes:

```python
# Premissas (status: a confirmar com FP&A). Preencha com os valores da empresa.
TAXA_DESCONTO_AM = ...       # hurdle, a.m.
TAXA_FUNDING_AM = ...        # captação, a.m.
ALIQ_IMPOSTOS = ...          # sobre receita de juros
MIN_OBS_FAIXA = 100          # padrão do método
```

## Cadeia de decisão

```
PD (modelo) → perda esperada em R$ (multiplicador de perda × EAD)
            → margem unitária em NPV (receita − funding − perda − custos, descontados)
            → margem ÷ ticket → corte onde cruza zero por faixa de risco
            → swap: Σ margem(só a nova aprova) − Σ margem(só a antiga aprova)
```

Em base grande, calcule margem por contrato em PySpark e agregue por faixa de risco
antes de trazer para o driver.

## Regras de método (valem em qualquer empresa)

- **Métrica de corte é a margem relativa** (margem ÷ ticket), não a margem absoluta.
- **EAD é o total do contrato com juros**, não o principal. Perda sobre o principal
  subestima a perda.
- **Risco sempre unitário e em valor**; **margem sempre com e sem serviço**, em colunas
  separadas.
- **Funding como empréstimo espelho** (PMT à taxa de captação), nunca % da receita.
  Taxa de desconto (hurdle) ≠ taxa de funding; trocar uma pela outra infla a margem.
- **Mix engana agregados**: EAD, bad rate e margem sempre por faixa de risco antes de
  agregar. Um agregado pode ir na direção oposta de todas as faixas (Simpson).
- **Hazard sobre sobreviventes, censura por parcela.** Safra sem janela de performance
  completa não entra como "boa".
- **Grupos pequenos não decidem corte**: mínimo de observações por faixa (padrão 100).
- **Forward × backtest**: diga se a perda veio da PD do modelo (forward) ou do target
  realizado (backtest).
- **Direção da escala de risco** (1 = pior ou 1 = melhor) escrita em toda tabela e
  gráfico: arquivos diferentes costumam usar direções opostas.
- **Limite por ticket não é de graça**: ao avaliar política com teto de valor, o swap
  precisa mostrar os contratos bons cortados, não só a faixa marginal que abriu.

## Rastreabilidade (obrigatória)

- Toda cifra citada tem tabela ou célula de origem.
- Número de base sintética é marcado como **sintético**; número real, com a tabela.
- Conversão derivada por você (anualizar taxa, trocar base de cálculo) é marcada como
  "cálculo meu", não como fato do FP&A.
- Parâmetro "a confirmar" aparece como premissa, com sensibilidade (tornado: varie cada
  premissa ±X% e mostre o impacto na margem) quando muda a decisão.
- Se documentação e código divergem, **o código manda** e a documentação é corrigida.

## Pergunta de comitê

Feche toda análise de política com: qual premissa, se errada, inverte a recomendação?
Mostre o número que a responde.
