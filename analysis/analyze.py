"""Анализ: сырые данные Locust + docker stats -> runs.csv, summary, A/A, стабильность, сравнения, графики.

Единица наблюдения - прогон (p95 за прогон). Использование: python analysis/analyze.py configs/experiment.yaml
"""
import json, sys
from itertools import combinations
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np, pandas as pd, yaml
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
cfg = yaml.safe_load((ROOT / (sys.argv[1] if len(sys.argv) > 1 else "configs/experiment.yaml")).read_text())
RAW = ROOT / cfg["out_dir"]
is_main = Path(cfg["out_dir"]).name == "raw"
PROC = ROOT / "results/processed" / ("" if is_main else Path(cfg["out_dir"]).name)
IMG = ROOT / "docs/img" if is_main else PROC
PROC.mkdir(parents=True, exist_ok=True); IMG.mkdir(parents=True, exist_ok=True)
GEN_LIMIT = float(cfg.get("load_cpus", 2)) * 100  # docker stats: 100% = 1 ядро
DRV = ["sync_psycopg2", "async_asyncpg", "async_psycopg3"]
COL = {"sync_psycopg2": "#c0504d", "async_asyncpg": "#1f77b4", "async_psycopg3": "#2ca02c"}
md = []


def table(df, floatfmt=2):
    d = df.copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].map(lambda v: f"{v:.{floatfmt}f}")
    h = "| " + " | ".join(map(str, d.columns)) + " |\n|" + "---|" * len(d.columns) + "\n"
    return h + "\n".join("| " + " | ".join(map(str, r)) + " |" for r in d.values) + "\n"


def cliff(x, y):
    x, y = np.asarray(x), np.asarray(y)
    return (np.sum(x[:, None] > y[None, :]) - np.sum(x[:, None] < y[None, :])) / (len(x) * len(y))


def magnitude(d):
    d = abs(d)
    return "пренебрежимый" if d < .147 else "малый" if d < .33 else "средний" if d < .474 else "большой"


def boot_ci(x, f=np.median, n=5000, seed=0):
    rng = np.random.default_rng(seed); x = np.asarray(x)
    b = [f(rng.choice(x, len(x))) for _ in range(n)]
    return np.percentile(b, [2.5, 97.5])


# ---------- загрузка ----------
rows, curves = [], []
PCT = ["50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%", "99.99%", "100%"]
for m in sorted(RAW.glob("*.json")):
    if m.name == "plan.json":
        continue
    man = json.loads(m.read_text()); rid = man["run_id"]
    agg = pd.read_csv(RAW / f"{rid}_stats.csv").query("Name == 'Aggregated'").iloc[0]
    ds = pd.read_csv(RAW / f"{rid}_docker_stats.csv")
    sut = ds[ds.container == "asyncstand-sut"].cpu_pct
    gen = ds[ds.container == "asyncstand-load"].cpu_pct
    rows.append({**man["params"], "label": man["label"], "rep": man["repetition"], "order": man["order"],
                 "p50": agg["50%"], "p95": agg["95%"], "p99": agg["99%"], "rps": agg["Requests/s"],
                 "requests": agg["Request Count"], "fail": agg["Failure Count"],
                 "sut_cpu": sut.median(), "gen_cpu_pct_of_limit": 100 * gen.median() / GEN_LIMIT,
                 "gen_cpu_max_pct_of_limit": 100 * gen.max() / GEN_LIMIT, "run_id": rid})
    curves.append({**man["params"], "label": man["label"], **{p: agg[p] for p in PCT}})
runs = pd.DataFrame(rows).sort_values("order"); runs.to_csv(PROC / "runs.csv", index=False)
main = runs[runs.label == "main"].copy(); aa = runs[runs.label == "aa"].copy()
main["cfg"] = main.driver + "|" + main.workload + "|u" + main.users.astype(str)
md.append(f"# Результаты анализа ({cfg['name']})\n\nПрогонов: {len(runs)} (основных {len(main)}, A/A {len(aa)}). "
          f"Единица наблюдения - прогон; p-значения - Манн-Уитни, эффект - δ Клиффа.\n")

