import os
from dotenv import load_dotenv
from typing import Optional

load_dotenv()


class Config:
    def __init__(self, db_config: Optional[dict] = None):
        self._db_config = db_config or {}

    def get_polygon_api_key(self) -> str:
        if self._db_config.get("polygon_api_key"):
            return self._db_config["polygon_api_key"]
        return os.getenv("POLYGON_API_KEY", "")

    def get_wechat_webhook_url(self) -> str:
        if self._db_config.get("wechat_webhook_url"):
            return self._db_config["wechat_webhook_url"]
        return os.getenv("WECHAT_WEBHOOK_URL", "")

    def is_qqq_rule_a_enabled(self) -> bool:
        if self._db_config.get("qqq_rule_a_enabled") is not None:
            return self._db_config["qqq_rule_a_enabled"]
        return os.getenv("QQQ_RULE_A_ENABLED", "true").lower() == "true"

    def is_qqq_rule_b_enabled(self) -> bool:
        if self._db_config.get("qqq_rule_b_enabled") is not None:
            return self._db_config["qqq_rule_b_enabled"]
        return os.getenv("QQQ_RULE_B_ENABLED", "true").lower() == "true"

    def is_qqq_rule_c_enabled(self) -> bool:
        if self._db_config.get("qqq_rule_c_enabled") is not None:
            return self._db_config["qqq_rule_c_enabled"]
        return os.getenv("QQQ_RULE_C_ENABLED", "true").lower() == "true"

    def is_qqq_rule_d_enabled(self) -> bool:
        if self._db_config.get("qqq_rule_d_enabled") is not None:
            return self._db_config["qqq_rule_d_enabled"]
        return os.getenv("QQQ_RULE_D_ENABLED", "true").lower() == "true"

    def get_max_holding_days(self) -> int:
        if self._db_config.get("max_holding_days"):
            return int(self._db_config["max_holding_days"])
        return int(os.getenv("MAX_HOLDING_DAYS", "270"))

    def get_take_profit_phase1_threshold(self) -> float:
        if self._db_config.get("take_profit_phase1_threshold"):
            return float(self._db_config["take_profit_phase1_threshold"])
        return float(os.getenv("TAKE_PROFIT_PHASE1_THRESHOLD", "0.50"))

    def get_take_profit_phase1_days(self) -> int:
        if self._db_config.get("take_profit_phase1_days"):
            return int(self._db_config["take_profit_phase1_days"])
        return int(os.getenv("TAKE_PROFIT_PHASE1_DAYS", "120"))

    def get_take_profit_phase2_threshold(self) -> float:
        if self._db_config.get("take_profit_phase2_threshold"):
            return float(self._db_config["take_profit_phase2_threshold"])
        return float(os.getenv("TAKE_PROFIT_PHASE2_THRESHOLD", "0.30"))

    def get_take_profit_phase2_days(self) -> int:
        if self._db_config.get("take_profit_phase2_days"):
            return int(self._db_config["take_profit_phase2_days"])
        return int(os.getenv("TAKE_PROFIT_PHASE2_DAYS", "180"))

    def get_take_profit_phase3_threshold(self) -> float:
        if self._db_config.get("take_profit_phase3_threshold"):
            return float(self._db_config["take_profit_phase3_threshold"])
        return float(os.getenv("TAKE_PROFIT_PHASE3_THRESHOLD", "0.10"))

    def get_stop_loss_threshold(self) -> float:
        if self._db_config.get("stop_loss_threshold"):
            return float(self._db_config["stop_loss_threshold"])
        return float(os.getenv("STOP_LOSS_THRESHOLD", "0.30"))

    def get_dte_warning_days(self) -> int:
        if self._db_config.get("dte_warning_days"):
            return int(self._db_config["dte_warning_days"])
        return int(os.getenv("DTE_WARNING_DAYS", "45"))

    def get_alert_log_retention_days(self) -> int:
        if self._db_config.get("alert_log_retention_days"):
            return int(self._db_config["alert_log_retention_days"])
        return int(os.getenv("ALERT_LOG_RETENTION_DAYS", "90"))

    def get_daily_qqq_data_retention_days(self) -> int:
        if self._db_config.get("daily_qqq_data_retention_days"):
            return int(self._db_config["daily_qqq_data_retention_days"])
        return int(os.getenv("DAILY_QQQ_DATA_RETENTION_DAYS", "30"))

    # 新版入场规则开关
    def is_entry_level1_enabled(self) -> bool:
        if self._db_config.get("entry_level1_enabled") is not None:
            return self._db_config["entry_level1_enabled"]
        return True

    def is_entry_level2_enabled(self) -> bool:
        if self._db_config.get("entry_level2_enabled") is not None:
            return self._db_config["entry_level2_enabled"]
        return True

    def is_entry_level3_enabled(self) -> bool:
        if self._db_config.get("entry_level3_enabled") is not None:
            return self._db_config["entry_level3_enabled"]
        return True

    # 新版出场规则开关
    def is_exit_hard_tp_enabled(self) -> bool:
        if self._db_config.get("exit_hard_tp_enabled") is not None:
            return self._db_config["exit_hard_tp_enabled"]
        return True

    def is_exit_fast_tp_enabled(self) -> bool:
        if self._db_config.get("exit_fast_tp_enabled") is not None:
            return self._db_config["exit_fast_tp_enabled"]
        return True

    def is_exit_trailing_tp_enabled(self) -> bool:
        if self._db_config.get("exit_trailing_tp_enabled") is not None:
            return self._db_config["exit_trailing_tp_enabled"]
        return True

    def is_exit_tech_tp_enabled(self) -> bool:
        if self._db_config.get("exit_tech_tp_enabled") is not None:
            return self._db_config["exit_tech_tp_enabled"]
        return True

    def is_exit_dte_warning_enabled(self) -> bool:
        if self._db_config.get("exit_dte_warning_enabled") is not None:
            return self._db_config["exit_dte_warning_enabled"]
        return True

    def is_exit_dte_force_enabled(self) -> bool:
        if self._db_config.get("exit_dte_force_enabled") is not None:
            return self._db_config["exit_dte_force_enabled"]
        return True

    def is_exit_trend_stop_enabled(self) -> bool:
        if self._db_config.get("exit_trend_stop_enabled") is not None:
            return self._db_config["exit_trend_stop_enabled"]
        return True


def get_config(db_config: Optional[dict] = None) -> Config:
    return Config(db_config)
