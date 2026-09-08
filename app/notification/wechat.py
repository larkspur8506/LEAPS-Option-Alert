"""企业微信通知统一格式化与发送层 (NDX Grid)。

设计原则:
- 日报负责"今天市场怎么样"; 事件通知负责"刚刚发生了什么、我是否需要处理"。
- 全部消息为中文、结构化、带 emoji; 市场状态文字由明确规则生成, 不含主观投资建议。
- 本系统不连接交易所, 不自动下单/平仓/重建 Grid, 所有文案如实声明。
- formatter 为模块级纯函数; WeChatNotifier 方法保留为向后兼容的薄封装。
"""
import re
import requests
from typing import Dict, Optional, Any
from datetime import datetime
from pytz import timezone

et_tz = timezone("America/New_York")

SEPARATOR = "━━━━━━━━━━━━━━"

DEFAULT_RSI_THRESHOLD = 35.0
DEFAULT_UPPER_PCT = 0.20
DEFAULT_LOWER_PCT = 0.20
DEFAULT_GRID_COUNT = 200
DEFAULT_LEVERAGE = 5.0
# Lower 被跌破后再向下触发止损提醒的默认比例 (运行时可覆盖)
DEFAULT_STOP_LOSS_AFTER_LOWER_PCT = 0.10


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


def _fmt_leverage(value: Any) -> str:
    return f"{value:.1f}x" if _is_num(value) else "N/A"


def _fmt_grid_count(value: Any) -> str:
    return f"{int(value)}格" if _is_num(value) else "N/A"


def _fmt_pct_compact(value: Any) -> str:
    """紧凑百分比: 0.10 -> '10%', 0.05 -> '5%', 0.125 -> '12.5%'"""
    if not _is_num(value):
        return "N/A"
    return f"{value * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _stop_loss_alert_price(lower_price: Any, stop_loss_after_lower_pct: Any) -> Optional[float]:
    """
    止损提醒线 = Lower × (1 - stop_loss_after_lower_pct)。
    百分比相对于 Lower (而非 Base); 数据不完整时返回 None (渲染 N/A)。
    """
    if (
        _is_num(lower_price) and lower_price > 0
        and _is_num(stop_loss_after_lower_pct) and 0 < stop_loss_after_lower_pct < 1
    ):
        return round(float(lower_price) * (1.0 - float(stop_loss_after_lower_pct)), 2)
    return None


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


def _grid_step(upper: Any, lower: Any, count: Any) -> Optional[float]:
    if _is_num(upper) and _is_num(lower) and _is_num(count) and count > 0:
        return (upper - lower) / count
    return None


def _market_section(current_price: Any, indicators: Dict[str, Any]) -> list:
    """行情区块: 价格/涨跌幅/RSI/SMA200/距SMA200"""
    indicators = indicators or {}
    lines = ["📈 纳斯达克100"]
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


def _entry_section(rsi: Any, threshold: Any) -> list:
    triggered = _is_num(rsi) and _is_num(threshold) and rsi < threshold
    status_text = "🟢 已触发" if triggered else "⚪ 未触发"
    return [
        "🎯 入场条件",
        f"RSI：{_fmt_num(rsi)}",
        f"入场阈值：{_fmt_num(threshold)}",
        f"状态：{status_text}",
    ]


# ---------------------------------------------------------------------------
# 事件 formatter (Step 2 统一命名)
# ---------------------------------------------------------------------------