# ---------- сводка ----------
out = []
for (w, u, d), g in main.groupby(["workload", "users", "driver"]):
    lo, hi = boot_ci(g.p95); rlo, rhi = boot_ci(g.rps)
    out.append(dict(workload=w, users=u, driver=d, n=len(g), rps_med=g.rps.median(), rps_lo=rlo, rps_hi=rhi,
                    p95_med=g.p95.median(), p95_iqr=g.p95.quantile(.75) - g.p95.quantile(.25), p95_lo=lo, p95_hi=hi,
                    p50_med=g.p50.median(), p99_med=g.p99.median(), cv_p95_pct=100 * g.p95.std() / g.p95.mean(),
                    sut_cpu=g.sut_cpu.median(), fail_total=int(g.fail.sum())))
summ = pd.DataFrame(out); summ.to_csv(PROC / "summary.csv", index=False)
md.append("## Сводка по конфигурациям\n\nМедианы по прогонам, 95% bootstrap-ДИ медианы (5000 ресэмплов), "
          "IQR; задержки в мс, rps - запросов/с, sut_cpu - % одного ядра.\n\n" + table(summ))

# ---------- сравнения ----------
res = []
for (w, u), g in main.groupby(["workload", "users"]):
    for a, b in [("async_asyncpg", "sync_psycopg2"), ("async_psycopg3", "sync_psycopg2"),
                 ("async_asyncpg", "async_psycopg3")]:
        for met in ("rps", "p95"):
            x, y = g[g.driver == a][met], g[g.driver == b][met]
            if len(x) and len(y):
                p = stats.mannwhitneyu(x, y, alternative="two-sided").pvalue
                dl = cliff(x, y)
                res.append(dict(workload=w, users=u, cmp=f"{a} vs {b}", metric=met, ratio=x.median() / y.median(),
                                cliff_delta=dl, effect=magnitude(dl), p=p))
cmpdf = pd.DataFrame(res)
if len(cmpdf):  # поправка Холма на все сравнения
    order = np.argsort(cmpdf.p.values); m = len(cmpdf); adj = np.zeros(m); mx = 0
    for rank, i in enumerate(order):
        mx = max(mx, min(1, (m - rank) * cmpdf.p.values[i])); adj[i] = mx
    cmpdf["p_holm"] = adj
cmpdf.to_csv(PROC / "comparisons.csv", index=False)
md.append(f"## Сравнение драйверов\n\nratio - отношение медиан (первый / второй); {len(cmpdf)} сравнений, "
          "поправка Холма.\n\n" + table(cmpdf, 3))

# ---------- A/A ----------
if len(aa) >= 4:
    rng = np.random.default_rng(cfg["seed"])
    idx = rng.permutation(len(aa)); h = len(aa) // 2
    g1, g2 = aa.iloc[idx[:h]], aa.iloc[idx[h:]]
    lines = []
    for met in ("p95", "rps"):
        p = stats.mannwhitneyu(g1[met], g2[met], alternative="two-sided").pvalue
        lines.append(dict(metric=met, n1=len(g1), n2=len(g2), median1=g1[met].median(), median2=g2[met].median(),
                          ratio=g1[met].median() / g2[met].median(), cliff_delta=cliff(g1[met], g2[met]),
                          p=p, cv_pct=100 * aa[met].std() / aa[met].mean()))
    aat = pd.DataFrame(lines)
    # частота ложных срабатываний по 2000 случайным разбиениям
    fp = {}
    for met in ("p95", "rps"):
        c = 0
        for _ in range(2000):
            pi = rng.permutation(len(aa))
            c += stats.mannwhitneyu(aa[met].values[pi[:h]], aa[met].values[pi[h:]]).pvalue < .05
        fp[met] = c / 2000
    n_main = int(main.groupby("cfg").size().median())
    mde = {met: 2.8 * np.sqrt(2 / n_main) * aa[met].std() / aa[met].mean() * 100 for met in ("p95", "rps")}
    md.append("## A/A-тест\n\n" + f"{len(aa)} повторов одной конфигурации ({aa.iloc[0].driver}, {aa.iloc[0].workload}, "
              f"{aa.iloc[0].users} пользователей), случайное деление {len(g1)}/{len(g2)}.\n\n" + table(aat, 3) +
              f"\nДоля ложных срабатываний (p<0.05) по 2000 случайным разбиениям: p95 = {fp['p95']:.3f}, rps = {fp['rps']:.3f} (ожидается ≈0.05).\n\n"
              f"Шум стенда (CV): p95 = {aa.p95.std()/aa.p95.mean()*100:.1f}%, rps = {aa.rps.std()/aa.rps.mean()*100:.1f}%. "
              f"Минимально обнаружимый эффект при n={n_main}, мощности 0.8, α=0.05 (≈2.8·√(2/n)·CV): "
              f"p95 ≈ {mde['p95']:.1f}%, rps ≈ {mde['rps']:.1f}%.\n")
    fig, ax = plt.subplots(1, 2, figsize=(9, 3.2))
    for a_, met in zip(ax, ("p95", "rps")):
        a_.scatter(aa.order, aa[met], c="#555"); a_.set_xlabel("порядковый номер прогона"); a_.set_ylabel(met)
        a_.set_title(f"A/A: {met}")
    fig.tight_layout(); fig.savefig(IMG / "aa.png", dpi=130); plt.close(fig)

