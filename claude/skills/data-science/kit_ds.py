"""kit_ds: peças de pipeline de modelagem de crédito que o Yggdrasil não cobre.

Cada função corrige um erro que já apareceu em pipelines reais:

- desenho amostral: split por entidade (não por linha), maturação por safra × alvo;
- montagem da base: join que não muda a contagem, flag de match antes de preencher,
  join point-in-time com defasagem de disponibilidade, reconciliação de fontes;
- avaliação: direção do score fixada antes (nunca `max(auc, 1 - auc)`), comparação
  pareada nas mesmas linhas com IC, decis com cortes do treino, PSI com NaN e
  sentinelas em faixas próprias, swap com taxa de aprovação fixa;
- LightGBM: tuning com validação temporal e filtro de gap, refit com nº de árvores
  fixo, poda por importância acumulada;
- produção: contrato do modelo, exportação ONNX com checagem de ida e volta,
  validação de entrada sem preenchimento silencioso, ledger de runs.

Convenção: "risco" é sempre orientado para **valor alto = mais risco**.
Dependências: pandas, numpy, scikit-learn; lightgbm, optuna, shap e onnx só nas
funções que os usam.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, roc_curve

SEED = 42


# --------------------------------------------------------------------------- #
# Desenho amostral
# --------------------------------------------------------------------------- #
def split_por_entidade(df: pd.DataFrame, col_entidade: str, frac_teste: float = 0.3,
                       seed: int = SEED, col_estrato: str | None = None) -> pd.Series:
    """Rotula cada linha como 'treino' ou 'teste' sorteando **entidades**, não linhas.

    O mesmo cliente em várias safras cai inteiro de um lado só; split por linha vaza o
    cliente entre treino e teste e infla a métrica de teste. Com `col_estrato`, o
    sorteio é estratificado pelo máximo da coluna por entidade (ex.: alguma vez mau).
    Determinístico para a mesma seed e o mesmo conjunto de entidades.
    """
    ent = df.groupby(col_entidade, sort=True)
    estrato = ent[col_estrato].max().fillna(-1) if col_estrato else pd.Series(0, index=ent.size().index)
    rng = np.random.default_rng(seed)
    teste = set()
    for _, ids in estrato.groupby(estrato):
        ids = ids.index.to_numpy()
        k = int(round(len(ids) * frac_teste))
        teste.update(rng.choice(ids, size=k, replace=False).tolist())
    return df[col_entidade].isin(teste).map({True: "teste", False: "treino"}).rename("split")


def tabela_maturacao(df: pd.DataFrame, col_safra: str, alvos: Sequence[str]) -> pd.DataFrame:
    """Por safra e alvo: linhas, % com alvo observado (maduro) e bad rate entre maduros.

    Alvo nulo = imaturo/censurado. Imaturo nunca vira "bom": sai da amostra daquele alvo.
    """
    partes = []
    for alvo in alvos:
        g = df.groupby(col_safra)[alvo]
        partes.append(pd.DataFrame({
            "alvo": alvo,
            "n": g.size(),
            "pct_maduro": 100 * g.count() / g.size(),
            "bad_rate": 100 * g.mean(),
        }))
    return pd.concat(partes).reset_index().rename(columns={col_safra: "safra"})


def ultima_safra_madura(tab: pd.DataFrame, alvo: str, pct_min: float = 99.0):
    """Última safra em que o alvo está maduro em pelo menos `pct_min`% das linhas,
    sem safra imatura antes dela. Define o fim da janela OOT daquele alvo."""
    t = tab[tab["alvo"] == alvo].sort_values("safra")
    maduras = t["pct_maduro"] >= pct_min
    if not maduras.iloc[0]:
        return None
    corte = maduras.cummin()
    return t.loc[corte, "safra"].iloc[-1]


# --------------------------------------------------------------------------- #
# Montagem da base
# --------------------------------------------------------------------------- #
def left_join_validado(esq: pd.DataFrame, dir: pd.DataFrame, on: str | list[str], nome: str,
                       col_safra: str | None = None, ordem_dedup: str | list[str] | None = None,
                       ) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Left join que não pode mudar a contagem de linhas da esquerda.

    - chave duplicada à direita: sem `ordem_dedup` é erro; com ela, fica a linha de maior
      `ordem_dedup` (ordenação estável, determinística);
    - cria `match_<nome>` **antes** de qualquer preenchimento: depois de um fillna não dá
      mais para separar "a fonte não tinha" de "a fonte tinha e era sentinela";
    - devolve a cobertura por safra (`col_safra`) para conferir que nenhum período ficou
      sem a fonte.
    """
    chaves = [on] if isinstance(on, str) else list(on)
    dup = dir.duplicated(chaves, keep=False)
    if dup.any():
        if ordem_dedup is None:
            raise ValueError(f"{nome}: {int(dup.sum())} linhas com chave duplicada à direita; "
                             "passe ordem_dedup ou deduplique antes")
        ordem = [ordem_dedup] if isinstance(ordem_dedup, str) else list(ordem_dedup)
        dir = (dir.sort_values(chaves + ordem, kind="mergesort")
                  .drop_duplicates(chaves, keep="last"))
    flag = f"match_{nome}"
    n_antes = len(esq)
    out = esq.merge(dir.assign(**{flag: True}), on=chaves, how="left", validate="m:1")
    out[flag] = out[flag].eq(True)
    if len(out) != n_antes:
        raise AssertionError(f"{nome}: join mudou a contagem ({n_antes} -> {len(out)})")
    cobertura = None
    if col_safra:
        cobertura = (out.groupby(col_safra)[flag].agg(n="size", pct_match="mean")
                        .assign(pct_match=lambda t: 100 * t["pct_match"]))
    return out, cobertura


