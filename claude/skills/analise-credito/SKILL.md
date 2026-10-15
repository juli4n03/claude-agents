---
name: analise-credito
description: Economia de produtos de crédito parcelado - NPV e margem unitária de contrato, perda esperada em R$, corte de política por faixa de risco, swap set entre políticas, hazard/EAD/curva de safra, accrual e stop accrual no P&L, funil e conversão. Use em qualquer pergunta ou notebook sobre viabilidade, unit economics, política de crédito ou leitura de safras.
---

# Análise de crédito: PD → R$ → margem em NPV → corte

## Onde estão o código e os números

Nada de parâmetro de empresa mora neste repositório. Antes de calcular:

1. Leia `~/.claude/local/credito.md` (se existir): taxas canônicas, direção da escala de
   risco, nome do target, tabelas e pendências em aberto da empresa atual.
2. O pacote de métricas fica no caminho `pacotes.credit_analytics` de
   `~/.claude/local/config.yaml` (padrão `~/.claude/local/pacotes/credit_analytics`). Se
   existir, **use as funções dele** em vez de reescrever a conta: elas têm testes com
   valores de referência. Leia o `README.md` do pacote para o mapa
   métrica → função → gráfico, e o `params.py` para saber quais números estão valendo.
3. Sem `credito.md` nem pacote: siga o método abaixo e declare cada parâmetro como
   premissa explícita no topo do notebook.

```python
import sys; sys.path.insert(0, str(Path(CAMINHO_PACOTE)))   # lido do config.yaml
import credit_analytics as ca
params = ca.DEFAULT                                           # sensibilidade: dataclasses.replace(params, discount_rate=...)
```

## Cadeia de decisão

```
PD (modelo) → perda esperada em R$ (multiplicador de perda × EAD)
            → margem unitária em NPV (receita − funding − perda − custos, descontados)
            → margem ÷ ticket → corte onde cruza zero por faixa de risco
            → swap: Σ margem(só a nova aprova) − Σ margem(só a antiga aprova)
```

## Regras de método (valem em qualquer empresa)

- **Métrica de corte é a margem relativa** (margem ÷ ticket), não a margem absoluta.
- **EAD é o total do contrato com juros**, não o principal. Perda sobre o principal
  subestima a perda (no caso de referência, ~37%).
- **Risco sempre unitário e em valor**; **margem sempre com e sem serviço**, em colunas
  separadas.
- **Funding como empréstimo espelho** (PMT à taxa de captação), nunca % da receita.
  Taxa de desconto (hurdle) ≠ taxa de funding; trocar uma pela outra infla a margem.
- **Mix engana agregados**: EAD, bad rate e margem sempre `by=` faixa de risco antes de
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

- Toda cifra citada tem arquivo ou célula de origem.
- Número de base sintética é marcado como **sintético**; número real, com a tabela.
- Conversão derivada por você (anualizar taxa, trocar base de cálculo) é marcada como
  "cálculo meu", não como fato do FP&A.
- Parâmetro marcado "a confirmar" no `credito.md` aparece como premissa, com
  sensibilidade quando muda a decisão (use o tornado do pacote).
- Se documentação e código divergem, **o código manda** e a documentação é corrigida.

## Pergunta de comitê

Feche toda análise de política com: qual premissa, se errada, inverte a recomendação?
Mostre o número que a responde.
