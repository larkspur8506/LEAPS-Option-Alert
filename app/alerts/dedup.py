import threading
from datetime import datetime, date
from typing import Set, Dict
from pytz import timezone

et_tz = timezone("America/New_York")


class AlertDeduplicator:
    def __init__(self):
        self.daily_rules: Dict[str, Set[str]] = {}
        self.weekly_rules: Dict[str, Set[str]] = {}
        # proximity 状态去重: 同一 Grid cycle 进入"接近入场区域"只提醒一次,
        # RSI 离开区域 (或 cycle 结束/手动 reset) 后才允许再次提醒。
        self.proximity_active: Dict[str, bool] = {}
        # 止损提醒 (STOP_LOSS) cycle 级去重: 跌破 Lower 后继续下跌到止损提醒线,
        # 同一 Grid cycle 只提醒一次; 新 cycle (新 id) 自然拥有新的去重 key。
        self.stop_loss_alerted: Set[str] = set()

    def get_today_key(self) -> str:
        return datetime.now(et_tz).strftime("%Y-%m-%d")

    def get_iso_week_key(self) -> str:
        dt = datetime.now(et_tz)
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"

    def _get_rule_key(self, rule_name: str, position_id: int = None) -> str:
        if position_id is not None:
            return f"{rule_name}_pos_{position_id}"
        return rule_name

    def should_alert(self, rule_name: str, position_id: int = None) -> bool:
        today = self.get_today_key()

        if today not in self.daily_rules:
            self.daily_rules[today] = set()

        rule_key = self._get_rule_key(rule_name, position_id)

        if rule_key in self.daily_rules[today]:
            return False

        self.daily_rules[today].add(rule_key)
        return True

    def should_alert_weekly(self, rule_name: str, position_id: int = None) -> bool:
        week_key = self.get_iso_week_key()

        if week_key not in self.weekly_rules:
            self.weekly_rules[week_key] = set()

        rule_key = self._get_rule_key(rule_name, position_id)

        if rule_key in self.weekly_rules[week_key]:
            return False

        self.weekly_rules[week_key].add(rule_key)
        return True

    def reset_daily(self):
        today = self.get_today_key()
        old_days = [day for day in self.daily_rules.keys() if day != today]
        for old_day in old_days:
            del self.daily_rules[old_day]

        week_key = self.get_iso_week_key()
        old_weeks = [w for w in self.weekly_rules.keys() if w != week_key]
        for old_week in old_weeks:
            del self.weekly_rules[old_week]

    def should_alert_proximity(self, cycle_key: str) -> bool:
        """
        接近入场阈值事件去重 (状态型, 非按日)。

        规则: 同一 cycle key 首次进入 proximity 区域返回 True 并标记;
        之后连续返回 False, 直到 mark_proximity_exit / clear_cycle 被调用
        (RSI 离开区域、cycle 结束或系统 reset)。
        """
        if self.proximity_active.get(cycle_key):
            return False
        self.proximity_active[cycle_key] = True
        return True

    def mark_proximity_exit(self, cycle_key: str):
        """RSI 离开 proximity 区域: 允许下次再进入时重新提醒"""
        self.proximity_active.pop(cycle_key, None)

    def clear_proximity(self, cycle_key: str):
        """Grid cycle 结束 / 系统明确 reset: 清除该 cycle 的 proximity 状态"""
        self.proximity_active.pop(cycle_key, None)

    def is_proximity_active(self, cycle_key: str) -> bool:
        return bool(self.proximity_active.get(cycle_key))

    def should_alert_stop_loss(self, cycle_key: str) -> bool:
        """
        止损提醒 (STOP_LOSS) 去重 (cycle 级, 非按日)。

        规则: 同一 Grid cycle (按 cycle key) 只允许发送一次 STOP_LOSS 提醒;
        即使价格继续下跌 (22,900 / 22,500 / 21,000 ...) 也不再重复推送。
        新 Grid cycle 使用新的 cycle id 作为 key, 可以再次触发。
        """
        if cycle_key in self.stop_loss_alerted:
            return False
        self.stop_loss_alerted.add(cycle_key)
        return True

    def clear_stop_loss(self, cycle_key: str):
        """清除指定 cycle 的 STOP_LOSS 去重状态 (仅测试/手动 reset 使用)"""
        self.stop_loss_alerted.discard(cycle_key)

    def clear(self):
        self.daily_rules.clear()
        self.weekly_rules.clear()
        self.proximity_active.clear()
        self.stop_loss_alerted.clear()


_deduplicator = AlertDeduplicator()

# 线程安全: APScheduler 的线程池与 FastAPI 请求线程会并发触碰去重状态
# (uvicorn --workers 1 下单进程多线程), 用可重入锁串行化。
_dedup_lock = threading.RLock()


def should_alert(rule_name: str, position_id: int = None) -> bool:
    with _dedup_lock:
        return _deduplicator.should_alert(rule_name, position_id)

def should_alert_weekly(rule_name: str, position_id: int = None) -> bool:
    with _dedup_lock:
        return _deduplicator.should_alert_weekly(rule_name, position_id)


# 接近入场阈值 (proximity) 事件: 状态型去重, 独立于按日/按周 key
PROXIMITY_KEY = "NDX_GRID_PROXIMITY"

def should_alert_proximity(cycle_key: str = PROXIMITY_KEY) -> bool:
    with _dedup_lock:
        return _deduplicator.should_alert_proximity(cycle_key)

def mark_proximity_exit(cycle_key: str = PROXIMITY_KEY) -> None:
    with _dedup_lock:
        _deduplicator.mark_proximity_exit(cycle_key)

def clear_proximity(cycle_key: str = PROXIMITY_KEY) -> None:
    with _dedup_lock:
        _deduplicator.clear_proximity(cycle_key)

def is_proximity_active(cycle_key: str = PROXIMITY_KEY) -> bool:
    with _dedup_lock:
        return _deduplicator.is_proximity_active(cycle_key)


# 止损提醒 (STOP_LOSS) 事件: cycle 级去重, 独立于按日/按周 key
STOP_LOSS_KEY_PREFIX = "NDX_GRID_STOP_LOSS_cycle_"

def stop_loss_cycle_key(cycle_id: int) -> str:
    """STOP_LOSS 去重 key: 绑定 GridCycle id, 新 cycle 自动获得新 key"""
    return f"{STOP_LOSS_KEY_PREFIX}{int(cycle_id)}"

def should_alert_stop_loss(cycle_key: str) -> bool:
    with _dedup_lock:
        return _deduplicator.should_alert_stop_loss(cycle_key)

def is_stop_loss_alerted(cycle_key: str) -> bool:
    """查询指定 cycle 的 STOP_LOSS 提醒是否已发送 (日报展示用, 只读)"""
    with _dedup_lock:
        return cycle_key in _deduplicator.stop_loss_alerted

def clear_stop_loss(cycle_key: str) -> None:
    with _dedup_lock:
        _deduplicator.clear_stop_loss(cycle_key)


def reset_daily_dedup():
    with _dedup_lock:
        _deduplicator.reset_daily()


def clear_dedup():
    with _dedup_lock:
        _deduplicator.clear()
