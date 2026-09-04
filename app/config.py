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
        return os.getenv("ENTRY_LEVEL1_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_entry_level2_enabled(self) -> bool:
        if self._db_config.get("entry_level2_enabled") is not None:
            return self._db_config["entry_level2_enabled"]
        return os.getenv("ENTRY_LEVEL2_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_entry_level3_enabled(self) -> bool:
        if self._db_config.get("entry_level3_enabled") is not None:
            return self._db_config["entry_level3_enabled"]
        return os.getenv("ENTRY_LEVEL3_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    # 新版出场规则开关
    def is_exit_hard_tp_enabled(self) -> bool:
        if self._db_config.get("exit_hard_tp_enabled") is not None:
            return self._db_config["exit_hard_tp_enabled"]
        return os.getenv("EXIT_HARD_TP_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_exit_fast_tp_enabled(self) -> bool:
        if self._db_config.get("exit_fast_tp_enabled") is not None:
            return self._db_config["exit_fast_tp_enabled"]
        return os.getenv("EXIT_FAST_TP_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_exit_trailing_tp_enabled(self) -> bool:
        if self._db_config.get("exit_trailing_tp_enabled") is not None:
            return self._db_config["exit_trailing_tp_enabled"]
        return os.getenv("EXIT_TRAILING_TP_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_exit_tech_tp_enabled(self) -> bool:
        if self._db_config.get("exit_tech_tp_enabled") is not None:
            return self._db_config["exit_tech_tp_enabled"]
        return os.getenv("EXIT_TECH_TP_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_exit_dte_warning_enabled(self) -> bool:
        if self._db_config.get("exit_dte_warning_enabled") is not None:
            return self._db_config["exit_dte_warning_enabled"]
        return os.getenv("EXIT_DTE_WARNING_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_exit_dte_force_enabled(self) -> bool:
        if self._db_config.get("exit_dte_force_enabled") is not None:
            return self._db_config["exit_dte_force_enabled"]
        return os.getenv("EXIT_DTE_FORCE_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    def is_exit_trend_stop_enabled(self) -> bool:
        if self._db_config.get("exit_trend_stop_enabled") is not None:
            return self._db_config["exit_trend_stop_enabled"]
        return os.getenv("EXIT_TREND_STOP_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    # NDX 做多网格策略配置参数 (可优先从环境变量或字典读取，支持后续灵活扩展)
    def get_rsi_threshold(self) -> float:
        if self._db_config.get("rsi_threshold") is not None:
            return float(self._db_config["rsi_threshold"])
        return float(os.getenv("RSI_THRESHOLD", "35.0"))

    def get_default_grid_upper_pct(self) -> float:
        if self._db_config.get("default_grid_upper_pct") is not None:
            return float(self._db_config["default_grid_upper_pct"])
        return float(os.getenv("DEFAULT_GRID_UPPER_PCT", "0.20"))

    def get_default_grid_lower_pct(self) -> float:
        if self._db_config.get("default_grid_lower_pct") is not None:
            return float(self._db_config["default_grid_lower_pct"])
        return float(os.getenv("DEFAULT_GRID_LOWER_PCT", "0.20"))

    def get_default_grid_count(self) -> int:
        if self._db_config.get("default_grid_count") is not None:
            return int(self._db_config["default_grid_count"])
        return int(os.getenv("DEFAULT_GRID_COUNT", "200"))

    def get_default_grid_leverage(self) -> float:
        if self._db_config.get("default_grid_leverage") is not None:
            return float(self._db_config["default_grid_leverage"])
        return float(os.getenv("DEFAULT_GRID_LEVERAGE", "5.0"))

    # Phase 6: 每日 16:30 日报模式
    DAILY_REPORT_MODES = ("off", "legacy", "ndx_grid")

    def get_daily_report_mode(self) -> str:
        """
        每日日报模式配置。
        优先级: DB configuration.daily_report_mode > 环境变量 DAILY_REPORT_MODE > 'legacy'。
        NULL / 缺失 / 非法值一律回退 'legacy' (保持升级前行为, 向后兼容)。
        """
        value = self._db_config.get("daily_report_mode")
        if value in self.DAILY_REPORT_MODES:
            return value
        value = os.getenv("DAILY_REPORT_MODE", "")
        if value in self.DAILY_REPORT_MODES:
            return value
        return "legacy"


def get_config(db_config: Optional[dict] = None) -> Config:
    return Config(db_config)
