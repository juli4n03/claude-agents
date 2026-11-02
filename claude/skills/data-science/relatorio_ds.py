"""relatorio_ds: relatórios PDF de modelo e de variáveis, sobre as funções do kit_ds.

- `relatorio_pdf`: avaliação do modelo (métricas por amostra e alvo, benchmarks na mesma
  população, curvas, decis, estabilidade por safra, segmentos, calibração, SHAP, PSI,
  swap). Gere **depois** da escolha final: o OOT entra no relatório, não nas decisões.
- `relatorio_variaveis_pdf`: uma página por feature com a distribuição por safra (faixas
  do treino + nulo + cada sentinela), bad rate por faixa e PSI por safra.

Todo número sai rotulado com a amostra de onde veio. Convenção: risco alto = mais risco.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

import kit_ds as k

ROTULOS_PADRAO = {"DES": "in-sample", "OPT": "in-sample", "VAL": "seleção (otimista)",
                  "TESTE": "teste in-time", "OOT": "fora do tempo"}
CORES = ["#1f4e79", "#c55a11", "#548235", "#7f6000", "#7030a0", "#2e75b6", "#a5a5a5", "#bf9000"]
PSI_ATENCAO, PSI_INSTAVEL = 0.10, 0.25
LINHAS_POR_PAGINA = 28


# --------------------------------------------------------------------------- #
# Estatísticas usadas no relatório
# --------------------------------------------------------------------------- #
def hosmer_lemeshow(y, prob, n_grupos: int = 10) -> dict:
    """Teste de Hosmer-Lemeshow com grupos por quantil da probabilidade prevista."""
    from scipy.stats import chi2
    d = pd.DataFrame({"y": np.asarray(y, dtype="float64"), "p": np.asarray(prob, dtype="float64")}).dropna()
    d["g"] = pd.qcut(d["p"], n_grupos, labels=False, duplicates="drop")
    t = d.groupby("g").agg(n=("y", "size"), obs=("y", "sum"), esp=("p", "sum"))
    pm = t["esp"] / t["n"]
    stat = float((((t["obs"] - t["esp"]) ** 2) / (t["n"] * pm * (1 - pm)).clip(lower=1e-12)).sum())
    gl = max(len(t) - 2, 1)
    return {"estatistica": stat, "gl": gl, "p_valor": float(chi2.sf(stat, gl)), "grupos": len(t)}


def tabela_calibracao(y, prob, cortes) -> pd.DataFrame:
    """Previsto médio × observado por faixa de risco (cortes do treino), com IC de Jeffreys."""
    d = pd.DataFrame({"y": np.asarray(y, dtype="float64"), "p": np.asarray(prob, dtype="float64")}).dropna()
    d["faixa"] = k.aplica_decis(d["p"], cortes)
    linhas = []
    for f, g in d.groupby("faixa"):
        lo, hi = k._ic_jeffreys(int(g["y"].sum()), len(g))
        linhas.append({"faixa": int(f), "n": len(g), "previsto": g["p"].mean(),
                       "observado": g["y"].mean(), "ic_low": lo, "ic_high": hi})
    return pd.DataFrame(linhas)


def curva_aprovacao(y, risco, pontos: int = 50) -> pd.DataFrame:
    """Bad rate da carteira aprovada aprovando os x% de menor risco."""
    d = pd.DataFrame({"y": np.asarray(y, dtype="float64"), "r": np.asarray(risco, dtype="float64")}).dropna()
    d = d.sort_values("r", kind="mergesort")
    acum = d["y"].cumsum().to_numpy() / np.arange(1, len(d) + 1)
    taxas = np.linspace(0.02, 1, pontos)
    idx = np.clip((taxas * len(d)).astype(int) - 1, 0, len(d) - 1)
    return pd.DataFrame({"taxa_aprovacao": taxas, "bad_rate_aprovados": acum[idx]})


# --------------------------------------------------------------------------- #
# Páginas genéricas (Figure direto, sem pyplot: funciona sem interface gráfica e não
# abre nem acumula janelas no notebook)
# --------------------------------------------------------------------------- #
def _figura(nrows: int = 1, ncols: int = 1, figsize=(11.7, 8.3), **kw):
    from matplotlib.figure import Figure
    fig = Figure(figsize=figsize)
    return fig, fig.subplots(nrows, ncols, **kw)


def _cmap(nome):
    import matplotlib
    return matplotlib.colormaps[nome]


def _beeswarm(fig, ax, sv, X: pd.DataFrame, max_display: int = 20, sentinelas: Sequence[float] = (), seed: int = 0):
    """Beeswarm de SHAP: cor = percentil do valor no domínio válido; cinza = nulo/sentinela."""
    imp = np.abs(sv).mean(axis=0)
    ordem = list(np.argsort(imp)[::-1][:max_display])[::-1]          # mais importante em cima
    rng = np.random.default_rng(seed)
    sc = None
    for pos, j in enumerate(ordem):
        x = X.iloc[:, j]
        s = sv[:, j]
        valido = (x.notna() & ~x.isin(sentinelas)).to_numpy()
        y = pos + rng.uniform(-0.3, 0.3, len(s))
        ax.scatter(s[~valido], y[~valido], s=3, c="#a5a5a5", alpha=0.5)
        if valido.any():
            cor = x[valido].rank(pct=True).to_numpy()
            sc = ax.scatter(s[valido], y[valido], s=3, c=cor, cmap="coolwarm", vmin=0, vmax=1, alpha=0.6)
    ax.set_yticks(range(len(ordem)))
    ax.set_yticklabels([X.columns[j] for j in ordem], fontsize=8)
    ax.axvline(0, color="grey", lw=0.8)
    ax.set_xlabel("SHAP (impacto no log-odds de mau)")
    if sc is not None:
        fig.colorbar(sc, ax=ax, label="percentil do valor (cinza = nulo/sentinela)")
def _milhar(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def _fmt(v):
    if isinstance(v, (float, np.floating)):
        if np.isnan(v):
            return ""
        if abs(v) < 10:
            return f"{v:.4f}"
        return f"{v:.1f}" if abs(v) < 1000 else _milhar(round(v))
    if isinstance(v, (int, np.integer)):
        return _milhar(v)
    return str(v)


def _pagina_tabela(pdf, df: pd.DataFrame, titulo: str, nota: str = ""):
    if df is None or df.empty:
        df = pd.DataFrame({"aviso": ["sem dados para esta tabela"]})
    for ini in range(0, len(df), LINHAS_POR_PAGINA):
        parte = df.iloc[ini:ini + LINHAS_POR_PAGINA]
        fig, ax = _figura()
        ax.axis("off")
        sufixo = f" ({ini // LINHAS_POR_PAGINA + 1})" if len(df) > LINHAS_POR_PAGINA else ""
        ax.set_title(titulo + sufixo, loc="left", fontsize=13, fontweight="bold")
        tab = ax.table(cellText=[[_fmt(v) for v in linha] for linha in parte.itertuples(index=False)],
                       colLabels=[str(c) for c in parte.columns], loc="upper center", cellLoc="center")
        tab.auto_set_font_size(False)
        tab.set_fontsize(8)
        tab.scale(1, 1.25)
        for (r, _), cel in tab.get_celld().items():
            if r == 0:
                cel.set_facecolor("#d9e1f2")
                cel.set_text_props(fontweight="bold")
        if nota:
            fig.text(0.02, 0.02, nota, fontsize=8, style="italic", wrap=True)
        pdf.savefig(fig)


def _pagina_texto(pdf, titulo: str, linhas: Sequence[str]):
    from matplotlib.figure import Figure
    fig = Figure(figsize=(11.7, 8.3))
    fig.text(0.05, 0.92, titulo, fontsize=18, fontweight="bold")
    y = 0.85
    for l in linhas:
        fig.text(0.05, y, l, fontsize=10, family="monospace")
        y -= 0.032
        if y < 0.05:
            break
    pdf.savefig(fig)


# --------------------------------------------------------------------------- #
# Relatório do modelo
# --------------------------------------------------------------------------- #
def relatorio_pdf(caminho: str | Path, df: pd.DataFrame, col_y: str, col_risco: str,
                  col_amostra: str, col_safra: str, *, titulo: str = "Relatório do modelo",
                  info: Mapping | None = None, amostra_treino: str = "DES", amostra_foco: str | None = None,
                  rotulos: Mapping[str, str] | None = None, risco_eh_probabilidade: bool = True,
                  benchmarks: Mapping[str, tuple[str, bool]] | None = None,
                  alvos_alternativos: Sequence[str] = (), col_segmento: str | None = None,
                  modelo=None, features: Sequence[str] | None = None, sentinelas: Sequence[float] = (),
                  n_shap: int = 5000, top_features: int = 12,
                  swap_contra: str | None = None, taxa_aprovacao: float | None = None,
                  n_min: int = 50, seed: int = k.SEED) -> dict:
    """Gera o PDF de avaliação e devolve `{"caminho", "n_paginas", "tabelas"}`.

    `df`: uma linha por observação de todas as amostras, com `col_risco` já orientado
    (alto = mais risco; probabilidade se `risco_eh_probabilidade`). `benchmarks`:
    `{rotulo: (coluna, maior_eh_risco)}`. `modelo` + `features`: liga as páginas de SHAP
    (LightGBM/árvores). `swap_contra`: rótulo de um benchmark para o swap em
    `taxa_aprovacao`. Decis sempre com cortes de `amostra_treino`.
    """
    import matplotlib
    from matplotlib.backends.backend_pdf import PdfPages
    from sklearn.metrics import roc_curve

    rotulos = {**ROTULOS_PADRAO, **dict(rotulos or {})}
    benchmarks = dict(benchmarks or {})
    amostras = [a for a in ["DES", "OPT", "VAL", "TESTE", "OOT"] if a in set(df[col_amostra])] + \
               [a for a in pd.unique(df[col_amostra]) if a not in {"DES", "OPT", "VAL", "TESTE", "OOT"}]
    foco = amostra_foco or ("OOT" if "OOT" in amostras else amostras[-1])
    treino = df[df[col_amostra] == amostra_treino]
    cortes = k.cortes_decis(treino[col_risco], 10)
    tabelas: dict[str, pd.DataFrame] = {}
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)

    with PdfPages(caminho) as pdf, matplotlib.rc_context({"axes.prop_cycle": matplotlib.cycler(color=CORES),
                                                          "axes.grid": True, "grid.alpha": 0.3}):
        # 1. capa
        linhas = [f"gerado em: {datetime.now():%Y-%m-%d %H:%M}", f"alvo: {col_y}",
                  f"amostra de treino (cortes de decil): {amostra_treino}", f"amostra em foco: {foco}",
                  "amostras: " + "; ".join(f"{a} ({rotulos.get(a, '?')}, n={_milhar((df[col_amostra] == a).sum())})"
                                            for a in amostras), ""]
        linhas += [f"{kk}: {v}" for kk, v in dict(info or {}).items()]
        if features is not None:
            linhas += ["", f"features ({len(features)}): " + ", ".join(list(features)[:40]) + (" ..." if len(features) > 40 else "")]
        linhas += ["", "Leitura: VAL escolheu iteração/trial/poda (otimista). OOT não decidiu nada:",
                   "é o número honesto. Benchmarks comparados na população comum com direção fixa."]
        _pagina_texto(pdf, titulo, linhas)

        # 2. métricas por amostra e alvo
        linhas_m = []
        for alvo in [col_y, *alvos_alternativos]:
            for a in amostras:
                sub = df[df[col_amostra] == a][[alvo, col_risco]].dropna()
                if sub.empty or sub[alvo].nunique() < 2:
                    continue
                m = k.metricas(sub[alvo], sub[col_risco])
                linhas_m.append({"alvo": alvo, "amostra": a, "tipo": rotulos.get(a, "?"), **m})
        tabelas["metricas"] = pd.DataFrame(linhas_m)
        _pagina_tabela(pdf, tabelas["metricas"], "Métricas por amostra e alvo",
                       "Alvos alternativos só avaliados, nunca otimizados. VAL: seleção (otimista).")

        # 3. benchmarks na mesma população, por amostra e por safra
        if benchmarks:
            scores = {"modelo": (col_risco, True), **benchmarks}
            linhas_b = []
            for a in amostras:
                sub = df[df[col_amostra] == a]
                try:
                    t, _ = k.compara_scores(sub, col_y, scores)
                except ValueError:
                    continue
                linhas_b += [{"amostra": a, **r} for r in t.to_dict("records")]
            tabelas["benchmarks"] = pd.DataFrame(linhas_b)
            _pagina_tabela(pdf, tabelas["benchmarks"], "Modelo × benchmarks (população comum)",
                           "Mesmas linhas para todos os scores em cada amostra; cobertura_pct na base inteira.")
            comum = df.dropna(subset=[col_y, col_risco, *[c for c, _ in benchmarks.values()]])
            fig, axes = _figura(1, 2, figsize=(11.7, 5.5))
            for rot, (col, alto_risco) in scores.items():
                tmp = comum.assign(_r=k.orienta(comum[col], alto_risco))
                g = k.metricas_por_grupo(tmp, col_y, "_r", col_safra, n_min=n_min).sort_values("grupo")
                axes[0].plot(g["grupo"].astype(str), g["auc"], marker="o", label=rot)
                axes[1].plot(g["grupo"].astype(str), g["ks"], marker="o", label=rot)
            safras_ord = sorted(comum[col_safra].astype(str).unique())
            amostra_safra = comum.assign(_s=comum[col_safra].astype(str)).groupby("_s")[col_amostra].agg(lambda x: x.mode().iloc[0])
            for ax in axes:
                for i, s_ in enumerate(safras_ord):
                    a = amostra_safra.get(s_)
                    if a in ("VAL", "OOT", "TESTE"):
                        ax.axvspan(i - 0.5, i + 0.5, color={"VAL": "#fff2cc", "OOT": "#fce4d6", "TESTE": "#e2efda"}[a], zorder=0)
            for ax, nome in zip(axes, ["AUC", "KS"]):
                ax.set_title(f"{nome} por safra (população comum)")
                ax.tick_params(axis="x", rotation=90, labelsize=7)
                ax.legend(fontsize=8)
            fig.text(0.01, 0.01, "fundo: amarelo = VAL, salmão = OOT, verde = TESTE, branco = DES", fontsize=8, style="italic")
            fig.tight_layout(rect=(0, 0.03, 1, 1))
            pdf.savefig(fig)

        # 4. métricas por safra (modelo, população própria)
        tabelas["por_safra"] = k.metricas_por_grupo(df, col_y, col_risco, col_safra, n_min=n_min) \
                                .rename(columns={"grupo": "safra"})
        amostra_da_safra = df.groupby(col_safra)[col_amostra].agg(lambda s: "/".join(sorted(set(s))))
        tabelas["por_safra"]["amostra"] = tabelas["por_safra"]["safra"].map(amostra_da_safra)
        _pagina_tabela(pdf, tabelas["por_safra"], "Modelo por safra",
                       f"Safra com menos de {n_min} maus ou bons sai sem métrica.")

        # 5. curvas ROC, KS e ganho na amostra em foco
        sub = df[df[col_amostra] == foco][[col_y, col_risco]].dropna()
        if sub[col_y].nunique() == 2:
            fpr, tpr, _ = roc_curve(sub[col_y], sub[col_risco])
            ordem = np.argsort(-sub[col_risco].to_numpy(), kind="mergesort")
            yo = sub[col_y].to_numpy()[ordem]
            pop = np.arange(1, len(yo) + 1) / len(yo)
            maus = np.cumsum(yo) / max(yo.sum(), 1)
            bons = np.cumsum(1 - yo) / max((1 - yo).sum(), 1)
            fig, axes = _figura(1, 3, figsize=(11.7, 4.6))
            axes[0].plot(fpr, tpr); axes[0].plot([0, 1], [0, 1], "--", color="grey")
            axes[0].set(title=f"ROC ({foco})", xlabel="FPR (bons reprovados)", ylabel="TPR (maus pegos)")
            axes[1].plot(pop, maus, label="maus acumulados"); axes[1].plot(pop, bons, label="bons acumulados")
            i = int(np.argmax(maus - bons))
            axes[1].vlines(pop[i], bons[i], maus[i], colors="black", linestyles=":")
            axes[1].set(title=f"KS = {maus[i] - bons[i]:.3f} ({foco})", xlabel="população ordenada por risco (maior primeiro)")
            axes[1].legend(fontsize=8)
            axes[2].plot(pop, maus); axes[2].plot([0, 1], [0, 1], "--", color="grey")
            axes[2].set(title=f"Ganho ({foco})", xlabel="população ordenada por risco", ylabel="% dos maus")
            fig.tight_layout()
            pdf.savefig(fig)

        # 6. decis e carteira aprovada
        df_f = df.assign(_faixa=k.aplica_decis(df[col_risco], cortes))
        tabelas["decis"] = (df_f.groupby([col_amostra, "_faixa"])[col_y].agg(n="size", bad_rate="mean")
                               .reset_index().rename(columns={"_faixa": "faixa", col_amostra: "amostra"}))
        fig, axes = _figura(1, 2, figsize=(11.7, 5))
        for a in amostras:
            t = tabelas["decis"][tabelas["decis"]["amostra"] == a]
            axes[0].plot(t["faixa"], 100 * t["bad_rate"], marker="o", label=f"{a} ({rotulos.get(a, '?')})")
        axes[0].set(title=f"Bad rate por faixa (cortes de {amostra_treino})", xlabel="faixa (1 = menor risco; 0 = sem score)", ylabel="bad rate %")
        axes[0].legend(fontsize=8)
        for a in amostras:
            s = df[df[col_amostra] == a]
            c = curva_aprovacao(s[col_y], s[col_risco])
            axes[1].plot(100 * c["taxa_aprovacao"], 100 * c["bad_rate_aprovados"], label=a)
        axes[1].set(title="Bad rate da carteira aprovada", xlabel="taxa de aprovação %", ylabel="bad rate aprovados %")
        axes[1].legend(fontsize=8)
        fig.tight_layout()
        pdf.savefig(fig)

        # 7. estabilidade dos decis por safra
        est = k.estabilidade_decis(df_f, col_safra, "_faixa", col_y)
        tabelas["composicao_decis"] = est["composicao_pct"]
        fig, axes = _figura(1, 2, figsize=(11.7, 5.5))
        comp = est["composicao_pct"]
        base = np.zeros(len(comp))
        for j, col in enumerate(comp.columns):
            axes[0].bar(comp.index.astype(str), comp[col], bottom=base, color=_cmap("RdYlGn_r")(j / max(len(comp.columns) - 1, 1)), label=str(col))
            base += comp[col].to_numpy()
        axes[0].set(title="Composição das faixas por safra (%)")
        axes[0].tick_params(axis="x", rotation=90, labelsize=7)
        axes[0].legend(fontsize=6, ncol=1, title="faixa", loc="upper left", bbox_to_anchor=(1.0, 1.0))
        br = est["bad_rate_pct"]
        im = axes[1].imshow(br.T.to_numpy(dtype=float), aspect="auto", cmap="RdYlGn_r", origin="lower")
        axes[1].set_xticks(range(len(br.index))); axes[1].set_xticklabels(br.index.astype(str), rotation=90, fontsize=7)
        axes[1].set_yticks(range(len(br.columns))); axes[1].set_yticklabels(br.columns.astype(str))
        axes[1].set(title="Bad rate % faixa × safra", ylabel="faixa")
        fig.colorbar(im, ax=axes[1])
        fig.tight_layout()
        pdf.savefig(fig)

        # 8. segmentos
        if col_segmento:
            sub = df[df[col_amostra] == foco]
            tabelas["segmentos"] = k.metricas_por_grupo(sub, col_y, col_risco, col_segmento, n_min=n_min) \
                                    .rename(columns={"grupo": "segmento"})
            _pagina_tabela(pdf, tabelas["segmentos"], f"Modelo por segmento ({foco})",
                           f"Segmento com menos de {n_min} maus ou bons sai sem métrica.")

        # 9. calibração
        if risco_eh_probabilidade:
            fig, axes = _figura(1, len(amostras), figsize=(11.7, 4.6), squeeze=False)
            linhas_hl = []
            for ax, a in zip(axes[0], amostras):
                s = df[df[col_amostra] == a][[col_y, col_risco]].dropna()
                if s[col_y].nunique() < 2:
                    ax.set_axis_off()
                    continue
                cal = tabela_calibracao(s[col_y], s[col_risco], cortes)
                hl = hosmer_lemeshow(s[col_y], s[col_risco])
                linhas_hl.append({"amostra": a, "tipo": rotulos.get(a, "?"), **hl})
                lim = max(cal["previsto"].max(), cal["observado"].max()) * 1.1
                ax.errorbar(cal["previsto"], cal["observado"],
                            yerr=[cal["observado"] - cal["ic_low"], cal["ic_high"] - cal["observado"]], fmt="o")
                ax.plot([0, lim], [0, lim], "--", color="grey")
                ax.set(title=f"{a}: HL p={hl['p_valor']:.3f}", xlabel="previsto", ylabel="observado")
            fig.suptitle("Calibração por faixa (cortes do treino, IC de Jeffreys)")
            fig.tight_layout()
            pdf.savefig(fig)
            tabelas["hosmer_lemeshow"] = pd.DataFrame(linhas_hl)

        # 10-11. SHAP
        if modelo is not None and features is not None:
            import shap
            feats = list(features)
            explainer = shap.TreeExplainer(modelo)
            valores = {}
            for a in [x for x in (amostra_treino, "VAL", foco) if x in amostras]:
                X = df[df[col_amostra] == a][feats]
                X = X.sample(min(n_shap, len(X)), random_state=seed)
                sv = explainer.shap_values(X)
                valores[a] = (X, sv[1] if isinstance(sv, list) else sv)
            imp = pd.Series(np.abs(valores[amostra_treino][1]).mean(axis=0) if amostra_treino in valores
                            else np.abs(next(iter(valores.values()))[1]).mean(axis=0), index=feats).sort_values(ascending=False)
            tabelas["shap_importancia"] = imp.rename("mean_abs_shap").reset_index().rename(columns={"index": "feature"})
            for a, (X, sv) in valores.items():
                fig, ax = _figura()
                _beeswarm(fig, ax, sv, X, max_display=20, sentinelas=sentinelas, seed=seed)
                ax.set_title(f"SHAP: {a} ({rotulos.get(a, '?')}, n={_milhar(len(X))})")
                fig.tight_layout()
                pdf.savefig(fig)
            X, sv = valores.get(amostra_treino, next(iter(valores.values())))
            for f in imp.index[:top_features]:
                j = feats.index(f)
                x = X[f]
                valido = x.notna() & ~x.isin(sentinelas)
                fig, axes = _figura(1, 2, figsize=(11.7, 4.6), gridspec_kw={"width_ratios": [3, 1]})
                xv = x[valido]
                lim = xv.quantile(0.99) if len(xv) else None
                m = valido & (x <= lim) if lim is not None else valido
                axes[0].scatter(x[m], sv[m.to_numpy(), j], s=4, alpha=0.4)
                axes[0].axhline(0, color="grey", lw=0.8)
                axes[0].set(title=f"{f}: domínio válido (até p99)", xlabel=f, ylabel="SHAP")
                grupos = {}
                if valido.any():
                    q = pd.qcut(x[valido], 4, labels=False, duplicates="drop")
                    for qi in sorted(q.unique()):
                        idx = valido.to_numpy().copy()
                        idx[valido.to_numpy()] = (q == qi).to_numpy()
                        grupos[f"q{int(qi) + 1}"] = sv[idx, j]
                if x.isna().any():
                    grupos["nulo"] = sv[x.isna().to_numpy(), j]
                for v in sentinelas:
                    if (x == v).any():
                        grupos[f"sent {v}"] = sv[(x == v).to_numpy(), j]
                axes[1].bar(list(grupos), [g.mean() for g in grupos.values()],
                            color=[CORES[i % len(CORES)] for i in range(len(grupos))])
                axes[1].axhline(0, color="grey", lw=0.8)
                axes[1].set(title="SHAP médio por faixa")
                axes[1].tick_params(axis="x", rotation=45, labelsize=8)
                fig.tight_layout()
                pdf.savefig(fig)

        # 12. PSI das features e do score
        cols_psi = [col_risco] + (list(tabelas["shap_importancia"]["feature"][:top_features])
                                  if "shap_importancia" in tabelas else list(features or [])[:top_features])
        linhas_psi = []
        for c in cols_psi:
            for a in amostras:
                if a == amostra_treino:
                    continue
                linhas_psi.append({"variavel": c, "amostra": a,
                                   "psi": k.psi(treino[c], df[df[col_amostra] == a][c], especiais=sentinelas)})
        tabelas["psi"] = pd.DataFrame(linhas_psi)
        if not tabelas["psi"].empty:
            piv = tabelas["psi"].pivot(index="variavel", columns="amostra", values="psi").reindex(cols_psi)
            fig, ax = _figura(figsize=(11.7, 6))
            altura = 0.8 / max(len(piv.columns), 1)
            for i, a in enumerate(piv.columns):
                ax.barh(np.arange(len(piv)) + i * altura, piv[a].to_numpy(), height=altura,
                        color=CORES[i % len(CORES)], label=str(a))
            ax.set_yticks(np.arange(len(piv)) + altura * (len(piv.columns) - 1) / 2)
            ax.set_yticklabels(piv.index.astype(str), fontsize=8)
            ax.axvline(PSI_ATENCAO, color="orange", ls="--", label="atenção 0,10")
            ax.axvline(PSI_INSTAVEL, color="red", ls="--", label="instável 0,25")
            ax.invert_yaxis()
            ax.set(title=f"PSI contra {amostra_treino} (nulo e sentinelas em faixa própria)", xlabel="PSI")
            ax.legend(fontsize=8)
            fig.tight_layout()
            pdf.savefig(fig)

        # 13. swap
        if swap_contra and taxa_aprovacao:
            col_b, alto_risco = benchmarks[swap_contra]
            sub = df[df[col_amostra] == foco].assign(_r_bench=lambda t: k.orienta(t[col_b], alto_risco))
            tabelas["swap"] = k.swap(sub, col_y, "_r_bench", col_risco, taxa_aprovacao)
            _pagina_tabela(pdf, tabelas["swap"], f"Swap contra {swap_contra} ({foco}, aprovação {taxa_aprovacao:.0%})",
                           "n_sem_alvo: sem performance (reprovados no passado ou imaturos), bad rate desconhecida, não zero.")

        n_paginas = pdf.get_pagecount()
    return {"caminho": str(caminho), "n_paginas": n_paginas, "tabelas": tabelas}


# --------------------------------------------------------------------------- #
# Relatório de variáveis
# --------------------------------------------------------------------------- #
def relatorio_variaveis_pdf(caminho: str | Path, df: pd.DataFrame, features: Sequence[str],
                            col_safra: str, *, mascara_treino=None, col_y: str | None = None,
                            sentinelas: Sequence[float] = (), n_faixas: int = 4,
                            titulo: str = "Variáveis por safra") -> dict:
    """Uma página por feature (na ordem recebida; passe já ordenada por importância).

    Faixas = quantis do treino (`mascara_treino`, default: tudo) + nulo + cada sentinela.
    Painéis: % da safra em cada faixa, bad rate por faixa (com `col_y`) e PSI da safra
    contra o treino. Primeira página: resumo com % nulo, % sentinela e PSI máximo.
    """
    import matplotlib
    from matplotlib.backends.backend_pdf import PdfPages

    treino = df if mascara_treino is None else df[np.asarray(mascara_treino, dtype=bool)]
    safras = sorted(df[col_safra].dropna().unique())
    resumo, paginas = [], []
    for f in features:
        x_tr = treino[f]
        reg = x_tr[x_tr.notna() & ~x_tr.isin(sentinelas)]
        cortes = np.unique(np.quantile(reg, np.linspace(0, 1, n_faixas + 1)[1:-1])) if len(reg) else np.array([])
        def faixa(x, cortes=cortes):
            r = pd.Series(np.searchsorted(cortes, x.fillna(0).to_numpy(), side="right") + 1, index=x.index).map(lambda i: f"q{i}")
            for v in sentinelas:
                r[x == v] = f"sent {v}"
            r[x.isna()] = "nulo"
            return r
        fx = faixa(df[f])
        comp = pd.crosstab(df[col_safra], fx, normalize="index") * 100
        ordem = [c for c in [f"q{i}" for i in range(1, len(cortes) + 2)] + ["nulo"] + [f"sent {v}" for v in sentinelas] if c in comp.columns]
        comp = comp[ordem]
        psi_safra = pd.Series({s: k.psi(x_tr, df.loc[df[col_safra] == s, f], especiais=sentinelas) for s in safras})
        br = None
        if col_y:
            br = df.assign(_fx=fx).pivot_table(index=col_safra, columns="_fx", values=col_y, aggfunc="mean") * 100
            br = br[[c for c in ordem if c in br.columns]]
        resumo.append({"feature": f, "pct_nulo": 100 * df[f].isna().mean(),
                       "pct_sentinela": 100 * df[f].isin(sentinelas).mean(),
                       "psi_max": psi_safra.max(), "safra_psi_max": psi_safra.idxmax() if len(psi_safra) else None})
        paginas.append((f, comp, br, psi_safra, cortes))

    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(caminho) as pdf, matplotlib.rc_context({"axes.grid": True, "grid.alpha": 0.3}):
        _pagina_tabela(pdf, pd.DataFrame(resumo), titulo,
                       "PSI de cada safra contra o treino; nulo e cada sentinela em faixa própria. "
                       f"Limites: atenção {PSI_ATENCAO}, instável {PSI_INSTAVEL}.")
        for f, comp, br, psi_safra, cortes in paginas:
            fig, axes = _figura(1, 3 if br is not None else 2, figsize=(11.7, 5),
                                     gridspec_kw={"width_ratios": [2, 2, 1.3] if br is not None else [2, 1.3]})
            base = np.zeros(len(comp))
            for j, c in enumerate(comp.columns):
                cor = "#bfbfbf" if c == "nulo" else ("#7f7f7f" if c.startswith("sent") else _cmap("Blues")(0.35 + 0.6 * j / max(len(cortes) + 1, 1)))
                axes[0].bar(comp.index.astype(str), comp[c], bottom=base, color=cor, label=c)
                base += comp[c].to_numpy()
            axes[0].set(title=f"{f}: % por faixa e safra")
            axes[0].tick_params(axis="x", rotation=90, labelsize=7)
            axes[0].legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0))
            if br is not None:
                for c in br.columns:
                    axes[1].plot(br.index.astype(str), br[c], marker=".", label=c)
                axes[1].set(title="bad rate % por faixa")
                axes[1].tick_params(axis="x", rotation=90, labelsize=7)
                axes[1].legend(fontsize=7)
            ax_psi = axes[-1]
            cores = ["red" if v > PSI_INSTAVEL else "orange" if v > PSI_ATENCAO else "#548235" for v in psi_safra]
            ax_psi.barh(psi_safra.index.astype(str), psi_safra.to_numpy(), color=cores)
            ax_psi.axvline(PSI_ATENCAO, color="orange", ls="--"); ax_psi.axvline(PSI_INSTAVEL, color="red", ls="--")
            ax_psi.set(title="PSI vs treino")
            ax_psi.tick_params(axis="y", labelsize=7)
            cortes_txt = ", ".join(f"{c:.4g}" for c in cortes)
            fig.text(0.01, 0.01, f"cortes do treino: {cortes_txt}", fontsize=7, style="italic")
            fig.tight_layout(rect=(0, 0.03, 1, 1))
            pdf.savefig(fig)
        n_paginas = pdf.get_pagecount()
    return {"caminho": str(caminho), "n_paginas": n_paginas, "resumo": pd.DataFrame(resumo)}
