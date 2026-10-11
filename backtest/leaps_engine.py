#!/usr/bin/env python3
"""QQQ LEAPS 信号回测引擎 (纯 stdlib)。

标的：纳斯达克100 指数（FRED NASDAQ100，1986→今）作为 QQQ 代理（信号口径等价；
期权收益对 S/K 同比缩放不变，故绝对价位不影响百分比收益）。
IV：FRED VXNCLS（2001-02 起真实隐含波动率）；更早用 20 日实现波动率年化兜底。
利率：FRED DGS3MO（3 个月国库券）。
定价：Black-Scholes（含分红率 q），0.65Δ 行权价按目标 delta 反解、四舍五入到 5 美元档。

规则（与 app/alerts/qqq_rules.py 一致）：
  入场: RSI14 < entry_rsi 且 最近 3 日收盘均 > SMA200 且 收盘 > 252 交易日前收盘
  退出(先到先出): RSI14 > tp_rsi  →  RSI>65 止盈全平
                 时间止损(可选): 持仓≥N 交易日且未回本
                 价格止损(可选): 权利金相对成本回撤 ≥ stop_pct
                 DTE ≤ dte_force → 强平
  加仓: 相对 signal_base_price 回撤达 -15%/-25% 且 RSI < entry_rsi，最多 max_qty 张
  分批止盈(可选): 总盈利 ≥ half_tp → 平掉一半张数，余仓继续
  成本: 每边按权利金 spread_pct 计（买价=mid*(1+s/2)，卖价=mid*(1-s/2)）
"""
import csv, json, math, os, statistics
from datetime import date, timedelta
from statistics import NormalDist

ND = NormalDist()
# 数据目录: 与脚本同级 (backtest/data/*.csv, 来自 FRED, 见 fetch_data.py)
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------------------------------------------------------- data
def load_csv(path, col):
    out = {}
    with open(path) as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 2:
                continue
            d, v = row[0], row[1]
            if v in (".", "", "NA"):
                continue
            try:
                out[d] = float(v)
            except ValueError:
                pass
    return out


def build_series():
    ndx = load_csv(os.path.join(DATA, "NASDAQ100.csv"), "NASDAQ100")
    vxn = load_csv(os.path.join(DATA, "VXNCLS.csv"), "VXNCLS")
    irx = load_csv(os.path.join(DATA, "DGS3MO.csv"), "DGS3MO")
    dates = sorted(ndx)
    closes = [ndx[d] for d in dates]
    n = len(dates)

    # forward-fill vxn / rate onto trading days
    ivs, rates = [], []
    last_v, last_r = None, 3.0
    for d in dates:
        if d in vxn:
            last_v = vxn[d]
        ivs.append(last_v)
        if d in irx:
            last_r = irx[d]
        rates.append(last_r)
    # 2001 前的 IV 兜底: 20 日实现波动率年化
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    for i in range(n):
        if ivs[i] is None:
            w = rets[max(0, i - 20):i + 1]
            if len(w) >= 5:
                sd = statistics.pstdev(w) * math.sqrt(252) * 100.0
                ivs[i] = max(15.0, min(60.0, sd))
            else:
                ivs[i] = 25.0

    # indicators
    rsi = [None] * n
    period = 14
    gains, losses = 0.0, 0.0
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= period:
            gains += g; losses += l
            if i == period:
                ag, al = gains / period, losses / period
                rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (period - 1) + g) / period
            al = (al * (period - 1) + l) / period
            rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    sma200 = [None] * n
    run = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= 200:
            run -= closes[i - 200]
        if i >= 199:
            sma200[i] = run / 200

    return dict(dates=dates, closes=closes, ivs=ivs, rates=rates, rsi=rsi, sma200=sma200)


# ---------------------------------------------------------------- pricing
def bs_call(S, K, T, r, q, sigma):
    if T <= 1e-6 or sigma <= 1e-6:
        return max(0.0, S - K)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    return S * math.exp(-q * T) * ND.cdf(d1) - K * math.exp(-r * T) * ND.cdf(d2)


def strike_for_delta(S, T, r, q, sigma, target_delta, round_to=5.0):
    """目标 delta 的反解: Δ_call = e^{-qT} Φ(d1) = target  =>  d1 = Φ^-1(target * e^{qT})"""
    tgt = min(0.95, target_delta * math.exp(q * T))
    d1 = ND.inv_cdf(tgt)
    K = S * math.exp((r - q + 0.5 * sigma * sigma) * T - d1 * sigma * math.sqrt(T))
    return round(K / round_to) * round_to