def join_pit(esq: pd.DataFrame, dir: pd.DataFrame, chave: str, data_esq: str, data_dir: str,
             nome: str, lag_dias: int = 0) -> pd.DataFrame:
    """Join point-in-time: para cada linha da esquerda, a observação mais recente da
    direita que **já estava disponível** na data da esquerda.

    Disponível = `data_dir + lag_dias <= data_esq`. Sem o lag, uma informação que chega
    com atraso (bureau, folha, contábil) entra antes de existir: vazamento do futuro.
    Mantém a ordem e a contagem da esquerda; cria `pit_<nome>` (achou) e
    `defasagem_<nome>_dias`.
    """
    e = esq.reset_index(names="_ordem_esq")
    d = dir.copy()
    d["_data_obs"] = pd.to_datetime(d[data_dir])
    d["_disponivel"] = d["_data_obs"] + pd.Timedelta(days=lag_dias)
    e["_data_esq"] = pd.to_datetime(e[data_esq])
    e = e.sort_values("_data_esq", kind="mergesort")
    d = d.sort_values("_disponivel", kind="mergesort")
    out = pd.merge_asof(e, d, left_on="_data_esq", right_on="_disponivel", by=chave,
                        direction="backward", suffixes=("", f"_{nome}"))
    out[f"pit_{nome}"] = out["_disponivel"].notna()
    out[f"defasagem_{nome}_dias"] = (out["_data_esq"] - out["_data_obs"]).dt.days
    out = (out.sort_values("_ordem_esq").set_index("_ordem_esq")
              .drop(columns=["_data_esq", "_disponivel", "_data_obs"]))
    out.index.name = esq.index.name
    if len(out) != len(esq):
        raise AssertionError(f"{nome}: join PIT mudou a contagem ({len(esq)} -> {len(out)})")
    return out


def reconcilia_fontes(df: pd.DataFrame, col_a: str, col_b: str, col_safra: str | None = None,
                      tol: float = 0.0) -> pd.DataFrame:
    """Compara duas fontes da mesma variável onde ambas existem (overlap).

    Use antes de trocar de fonte entre amostras (ex.: retroativo no DES, produção no
    OOT): se não batem no overlap, a variável muda de definição entre treino e teste.
    """
    ambos = df[col_a].notna() & df[col_b].notna()
    diff = (df.loc[ambos, col_a] - df.loc[ambos, col_b]).abs()
    base = df.loc[ambos].assign(_igual=diff <= tol, _diff=diff)
    grupos = base.groupby(col_safra) if col_safra else [("total", base)]
    linhas = []
    for chave, g in grupos:
        linhas.append({"safra": chave, "n_overlap": len(g), "pct_igual": 100 * g["_igual"].mean(),
                       "diff_media": g["_diff"].mean(), "diff_max": g["_diff"].max()})
    return pd.DataFrame(linhas)


