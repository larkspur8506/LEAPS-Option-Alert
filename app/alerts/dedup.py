from datetime import datetime, date
from typing import Set, Dict
from pytz import timezone

et_tz = timezone("America/New_York")


class AlertDeduplicator:
    def __init__(self):
        self.daily_rules: Dict[str, Set[str]] = {}
        self.weekly_rules: Dict[str, Set[str]] = {}

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

    def clear(self):
        self.daily_rules.clear()
        self.weekly_rules.clear()


_deduplicator = AlertDeduplicator()


def should_alert(rule_name: str, position_id: int = None) -> bool:
    return _deduplicator.should_alert(rule_name, position_id)

def should_alert_weekly(rule_name: str, position_id: int = None) -> bool:
    return _deduplicator.should_alert_weekly(rule_name, position_id)


def reset_daily_dedup():
    _deduplicator.reset_daily()


def clear_dedup():
    _deduplicator.clear()
