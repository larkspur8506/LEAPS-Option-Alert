import os
from dotenv import load_dotenv
from typing import Optional

load_dotenv()


class Config:
    def __init__(self, db_config: Optional[dict] = None):
        self._db_config = db_config or {}

    def get_wechat_webhook_url(self) -> str:
        if self._db_config.get("wechat_webhook_url"):
            return self._db_config["wechat_webhook_url"]
        return os.getenv("WECHAT_WEBHOOK_URL", "")

    def get_alert_log_retention_days(self) -> int:
        if self._db_config.get("alert_log_retention_days"):
            return int(self._db_config["alert_log_retention_days"])
        return int(os.getenv("ALERT_LOG_RETENTION_DAYS", "90"))

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

    def get_default_grid_stop_loss_after_lower_pct(self) -> float:
        """
        Grid Lower 被跌破后, 再向下多少比例触发止损提醒。
        仅用于 NDX_GRID_STOP_LOSS 通知事件, 不影响 Grid 状态机 (STOPPED 语义不变)。
        计算公式: stop_loss_alert_price = lower_price * (1 - pct)
        """
        if self._db_config.get("default_grid_stop_loss_after_lower_pct") is not None:
            return float(self._db_config["default_grid_stop_loss_after_lower_pct"])
        return float(os.getenv("DEFAULT_GRID_STOP_LOSS_AFTER_LOWER_PCT", "0.10"))

    # 每日 16:30 日报模式
    DAILY_REPORT_MODES = ("off", "ndx_grid")

    def get_daily_report_mode(self) -> str:
        """
        每日日报模式配置。
        优先级: DB configuration.daily_report_mode > 环境变量 DAILY_REPORT_MODE > 'ndx_grid'。
        历史 'legacy' 值归一化为 'ndx_grid' (删除 legacy 日报路径后的安全迁移, 不改历史数据)。
        NULL / 缺失 / 非法值一律回退默认 'ndx_grid'。
        """
        value = self._db_config.get("daily_report_mode")
        if value == "legacy":
            # 历史配置兼容: legacy 日报已删除, 读取层归一化为 NDX Grid 日报
            return "ndx_grid"
        if value in self.DAILY_REPORT_MODES:
            return value
        value = os.getenv("DAILY_REPORT_MODE", "")
        if value == "legacy":
            return "ndx_grid"
        if value in self.DAILY_REPORT_MODES:
            return value
        return "ndx_grid"


def get_config(db_config: Optional[dict] = None) -> Config:
    return Config(db_config)
