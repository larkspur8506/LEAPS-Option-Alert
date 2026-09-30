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

    # ---- QQQ LEAPS 策略参数 (DB 优先于 env; 数值非法时回退默认) ----

    def _float(self, key: str, env_key: str, default: float) -> float:
        try:
            raw = self._db_config.get(key)
            if raw is not None and raw != "":
                return float(raw)
        except (TypeError, ValueError):
            pass
        try:
            return float(os.getenv(env_key, str(default)))
        except (TypeError, ValueError):
            return default

    def _int(self, key: str, env_key: str, default: int) -> int:
        try:
            raw = self._db_config.get(key)
            if raw is not None and raw != "":
                return int(raw)
        except (TypeError, ValueError):
            pass
        try:
            return int(os.getenv(env_key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_entry_rsi_threshold(self) -> float:
        """入场: RSI14 < 阈值 (回测定稿 35)"""
        return self._float("rsi_threshold", "RSI_THRESHOLD", 35.0)

    def get_tp_rsi(self) -> float:
        """止盈: RSI14 > 阈值 (QQQ 回测最优 65)"""
        return self._float("leaps_tp_rsi", "LEAPS_TP_RSI", 65.0)

    def get_time_stop_trading_days(self) -> int:
        """时间止损: 持仓 N 个交易日仍未回本 -> 提醒平仓 (回测 126)"""
        return self._int("leaps_time_stop_trading_days", "LEAPS_TIME_STOP_TRADING_DAYS", 126)

    def get_dte_force_days(self) -> int:
        """DTE 强制平仓: 距到期 N 个自然日强制提醒 (沿用早期 app 的 180)"""
        return self._int("leaps_dte_force_days", "LEAPS_DTE_FORCE_DAYS", 180)

    def get_add_levels(self) -> list:
        """加仓回撤档位列表 (相对 signal_base_price), 默认 -10% / -20%"""
        raw = None
        try:
            raw = self._db_config.get("leaps_add_levels")
        except Exception:
            raw = None
        if not raw:
            raw = os.getenv("LEAPS_ADD_LEVELS", "0.10,0.20")
        levels = []
        for part in str(raw).split(","):
            try:
                v = float(part.strip())
                if v > 0:
                    levels.append(v)
            except ValueError:
                continue
        return levels or [0.10, 0.20]

    def get_max_quantity(self) -> int:
        """单信号最大张数 (首张 + 加仓), 默认 3"""
        v = self._int("leaps_max_quantity", "LEAPS_MAX_QUANTITY", 3)
        return max(1, v)

    def get_target_delta(self) -> float:
        """建议 Delta (回测 0.60-0.70 差异小, 取 0.65)"""
        return self._float("leaps_target_delta", "LEAPS_TARGET_DELTA", 0.65)

    def get_target_tenor_days(self) -> int:
        """建议期限 (自然日, 默认约 2 年 = 730)"""
        return self._int("leaps_target_tenor_days", "LEAPS_TARGET_TENOR_DAYS", 730)

    # ---- 每日 16:30 日报模式 ----
    DAILY_REPORT_MODES = ("off", "leaps")

    def get_daily_report_mode(self) -> str:
        """
        日报模式。优先级: DB > env > 'leaps'。
        历史 'ndx_grid' / 'legacy' 值归一化为 'leaps' (策略已切换)。
        """
        value = self._db_config.get("daily_report_mode")
        if value in self.DAILY_REPORT_MODES:
            return value
        value = os.getenv("DAILY_REPORT_MODE", "")
        if value in self.DAILY_REPORT_MODES:
            return value
        return "leaps"

    # ---- WAITING 信号生命周期 ----
    def get_waiting_ttl_trading_days(self) -> int:
        """WAITING 建议在多少个交易日后自动过期 (0 = 永不过期)"""
        return self._int("waiting_ttl_trading_days", "WAITING_TTL_TRADING_DAYS", 3)


def get_config(db_config: Optional[dict] = None) -> Config:
    return Config(db_config)


def load_config_from_db(db) -> Config:
    """
    从 DB `configuration` 行构造运行时配置 (与 app.main 启动时/保存后的配置同源)。
    任何异常都退回仅环境变量的配置, 不抛出。
    """
    try:
        from app.database.models import Configuration

        row = db.query(Configuration).first()
        if not row:
            return get_config()
        return get_config({
            "wechat_webhook_url": row.wechat_webhook_url,
            "alert_log_retention_days": row.alert_log_retention_days,
            "daily_report_mode": getattr(row, "daily_report_mode", None),
            "leaps_tp_rsi": getattr(row, "leaps_tp_rsi", None),
            "leaps_time_stop_trading_days": getattr(row, "leaps_time_stop_trading_days", None),
            "leaps_dte_force_days": getattr(row, "leaps_dte_force_days", None),
            "leaps_add_levels": getattr(row, "leaps_add_levels", None),
            "leaps_max_quantity": getattr(row, "leaps_max_quantity", None),
            "leaps_target_delta": getattr(row, "leaps_target_delta", None),
            "leaps_target_tenor_days": getattr(row, "leaps_target_tenor_days", None),
        })
    except Exception:
        return get_config()