# ---------- генератор ----------
gen = runs.groupby(["label", "workload", "users"]).agg(gen_med=("gen_cpu_pct_of_limit", "median"),
                                                      gen_max=("gen_cpu_max_pct_of_limit", "max"),
                                                      sut_cpu_med=("sut_cpu", "median")).reset_index()
gen["ok(<80%)"] = np.where(gen.gen_max < 80, "да", "НЕТ")
gen.to_csv(PROC / "generator.csv", index=False)
md.append("## Загрузка генератора нагрузки\n\nCPU контейнера Locust в % от его лимита "
          f"({GEN_LIMIT/100:.0f} CPU), docker stats раз в ~2 c; gen_max - максимум по всем прогонам группы.\n\n"
          + table(gen) + f"\nОшибок запросов всего: {int(runs.fail.sum())} из {int(runs.requests.sum())}.\n")

# ---------- стабильность ----------
main["norm_p95"] = main.p95 / main.groupby("cfg").p95.transform("median")
main["norm_rps"] = main.rps / main.groupby("cfg").rps.transform("median")
st = []
for met in ("norm_p95", "norm_rps"):
    r, p = stats.spearmanr(main.order, main[met]); st.append(dict(metric=met, spearman_rho=r, p=p))
if len(aa) >= 4:
    for met in ("p95", "rps"):
        r, p = stats.spearmanr(aa.order, aa[met]); st.append(dict(metric=f"A/A {met}", spearman_rho=r, p=p))
md.append("## Стабильность во времени\n\nSpearman: метрика (нормированная на медиану своей конфигурации) "
          "vs порядковый номер прогона. Тренда нет, если |ρ| мало и p>0.05.\n\n" + table(pd.DataFrame(st), 3))
fig, ax = plt.subplots(1, 2, figsize=(9, 3.2))
for a_, met in zip(ax, ("norm_p95", "norm_rps")):
    a_.scatter(main.order, main[met], s=12, c=[COL[d] for d in main.driver])
    a_.axhline(1, c="k", lw=.5); a_.set_xlabel("порядковый номер прогона"); a_.set_ylabel(met)
fig.tight_layout(); fig.savefig(IMG / "stability.png", dpi=130); plt.close(fig)


# ---------- чувствительность к выбросам ----------
def holm(p):
    o = np.argsort(p); m = len(p); adj = np.zeros(m); mx = 0
    for k, i in enumerate(o):
        mx = max(mx, min(1, (m - k) * p[i])); adj[i] = mx
    return adj


bad = main[(main.norm_p95 > 1.4) | (main.norm_rps < 0.7)]
m2 = main.drop(bad.index); rs = []
for (w, u), g in m2.groupby(["workload", "users"]):
    for a, b in [("async_asyncpg", "sync_psycopg2"), ("async_psycopg3", "sync_psycopg2"), ("async_asyncpg", "async_psycopg3")]:
        for met in ("rps", "p95"):
            x, y = g[g.driver == a][met], g[g.driver == b][met]
            rs.append(dict(workload=w, users=u, cmp=f"{a} vs {b}", metric=met, ratio=x.median() / y.median(),
                           cliff_delta=cliff(x, y), p=stats.mannwhitneyu(x, y).pvalue))