def format_ndx_grid_entry(
    cycle: Any,
    current_price: float,
    indicators: Optional[Dict[str, Any]] = None,
    rsi_threshold: Any = DEFAULT_RSI_THRESHOLD,
    now: Optional[datetime] = None,
) -> str:
    """🟢 Grid Entry 入场信号 (Entry Signal 真正触发并创建 WAITING 后)"""
    indicators = indicators or {}
    if not _is_num(rsi_threshold):
        rsi_threshold = DEFAULT_RSI_THRESHOLD

    # 支持 GridCycle ORM (suggested_*) 与 grid_math 输出 dict (base_price 等)
    if isinstance(cycle, dict) and "suggested_base_price" not in cycle:
        base = cycle.get("base_price")
        upper = cycle.get("upper_price")
        lower = cycle.get("lower_price")
        count = cycle.get("grid_count")
        leverage = cycle.get("leverage")
        step = cycle.get("grid_step")
    else:
        base = _get(cycle, "suggested_base_price")
        upper = _get(cycle, "suggested_upper_price")
        lower = _get(cycle, "suggested_lower_price")
        count = _get(cycle, "suggested_grid_count")
        leverage = _get(cycle, "suggested_leverage")
        step = None
    if step is None:
        step = _grid_step(upper, lower, count)

    lines = [
        "🚨 NDX Grid 入场信号",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "🟢 RSI 已进入入场区域",
        "",
    ]
    lines += _market_section(current_price, indicators)
    lines += [""]
    lines += _entry_section(indicators.get("rsi"), rsi_threshold)
    lines += [
        "",
        "📐 建议建立 Grid",
        f"Base：{_fmt_price(base)}",
        f"Upper：{_fmt_price(upper)}",
        f"Lower：{_fmt_price(lower)}",
        f"网格：{_fmt_grid_count(count)}",
        f"每格：{_fmt_price(step)}",
        f"杠杆：{_fmt_leverage(leverage)}",
        "",
        "💡 操作提示",
        "当前已满足 Grid 入场条件。",
        "请在交易所手动建立对应 Grid,",
        "并在系统后台录入实际参数确认启动。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供信号及 Grid 参数提醒,",
        "不连接交易所, 不自动交易。",
    ]
    return "\n".join(lines)


