#!/usr/bin/env python3
"""扫描（含美元/固定本金口径）+ 联合网格 + 分段稳健性。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leaps_engine import build_series, run

s = build_series()

def show(tag, p=None, trades=False):
    r = run(p, series=s, collect_trades=trades)
    print("%-40s n=%3d win=%4.1f%% mean=%+6.1f%% med=%+6.1f%% p10=%+6.1f%% worst=%+7.1f%% eq=%7.2fx CAGR=%+6.1f%% mdd=%6.1f%% hold=%3.0fd | 均成本$%5.0f 利润$%+7.0f/年 池=%+5.1f%%/年" % (
        tag, r["n"], r["win_rate"] * 100, r["mean"] * 100, r["median"] * 100, r["p10"] * 100,
        r["worst"] * 100, r["equity"], r["cagr"] * 100, r["mdd"] * 100, r["avg_hold"],
        r["usd_cost_avg"], r["usd_per_year"], r["pool_pct_per_year"]))
    return r

print("### 0. 基线（线上参数）")
base = show("live: 0.65Δ/365d/RSI35/TP65/DTE90/adds15-25")

print("\n### I. 加仓档位")
for lv in ((), (0.15, 0.25), (0.10, 0.20), (0.20, 0.30), (0.15, 0.25, 0.35), (0.25, 0.40)):
    show("adds=%s" % (list(lv) or "off"), dict(add_levels=lv, max_qty=max(1, len(lv) + 1)))

print("\n### J. 分批止盈")
for h in (0, 0.5, 0.75, 1.0, 1.5):
    show("half_tp=%s" % (h or "off"), dict(half_tp=h))

print("\n### K. 成本（每边价差）")
for sp in (0.0, 0.01, 0.02, 0.03, 0.05):
    show("spread=%.0f%%/边" % (sp * 100), dict(spread_pct=sp))

print("\n### L. 分红率假设")
for q in (0.0, 0.0055, 0.012):
    show("q=%.2f%%" % (q * 100), dict(q_div=q))

print("\n### M. 联合网格 delta x tenor x tp（无成本；按 CAGR 排序前 15）")
grid = []
for d in (0.60, 0.65, 0.70, 0.75):
    for t in (180, 270, 365, 545):
        for tp in (60.0, 65.0, 70.0, 80.0):
            r = run(dict(delta=d, tenor_days=t, tp_rsi=tp), series=s, collect_trades=False)
            grid.append((r["cagr"], r["mdd"], r["n"], r["mean"], r["pool_pct_per_year"], d, t, tp, r["worst"]))
grid.sort(reverse=True)
for g in grid[:15]:
    print("  CAGR=%+6.1f%% mdd=%6.1f%% n=%2d mean=%+6.1f%% 池=%+5.1f%%/年  worst=%+6.1f%%  Δ=%.2f tenor=%3d tp=%.0f" %
          (g[0] * 100, g[1] * 100, g[2], g[3] * 100, g[4], g[8] * 100, g[5], g[6], g[7]))
print("  --- 按「池级年化」排序前 8 ---")
for g in sorted(grid, key=lambda x: -x[4])[:8]:
    print("  池=%+5.1f%%/年 CAGR=%+6.1f%% mdd=%6.1f%% n=%2d  Δ=%.2f tenor=%3d tp=%.0f" % (g[4], g[0] * 100, g[1] * 100, g[2], g[5], g[6], g[7]))

print("\n### N. 分段稳健性（基线 vs 候选）")
cands = {
    "live 0.65Δ/365/tp65": {},
    "0.70Δ/365/tp65": dict(delta=0.70),
    "0.65Δ/270/tp65": dict(tenor_days=270),
    "0.70Δ/270/tp65": dict(delta=0.70, tenor_days=270),
    "0.75Δ/365/tp65": dict(delta=0.75),
    "0.65Δ/365/tp65/stop-45%": dict(stop_pct=0.45),
}
for name, p in cands.items():
    for a, b in (("2001-02", "2013-12-31"), ("2014-01", "2026-12-31")):
        pp = dict(p); pp["start"] = a; pp["end"] = b
        show("%-26s %s" % (name, a[:7] + ".." + b[:4]), pp)

print("\n### O. 长样本（1986 起，2001 前 IV 用实现波动率兜底）——纯稳健性参考")
for name, p in cands.items():
    pp = dict(p); pp["start"] = "1987-01-02"
    show("%-26s 1987..2026" % name, pp)