# ---------------------------------------------------------------- engine
DEFAULTS = dict(
    entry_rsi=35.0, trend_filter="sma200_3d", one_year_filter=True,
    delta=0.65, tenor_days=365, dte_force=90, tp_rsi=65.0,
    time_stop_days=0, stop_pct=0.0, add_levels=(0.15, 0.25), max_qty=3,
    half_tp=0.0, tp_rsi_half=0.0, spread_pct=0.0, q_div=0.0055, start="2001-02-02", end="2026-12-31",
    pool=100000.0, ndx_per_qqq=40.8,   # 指数点位 -> QQQ 等价价（用于美元口径）
)


def run(params=None, series=None, collect_trades=True):
    p = dict(DEFAULTS); p.update(params or {})
    s = series or build_series()
    dates, closes, ivs, rates = s["dates"], s["closes"], s["ivs"], s["rates"]
    rsi, sma200 = s["rsi"], s["sma200"]
    n = len(dates)
    start_i = next((i for i, d in enumerate(dates) if d >= p["start"]), 0)
    end_i = max(i for i, d in enumerate(dates) if d <= p["end"])
    start_i = max(start_i, 205, 260)

    trades = []
    pos = None
    equity = 1.0
    curve = []
    days_in_market = 0
    day_count = 0

    for i in range(start_i, end_i + 1):
        S = closes[i]
        sigma = max(0.05, ivs[i] / 100.0)
        r = max(0.0005, rates[i] / 100.0)
        q = p["q_div"]
        day_count += 1

        if pos is not None:
            days_in_market += 1
            T = max(0.0, (pos["expiry"] - date.fromisoformat(dates[i])).days / 365.0)
            prem = bs_call(S, pos["strike"], T, r, q, sigma)
            dte = (pos["expiry"] - date.fromisoformat(dates[i])).days
            hold = i - pos["entry_i"]
            cost = sum(l["qty"] * l["px"] for l in pos["lots"])
            val = sum(l["qty"] for l in pos["lots"]) * prem
            pnl = (val / cost - 1.0) * 100.0 if cost > 0 else 0.0

            # 分批止盈（仅一次，平一半）—— 两种触发：盈利阈值 或 RSI 阈值
            do_half = False
            if p["half_tp"] and not pos["half_done"] and pnl / 100.0 >= p["half_tp"]:
                do_half = True
            if p["tp_rsi_half"] and not pos["half_done"] and rsi[i] is not None and rsi[i] > p["tp_rsi_half"]:
                do_half = True
            if do_half:
                lots = pos["lots"]
                tot = sum(l["qty"] for l in lots)
                sell = tot // 2
                if sell > 0:
                    proceeds = sell * prem * (1 - p["spread_pct"] / 2)
                    pos["realized"] += proceeds
                    pos["realized_cost"] += (cost / tot) * sell
                    left = tot - sell
                    pos["lots"] = [dict(qty=left, px=(cost / tot) * left / left if left else 0.0)]
                    pos["half_done"] = True
                    pos["sold_qty"] += sell

            # 退出判定（先到先出）
            reason = None
            if rsi[i] is not None and p["tp_rsi"] and rsi[i] > p["tp_rsi"]:
                reason = "RSI_TP"
            elif p["stop_pct"] and pnl <= -abs(p["stop_pct"]) * 100.0:
                reason = "STOP"
            elif p["time_stop_days"] and hold >= p["time_stop_days"] and pnl < 0:
                reason = "TIME_STOP"
            elif p["dte_force"] and dte <= p["dte_force"]:
                reason = "DTE_FORCE"

            if reason:
                lots = pos["lots"]
                tot = sum(l["qty"] for l in lots)
                cost = sum(l["qty"] * l["px"] for l in lots)
                gross = tot * prem
                net = gross * (1 - p["spread_pct"] / 2)
                realized = pos["realized"]
                realized_cost = pos["realized_cost"]
                proceeds = net + realized
                total_cost = cost + realized_cost
                ret = (proceeds / total_cost - 1.0) if total_cost > 0 else 0.0
                scale = 100.0 / p["ndx_per_qqq"]     # 指数点位 -> QQQ 等价价 -> 每张 100 股美元
                pnl_usd = total_cost * scale * ret
                trades.append(dict(entry=pos["entry_date"], exit=dates[i], ret=ret,
                                   hold=hold, reason=reason, qty=sum(l["qty"] for l in pos["lots"]) + pos["sold_qty"],
                                   base=pos["base"], strike=pos["strike"], expiry=pos["expiry"].isoformat(),
                                   entry_px=pos["entry_px"], exit_px=prem, adds=pos["add_count"],
                                   rsi_entry=pos["rsi_entry"], rsi_exit=rsi[i],
                                   cost_usd=total_cost * scale, pnl_usd=pnl_usd))
                equity *= (1 + ret)
                pos = None
            else:
                # 加仓
                if pos["add_count"] < len(p["add_levels"]) and sum(l["qty"] for l in pos["lots"]) < p["max_qty"]:
                    lvl = p["add_levels"][pos["add_count"]]
                    if S <= pos["base"] * (1 - lvl) and rsi[i] is not None and rsi[i] < p["entry_rsi"]:
                        buy = prem * (1 + p["spread_pct"] / 2)
                        pos["lots"].append(dict(qty=1, px=buy))
                        pos["add_count"] += 1

        if pos is None:
            ok = True
            if p["entry_rsi"] and (rsi[i] is None or not (rsi[i] < p["entry_rsi"])):
                ok = False
            if ok and p["trend_filter"]:
                if p["trend_filter"] == "sma200_3d":
                    if sma200[i] is None or not all(closes[j] > sma200[j] for j in (i, i - 1, i - 2)):
                        ok = False
                elif p["trend_filter"] == "sma200":
                    if sma200[i] is None or not closes[i] > sma200[i]:
                        ok = False
            if ok and p["one_year_filter"] and closes[i] <= closes[i - 252]:
                ok = False
            if ok:
                T = p["tenor_days"] / 365.0
                K = strike_for_delta(S, T, r, q, sigma, p["delta"])
                prem = bs_call(S, K, T, r, q, sigma)
                pos = dict(entry_date=dates[i], entry_i=i, base=S, strike=K,
                           expiry=date.fromisoformat(dates[i]) + timedelta(days=p["tenor_days"]),
                           lots=[dict(qty=1, px=prem * (1 + p["spread_pct"] / 2))],
                           add_count=0, rsi_entry=rsi[i], half_done=False, realized=0.0,
                           realized_cost=0.0, entry_px=prem, sold_qty=0)
        curve.append(equity if pos is None else equity * 1.0)

    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    years = (date.fromisoformat(dates[end_i]) - date.fromisoformat(dates[start_i])).days / 365.25
    cagr = (equity ** (1 / years) - 1) if equity > 0 and years > 0 else -1
    # 最大回撤（按交易序列复利）
    peak, mdd, eq = 1.0, 0.0, 1.0
    for t in trades:
        eq *= (1 + t["ret"])
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    out = dict(
        n=len(trades), win_rate=len(wins) / len(rets) if rets else 0,
        mean=statistics.mean(rets) if rets else 0,
        median=statistics.median(rets) if rets else 0,
        p10=sorted(rets)[max(0, int(0.1 * len(rets)) - 1)] if rets else 0,
        worst=min(rets) if rets else 0, best=max(rets) if rets else 0,
        equity=equity, cagr=cagr, mdd=mdd,
        avg_hold=statistics.mean([t["hold"] for t in trades]) if trades else 0,
        time_in_market=days_in_market / max(1, day_count),
        span="%s..%s" % (dates[start_i], dates[end_i]),
        years=years,
        # 固定本金口径（用户实盘口径：$100k 常驻、利润年末抽走、不滚动）
        usd_profit=sum(t["pnl_usd"] for t in trades) if trades else 0.0,
        usd_per_year=(sum(t["pnl_usd"] for t in trades) / years) if trades and years > 0 else 0.0,
        pool_pct_per_year=(sum(t["pnl_usd"] for t in trades) / years / p["pool"] * 100.0) if trades and years > 0 else 0.0,
        usd_cost_avg=statistics.mean([t["cost_usd"] for t in trades]) if trades else 0.0,
        trades_per_year=len(trades) / years if years > 0 else 0,
    )
    if collect_trades:
        out["trades"] = trades
    return out


if __name__ == "__main__":
    s = build_series()
    print("data:", s["dates"][0], "..", s["dates"][-1], "n=", len(s["dates"]),
          "| last close", round(s["closes"][-1], 2), "| last vxn", round(s["ivs"][-1], 2),
          "| last r", s["rates"][-1])
    base = run(series=s)
    print("\n=== 基线（当前线上参数, 2001-02 起, 无成本）===")
    for k in ("span", "n", "win_rate", "mean", "median", "p10", "worst", "best", "equity", "cagr", "mdd", "avg_hold", "time_in_market"):
        v = base[k]
        print("  %-15s %s" % (k, ("%.4f" % v) if isinstance(v, float) else v))
    print("\n  逐笔:")
    for t in base["trades"]:
        print("   %s -> %s  %+7.1f%%  hold=%3d  %-9s K=%s  qty=%d" %
              (t["entry"], t["exit"], t["ret"] * 100, t["hold"], t["reason"], t["strike"], t["qty"]))
