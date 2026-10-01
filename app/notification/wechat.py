"""企业微信通知统一格式化与发送层 (QQQ LEAPS)。

设计原则:
- 日报负责"今天市场怎么样"; 事件通知负责"刚刚发生了什么、我是否需要处理"。
- 全部消息为中文、结构化、带 emoji; 市场状态文字由明确规则生成, 不含主观投资建议。
- 本系统不连接券商, 不自动下单/平仓, 所有文案如实声明。
- formatter 为模块级纯函数; WeChatNotifier 方法保留为向后兼容的薄封装。
"""
import re
import requests
from typing import Dict, Optional, Any
from datetime import datetime
from pytz import timezone

et_tz = timezone("America/New_York")

SEPARATOR = "━━━━━━━━━━━━━━"

DEFAULT_ENTRY_RSI = 35.0
DEFAULT_TP_RSI = 65.0
DEFAULT_TIME_STOP_DAYS = 0
DEFAULT_DTE_FORCE_DAYS = 90
DEFAULT_TARGET_DELTA = 0.65
DEFAULT_TARGET_TENOR_DAYS = 365


def _redact_secrets(text: str) -> str:
    """移除日志中的 webhook key 等敏感参数, 防止 secret 泄漏到日志"""
    if not text:
        return text
    return re.sub(r"(key=)[^&\s'\"]+", r"\1***", text, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# 通用格式化 helper (全部带类型防御, 数据缺失渲染 N/A, 不误报)
# ---------------------------------------------------------------------------

def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fmt_price(value: Any) -> str:
    return f"{value:,.2f}" if _is_num(value) else "N/A"


def _fmt_num(value: Any, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if _is_num(value) else "N/A"


def _fmt_pct(value: Any, digits: int = 2) -> str:
    return f"{value:+.{digits}f}%" if _is_num(value) else "N/A"


def _fmt_pct_compact(value: Any) -> str:
    if not _is_num(value):
        return "N/A"
    return f"{value * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _change_pct(prev_close: Any, current: Any) -> Optional[float]:
    if _is_num(prev_close) and _is_num(current) and prev_close != 0:
        return (current - prev_close) / prev_close * 100.0
    return None


def _fmt_change(value: Optional[float], inverted: bool = False) -> str:
    """渲染涨跌幅: 默认上涨🟢/下跌🔴; inverted=True 时反转 (VIX 下跌🟢)"""
    if not _is_num(value):
        return ""
    up = value >= 0
    if inverted:
        up = not up
    return f"  {'🟢' if up else '🔴'} {value:+.2f}%"


def _fmt_date_cn(now: Optional[datetime] = None, with_time: bool = False) -> str:
    if now is None:
        now = datetime.now(et_tz)
    if now.tzinfo is not None:
        now = now.astimezone(et_tz)
    if with_time:
        return f"{now.year}年{now.month}月{now.day}日 {now.strftime('%H:%M')} ET"
    return f"{now.year}年{now.month}月{now.day}日"


def _market_section(current_price: Any, indicators: Dict[str, Any]) -> list:
    """行情区块: 价格/涨跌幅/RSI/SMA200/距SMA200"""
    indicators = indicators or {}
    lines = ["📈 纳指100 ETF (QQQ)"]
    change = _change_pct(indicators.get("prev_close"), current_price)
    lines.append(f"价格：{_fmt_price(current_price)}{_fmt_change(change)}")
    lines.append(f"RSI(14)：{_fmt_num(indicators.get('rsi'))}")
    sma = indicators.get("ma200", indicators.get("sma200"))
    lines.append(f"SMA200：{_fmt_price(sma)}")
    if _is_num(sma) and _is_num(current_price) and sma != 0:
        lines.append(f"距SMA200：{_fmt_pct((current_price - sma) / sma * 100.0)}")
    else:
        lines.append("距SMA200：N/A")
    return lines


def _suggest_contract_lines(position: Any) -> list:
    """建议合约区块 (0.65Δ / 约18个月 LEAPS call)"""
    delta = _get(position, "suggested_delta", DEFAULT_TARGET_DELTA)
    tenor = _get(position, "suggested_tenor_days", DEFAULT_TARGET_TENOR_DAYS)
    return [
        "📐 建议合约 (仅供参考)",
        f"类型：QQQ Call (LEAPS)",
        f"Delta：≈ {_fmt_num(delta)} (轻微实值)",
        f"期限：≈ {int(tenor)} 天 (约 {tenor/365*12:.0f} 个月) 到期",
        "张数：1 张起 (回落加仓, 见下方计划)",
    ]


def _add_plan_lines(add_levels: Any, max_qty: Any) -> list:
    levels = add_levels if isinstance(add_levels, (list, tuple)) else [0.15, 0.25]
    level_text = " / ".join(f"-{_fmt_pct_compact(l)}" for l in levels)
    return [
        "➕ 加仓计划",
        f"回撤档位：{level_text} (且 RSI 仍 < 入场阈值)",
        f"最大张数：{_fmt_num(max_qty, 0)} 张",
    ]


def _exit_rules_lines(tp_rsi: Any, ts_days: Any, dte_force: Any) -> list:
    lines = [
        "🚪 退出规则 (先到先出)",
        f"止盈：RSI(14) > {_fmt_num(tp_rsi, 0)}",
    ]
    if _is_num(ts_days) and ts_days > 0:
        lines.append(f"时间止损：持仓 {_fmt_num(ts_days, 0)} 个交易日仍未回本")
    lines.append(f"到期风控：距到期 < {_fmt_num(dte_force, 0)} 天强制提醒")
    return lines


# ---------------------------------------------------------------------------
# 事件 formatter
# ---------------------------------------------------------------------------

def format_leaps_entry(position: Any, qqq_data: Dict[str, Any], config: Any,
                       now: Optional[datetime] = None) -> str:
    """🚨 LEAPS 入场信号 (三条件全部满足, 已创建 WAITING 建议)"""
    qqq_data = qqq_data or {}
    entry_rsi = config.get_entry_rsi_threshold() if config else DEFAULT_ENTRY_RSI

    lines = [
        "🚨 QQQ LEAPS 入场信号",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "🟢 超卖 + 趋势过滤条件全部满足",
        f"（判定基准：已收盘日K {qqq_data.get('closed_bar_date') or 'N/A'}）",
        "",
    ]
    lines += _market_section(qqq_data.get("last_price"), qqq_data)
    lines += [
        "",
        "🎯 入场条件",
        f"RSI：{_fmt_num(qqq_data.get('closed_rsi'))} (阈值 < {_fmt_num(entry_rsi, 0)})",
        "SMA200：连续 3 日收盘在上方 ✓",
        "52周：收盘价高于一年前 ✓",
        "",
    ]
    lines += _suggest_contract_lines(position)
    lines += [""]
    lines += _add_plan_lines(
        config.get_add_levels() if config else None,
        config.get_max_quantity() if config else 3,
    )
    lines += [
        "",
        "💡 操作提示",
        "请在券商手动买入合约, 并到后台「持仓页」确认建仓;",
        "未确认的建议将在 TTL 后自动过期。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供信号及合约参数提醒,",
        "不连接券商, 不自动交易。",
    ]
    return "\n".join(lines)


def format_leaps_waiting_expired(position: Any, ttl_state: Optional[Dict[str, Any]] = None,
                                 now: Optional[datetime] = None) -> str:
    """⌛ WAITING 建议超时过期 (未确认, 信号闸门重新打开)"""
    ttl_state = ttl_state or {}
    ttl_days = ttl_state.get("ttl_trading_days")
    elapsed = ttl_state.get("elapsed_trading_days")

    lines = [
        "⌛ LEAPS 入场建议已过期",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "⚠️ 该入场信号已超过确认期限",
        f"记录 ID：{_get(position, 'id', 'N/A')}",
        f"信号基准价：{_fmt_price(_get(position, 'signal_base_price'))}",
        f"触发时 RSI：{_fmt_num(_get(position, 'signal_rsi'))}",
    ]
    if _is_num(ttl_days):
        lines.append(f"确认期限：{int(ttl_days)} 个交易日"
                     + (f"（已过 {int(elapsed)} 个交易日）" if _is_num(elapsed) else ""))
    lines += [
        "",
        "💡 说明",
        "该记录已标记为 EXPIRED (终态), 未在券商执行。",
        "信号闸门已重新打开, 后续满足条件时会重新发出建议。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供信号提醒, 不连接券商, 不自动交易。",
    ]
    return "\n".join(lines)


def format_leaps_add_lot(position: Any, add_trigger: Dict[str, Any],
                         qqq_data: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """➕ LEAPS 加仓提醒 (回撤达下一档位且 RSI 仍在入场区)"""
    qqq_data = qqq_data or {}
    level = (add_trigger or {}).get("level")
    next_count = (add_trigger or {}).get("next_count")

    lines = [
        "➕ QQQ LEAPS 加仓提醒",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        f"🟢 已达到第 {next_count} 张加仓档位",
        f"（{(add_trigger or {}).get('reason') or 'N/A'}）",
        "",
    ]
    lines += _market_section(qqq_data.get("last_price"), qqq_data)
    lines += [
        "",
        "📐 当前仓位",
        f"信号基准价：{_fmt_price(_get(position, 'signal_base_price'))}",
        f"已持张数：{_fmt_num(_get(position, 'quantity'), 0)}",
        f"累计成本：{_fmt_price(_get(position, 'total_cost'))}",
        "",
        "💡 操作提示",
        "如执行加仓, 请到后台「持仓页」录入加仓张数与成交价;",
        "不执行则忽略本提醒 (同一档位不会重复提醒)。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供提醒, 不连接券商, 不自动交易。",
    ]
    return "\n".join(lines)


def format_leaps_exit(position: Any, breach: Dict[str, Any], qqq_data: Dict[str, Any],
                      now: Optional[datetime] = None) -> str:
    """🚪 LEAPS 退出提醒 (RSI 止盈 / 时间止损 / DTE 强制平仓)"""
    qqq_data = qqq_data or {}
    btype = (breach or {}).get("type") or "MANUAL"
    title = {
        "RSI_TP": "🎉 QQQ LEAPS 止盈信号",
        "TIME_STOP": "⛔ QQQ LEAPS 时间止损",
        "DTE_FORCE": "⏰ QQQ LEAPS 到期风控",
    }.get(btype, "🚪 QQQ LEAPS 退出提醒")
    headline = {
        "RSI_TP": "RSI 已突破止盈阈值, 建议全部清仓",
        "TIME_STOP": "持仓超期且未回本, 建议平仓止损",
        "DTE_FORCE": "距到期日已不足风控天数, 强制清仓提醒",
    }.get(btype, "触发退出条件, 建议平仓")

    pnl = _get(position, "current_premium")
    entry = _get(position, "entry_price")
    pnl_text = "N/A"
    if _is_num(pnl) and _is_num(entry) and entry > 0:
        pnl_text = f"{(pnl / entry - 1) * 100:+.1f}%"

    lines = [
        title,
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        f"⚠️ {headline}",
        f"（{(breach or {}).get('reason') or 'N/A'}）",
        "",
    ]
    lines += _market_section(qqq_data.get("last_price"), qqq_data)
    lines += [
        "",
        "📐 当前仓位",
        f"合约：QQQ Call {_fmt_price(_get(position, 'strike'))} @ "
        f"{_get(position, 'expiration_date') or 'N/A'}",
        f"张数：{_fmt_num(_get(position, 'quantity'), 0)} (加仓 {_fmt_num(_get(position, 'add_count'), 0)} 次)",
        f"入场权利金：{_fmt_price(entry)} / 张",
        f"最新权利金：{_fmt_price(pnl)} (PnL {pnl_text})",
        f"累计成本：{_fmt_price(_get(position, 'total_cost'))}",
        "",
        "💡 操作提示",
        "请在券商手动平仓, 并到后台「持仓页」确认关闭;",
        "系统不会自动交易。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供提醒, 不连接券商, 不自动交易。",
    ]
    return "\n".join(lines)


def format_leaps_half_tp(position: Any, half_tp_trigger: Dict[str, Any],
                         qqq_data: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """💰 LEAPS 分批止盈提醒 (总盈利≥阈值, 建议卖出一半; 仅提醒, 不改仓位状态)"""
    qqq_data = qqq_data or {}
    sell_qty = int((half_tp_trigger or {}).get("sell_qty") or 0)
    qty = int((half_tp_trigger or {}).get("quantity") or 0)
    entry = _get(position, "entry_price")
    pnl = _get(position, "current_premium")
    pnl_text = "N/A"
    if _is_num(pnl) and _is_num(entry) and entry > 0:
        pnl_text = f"{(pnl / entry - 1) * 100:+.1f}%"

    lines = [
        "💰 QQQ LEAPS 分批止盈提醒",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        f"✅ 总盈利已达 +{(half_tp_trigger or {}).get('threshold', 0.5) * 100:.0f}%, 建议卖出一半锁定利润",
        f"（{(half_tp_trigger or {}).get('reason') or 'N/A'}）",
        "",
    ]
    lines += _market_section(qqq_data.get("last_price"), qqq_data)
    lines += [
        "",
        "📐 当前仓位",
        f"合约：QQQ Call {_fmt_price(_get(position, 'strike'))} @ "
        f"{_get(position, 'expiration_date') or 'N/A'}",
        f"张数：{_fmt_num(_get(position, 'quantity'), 0)} (建议卖出 {sell_qty} 张, 保留 {qty - sell_qty} 张)",
        f"入场权利金：{_fmt_price(entry)} / 张",
        f"最新权利金：{_fmt_price(pnl)} (PnL {pnl_text})",
        "",
        "💡 操作提示",
        f"如执行请在券商卖出 {sell_qty} 张, 然后到后台「持仓页」点「部分平仓」录入实际卖出价;",
        "剩余仓位继续按 RSI 止盈 / DTE 风控跟踪。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供提醒, 不连接券商, 不自动交易。",
    ]
    return "\n".join(lines)


def format_leaps_data_stale(data_timestamp: Any = None, now: Optional[datetime] = None) -> str:
    """⚠️ 数据过期 (fail-closed: 监控跳过, 不触发任何信号)"""
    ts_text = str(data_timestamp) if data_timestamp else "N/A"
    return "\n".join([
        "⚠️ QQQ 行情数据过期",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "QQQ 行情数据未能在允许时间内更新。",
        "",
        "数据状态：🔴 STALE",
        f"最后有效数据：{ts_text}",
        "",
        "💡 本次 LEAPS 监控已跳过,",
        "系统不会基于过期行情触发新的信号。",
        "",
        SEPARATOR,
    ])


def format_leaps_data_unavailable(now: Optional[datetime] = None) -> str:
    """⚠️ 数据不可用 (fail-closed: 监控跳过, 不执行信号判断)"""
    return "\n".join([
        "⚠️ QQQ 行情数据不可用",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "当前无法获取有效 QQQ 行情数据。",
        "",
        "数据状态：🔴 UNAVAILABLE",
        "",
        "💡 本次 LEAPS 监控已跳过。",
        "系统不会在数据异常时执行信号判断。",
        "",
        "请检查行情数据源。",
        "",
        SEPARATOR,
    ])


# ---------------------------------------------------------------------------
# 每日简报
# ---------------------------------------------------------------------------

def format_leaps_daily_report(data: Dict, now: Optional[datetime] = None) -> str:
    """📊 每日 QQQ 市场简报 (LEAPS 模式)。纯展示层: 只格式化 dashboard 数据。"""
    qqq = data.get("dashboard", {}).get("qqq") or {}
    holding = data.get("dashboard", {}).get("holding_position")
    waiting = data.get("dashboard", {}).get("waiting_position")
    latest = data.get("dashboard", {}).get("latest_position") or {}
    strategy = data.get("strategy", {})
    breadth = data.get("breadth") if isinstance(data.get("breadth"), dict) else None

    price = qqq.get("last_price")
    rsi = qqq.get("rsi")
    sma = qqq.get("ma200")
    price_1y_ago = qqq.get("price_1y_ago")
    entry_rsi = strategy.get("entry_rsi", DEFAULT_ENTRY_RSI)
    tp_rsi = strategy.get("tp_rsi", DEFAULT_TP_RSI)

    data_valid = bool(qqq.get("is_data_valid"))
    data_fresh = bool(qqq.get("is_data_fresh"))
    if data_valid and data_fresh:
        data_state = "🟢 正常"
    elif data_valid:
        data_state = "🟡 过期"
    else:
        data_state = "🔴 不可用"

    change = _change_pct(qqq.get("prev_close"), price)

    lines = [
        "📊 QQQ 每日市场简报 (LEAPS)",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now)}",
        "",
        "📈 纳指100 ETF (QQQ)",
        f"价格：{_fmt_price(price)}{_fmt_change(change)}",
        f"RSI(14)：{_fmt_num(rsi)}",
        f"SMA200：{_fmt_price(sma)}",
    ]
    if _is_num(sma) and _is_num(price) and sma != 0:
        lines.append(f"距SMA200：{_fmt_pct((price - sma) / sma * 100.0)}")
    else:
        lines.append("距SMA200：N/A")
    if _is_num(price) and _is_num(price_1y_ago) and price_1y_ago != 0:
        lines.append(f"过去1年：{_fmt_pct((price - price_1y_ago) / price_1y_ago * 100.0)}")
    else:
        lines.append("过去1年：N/A")
    lines.append(f"数据状态：{data_state}")

    sp = (breadth or {}).get("sp500") or {}
    vix = (breadth or {}).get("vix") or {}
    lines += [
        "",
        "🇺🇸 美股大盘",
        f"标普500：{_fmt_price(sp.get('price') if isinstance(sp, dict) else None)}"
        f"{_fmt_change(sp.get('change_pct') if isinstance(sp, dict) else None)}",
        f"VIX：{_fmt_num(vix.get('price') if isinstance(vix, dict) else None)}"
        f"{_fmt_change(vix.get('change_pct') if isinstance(vix, dict) else None, inverted=True)}",
    ]

    # 入场信号状态
    rsi_ok = _is_num(rsi) and _is_num(entry_rsi) and rsi < entry_rsi
    trend_ok = bool(qqq.get("is_above_sma200_3d"))
    y1y_ok = (_is_num(price) and _is_num(price_1y_ago) and price > price_1y_ago)
    signal_all = rsi_ok and trend_ok and y1y_ok
    lines += [
        "",
        "🎯 入场信号",
        f"状态：{'🟢 已触发' if signal_all else '⚪ 未触发'}",
        f"RSI < {_fmt_num(entry_rsi, 0)}：{'✓' if rsi_ok else '✗'} ({_fmt_num(rsi)})",
        f"SMA200 三日上方：{'✓' if trend_ok else '✗'}",
        f"高于一年前：{'✓' if y1y_ok else '✗'}",
    ]

    # 仓位状态 (含距离信息: 距止盈 RSI / 距下一加仓档 / 持仓天数与 DTE / WAITING TTL)
    lines += ["", "📐 仓位状态"]
    if holding:
        pnl_text = "N/A"
        if _is_num(holding.get("current_premium")) and _is_num(holding.get("entry_price")) \
                and holding.get("entry_price"):
            pnl_text = f"{(holding['current_premium'] / holding['entry_price'] - 1) * 100:+.1f}%"
        lines += [
            f"当前：🟢 HOLDING (#{holding.get('id')})",
            f"合约：Call {_fmt_price(holding.get('strike'))} @ {holding.get('expiration_date') or 'N/A'}",
            f"张数：{_fmt_num(holding.get('quantity'), 0)} (加仓 {holding.get('add_count', 0)} 次)",
            f"PnL：{pnl_text}",
        ]
        summary = data.get("dashboard", {}).get("holding_summary") or {}
        hd = summary.get("holding_days")
        if _is_num(hd):
            ts_days = strategy.get("time_stop_days")
            ts_text = f" / 止损 {ts_days} 交易日" if _is_num(ts_days) and ts_days > 0 else ""
            lines.append(f"已持仓：{int(hd)} 个交易日{ts_text}")
        realized = holding.get("realized_premium")
        if _is_num(realized) and realized != 0:
            lines.append(f"已落袋：{realized:.2f} (部分平仓累计卖出权利金)")
        dte = summary.get("dte")
        if _is_num(dte):
            dte_force = strategy.get("dte_force_days")
            dte_text = f" (强制平仓线 {dte_force} 天)" if _is_num(dte_force) else ""
            lines.append(f"距到期：{int(dte)} 天{dte_text}")
        # 距下一加仓档
        if _is_num(rsi) and _is_num(tp_rsi):
            rsi_room = rsi - tp_rsi
            lines.append(f"距止盈：RSI 还差 {rsi_room:.2f} (阈值 > {_fmt_num(tp_rsi, 0)})")
        nxt_pct = summary.get("next_add_level_pct")
        nxt_dist = summary.get("next_add_distance_pct")
        if _is_num(nxt_pct) and _is_num(nxt_dist) and nxt_dist > 0:
            lines.append(
                f"距加仓：下一档 -{_fmt_pct_compact(nxt_pct)}, "
                f"现价高 {nxt_dist:.2f}% (触发价 {_fmt_price(summary.get('next_add_trigger_price'))})"
            )
        elif _is_num(nxt_pct) and _is_num(nxt_dist):
            lines.append("距加仓：已处于/穿过下一档位 (待 RSI 条件确认)")
    elif waiting:
        lines.append(f"当前：🟡 WAITING — 等待确认建仓 (#{waiting.get('id')})")
        ttl = waiting.get("ttl") or {}
        if ttl.get("ttl_enabled") and _is_num(ttl.get("remaining_trading_days")):
            lines.append(
                f"建议有效期：剩 {int(ttl['remaining_trading_days'])} 个交易日 "
                f"(共 {ttl.get('ttl_trading_days')})"
            )
    else:
        if latest and latest.get("status") in ("CLOSED", "DISMISSED", "EXPIRED"):
            reason = f"（{latest.get('close_reason')}）" if latest.get("close_reason") else ""
            lines.append(f"当前：⚪ 无持仓 · 最近记录 #{latest.get('id')} {latest.get('status')}{reason}")
        else:
            lines.append("当前：⚪ 无持仓")
    if _is_num(tp_rsi) and not holding:
        lines.append(f"止盈线：RSI > {_fmt_num(tp_rsi, 0)}")

    # 市场状态
    market_lines = []
    if _is_num(sma) and _is_num(price) and sma != 0:
        pos_pct = (price - sma) / sma * 100.0
        market_lines.append(
            f"QQQ 当前位于 SMA200 {'上方' if pos_pct >= 0 else '下方'} {abs(pos_pct):.2f}%"
        )
    if _is_num(rsi) and _is_num(entry_rsi):
        if rsi <= entry_rsi:
            market_lines.append("RSI 已进入入场区域")
        else:
            market_lines.append(f"RSI 距入场阈值还有 {rsi - entry_rsi:.2f}")
    if market_lines:
        lines += ["", "💡 市场状态"] + market_lines

    lines += [
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供行情、信号及仓位提醒,",
        "不连接券商, 不自动交易。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# WeChatNotifier (发送层)
# ---------------------------------------------------------------------------

class WeChatNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_message(self, message: str) -> bool:
        """发送已格式化的完整消息文本 (AlertLog 应保存同一 message)"""
        return self._send_message(message)

    # ---- 便捷封装 ----

    def send_leaps_report(self, report_data: Dict) -> bool:
        return self.send_message(format_leaps_daily_report(report_data))

    def format_leaps_report(self, data: Dict) -> str:
        return format_leaps_daily_report(data)

    def _send_message(self, message: str) -> bool:
        if not self.webhook_url:
            print(f"[WARN] WeChat webhook URL not configured, skipping alert: {message[:100]}")
            return False

        try:
            payload = {
                "msgtype": "text",
                "text": {
                    "content": message,
                    "mentioned_list": []
                }
            }

            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("errcode") == 0:
                    print(f"[INFO] WeChat alert sent successfully")
                    return True
                else:
                    print(f"[ERROR] WeChat API error: {result}")
                    return False
            else:
                print(f"[ERROR] WeChat HTTP error: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            # 异常消息可能包含完整 webhook URL (含 key=SECRET), 脱敏后再输出
            print(f"[ERROR] Failed to send WeChat message: {_redact_secrets(str(e))}")
            return False


def get_wechat_notifier(webhook_url: str) -> WeChatNotifier:
    return WeChatNotifier(webhook_url)
