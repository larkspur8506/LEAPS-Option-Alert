"""期权定价与仓位规模换算 (纯函数, 无网络/DB 依赖)。

用途:
  1) 按目标 delta + 期限反解建议行权价 (Black-Scholes + IV 假设);
  2) 到期日对齐到标准月度到期 (第三个周五), 便于券商链上有该合约;
  3) 按资金额度上限换算可买张数 (额度 / 单张权利金)。

回测依据见 devops/qqq-option-alert-ops「回测复核与优化」节:
  - Δ 0.65 -> 0.70 是唯一跨样本 (2001-2026 与 1987-2026) 同时改善收益与尾部的改动;
  - 池级收益几乎完全由下单规模决定: 固定 1 张 ≈ +0.5%/年, 满仓 (额度/单张成本) ≈ +14-18%/年。
"""
import math
from datetime import date, timedelta
from statistics import NormalDist
from typing import Optional, Tuple

_ND = NormalDist()

DEFAULT_RATE = 0.04          # 无风险利率假设 (建议行权价用)
DEFAULT_DIV_YIELD = 0.0055   # QQQ 分红率假设
DEFAULT_IV = 0.22            # 期权 IV 假设 (真实报价优先用于张数换算)
DEFAULT_ROUND_TO = 5.0       # 行权价取整档位 ($5)


def bs_call_price(spot: float, strike: float, tenor_days: float,
                  rate: float = DEFAULT_RATE, div_yield: float = DEFAULT_DIV_YIELD,
                  iv: float = DEFAULT_IV) -> float:
    """Black-Scholes 看涨期权理论价 (每股价)。"""
    t = max(0.0, float(tenor_days) / 365.0)
    if t <= 0 or iv <= 0:
        return max(0.0, float(spot) - float(strike))
    sq = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate - div_yield + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    return spot * math.exp(-div_yield * t) * _ND.cdf(d1) - strike * math.exp(-rate * t) * _ND.cdf(d2)


def strike_for_delta(spot: float, tenor_days: float, target_delta: float,
                     iv: float = DEFAULT_IV, rate: float = DEFAULT_RATE,
                     div_yield: float = DEFAULT_DIV_YIELD,
                     round_to: float = DEFAULT_ROUND_TO) -> float:
    """按目标 delta 反解行权价 (Δ_call = e^{-qT}·N(d1) = target)。"""
    t = max(1.0, float(tenor_days)) / 365.0
    tgt = min(0.95, max(0.05, float(target_delta) * math.exp(div_yield * t)))
    d1 = _ND.inv_cdf(tgt)
    strike = spot * math.exp((rate - div_yield + 0.5 * iv * iv) * t - d1 * iv * math.sqrt(t))
    return round(strike / round_to) * round_to


def third_friday(year: int, month: int) -> date:
    """当月第三个周五 (美股标准月度到期日)。"""
    d = date(year, month, 1)
    first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + timedelta(days=14)


def standard_expiry(target: date) -> date:
    """把目标日期对齐到「不早于它的最近一个标准月度到期日」。"""
    y, m = target.year, target.month
    for _ in range(3):
        exp = third_friday(y, m)
        if exp >= target:
            return exp
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return third_friday(y, m)


def suggest_contract(spot: float, tenor_days: int, target_delta: float,
                     iv: float = DEFAULT_IV, rate: float = DEFAULT_RATE,
                     div_yield: float = DEFAULT_DIV_YIELD,
                     today: Optional[date] = None) -> Tuple[float, date, float]:
    """返回 (建议行权价, 建议到期日, 理论权利金/股)。到期日对齐标准月度到期。"""
    today = today or date.today()
    strike = strike_for_delta(spot, tenor_days, target_delta, iv, rate, div_yield)
    expiry = standard_expiry(today + timedelta(days=int(tenor_days)))
    theo = bs_call_price(spot, strike, (expiry - today).days, rate, div_yield, iv)
    return strike, expiry, theo


def effective_max_contracts(hard_cap: int, capital_cap: float,
                            premium_per_share: float) -> int:
    """有效最大张数 = min(硬上限, 额度上限); 两者都无限制时返回 0 (表示不限)。"""
    cands = []
    try:
        h = int(hard_cap or 0)
    except (TypeError, ValueError):
        h = 0
    if h > 0:
        cands.append(h)
    c = contracts_for_capital(capital_cap, premium_per_share)
    if c > 0:
        cands.append(c)
    return min(cands) if cands else 0


def contracts_for_capital(capital_cap: float, premium_per_share: float,
                          min_contracts: int = 1) -> int:
    """按额度上限换算张数 = floor(额度 / (权利金×100)), 至少 min_contracts 张。

    capital_cap <= 0 表示不限额度 -> 返回 0 (调用方按「不限」处理)。
    premium_per_share <= 0 (无报价) -> 返回 min_contracts。
    """
    try:
        cap = float(capital_cap or 0)
        px = float(premium_per_share or 0)
    except (TypeError, ValueError):
        return min_contracts
    if cap <= 0:
        return 0
    if px <= 0:
        return min_contracts
    return max(min_contracts, int(cap // (px * 100.0)))