sens = pd.DataFrame(rs); sens["p_holm"] = holm(sens.p.values); sens.to_csv(PROC / "comparisons_no_outliers.csv", index=False)
rcv = lambda x: (x.quantile(.75) - x.quantile(.25)) / 1.349 / x.median() * 100
md.append("## Чувствительность к выбросам\n\nВыбросом считается прогон, у которого p95 более чем в 1,4 раза выше медианы своей "
          "конфигурации или пропускная способность ниже 0,7 медианы (критерий задан после просмотра данных, используется только как проверка "
          f"устойчивости). Исключено прогонов: {len(bad)}.\n\n" + table(bad[["order", "driver", "workload", "users", "p95", "rps", "norm_p95", "norm_rps"]], 2) +
          f"\nЗначимых сравнений после Холма (p_holm<0.05): со всеми прогонами {(cmpdf.p_holm < .05).sum()} из {len(cmpdf)}, "
          f"без выбросов {(sens.p_holm < .05).sum()} из {len(sens)}.\n")
if len(aa) >= 4:
    md.append(f"Робастный разброс A/A (IQR/1,349 к медиане): p95 = {rcv(aa.p95):.1f}%, rps = {rcv(aa.rps):.1f}%. "
              f"CV без прогона с наибольшим p95: p95 = {aa[aa.p95 < aa.p95.max()].p95.std()/aa[aa.p95 < aa.p95.max()].p95.mean()*100:.1f}%, "
              f"rps = {aa[aa.p95 < aa.p95.max()].rps.std()/aa[aa.p95 < aa.p95.max()].rps.mean()*100:.1f}%.\n")

# ---------- графики ----------
def box(metric, ylabel, fname):
    cfgs = sorted(main.groupby(["workload", "users"]).groups)
    fig, axs = plt.subplots(1, len(cfgs), figsize=(3.2 * len(cfgs), 3.6), squeeze=False)
    for a_, (w, u) in zip(axs[0], cfgs):
        g = main[(main.workload == w) & (main.users == u)]
        data = [g[g.driver == d][metric].values for d in DRV]
        a_.boxplot(data, widths=.6, showfliers=False)
        for i, (d, v) in enumerate(zip(DRV, data), 1):
            a_.scatter(np.random.default_rng(i).normal(i, .05, len(v)), v, s=10, c=COL[d], zorder=3)
        a_.set_xticks(range(1, 4)); a_.set_xticklabels(["sync\npsycopg2", "asyncpg", "psycopg3\nasync"], fontsize=7)
        a_.set_title(f"{w}, u={u}", fontsize=9); a_.set_ylabel(ylabel)
    fig.tight_layout(); fig.savefig(IMG / fname, dpi=130); plt.close(fig)


box("p95", "p95, мс", "box_p95.png"); box("rps", "запросов/с", "box_rps.png")
cdf = pd.DataFrame(curves); cdf = cdf[cdf.label == "main"]
cfgs = sorted(cdf.groupby(["workload", "users"]).groups)
fig, axs = plt.subplots(1, len(cfgs), figsize=(3.2 * len(cfgs), 3.6), squeeze=False)
xs = [50, 66, 75, 80, 90, 95, 98, 99, 99.9, 99.99]
for a_, (w, u) in zip(axs[0], cfgs):
    for d in DRV:
        g = cdf[(cdf.workload == w) & (cdf.users == u) & (cdf.driver == d)]
        if len(g):
            a_.plot(range(len(xs)), [g[f"{p}%" if p != 50 else "50%"].median() for p in xs], marker="o", ms=3, c=COL[d], label=d)
    a_.set_xticks(range(len(xs))); a_.set_xticklabels([str(x) for x in xs], fontsize=6, rotation=60)
    a_.set_yscale("log"); a_.set_title(f"{w}, u={u}", fontsize=9); a_.set_xlabel("перцентиль"); a_.set_ylabel("мс")
axs[0][0].legend(fontsize=6)
fig.tight_layout(); fig.savefig(IMG / "percentiles.png", dpi=130); plt.close(fig)

(PROC / "report.md").write_text("\n".join(md))
print("\n".join(md))
