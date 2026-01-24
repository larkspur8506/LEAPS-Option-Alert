from datetime import datetime, date
from typing import Set, Dict
from pytz import timezone

et_tz = timezone("America/New_York")


class AlertDeduplicator:
    def __init__(self):
        self.daily_rules: Dict[str, Set[str]] = {}

    def get_today_key(self) -> str:
        return datetime.now(et_tz).strftime("%Y-%m-%d")

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

    def reset_daily(self):
        today = self.get_today_key()

        old_days = [day for day in self.daily_rules.keys() if day != today]
        for old_day in old_days:
            del self.daily_rules[old_day]

    def clear(self):
        self.daily_rules.clear()


_deduplicator = AlertDeduplicator()


def should_alert(rule_name: str, position_id: int = None) -> bool:
    return _deduplicator.should_alert(rule_name, position_id)


def reset_daily_dedup():
    _deduplicator.reset_daily()


def clear_dedup():
    _deduplicator.clear()
