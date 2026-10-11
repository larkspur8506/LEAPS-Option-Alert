#!/usr/bin/env python3
"""信号计数诊断 + 单因子扫描。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leaps_engine import build_series, run, DEFAULTS

s = build_series()

def show(tag, p):
    r = run(p, series=s)
    print("%-52s n=%3d win=%4.1f%% mean=%+6.1f%% med=%+6.1f%% p10=%+6.1f%% worst=%+7.1f%% eq=%6.2fx CAGR=%+6.1f%% mdd=%6.1f%% hold=%4.0fd" % (
        tag, r["n"], r["win_rate"] * 100, r["mean"] * 100, r["median"] * 100, r["p10"] * 100,
        r["worst"] * 100, r["equity"], r["cagr"] * 100, r["mdd"] * 100, r["avg_hold"]))

print("### A. 入场过滤组合（其余=线上参数, 2001-02 起）")
for tf in ("sma200_3d", "sma200", None):
    for oy in (True, False):
        for rsi in (35.0,):
            show("trend=%-10s 1y=%-5s rsi<%.0f" % (tf, oy, rsi), dict(trend_filter=tf, one_year_filter=oy, entry_rsi=rsi))

print("\n### A2. 同样组合但样本拉到 1999-03（含 2000 顶，IV 用实现波动率兜底）")
for tf in ("sma200_3d", "sma200", None):
    for oy in (True, False):
        show("trend=%-10s 1y=%-5s" % (tf, oy), dict(trend_filter=tf, one_year_filter=oy, start="1999-03-10"))

print("\n### B. 入场 RSI 阈值")
for rsi in (25, 30, 35, 40, 45, 50):
    show("entry_rsi<%d" % rsi, dict(entry_rsi=float(rsi)))

print("\n### C. 期限 tenor（DTE 强平同步缩放: 90 固定）")
for t in (180, 270, 365, 545, 730):
    show("tenor=%dd (DTE90)" % t, dict(tenor_days=t))

print("\n### D. delta")
for d in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75):
    show("delta=%.2f" % d, dict(delta=d))

print("\n### E. 止盈 RSI 阈值")
for tp in (55, 60, 65, 70, 75, 80, 999):
    show("tp_rsi=%s" % (tp if tp < 999 else "never"), dict(tp_rsi=float(tp)))

print("\n### F. DTE 强平")
for dte in (30, 60, 90, 120, 180, 999):
    show("dte_force=%s" % (dte if dte < 999 else "never"), dict(dte_force=0 if dte >= 999 else dte))

print("\n### G. 价格止损（线上没有这条）")
for st in (0, 0.25, 0.35, 0.45, 0.55):
    show("stop=%s" % ("off" if not st else "-%.0f%%" % (st * 100)), dict(stop_pct=st))

print("\n### H. 时间止损（交易日，未回本才平）")
for ts in (0, 60, 90, 126, 180):
    show("time_stop=%s" % (ts if ts else "off"), dict(time_stop_days=ts))

print("\n### I. 加仓档位")
for lv in ((), (0.15, 0.25), (0.10, 0.20), (0.20, 0.30), (0.15, 0.25, 0.35)):
    show("adds=%s" % (lv or "off"), dict(add_levels=lv, max_qty=1 + len(lv)))

print("\n### J. 分批止盈")
for h in (0, 0.5, 0.75, 1.0, 1.5):
    show("half_tp=%s" % (h or "off"), dict(half_tp=h))

print("\n### K. 成本（每边价差）")
for sp in (0.0, 0.01, 0.02, 0.03):
    show("spread=%.0f%%/边" % (sp * 100), dict(spread_pct=sp))

print("\n### L. 分红率假设")
for q in (0.0, 0.0055, 0.012):
    show("q=%.2f%%" % (q * 100), dict(q_div=q))