def format_ndx_grid_proximity(
    current_price: float,
    rsi: Any,
    threshold: Any = DEFAULT_RSI_THRESHOLD,
    indicators: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """🟡 Grid 接近入场阈值 (RSI 处于 threshold ~ threshold+5 区间)"""
    indicators = indicators or {}
    if not _is_num(threshold):
        threshold = DEFAULT_RSI_THRESHOLD
    distance = (rsi - threshold) if (_is_num(rsi) and _is_num(threshold)) else None

    lines = [
        "🟡 NDX Grid 接近入场",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
    ]
    lines += _market_section(current_price, indicators)
    lines += [
        "",
        "🎯 入场信号",
        "状态：🟡 接近触发",
        f"入场阈值：{_fmt_num(threshold)}",
        f"距离阈值：{_fmt_num(distance)}",
        "",
        f"💡 RSI 继续下降至 {_fmt_num(threshold)} 以下,",
        "将触发 Grid 入场信号。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供行情及信号提醒,",
        "不构成投资建议。",
    ]
    return "\n".join(lines)


def _actual_grid_params(cycle: Any) -> list:
    base = _get(cycle, "actual_base_price")
    upper = _get(cycle, "actual_upper_price")
    lower = _get(cycle, "actual_lower_price")
    count = _get(cycle, "actual_grid_count")
    leverage = _get(cycle, "actual_leverage")
    return [
        f"Base：{_fmt_price(base)}",
        f"Upper：{_fmt_price(upper)}",
        f"Lower：{_fmt_price(lower)}",
        f"网格：{_fmt_grid_count(count)}",
        f"杠杆：{_fmt_leverage(leverage)}",
    ]


def format_ndx_grid_stopped(
    cycle: Any,
    current_price: float,
    indicators: Optional[Dict[str, Any]] = None,
    stop_loss_after_lower_pct: Any = DEFAULT_STOP_LOSS_AFTER_LOWER_PCT,
    now: Optional[datetime] = None,
) -> str:
    """🔴 Grid 跌破下轨 / STOPPED (RUNNING -> STOPPED, 进入风险观察阶段)

    重要语义: STOPPED != 已止损。跌破 Lower 只是停止网格运行并进入风险观察,
    只有价格从 Lower 继续下跌 stop_loss_after_lower_pct (默认 10%) 才发送止损提醒。
    """
    indicators = indicators or {}
    if not _is_num(stop_loss_after_lower_pct) or not (0 < stop_loss_after_lower_pct < 1):
        stop_loss_after_lower_pct = DEFAULT_STOP_LOSS_AFTER_LOWER_PCT
    reason = _get(cycle, "close_reason") or "LOWER_BREACHED"
    lower = _get(cycle, "actual_lower_price")
    stop_loss_price = _stop_loss_alert_price(lower, stop_loss_after_lower_pct)
    pct_text = _fmt_pct_compact(stop_loss_after_lower_pct)

    lines = [
        "🛑 NDX Grid 已跌破下轨",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "⚠️ Grid 已跌破 Lower，进入风险观察区",
        "",
    ]
    lines += _market_section(current_price, indicators)
    lines += [
        "",
        "🎯 风险观察",
        f"Grid Lower：{_fmt_price(lower)}",
        f"止损提醒线：{_fmt_price(stop_loss_price)}",
        f"当前价格：{_fmt_price(current_price)}",
        "",
        "📐 当前 Grid",
    ]
    lines += _actual_grid_params(cycle)
    lines += [
        "",
        "🔴 Grid 状态",
        "状态：STOPPED",
        "原因：价格跌破 Lower",
        "",
        "💡 操作提示",
        "Grid 已停止运行，目前进入风险观察阶段。",
        f"如果 NDX 从 Lower 继续下跌 {pct_text}，",
        "系统将发送止损提醒。",
        "",
        "系统不会自动平仓，",
        "也不会自动重新建立 Grid。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供行情、Grid 状态及风险提醒，",
        "不连接交易所，不自动交易。",
    ]
    return "\n".join(lines)


def format_ndx_grid_stop_loss(
    cycle: Any,
    current_price: float,
    indicators: Optional[Dict[str, Any]] = None,
    stop_loss_after_lower_pct: Any = DEFAULT_STOP_LOSS_AFTER_LOWER_PCT,
    now: Optional[datetime] = None,
) -> str:
    """🛑 NDX Grid 止损提醒 (Lower 被跌破后继续下跌 stop_loss_after_lower_pct)

    独立通知事件: 不改变 GridCycle 状态 (仍为 STOPPED), 不自动平仓, 不自动重建 Grid。
    止损提醒线 = actual_lower_price × (1 - stop_loss_after_lower_pct)。
    """
    indicators = indicators or {}
    if not _is_num(stop_loss_after_lower_pct) or not (0 < stop_loss_after_lower_pct < 1):
        stop_loss_after_lower_pct = DEFAULT_STOP_LOSS_AFTER_LOWER_PCT
    lower = _get(cycle, "actual_lower_price")
    stop_loss_price = _stop_loss_alert_price(lower, stop_loss_after_lower_pct)
    pct_text = _fmt_pct_compact(stop_loss_after_lower_pct)

    lines = [
        "🛑 NDX Grid 止损提醒",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        f"⚠️ NDX 已跌破 Grid 下轨，并进一步下跌 {pct_text}",
        "",
    ]
    lines += _market_section(current_price, indicators)
    lines += [
        "",
        "🎯 风险观察",
        f"Grid Lower：{_fmt_price(lower)}",
        f"止损提醒线：{_fmt_price(stop_loss_price)}",
        f"当前价格：{_fmt_price(current_price)}",
        "状态：🔴 已触发",
        "",
        "📐 当前 Grid",
    ]
    lines += _actual_grid_params(cycle)
    lines += [
        "",
        "🔴 Grid 状态",
        "状态：STOPPED",
        f"原因：价格跌破 Lower 后继续下跌 {pct_text}",
        "",
        "💡 操作提示",
        "当前已经触发止损提醒。",
        "请检查交易所实际 Grid 及持仓情况，",
        "并根据实际情况手动处理。",
        "",
        "系统不会自动平仓，",
        "也不会自动重新建立 Grid。",
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供行情、Grid 状态及风险提醒，",
        "不连接交易所，不自动交易。",
    ]
    return "\n".join(lines)


def format_ndx_grid_closed(
    cycle: Any,
    current_price: float,
    indicators: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """🟢 Grid Upper / CLOSED (RUNNING -> CLOSED, 触及上限)"""
    indicators = indicators or {}
    reason = _get(cycle, "close_reason") or "UPPER_REACHED"

    lines = [
        "🎉 NDX Grid 已触及上限",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "🚀 Grid 上限已触发",
        "",
    ]
    lines += _market_section(current_price, indicators)
    lines += [
        "",
        "📐 当前 Grid",
    ]
    lines += _actual_grid_params(cycle)
    lines += [
        "",
        "🟢 Grid 状态",
        "状态：CLOSED",
        f"触发原因：{reason}",
        "",
        "💡 操作提示",
        "当前 Grid 已完成本轮运行。",
        "新的 Grid 不会自动建立,",
        "需等待下一次入场信号。",
        "",
        SEPARATOR,
        "⚠️ 本系统不连接交易所,",
        "不会自动交易。",
    ]
    return "\n".join(lines)


def format_ndx_grid_manual_close(
    cycle: Any,
    current_price: Any = None,
    indicators: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """🔄 Grid 手动关闭 (MANUAL_CLOSE, 记录性质通知)"""
    indicators = indicators or {}

    lines = [
        "🔄 NDX Grid 状态更新",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "📐 Grid",
        "状态：🔵 已手动关闭",
        "",
    ]
    lines += _actual_grid_params(cycle)
    change = _change_pct(indicators.get("prev_close"), current_price)
    lines += [
        "",
        "📈 NDX 当前价格",
        f"{_fmt_price(current_price)}{_fmt_change(change)}",
        "",
        "💡 本轮 Grid 已结束。",
        "系统将等待下一次入场信号。",
        "",
        SEPARATOR,
    ]
    return "\n".join(lines)


def format_ndx_grid_data_stale(
    data_timestamp: Any = None,
    now: Optional[datetime] = None,
) -> str:
    """⚠️ 数据过期 (fail-closed: 监控跳过, 不触发任何信号)"""
    ts_text = str(data_timestamp) if data_timestamp else "N/A"
    return "\n".join([
        "⚠️ NDX 行情数据过期",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "NDX 行情数据未能在允许时间内更新。",
        "",
        "数据状态：🔴 STALE",
        f"最后有效数据：{ts_text}",
        "",
        "💡 本次 Grid 监控已跳过,",
        "系统不会基于过期行情触发新的信号。",
        "",
        "请检查行情数据源。",
        "",
        SEPARATOR,
    ])


def format_ndx_grid_data_unavailable(now: Optional[datetime] = None) -> str:
    """⚠️ 数据不可用 (fail-closed: 监控跳过, 不执行信号判断)"""
    return "\n".join([
        "⚠️ NDX 行情数据不可用",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now, with_time=True)}",
        "",
        "当前无法获取有效 NDX 行情数据。",
        "",
        "数据状态：🔴 UNAVAILABLE",
        "",
        "💡 本次 Grid 监控已跳过。",
        "系统不会在数据异常时执行信号判断。",
        "",
        "请检查行情数据源。",
        "",
        SEPARATOR,
    ])


# ---------------------------------------------------------------------------
# 每日市场简报 (正式确认模板)
# ---------------------------------------------------------------------------

_DATA_STATE_TEXT = {
    ("FRESH",): "🟢 正常",
}
_SIGNAL_STATE_TEXT = {True: "🟢 已触发", False: "⚪ 未触发", None: "❓ 未评估"}
_CYCLE_STATUS_TEXT = {"CLOSED": "🟢 CLOSED", "STOPPED": "🔴 STOPPED"}


def format_ndx_grid_daily_report(data: Dict, now: Optional[datetime] = None) -> str:
    """📊 每日 NDX 市场简报。

    纯展示层: 只格式化 dashboard 数据, 不计算任何网格/信号逻辑。
    数据缺失/异常时渲染 N/A / 未评估, 不误报。
    """
    ndx = data.get("dashboard", {}).get("ndx") or {}
    running = data.get("dashboard", {}).get("running_cycle")
    waiting = data.get("dashboard", {}).get("waiting_cycle")
    theo = data.get("dashboard", {}).get("theoretical_grid_position")
    latest = data.get("latest_cycle") or {}
    strategy = data.get("strategy", {})
    breadth = data.get("breadth") if isinstance(data.get("breadth"), dict) else None

    price = ndx.get("last_price")
    rsi = ndx.get("rsi")
    sma = ndx.get("ma200")
    price_1y_ago = ndx.get("price_1y_ago")
    rsi_threshold = strategy.get("rsi_threshold")
    if not _is_num(rsi_threshold):
        rsi_threshold = DEFAULT_RSI_THRESHOLD
    upper_pct = strategy.get("upper_pct", DEFAULT_UPPER_PCT)
    lower_pct = strategy.get("lower_pct", DEFAULT_LOWER_PCT)
    grid_count = strategy.get("grid_count", DEFAULT_GRID_COUNT)
    leverage = strategy.get("leverage", DEFAULT_LEVERAGE)
    stop_loss_after_lower_pct = strategy.get(
        "stop_loss_after_lower_pct", DEFAULT_STOP_LOSS_AFTER_LOWER_PCT
    )
    if not _is_num(stop_loss_after_lower_pct) or not (0 < stop_loss_after_lower_pct < 1):
        stop_loss_after_lower_pct = DEFAULT_STOP_LOSS_AFTER_LOWER_PCT
    stop_loss_alerted = bool(data.get("stop_loss_alerted"))

    # 数据状态 (明确规则, 无主观判断)
    data_valid = bool(ndx.get("is_data_valid"))
    data_fresh = bool(ndx.get("is_data_fresh"))
    if data_valid and data_fresh:
        data_state = "🟢 正常"
    elif data_valid:
        data_state = "🟡 过期"
    else:
        data_state = "🔴 不可用"

    # 日报涨跌幅 (prev_close 由 scheduler 注入, 缺失则不显示)
    change = _change_pct(ndx.get("prev_close"), price)

    lines = [
        "📊 NDX 每日市场简报",
        SEPARATOR,
        f"📅 {_fmt_date_cn(now)}",
        "",
        "📈 纳斯达克100",
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

    # 辅助指标: S&P 500 / VIX (缺失显示 N/A, 不影响主数据)
    sp = (breadth or {}).get("sp500") or {}
    vix = (breadth or {}).get("vix") or {}
    sp_price = sp.get("price") if isinstance(sp, dict) else None
    vix_price = vix.get("price") if isinstance(vix, dict) else None
    lines += [
        "",
        "🇺🇸 美股大盘",
        f"标普500：{_fmt_price(sp_price)}{_fmt_change(sp.get('change_pct') if isinstance(sp, dict) else None)}",
        f"VIX：{_fmt_num(vix_price)}{_fmt_change(vix.get('change_pct') if isinstance(vix, dict) else None, inverted=True)}",
    ]

    # 入场信号 (规则判定结果, 不做主观解读)
    signal = ndx.get("entry_signal")
    signal_text = _SIGNAL_STATE_TEXT.get(signal, "❓ 未评估")
    if _is_num(rsi) and _is_num(rsi_threshold):
        distance = rsi - rsi_threshold
        distance_text = f"{distance:.2f}" if distance > 0 else "0.00"
    else:
        distance_text = "N/A"
    lines += [
        "",
        "🎯 Grid 入场信号",
        f"状态：{signal_text}",
        f"RSI(14)：{_fmt_num(rsi)}",
        f"入场阈值：{_fmt_num(rsi_threshold)}",
        f"距离阈值：{distance_text}",
    ]

    # Grid 状态 (只有需要用户操作/状态变化时才展开完整参数)
    lines += ["", "📐 Grid 状态"]
    if running:
        lines.append("当前：🟢 RUNNING")
        if theo and isinstance(theo, dict) and theo.get("grid_interval_index") is not None:
            lines.append(
                f"理论位置：第 {theo.get('grid_interval_index')} / {_fmt_grid_count(theo.get('grid_count'))}"
            )
        if running.get("started_at"):
            lines.append(f"启动时间：{running.get('started_at')}")
    elif waiting:
        lines.append("当前：🟡 WAITING — 等待确认启动")
        lines += [
            f"Base：{_fmt_price(_get(waiting, 'suggested_base_price'))}",
            f"Upper：{_fmt_price(_get(waiting, 'suggested_upper_price'))}",
            f"Lower：{_fmt_price(_get(waiting, 'suggested_lower_price'))}",
            f"网格：{_fmt_grid_count(_get(waiting, 'suggested_grid_count'))}",
            f"杠杆：{_fmt_leverage(_get(waiting, 'suggested_leverage'))}",
        ]
    else:
        # 最新一轮为 STOPPED 时展开风险观察详情 (最近一轮 CLOSED 仍走简洁展示)
        latest_is_stopped = bool(latest) and latest.get("status") == "STOPPED"
        if latest_is_stopped:
            if stop_loss_alerted:
                lines.append("当前：🛑 STOPPED / 已触发止损提醒")
            else:
                lines.append("当前：🔴 STOPPED / 风险观察")
            stop_loss_price = _stop_loss_alert_price(
                latest.get("actual_lower_price"), stop_loss_after_lower_pct
            )
            lines += [
                "",
                f"Base：{_fmt_price(latest.get('actual_base_price'))}",
                f"Upper：{_fmt_price(latest.get('actual_upper_price'))}",
                f"Lower：{_fmt_price(latest.get('actual_lower_price'))}",
                f"止损提醒线：{_fmt_price(stop_loss_price)}",
                f"网格：{_fmt_grid_count(latest.get('actual_grid_count'))}",
                f"杠杆：{_fmt_leverage(latest.get('actual_leverage'))}",
            ]
        else:
            lines.append("当前：⚪ 无运行中的 Grid")
            if latest and latest.get("status"):
                status_text = _CYCLE_STATUS_TEXT.get(latest.get("status"), latest.get("status"))
                if latest.get("close_reason"):
                    status_text += f"（{latest.get('close_reason')}）"
                lines.append(f"最近一轮：{status_text}")
            if _is_num(upper_pct) and _is_num(lower_pct) and upper_pct == lower_pct:
                range_text = f"±{upper_pct * 100:.0f}%"
            else:
                range_text = f"+{_fmt_num(upper_pct * 100, 0)}% / -{_fmt_num(lower_pct * 100, 0)}%"
            lines += [
                f"默认区间：{range_text}",
                f"网格：{_fmt_grid_count(grid_count)}",
                f"杠杆：{_fmt_leverage(leverage)}",
            ]

    # 市场状态: 纯事实规则生成, 不含建议/预测
    market_lines = []
    if _is_num(sma) and _is_num(price) and sma != 0:
        pos_pct = (price - sma) / sma * 100.0
        market_lines.append(
            f"NDX 当前位于 SMA200 {'上方' if pos_pct >= 0 else '下方'} {abs(pos_pct):.2f}%"
        )
    if _is_num(price) and _is_num(price_1y_ago) and price_1y_ago != 0:
        y_pct = (price - price_1y_ago) / price_1y_ago * 100.0
        market_lines.append(f"过去一年{'上涨' if y_pct >= 0 else '下跌'} {abs(y_pct):.2f}%")
    if _is_num(rsi) and _is_num(rsi_threshold):
        if rsi <= rsi_threshold:
            market_lines.append("RSI 已进入入场区域")
        else:
            market_lines.append(
                f"RSI 尚未进入入场区域, 距离入场阈值还有 {rsi - rsi_threshold:.2f}"
            )
    if market_lines:
        lines += ["", "💡 市场状态"] + market_lines

    lines += [
        "",
        SEPARATOR,
        "⚠️ 本系统仅提供行情、信号及 Grid 状态提醒,",
        "不连接交易所, 不自动交易。",
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

    # ---- 向后兼容的便捷封装 (内部委托统一 formatter) ----

    def send_ndx_grid_report(self, report_data: Dict) -> bool:
        return self.send_message(format_ndx_grid_daily_report(report_data))

    def send_ndx_entry_alert(self, cycle, current_price: float, indicators: Optional[Dict] = None,
                             rsi_threshold: Any = DEFAULT_RSI_THRESHOLD) -> bool:
        return self.send_message(format_ndx_grid_entry(cycle, current_price, indicators=indicators,
                                                       rsi_threshold=rsi_threshold))

    def send_ndx_upper_alert(self, cycle, current_price: float, indicators: Optional[Dict] = None) -> bool:
        return self.send_message(format_ndx_grid_closed(cycle, current_price, indicators=indicators))

    def send_ndx_lower_alert(self, cycle, current_price: float, indicators: Optional[Dict] = None) -> bool:
        return self.send_message(format_ndx_grid_stopped(cycle, current_price, indicators=indicators))

    # ---- formatter 薄封装 (保持实例方法可用) ----

    def format_ndx_grid_report(self, data: Dict) -> str:
        return format_ndx_grid_daily_report(data)

    def format_ndx_entry_alert(self, cycle, current_price: float, **kwargs) -> str:
        return format_ndx_grid_entry(cycle, current_price, **kwargs)

    def format_ndx_upper_alert(self, cycle, current_price: float, **kwargs) -> str:
        return format_ndx_grid_closed(cycle, current_price, **kwargs)

    def format_ndx_lower_alert(self, cycle, current_price: float, **kwargs) -> str:
        return format_ndx_grid_stopped(cycle, current_price, **kwargs)

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
