#!/usr/bin/env python3
"""分层止盈（RSI 卖半 + 余仓放宽）扫描 + 池级口径。"""
import os, sys, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leaps_engine import build_series, run

s = build_series()
POOL = 100000.0

def pool(trades, mode, fixed_n=1):
    tot = 0.0
    for t in trades:
        nc = max(1, t["qty"])
        cper = t["cost_usd"] / nc
        mult = (fixed_n / nc) if mode == "fixed" else (max(1, int(POOL // cper)) / nc)
        tot += t["cost_usd"] * mult * t["ret"]
    return tot

def show(tag, p):
    r = run(p, series=s, collect_trades=True)
    t3 = pool(r["trades"], "fixed", 3) / r["years"] / POOL * 100
    tf = pool(r["trades"], "full") / r["years"] / POOL * 100
    print("%-34s n=%3d win=%4.1f%% mean=%+6.1f%% med=%+6.1f%% p10=%+6.1f%% worst=%+7.1f%% CAGR=%+6.1f%% mdd=%6.1f%% | 池: 3张%+5.2f%% 满仓%+6.2f%%/年" % (
        tag, r["n"], r["win_rate"] * 100, r["mean"] * 100, r["median"] * 100, r["p10"] * 100,
        r["worst"] * 100, r["cagr"] * 100, r["mdd"] * 100, t3, tf))
    return r

print("=== 单层止盈（现状）===")
show("0.65Δ/365 tp65", {})
show("0.65Δ/365 tp70", dict(tp_rsi=70.0))
show("0.65Δ/365 tp80", dict(tp_rsi=80.0))
print("\n=== 分层止盈: RSI>X 卖半, 余仓 RSI>Y 或持有到 DTE ===")
for x in (60.0, 65.0):
    for y in (75.0, 80.0, 999.0):
        show("0.65Δ/365 half@%.0f rest@%s" % (x, "hold" if y > 900 else "%.0f" % y),
             dict(tp_rsi_half=x, tp_rsi=y))
print("\n=== 分层 + 高 delta / 短期限 ===")
for d in (0.70, 0.75):
    for t in (270, 365):
        show("%.2fΔ/%3dd half@65 rest@80" % (d, t), dict(delta=d, tenor_days=t, tp_rsi_half=65.0, tp_rsi=80.0))
print("\n=== 分层 + 成本 1.5%/边 ===")
show("0.65Δ/365 half@65 rest@80 s1.5%", dict(tp_rsi_half=65.0, tp_rsi=80.0, spread_pct=0.015))
show("0.70Δ/270 half@65 rest@80 s1.5%", dict(delta=0.70, tenor_days=270, tp_rsi_half=65.0, tp_rsi=80.0, spread_pct=0.015))
print("\n=== 分段稳健性（分层方案）===")
for a, b in (("2001-02-02", "2013-12-31"), ("2014-01-01", "2026-12-31")):
    for tag, p in (("现状 tp65", {}),
                   ("分层 half@65 rest@80", dict(tp_rsi_half=65.0, tp_rsi=80.0)),
                   ("分层 0.70Δ/270 rest@80", dict(delta=0.70, tenor_days=270, tp_rsi_half=65.0, tp_rsi=80.0))):
        pp = dict(p); pp["start"] = a; pp["end"] = b
        show("%-24s %s..%s" % (tag, a[:4], b[:4]), pp)