def checa_vazamento(features: Iterable[str], proibidas: Mapping[str, Sequence[str]]) -> None:
    """Falha se alguma feature casa um padrão proibido (regex, sem diferenciar maiúsculas).

    `proibidas` por categoria, ex.: {"alvo": ["^target", "ever\\d+"], "chave": ["^cpf$"],
    "pos_decisao": ["atraso", "pagamento"], "score_benchmark": ["^score_"]}.
    """
    achados = {cat: sorted(f for f in features if any(re.search(p, f, re.I) for p in pads))
               for cat, pads in proibidas.items()}
    achados = {c: f for c, f in achados.items() if f}
    if achados:
        raise AssertionError(f"features proibidas no modelo: {achados}")


# --------------------------------------------------------------------------- #
# Métricas com direção fixa
# --------------------------------------------------------------------------- #
def orienta(score, maior_eh_risco: bool) -> np.ndarray:
    """Converte um score para a convenção valor alto = mais risco.

    A direção vem da definição do score (PD: alto = risco; score de bureau ou do
    Yggdrasil: alto = bom), **nunca** do AUC na amostra avaliada. Escolher a direção
    pelo resultado é otimista e, aplicado ao KS, transforma 0,3 em 0,7.
    """
    s = np.asarray(score, dtype="float64")
    return s if maior_eh_risco else -s


def ks(y, risco) -> float:
    fpr, tpr, _ = roc_curve(np.asarray(y), np.asarray(risco))
    return float(np.max(tpr - fpr))


def metricas(y, risco) -> dict:
    """AUC, Gini e KS de um score já orientado. Não inverte nada: AUC < 0,5 é sinal de
    direção errada ou modelo pior que o acaso, e só gera um aviso."""
    y = np.asarray(y)
    r = np.asarray(risco, dtype="float64")
    ok = ~np.isnan(r)
    y, r = y[ok], r[ok]
    auc = float(roc_auc_score(y, r))
    if auc < 0.5:
        warnings.warn(f"AUC {auc:.3f} < 0,5: confira a direção do score (não inverta pelo resultado)")
    return {"n": int(len(y)), "bad_rate": float(y.mean()), "auc": auc, "gini": 2 * auc - 1, "ks": ks(y, r)}


def metricas_por_grupo(df: pd.DataFrame, col_y: str, col_risco: str, col_grupo: str,
                       n_min: int = 50) -> pd.DataFrame:
    """Métricas por safra ou segmento. Grupo com menos de `n_min` maus ou bons, ou com
    uma classe só, sai com métricas nulas em vez de um número instável."""
    linhas = []
    for g, sub in df[[col_y, col_risco, col_grupo]].dropna().groupby(col_grupo):
        n_maus = int(sub[col_y].sum())
        n_bons = len(sub) - n_maus
        base = {"grupo": g, "n": len(sub), "n_maus": n_maus, "bad_rate": sub[col_y].mean()}
        if min(n_maus, n_bons) < n_min:
            linhas.append({**base, "auc": np.nan, "gini": np.nan, "ks": np.nan})
            continue
        m = metricas(sub[col_y], sub[col_risco])
        linhas.append({**base, "auc": m["auc"], "gini": m["gini"], "ks": m["ks"]})
    return pd.DataFrame(linhas)


_FUNCOES = {"auc": lambda y, r: roc_auc_score(y, r), "gini": lambda y, r: 2 * roc_auc_score(y, r) - 1, "ks": ks}


def delta_pareado(y, risco_a, risco_b, metricas_: Sequence[str] = ("auc", "ks"),
                  n_boot: int = 500, seed: int = SEED, alpha: float = 0.05) -> pd.DataFrame:
    """Ganho de B sobre A **nas mesmas linhas**, com IC por bootstrap pareado e
    estratificado por classe. É o teste certo para "a fonte nova agrega?": comparar
    números de modelos rodados em populações diferentes não diz nada."""
    y = np.asarray(y)
    a = np.asarray(risco_a, dtype="float64")
    b = np.asarray(risco_b, dtype="float64")
    ok = ~(np.isnan(a) | np.isnan(b))
    y, a, b = y[ok], a[ok], b[ok]
    rng = np.random.default_rng(seed)
    idx1, idx0 = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    linhas = []
    for nome in metricas_:
        f = _FUNCOES[nome]
        ponto = f(y, b) - f(y, a)
        deltas = np.empty(n_boot)
        for i in range(n_boot):
            s = np.concatenate([rng.choice(idx1, len(idx1)), rng.choice(idx0, len(idx0))])
            deltas[i] = f(y[s], b[s]) - f(y[s], a[s])
        lo, hi = np.quantile(deltas, [alpha / 2, 1 - alpha / 2])
        linhas.append({"metrica": nome, "a": f(y, a), "b": f(y, b), "delta": ponto,
                       "ic_low": lo, "ic_high": hi, "p_delta_le_0": float((deltas <= 0).mean()),
                       "n": int(len(y))})
    return pd.DataFrame(linhas)


