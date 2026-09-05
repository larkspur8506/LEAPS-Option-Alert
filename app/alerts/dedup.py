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

    def clear(self):
        self.daily_rules.clear()
        self.weekly_rules.clear()
        self.proximity_active.clear()


_deduplicator = AlertDeduplicator()


def should_alert(rule_name: str, position_id: int = None) -> bool:
    return _deduplicator.should_alert(rule_name, position_id)

def should_alert_weekly(rule_name: str, position_id: int = None) -> bool:
    return _deduplicator.should_alert_weekly(rule_name, position_id)


# 接近入场阈值 (proximity) 事件: 状态型去重, 独立于按日/按周 key
PROXIMITY_KEY = "NDX_GRID_PROXIMITY"

def should_alert_proximity(cycle_key: str = PROXIMITY_KEY) -> bool:
    return _deduplicator.should_alert_proximity(cycle_key)

def mark_proximity_exit(cycle_key: str = PROXIMITY_KEY) -> None:
    _deduplicator.mark_proximity_exit(cycle_key)

def clear_proximity(cycle_key: str = PROXIMITY_KEY) -> None:
    _deduplicator.clear_proximity(cycle_key)

def is_proximity_active(cycle_key: str = PROXIMITY_KEY) -> bool:
    return _deduplicator.is_proximity_active(cycle_key)


def reset_daily_dedup():
    _deduplicator.reset_daily()


def clear_dedup():
    _deduplicator.clear()
