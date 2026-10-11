#!/usr/bin/env python3
"""池级口径（真实资金）分析：不同下单规模下的年化 / 尾部。"""
import os, sys, math, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leaps_engine import build_series, run

s = build_series()
POOL = 100000.0

def pool_stats(trades, pool=POOL, mode="fixed1", fixed_n=1):
    rows = []
    for t in trades:
        n_contracts = max(1, t["qty"])
        cost_per = t["cost_usd"] / n_contracts
        if mode == "fixed":
            mult = fixed_n / n_contracts
        else:  # full deployment
            c = max(1, int(pool // cost_per))
            mult = c / n_contracts
        pnl = t["cost_usd"] * mult * t["ret"]
        rows.append((t["entry"], t["exit"], t["ret"], t["cost_usd"] * mult, pnl, cost_per, mult))
    total = sum(r[4] for r in rows)
    worst = min(rows, key=lambda r: r[4])
    return dict(n=len(rows), total=total, worst_usd=worst[4], worst_pct=worst[4] / pool * 100,
                worst_date=worst[0], avg_deploy=statistics.mean([r[3] for r in rows]) / pool * 100,
                rows=rows)

CANDS = {
    "live 0.65Δ/365/tp65": {},
    "0.70Δ/365/tp65": dict(delta=0.70),
    "0.75Δ/365/tp65": dict(delta=0.75),
    "0.75Δ/270/tp80": dict(delta=0.75, tenor_days=270, tp_rsi=80.0),
    "0.70Δ/270/tp70": dict(delta=0.70, tenor_days=270, tp_rsi=70.0),
    "无SMA过滤 0.65Δ/365/tp65": dict(trend_filter=None),
    "无SMA过滤 0.70Δ/365/tp65": dict(trend_filter=None, delta=0.70),
}

print("=== 池级口径：$100k 常驻、利润不滚动（用户实盘口径），2001-02..2026-10 ===")
print("%-28s %-22s %8s %8s %8s %9s %9s" % ("配置", "下单规模", "笔数", "总利润$", "年均%", "均占用%", "最差单笔%"))
for name, p in CANDS.items():
    r = run(p, series=s, collect_trades=True)
    for mode, fn, label in (("fixed", 1, "固定1张"), ("fixed", 3, "固定3张"), ("full", 0, "满仓(≈池/成本)")):
        st = pool_stats(r["trades"], mode=mode, fixed_n=fn)
        yrs = r["years"]
        print("%-28s %-22s %8d %8.0f %8.2f %9.1f %9.1f" % (
            name, label, st["n"], st["total"], st["total"] / yrs / POOL * 100, st["avg_deploy"], st["worst_pct"]))
    print()

print("=== 近年（2024-2026）单张成本参考（QQQ 等价） ===")
r = run(dict(start="2024-01-01"), series=s, collect_trades=True)
for t in r["trades"]:
    print("   %s 成本≈$%.0f/张  QQQ≈%.0f  K≈%.0f  收益%+.1f%%" % (
        t["entry"], t["cost_usd"], t["base"] / 40.8, t["strike"] / 40.8, t["ret"] * 100))

print("\n=== 趋势过滤对比（含分段）===")
for tf in ("sma200_3d", None):
    for a, b in (("2001-02-02", "2013-12-31"), ("2014-01-01", "2026-12-31"), ("2001-02-02", "2026-12-31")):
        rr = run(dict(trend_filter=tf, start=a, end=b), series=s)
        print("  trend=%-10s %s..%s n=%2d win=%4.1f%% mean=%+6.1f%% worst=%+7.1f%% CAGR=%+6.1f%% mdd=%6.1f%%" % (
            tf, a[:4], b[:4], rr["n"], rr["win_rate"] * 100, rr["mean"] * 100, rr["worst"] * 100,
            rr["cagr"] * 100, rr["mdd"] * 100))