def compara_scores(df: pd.DataFrame, col_y: str, scores: Mapping[str, tuple[str, bool]],
                   referencia: str | None = None, **kw) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Compara vários scores na **interseção** em que todos existem.

    `scores` = {rotulo: (coluna, maior_eh_risco)}. Devolve as métricas de cada um na
    população comum (com a cobertura de cada score na base inteira) e, com
    `referencia`, o delta pareado de cada score contra ela.
    """
    cols = {r: c for r, (c, _) in scores.items()}
    comum = df[[col_y, *cols.values()]].dropna()
    linhas = []
    for rot, (col, alto_risco) in scores.items():
        m = metricas(comum[col_y], orienta(comum[col], alto_risco))
        linhas.append({"score": rot, "cobertura_pct": 100 * df[col].notna().mean(), **m})
    tabela = pd.DataFrame(linhas)
    deltas = None
    if referencia:
        ref_col, ref_dir = scores[referencia]
        partes = []
        for rot, (col, alto_risco) in scores.items():
            if rot == referencia:
                continue
            d = delta_pareado(comum[col_y], orienta(comum[ref_col], ref_dir),
                              orienta(comum[col], alto_risco), **kw)
            partes.append(d.assign(score=rot, contra=referencia))
        deltas = pd.concat(partes, ignore_index=True) if partes else None
    return tabela, deltas


# --------------------------------------------------------------------------- #
# Decis e estabilidade
# --------------------------------------------------------------------------- #
def cortes_decis(risco_treino, n: int = 10) -> np.ndarray:
    """Cortes de quantil calculados **só no treino**; reaplique os mesmos em toda amostra."""
    r = np.asarray(risco_treino, dtype="float64")
    cortes = np.unique(np.nanquantile(r, np.linspace(0, 1, n + 1)[1:-1]))
    return cortes


def aplica_decis(risco, cortes: np.ndarray) -> np.ndarray:
    """Faixa 1 = menor risco. Valores nulos ficam na faixa 0 (sem score / sem cobertura)."""
    r = np.asarray(risco, dtype="float64")
    faixa = np.searchsorted(cortes, r, side="right") + 1
    return np.where(np.isnan(r), 0, faixa)


def estabilidade_decis(df: pd.DataFrame, col_safra: str, col_faixa: str,
                       col_y: str | None = None) -> dict[str, pd.DataFrame]:
    """Composição (% da safra em cada faixa) e, com `col_y`, bad rate faixa × safra."""
    comp = pd.crosstab(df[col_safra], df[col_faixa], normalize="index") * 100
    out = {"composicao_pct": comp}
    if col_y:
        out["bad_rate_pct"] = df.pivot_table(index=col_safra, columns=col_faixa, values=col_y,
                                             aggfunc="mean") * 100
    return out


def psi(esperado, atual, n_bins: int = 10, especiais: Sequence[float] = (),
        eps: float = 1e-6) -> float:
    """PSI com cortes do esperado, uma faixa para nulos e uma para cada valor especial.

    Diferente das versões que descartam os nulos: as proporções somam 1 dos dois lados,
    então um aumento de preenchimento nulo aparece no PSI.
    """
    def faixas(x, cortes):
        x = np.asarray(x, dtype="float64")
        f = np.searchsorted(cortes, x, side="right").astype(object)
        for v in especiais:
            f[x == v] = f"esp_{v}"
        f[np.isnan(x)] = "nulo"
        return pd.Series(f).astype(str)

    e = np.asarray(esperado, dtype="float64")
    regular = e[~np.isnan(e) & ~np.isin(e, especiais)]
    cortes = np.unique(np.quantile(regular, np.linspace(0, 1, n_bins + 1)[1:-1])) if len(regular) else np.array([])
    pe = faixas(e, cortes).value_counts(normalize=True)
    pa = faixas(atual, cortes).value_counts(normalize=True)
    todas = pe.index.union(pa.index)
    pe = pe.reindex(todas, fill_value=0).clip(lower=eps)
    pa = pa.reindex(todas, fill_value=0).clip(lower=eps)
    return float(((pa - pe) * np.log(pa / pe)).sum())


# --------------------------------------------------------------------------- #
# Swap
# --------------------------------------------------------------------------- #
def _ic_jeffreys(k, n, alpha=0.05):
    from scipy.stats import beta
    if n == 0:
        return np.nan, np.nan
    return (beta.ppf(alpha / 2, k + 0.5, n - k + 0.5) if k > 0 else 0.0,
            beta.ppf(1 - alpha / 2, k + 0.5, n - k + 0.5) if k < n else 1.0)


def swap(df: pd.DataFrame, col_y: str, risco_atual: str, risco_novo: str,
         taxa_aprovacao: float) -> pd.DataFrame:
    """Quadrantes de swap com a **mesma taxa de aprovação** nos dois scores.

    Cada score aprova os `taxa_aprovacao` de menor risco desta população. Quadrantes:
    mantidos, swap_in (o novo aprova, o atual reprovava), swap_out (o atual aprovava,
    o novo reprova) e reprovados. A bad rate (com IC de Jeffreys) só usa linhas com
    alvo; `n_sem_alvo` mostra quanto do quadrante não tem performance (reprovados no
    passado ou imaturos): ali a bad rate é desconhecida, não zero.
    """
    d = df[[col_y, risco_atual, risco_novo]].dropna(subset=[risco_atual, risco_novo])
    corte_a = d[risco_atual].quantile(taxa_aprovacao)
    corte_n = d[risco_novo].quantile(taxa_aprovacao)
    ap_a = d[risco_atual] <= corte_a
    ap_n = d[risco_novo] <= corte_n
    quadrante = np.select([ap_a & ap_n, ~ap_a & ap_n, ap_a & ~ap_n],
                          ["mantidos", "swap_in", "swap_out"], default="reprovados")
    linhas = []
    for q in ["mantidos", "swap_in", "swap_out", "reprovados"]:
        sub = d[quadrante == q]
        y = sub[col_y].dropna()
        k, n = int(y.sum()), int(len(y))
        lo, hi = _ic_jeffreys(k, n)
        linhas.append({"quadrante": q, "n": len(sub), "pct": 100 * len(sub) / len(d),
                       "n_com_alvo": n, "n_sem_alvo": len(sub) - n,
                       "bad_rate": k / n if n else np.nan, "ic_low": lo, "ic_high": hi})
    tab = pd.DataFrame(linhas)
    for nome, mask in (("carteira_atual", ap_a), ("carteira_nova", ap_n)):
        y = d.loc[mask, col_y].dropna()
        lo, hi = _ic_jeffreys(int(y.sum()), len(y))
        tab = pd.concat([tab, pd.DataFrame([{"quadrante": nome, "n": int(mask.sum()),
                                             "pct": 100 * mask.mean(), "n_com_alvo": len(y),
                                             "n_sem_alvo": int(mask.sum()) - len(y),
                                             "bad_rate": y.mean() if len(y) else np.nan,
                                             "ic_low": lo, "ic_high": hi}])], ignore_index=True)
    return tab


# --------------------------------------------------------------------------- #
# LightGBM: tuning temporal, refit e poda
# --------------------------------------------------------------------------- #
ESPACO_PADRAO = {
    "max_depth": ("int", 3, 5),
    "num_leaves_frac": ("float", 0.3, 0.9),          # num_leaves = max(4, frac · 2^max_depth)
    "learning_rate": ("log", 0.01, 0.1),
    "min_child_samples": ("log_int", 200, 2000),
    "subsample": ("float", 0.5, 0.9),
    "colsample_bytree": ("float", 0.4, 0.9),
    "min_split_gain": ("log", 1e-3, 0.5),
    "reg_alpha": ("log", 1e-3, 10.0),
    "reg_lambda": ("log", 1e-3, 10.0),
}


@dataclass
class ResultadoTuning:
    params: dict
    best_iter: int
    auc_opt: float
    auc_val: float
    gap: float
    trials: pd.DataFrame
    aviso: str = ""
    estudo: object = field(default=None, repr=False)


def _sugere(trial, espaco):
    p = {}
    for nome, (tipo, lo, hi) in espaco.items():
        if tipo == "int":
            p[nome] = trial.suggest_int(nome, lo, hi)
        elif tipo == "log_int":
            p[nome] = trial.suggest_int(nome, lo, hi, log=True)
        elif tipo == "log":
            p[nome] = trial.suggest_float(nome, lo, hi, log=True)
        else:
            p[nome] = trial.suggest_float(nome, lo, hi)
    frac = p.pop("num_leaves_frac", None)
    if frac is not None:
        p["num_leaves"] = max(4, int(frac * 2 ** p["max_depth"]))
    return p


def tune_lgbm_temporal(X_opt, y_opt, X_val, y_val, n_trials: int = 80, gap_max: float = 0.05,
                       seed: int = SEED, espaco: Mapping | None = None, monotone: Sequence[int] | None = None,
                       peso_opt=None, max_arvores: int = 4000, paciencia: int = 100,
                       trials_iniciais: Sequence[dict] = ()) -> ResultadoTuning:
    """Optuna (TPE com seed) treinando em OPT e validando em VAL, uma janela **temporal**
    posterior ao OPT dentro do desenvolvimento.

    - objetivo: AUC em VAL; o vencedor é o de maior AUC_val entre os trials com
      gap (AUC_opt − AUC_val) ≤ `gap_max` (filtro, não penalidade). Sem nenhum trial
      dentro do gap, fica o de menor gap e `aviso` diz isso;
    - early stopping em VAL define `best_iter`. VAL escolhe iteração e trial, então o
      AUC_val é **otimista**: o número honesto é o do OOT, que não entra aqui;
    - `trials_iniciais` (warm start) só com parâmetros do **mesmo** espaço.
    """
    import lightgbm as lgb
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    espaco = dict(espaco or ESPACO_PADRAO)

    def objetivo(trial):
        p = _sugere(trial, espaco)
        m = lgb.LGBMClassifier(n_estimators=max_arvores, subsample_freq=1, random_state=seed,
                               deterministic=True, force_row_wise=True, verbose=-1,
                               monotone_constraints=list(monotone) if monotone is not None else None, **p)
        m.fit(X_opt, y_opt, sample_weight=peso_opt, eval_set=[(X_val, y_val)], eval_metric="auc",
              callbacks=[lgb.early_stopping(paciencia, verbose=False)])
        it = int(m.best_iteration_ or max_arvores)
        auc_val = roc_auc_score(y_val, m.predict_proba(X_val, num_iteration=it)[:, 1])
        auc_opt = roc_auc_score(y_opt, m.predict_proba(X_opt, num_iteration=it)[:, 1])
        trial.set_user_attr("auc_opt", auc_opt)
        trial.set_user_attr("gap", auc_opt - auc_val)
        trial.set_user_attr("best_iter", it)
        trial.set_user_attr("params", p)
        return auc_val

    estudo = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    for t in trials_iniciais:
        estudo.enqueue_trial(t)
    estudo.optimize(objetivo, n_trials=n_trials)
    tab = pd.DataFrame([{"trial": t.number, "auc_val": t.value, **{k: t.user_attrs[k] for k in ("auc_opt", "gap", "best_iter")}}
                        for t in estudo.trials if t.value is not None])
    dentro = tab[tab["gap"] <= gap_max]
    aviso = ""
    if dentro.empty:
        melhor = tab.sort_values("gap").iloc[0]
        aviso = f"nenhum trial com gap <= {gap_max}; usado o de menor gap ({melhor['gap']:.3f})"
    else:
        melhor = dentro.sort_values("auc_val", ascending=False).iloc[0]
    t = estudo.trials[int(melhor["trial"])]
    return ResultadoTuning(params=t.user_attrs["params"], best_iter=int(melhor["best_iter"]),
                           auc_opt=float(melhor["auc_opt"]), auc_val=float(melhor["auc_val"]),
                           gap=float(melhor["gap"]), trials=tab, aviso=aviso, estudo=estudo)


def refit_final(res: ResultadoTuning, X, y, seed: int = SEED, monotone: Sequence[int] | None = None,
                peso=None):
    """Refit em OPT ∪ VAL com `n_estimators = best_iter` fixo e **sem** early stopping:
    não sobra amostra para parar, e parar no próprio treino não regulariza nada."""
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=res.best_iter, subsample_freq=1, random_state=seed,
                           deterministic=True, force_row_wise=True, verbose=-1,
                           monotone_constraints=list(monotone) if monotone is not None else None, **res.params)
    return m.fit(X, y, sample_weight=peso)


def importancia_acumulada(modelo, X_amostra: pd.DataFrame | None = None, metodo: str = "shap") -> pd.DataFrame:
    """Importância por feature e acumulado (%). `metodo="shap"` usa mean|SHAP| numa
    amostra (precisa de `X_amostra`, ~30 mil linhas bastam); `"gain"` usa o gain do
    booster. Nada de editar a amostra antes (trocar extremos por NaN muda o ramo das
    árvores e mede outro modelo)."""
    nomes = list(modelo.feature_name_)
    if metodo == "gain":
        imp = modelo.booster_.feature_importance(importance_type="gain")
    else:
        import shap
        sv = shap.TreeExplainer(modelo).shap_values(X_amostra[nomes])
        sv = sv[1] if isinstance(sv, list) else sv
        imp = np.abs(sv).mean(axis=0)
    t = pd.DataFrame({"feature": nomes, "importancia": imp}).sort_values("importancia", ascending=False)
    t["acumulado_pct"] = 100 * t["importancia"].cumsum() / t["importancia"].sum()
    return t.reset_index(drop=True)


def shap_por_faixa(modelo, X_amostra: pd.DataFrame, feature: str, sentinelas: Sequence[float] = (),
                   n_quartis: int = 4) -> pd.DataFrame:
    """SHAP médio da feature por faixa: quartis do domínio válido, nulo e cada sentinela.

    Checagem de plausibilidade quando não há `monotone`: o efeito deve andar na direção
    esperada ao longo dos quartis, e cada sentinela ("sem histórico", "sem evento")
    deve empurrar o risco para um lado que faça sentido de negócio.
    """
    import shap
    nomes = list(modelo.feature_name_)
    sv = shap.TreeExplainer(modelo).shap_values(X_amostra[nomes])
    sv = sv[1] if isinstance(sv, list) else sv
    x = X_amostra[feature]
    s = pd.Series(sv[:, nomes.index(feature)], index=X_amostra.index)
    valido = x.notna() & ~x.isin(sentinelas)
    faixa = pd.Series("", index=x.index, dtype=object)
    ordem: list[str] = []
    if valido.any():
        q = pd.qcut(x[valido], n_quartis, duplicates="drop")
        faixa[valido] = q.astype(str)
        ordem += [str(c) for c in q.cat.categories]
    faixa[x.isna()] = "nulo"
    for v in sentinelas:
        faixa[x == v] = f"sentinela {v}"
    ordem += ["nulo"] + [f"sentinela {v}" for v in sentinelas]
    t = (pd.DataFrame({"faixa": faixa, "shap": s}).groupby("faixa")["shap"]
           .agg(n="size", shap_medio="mean"))
    return t.reindex([o for o in ordem if o in t.index]).reset_index()


def corta_por_importancia(tab: pd.DataFrame, pct: float) -> list[str]:
    """Features até `pct`% da importância acumulada (inclui a que cruza o limiar)."""
    n = int((tab["acumulado_pct"] < pct).sum()) + 1
    return tab["feature"].head(n).tolist()


# --------------------------------------------------------------------------- #
# Produção: contrato, ONNX, validação de entrada, ledger
# --------------------------------------------------------------------------- #
def _hash_dados(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()[:16]


def _versoes() -> dict:
    v = {"python": platform.python_version()}
    for mod in ("numpy", "pandas", "sklearn", "lightgbm", "onnxruntime", "yggdrasil"):
        try:
            v[mod] = __import__(mod).__version__
        except Exception:
            pass
    return v


def contrato_modelo(modelo, X_treino: pd.DataFrame, features: Sequence[str], alvo: str,
                    sentinelas: Sequence[float] = (), janelas: Mapping | None = None,
                    metricas_: Mapping | None = None, commit: str | None = None,
                    extras: Mapping | None = None) -> dict:
    """Documento do modelo para produção e monitoramento.

    Por feature, na ordem de entrada: dtype, % nulo, % de cada sentinela e percentis do
    domínio válido no treino (baseline de drift). Mais: alvo, janelas, métricas, params,
    versões das bibliotecas, hash **do conteúdo** do treino e commit do código.
    """
    schema = []
    for i, f in enumerate(features):
        x = X_treino[f]
        valido = x[x.notna() & ~x.isin(sentinelas)]
        schema.append({
            "posicao": i, "nome": f, "dtype": str(x.dtype),
            "pct_nulo": round(100 * x.isna().mean(), 4),
            "pct_sentinela": {str(s): round(100 * (x == s).mean(), 4) for s in sentinelas if (x == s).any()},
            "percentis": {f"p{int(q * 100):02d}": float(valido.quantile(q))
                          for q in (0.01, 0.25, 0.5, 0.75, 0.99)} if len(valido) else {},
        })
    params = modelo.get_params() if hasattr(modelo, "get_params") else {}
    return {
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "alvo": alvo,
        "n_treino": int(len(X_treino)),
        "hash_treino": _hash_dados(X_treino[list(features)]),
        "features": schema,
        "sentinelas": list(sentinelas),
        "janelas": dict(janelas or {}),
        "metricas": dict(metricas_ or {}),
        "params": {k: v for k, v in params.items() if isinstance(v, (int, float, str, bool, type(None)))},
        "versoes": _versoes(),
        "commit": commit,
        **dict(extras or {}),
    }


def salva_contrato(contrato: dict, caminho: str | Path) -> Path:
    p = Path(caminho)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(contrato, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def valida_entrada(df: pd.DataFrame, contrato: dict) -> pd.DataFrame:
    """Devolve as colunas na ordem do contrato. Falha se faltar feature ou se o tipo não
    for numérico; não preenche nada (preencher feature ausente com sentinela em silêncio
    é pontuar outro cliente)."""
    nomes = [f["nome"] for f in contrato["features"]]
    faltando = [n for n in nomes if n not in df.columns]
    if faltando:
        raise KeyError(f"features ausentes na entrada: {faltando}")
    nao_num = [n for n in nomes if not pd.api.types.is_numeric_dtype(df[n])]
    if nao_num:
        raise TypeError(f"features não numéricas: {nao_num}")
    return df[nomes]


def exporta_onnx(modelo, X_amostra: pd.DataFrame, caminho: str | Path, tol_prob: float = 1e-4,
                 cortes: Sequence[float] = ()) -> dict:
    """Converte um LGBMClassifier para ONNX e confere ida e volta na amostra.

    Falha se a maior diferença de probabilidade passar de `tol_prob` ou se algum corte de
    decisão (`cortes`, em probabilidade) aprovar linhas diferentes. Spearman alto não
    basta: uma diferença pequena perto do corte muda a decisão. Grava também
    `<caminho>.io.json` com a ordem das features.
    """
    import onnxmltools
    import onnxruntime as ort
    from onnxmltools.convert.common.data_types import FloatTensorType

    nomes = list(modelo.feature_name_)
    onx = onnxmltools.convert_lightgbm(modelo, initial_types=[("entrada", FloatTensorType([None, len(nomes)]))],
                                       zipmap=False, target_opset=15)
    p = Path(caminho)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(onx.SerializeToString())
    opcoes = ort.SessionOptions()
    opcoes.log_severity_level = 3          # o aviso de shape da saída "label" não afeta a probabilidade
    sess = ort.InferenceSession(str(p), sess_options=opcoes, providers=["CPUExecutionProvider"])
    X32 = X_amostra[nomes].to_numpy(dtype="float32")
    prob_onnx = sess.run(None, {"entrada": X32})[1][:, 1]
    prob_lgb = modelo.predict_proba(X_amostra[nomes])[:, 1]
    diff = float(np.max(np.abs(prob_onnx - prob_lgb)))
    decisoes_diferentes = {str(c): int(((prob_onnx <= c) != (prob_lgb <= c)).sum()) for c in cortes}
    rel = {"caminho": str(p), "n_amostra": int(len(X32)), "diff_max_prob": diff, "tol_prob": tol_prob,
           "decisoes_diferentes": decisoes_diferentes}
    Path(str(p) + ".io.json").write_text(json.dumps({"entrada": "entrada", "features": nomes,
                                                     "dtype": "float32", "saida_prob": 1}, indent=2),
                                         encoding="utf-8")
    if diff > tol_prob or any(decisoes_diferentes.values()):
        raise AssertionError(f"ONNX diverge do LightGBM: {rel}")
    return rel


def registra_run(ledger: str | Path, **campos) -> pd.DataFrame:
    """Acrescenta uma linha ao CSV de experimentos (cria se não existir) e devolve o
    ledger. Registre toda variante avaliada no OOT: o número de comparações feitas no
    OOT é o que diz quanto ele ainda vale como teste."""
    p = Path(ledger)
    linha = pd.DataFrame([{"registrado_em": datetime.now().isoformat(timespec="seconds"),
                           **{k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
                              for k, v in campos.items()}}])
    if p.exists():
        linha = pd.concat([pd.read_csv(p), linha], ignore_index=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    linha.to_csv(p, index=False)
    return linha
