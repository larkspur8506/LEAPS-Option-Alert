"""
Phase 6 Tests: Configurable Daily Report.

Covers:
- Default mode = ndx_grid (NULL / missing / invalid config -> ndx_grid; legacy -> ndx_grid)
- mode = off / ndx_grid dispatching in scheduler job
- Rules page renders radio controls, POST saves to DB (real runtime config)
- ndx_grid generates NDX Grid daily report (reuses grid_service dashboard)
- WAITING / RUNNING / CLOSED / STOPPED / NO ACTIVE GRID report content
- stale / unavailable NDX -> N/A + NOT EVALUATED, never fake "NO" signal
- suggested_*/actual_* strictly separated in RUNNING report
- dedup: one report per day (shared DAILY_REPORT key)
- WeChat failure does not affect scheduler / grid monitoring
- report exception does not affect grid monitoring
- secrets never in report output
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pytz import timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, GridCycle, Configuration
from app.alerts.grid_cycle import create_waiting_grid_cycle
from app.services import grid_service
from app.notification.wechat import WeChatNotifier
from app.scheduler.jobs import send_daily_report_job

et_tz = timezone("America/New_York")

SUGGESTED = {
    "base_price": 20000.0,
    "upper_price": 24000.0,
    "lower_price": 16000.0,
    "grid_count": 200,
    "leverage": 5.0,
}

VALID_ACTUAL = {
    "actual_base_price": 20000.0,
    "actual_upper_price": 24000.0,
    "actual_lower_price": 16000.0,
    "actual_grid_count": 150,
    "actual_leverage": 3.0,
    "actual_margin": 1000.0,
}


def make_ndx_data(price=22000.0, valid=True, fresh=True):
    return {
        "ticker": "^NDX",
        "is_data_valid": valid,
        "last_price": price,
        "rsi": 30.0,
        "ma200": 19000.0,
        "is_above_sma200_3d": True,
        "price_1y_ago": 18000.0,
        "price_1y_ago_available": True,
        "is_data_fresh": fresh,
        "data_timestamp": str(datetime.now(et_tz)) if valid else None,
    }


class DailyReportTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        self.mock_fetcher = MagicMock()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data()

        # 日报 job 的美国交易日检查默认视为交易日 (休市日行为在专门用例中单独覆盖)
        self._trading_day_patch = patch("app.scheduler.jobs.is_trading_day", return_value=True)
        self._trading_day_patch.start()
        self.addCleanup(self._trading_day_patch.stop)

        # Configuration 行 (daily_report_mode 列)
        self.config_row = Configuration(
            admin_password_hash="x", daily_report_mode=None  # NULL -> ndx_grid 默认
        )
        self.db.add(self.config_row)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _config(self, mode=None):
        """构造运行时 config: mode=None 表示 DB 中无配置 (走 ndx_grid 默认)"""
        from app.config import Config
        row = self.db.query(Configuration).first()
        self.db.refresh(row) if row else None
        cfg = Config({
            "daily_report_mode": mode if mode is not None else (row.daily_report_mode if row else None),
            "wechat_webhook_url": "https://mock.webhook/key=SECRET",
            "default_grid_upper_pct": 0.20,
            "default_grid_lower_pct": 0.20,
            "default_grid_count": 200,
            "default_grid_leverage": 5.0,
            "default_grid_stop_loss_after_lower_pct": 0.10,
        })
        return cfg

    def _clear_dedup(self):
        from app.alerts.dedup import clear_dedup
        clear_dedup()


class TestConfigDefaults(DailyReportTestBase):
    def test_1_default_mode_is_ndx_grid(self):
        """1. 默认 mode = ndx_grid (DB NULL / 缺失 / 非法值 / 历史 legacy 值均归一化)"""
        self.assertEqual(self._config(None).get_daily_report_mode(), "ndx_grid")  # DB NULL
        from app.config import Config
        self.assertEqual(Config({}).get_daily_report_mode(), "ndx_grid")           # 完全缺失
        self.assertEqual(Config({"daily_report_mode": "bogus"}).get_daily_report_mode(), "ndx_grid")  # 非法
        self.assertEqual(Config({"daily_report_mode": "legacy"}).get_daily_report_mode(), "ndx_grid")  # 历史 legacy -> 归一化
        self.assertEqual(Config({"daily_report_mode": "off"}).get_daily_report_mode(), "off")
        self.assertEqual(Config({"daily_report_mode": "ndx_grid"}).get_daily_report_mode(), "ndx_grid")


class TestSchedulerDispatch(DailyReportTestBase):
    def test_2_mode_off_skips(self):
        """2/19. mode=off -> 不发送任何日报"""
        self._clear_dedup()
        notifier = MagicMock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("off"))
        self.assertEqual(res["status"], "SKIPPED")
        notifier.send_ndx_grid_report.assert_not_called()

    def test_4_mode_ndx_grid_generates_ndx_report(self):
        """4/8. mode=ndx_grid -> 生成 NDX Grid 日报 (复用 grid_service)"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = True
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(res["status"], "OK")
        notifier.format_ndx_grid_report.assert_called_once()
        notifier.send_ndx_grid_report.assert_called_once()
        report = notifier.send_ndx_grid_report.call_args[0][0]
        self.assertIn("dashboard", report)
        self.assertIn("strategy", report)

    def test_6_mode_read_at_execution_time(self):
        """6. scheduler 每次执行时读取当前配置 (运行时配置, 非启动时快照)"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = True
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        notifier.send_ndx_grid_report.assert_called_once()
        # 同一天第二次执行 (任何 mode) -> dedup, 不再发送
        notifier.reset_mock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        notifier.send_ndx_grid_report.assert_not_called()

    def test_20_wechat_failure_does_not_break_scheduler(self):
        """20. WeChat 发送失败 -> job 正常返回, 不抛异常"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = False
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(res["status"], "OK")

    def test_21_report_exception_does_not_break_scheduler(self):
        """21. 日报内部异常 -> job 返回 ERROR, 不向上抛, 不影响其他 job
        (NDX 数据异常被设计为降级 N/A 继续发报, 因此这里用 dashboard 聚合异常模拟内部错误)"""
        self._clear_dedup()
        with patch("app.services.grid_service.get_grid_dashboard", side_effect=RuntimeError("boom")):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(res["status"], "ERROR")

        # Grid monitoring 不受影响
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data()
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        self.assertIsNotNone(cycle.id)


class TestConfigUpdateViaUI(DailyReportTestBase):
    def test_5_ui_saves_mode_to_db(self):
        """5. 后台修改 mode -> 真正写入 DB configuration (运行时配置)"""
        from app.main import app
        from app.database.init_db import get_db
        app.dependency_overrides[get_db] = lambda: self.db
        client = TestClient(app, follow_redirects=False)
        client.cookies.set("admin_logged_in", "true")
        try:
            res = client.post(
                "/admin/rules/daily-report-mode",
                data={"daily_report_mode": "ndx_grid"},
            )
            self.assertEqual(res.status_code, 303)
            self.assertIn("saved=1", res.headers["location"])

            row = self.db.query(Configuration).first()
            self.db.refresh(row)
            self.assertEqual(row.daily_report_mode, "ndx_grid")

            # 保存后真正影响 scheduler 调度
            self._clear_dedup()
            notifier = MagicMock()
            notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
            notifier.send_ndx_grid_report.return_value = True
            with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
                cfg_row = self.db.query(Configuration).first()
                from app.config import Config
                cfg = Config({"daily_report_mode": cfg_row.daily_report_mode})
                send_daily_report_job(self.mock_fetcher, db=self.db, config=cfg)
            notifier.send_ndx_grid_report.assert_called_once()
        finally:
            client.close()
            app.dependency_overrides.clear()

    def test_5b_ui_get_rules_renders_persisted_mode(self):
        """5b. POST 保存 daily_report_mode 后的 GET /admin/rules 渲染为 selected/checked"""
        from app.main import app
        from app.database.init_db import get_db
        app.dependency_overrides[get_db] = lambda: self.db
        client = TestClient(app, follow_redirects=True)
        client.cookies.set("admin_logged_in", "true")
        try:
            # 1. 保存 ndx_grid
            res = client.post("/admin/rules/daily-report-mode", data={"daily_report_mode": "ndx_grid"})
            self.assertEqual(res.status_code, 200)
            self.assertIn('value="ndx_grid"\n                        checked', res.text)

            # 2. 刷新 GET /admin/rules -> 必须保持 ndx_grid
            res_get = client.get("/admin/rules")
            self.assertEqual(res_get.status_code, 200)
            self.assertIn('value="ndx_grid"\n                        checked', res_get.text)

            # 3. 保存 off -> 刷新 GET /admin/rules -> 必须保持 off
            res_off = client.post("/admin/rules/daily-report-mode", data={"daily_report_mode": "off"})
            self.assertEqual(res_off.status_code, 200)
            self.assertIn('value="off"\n                        checked', res_off.text)

            # 4. 非法值 / 历史 legacy 值 -> 归一化 fallback ndx_grid
            res_bogus = client.post("/admin/rules/daily-report-mode", data={"daily_report_mode": "invalid_mode"})
            self.assertEqual(res_bogus.status_code, 200)
            self.assertIn('value="ndx_grid"\n                        checked', res_bogus.text)

            # 5. 历史 legacy 值 -> 保存层归一化为 ndx_grid
            res_legacy = client.post("/admin/rules/daily-report-mode", data={"daily_report_mode": "legacy"})
            self.assertEqual(res_legacy.status_code, 200)
            self.assertIn('value="ndx_grid"\n                        checked', res_legacy.text)
            row = self.db.query(Configuration).first()
            self.db.refresh(row)
            self.assertEqual(row.daily_report_mode, "ndx_grid")
        finally:
            client.close()
            app.dependency_overrides.clear()

    def test_5c_db_overrides_env_var_priority(self):
        """5c. Priority test: DB Configuration.daily_report_mode > DAILY_REPORT_MODE env > 'ndx_grid'"""
        import os
        from app.config import Config
        row = self.db.query(Configuration).first()
        row.daily_report_mode = "ndx_grid"
        self.db.commit()

        with patch.dict(os.environ, {"DAILY_REPORT_MODE": "off"}):
            cfg = Config({"daily_report_mode": row.daily_report_mode})
            # DB (ndx_grid) takes precedence over ENV (off)
            self.assertEqual(cfg.get_daily_report_mode(), "ndx_grid")

            # If DB value is NULL or invalid -> falls back to ENV (off)
            cfg_null = Config({"daily_report_mode": None})
            self.assertEqual(cfg_null.get_daily_report_mode(), "off")


class TestNDXReportContent(DailyReportTestBase):
    def _format(self, dashboard, latest_cycle=None):
        notifier = WeChatNotifier("https://mock.webhook/key=SECRET")
        return notifier.format_ndx_grid_report({
            "date": "2026-09-04",
            "dashboard": dashboard,
            "latest_cycle": latest_cycle,
            "strategy": {"upper_pct": 0.20, "lower_pct": 0.20, "grid_count": 200, "leverage": 5.0},
        })

    def _dashboard(self, running=None, waiting=None, latest=None):
        return {
            "ndx": {
                "last_price": 22000.0, "rsi": 30.0, "ma200": 19000.0,
                "price_1y_ago": 18000.0, "entry_signal": False,
                "is_data_valid": True, "is_data_fresh": True,
                "data_timestamp": str(datetime.now(et_tz)),
            },
            "running_cycle": running, "waiting_cycle": waiting,
            "theoretical_grid_position": None,
        }

    def test_13_no_active_grid(self):
        """13. NO ACTIVE GRID -> 简洁显示, 不填充无意义字段"""
        msg = self._format(self._dashboard(), latest_cycle={"status": "CLOSED", "close_reason": "UPPER_REACHED", "id": 3})
        self.assertIn("当前：⚪ 无运行中的 Grid", msg)
        self.assertIn("最近一轮：🟢 CLOSED（UPPER_REACHED）", msg)
        self.assertNotIn("Base：", msg)
        self.assertNotIn("Upper：", msg)
        self.assertNotIn("Lower：", msg)
        self.assertIn("默认区间：±20%", msg)
        self.assertIn("网格：200格", msg)
        self.assertIn("杠杆：5.0x", msg)

    def test_9_waiting_report_suggested_only(self):
        """9. WAITING 日报只使用 suggested_* (需用户操作, 展开建议参数)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        ser = grid_service.serialize_cycle(cycle)
        msg = self._format(self._dashboard(waiting=ser))
        self.assertIn("当前：🟡 WAITING — 等待确认启动", msg)
        self.assertIn("Base：20,000.00", msg)
        self.assertIn("Upper：24,000.00", msg)
        self.assertIn("Lower：16,000.00", msg)
        self.assertIn("网格：200格", msg)
        self.assertIn("杠杆：5.0x", msg)
        self.assertNotIn("actual_", msg)
        self.assertNotIn("Actual", msg)
        # 无活动 Grid 时的默认参数区不应出现 (已展开 WAITING 参数)
        self.assertNotIn("默认区间", msg)

    def test_10_running_report_actual_only(self):
        """10/16. RUNNING 日报显示状态, 不重复展开固定参数, suggested 不混淆"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        running = grid_service.get_running_cycle(self.db)

        msg = self._format(self._dashboard(running=running))
        self.assertIn("当前：🟢 RUNNING", msg)
        # 日报不每天重复展开 Base/Upper/Lower/Grid 参数 (参数只在事件通知展开)
        self.assertNotIn("Base：20,000.00", msg)
        self.assertNotIn("Base：25,150.00", msg)
        self.assertNotIn("网格：200格", msg)
        self.assertNotIn("网格：150格", msg)
        self.assertNotIn("杠杆：5.0x", msg)
        self.assertNotIn("杠杆：3.0x", msg)

    def test_10b_running_report_actual_survives_strategy_change(self):
        """10b. 默认策略变化后 RUNNING 日报不受影响 (不展示策略默认参数)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        running = grid_service.get_running_cycle(self.db)

        # 策略"变成" 200格/5x —— 传给 report 的 strategy 变了, 但 running cycle 不变
        msg = self._format(self._dashboard(running=running))
        self.assertIn("当前：🟢 RUNNING", msg)
        self.assertNotIn("杠杆：5.0x", msg)
        self.assertNotIn("网格：200格", msg)

    def test_11_closed_report(self):
        """11. CLOSED -> 最近一轮 CLOSED + 原因"""
        msg = self._format(self._dashboard(), latest_cycle={"status": "CLOSED", "close_reason": "UPPER_REACHED", "id": 1})
        self.assertIn("最近一轮：🟢 CLOSED（UPPER_REACHED）", msg)

    def test_12_stopped_report(self):
        """12. STOPPED -> 风险观察展示: 止损提醒线 + 实际参数 (不虚构)"""
        latest = {
            "id": 2, "status": "STOPPED", "close_reason": "LOWER_BREACHED",
            "actual_base_price": 30000.0, "actual_upper_price": 34500.0,
            "actual_lower_price": 25500.0, "actual_grid_count": 300,
            "actual_leverage": 3.0,
        }
        msg = self._format(self._dashboard(), latest_cycle=latest)
        self.assertIn("当前：🔴 STOPPED / 风险观察", msg)
        self.assertIn("止损提醒线：22,950.00", msg)  # 25500 × (1 - 0.10)
        self.assertIn("Base：30,000.00", msg)
        self.assertIn("Upper：34,500.00", msg)
        self.assertIn("Lower：25,500.00", msg)
        self.assertIn("网格：300格", msg)
        self.assertIn("杠杆：3.0x", msg)

    def test_12b_stopped_report_stop_loss_triggered(self):
        """12b. STOPPED + 已触发止损提醒 -> 明确显示 已触发止损提醒"""
        latest = {
            "id": 2, "status": "STOPPED", "close_reason": "LOWER_BREACHED",
            "actual_base_price": 30000.0, "actual_upper_price": 34500.0,
            "actual_lower_price": 25500.0, "actual_grid_count": 300,
            "actual_leverage": 3.0,
        }
        notifier = WeChatNotifier("https://mock.webhook/key=SECRET")
        report_data = {
            "date": "2026-09-04",
            "dashboard": self._dashboard(),
            "latest_cycle": latest,
            "stop_loss_alerted": True,
            "strategy": {"upper_pct": 0.20, "lower_pct": 0.20, "grid_count": 200,
                         "leverage": 5.0, "stop_loss_after_lower_pct": 0.10},
        }
        msg = notifier.format_ndx_grid_report(report_data)
        self.assertIn("当前：🛑 STOPPED / 已触发止损提醒", msg)
        self.assertIn("止损提醒线：22,950.00", msg)

    def test_14_stale_ndx_not_fake_signal(self):
        """14. NDX 数据 stale -> 数据状态：🟡 过期, 入场信号未评估 (不是 未触发)"""
        dash = self._dashboard()
        dash["ndx"] = {
            "last_price": 22000.0, "rsi": 30.0, "ma200": 19000.0,
            "price_1y_ago": 18000.0, "entry_signal": None,
            "is_data_valid": True, "is_data_fresh": False,
            "data_timestamp": "2026-08-25 00:00:00-04:00",
        }
        msg = self._format(dash)
        self.assertIn("数据状态：🟡 过期", msg)
        self.assertIn("状态：❓ 未评估", msg)

    def test_15_unavailable_ndx(self):
        """15. NDX 数据完全不可用 -> N/A + 未评估, 日报仍发送"""
        dash = self._dashboard()
        dash["ndx"] = {
            "last_price": None, "rsi": None, "ma200": None, "price_1y_ago": None,
            "entry_signal": None, "is_data_valid": False, "is_data_fresh": False,
            "data_timestamp": None,
        }
        msg = self._format(dash)
        self.assertIn("价格：N/A", msg)
        self.assertIn("RSI(14)：N/A", msg)
        self.assertIn("数据状态：🔴 不可用", msg)
        self.assertIn("状态：❓ 未评估", msg)

    def test_17_18_dedup_single_report_per_day(self):
        """17/18. 同一天最多一份日报 (dedup key DAILY_REPORT); 第二次调用不再发送"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = True
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(notifier.send_ndx_grid_report.call_count, 1)

        # 同一天再次执行 (如重启后) -> dedup, 不再发送第二份
        notifier.reset_mock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        notifier.send_ndx_grid_report.assert_not_called()

    def test_22_no_secrets_in_report(self):
        """22. 日报内容不包含 webhook/secret"""
        dash = self._dashboard()
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        dash["waiting_cycle"] = grid_service.serialize_cycle(cycle)
        msg = self._format(dash)
        self.assertNotIn("SECRET", msg)
        self.assertNotIn("webhook", msg)


class TestNDXAlertLogConsistency(DailyReportTestBase):
    """
    测试 A-G: NDX Grid 日报 AlertLog.message 与企业微信发送内容一致性。
    """

    def _dashboard(self, running=None, waiting=None):
        return {
            "ndx": {
                "last_price": 22000.0, "rsi": 52.5, "ma200": 19000.0,
                "price_1y_ago": 18000.0, "entry_signal": True,
                "is_data_valid": True, "is_data_fresh": True,
                "data_timestamp": str(datetime.now(et_tz)),
            },
            "running_cycle": running,
            "waiting_cycle": waiting,
            "theoretical_grid_position": None,
        }

    def test_A_formatter_generates_complete_message(self):
        """A. format_ndx_grid_report() 生成的完整 message 包含所有关键字段。"""
        notifier = WeChatNotifier("https://mock.webhook/key=SECRET")
        report_data = {
            "date": "2026-09-04",
            "dashboard": self._dashboard(),
            "latest_cycle": None,
            "strategy": {"upper_pct": 0.20, "lower_pct": 0.20, "grid_count": 200, "leverage": 5.0},
        }
        msg = notifier.format_ndx_grid_report(report_data)
        self.assertIn("纳斯达克100", msg)
        self.assertIn("RSI(14)", msg)
        self.assertIn("Grid 状态", msg)
        self.assertIn("Grid 入场信号", msg)
        self.assertIn("市场状态", msg)
        # 多行，不是单行摘要
        self.assertGreater(msg.count("\n"), 5)

    def test_B_alertlog_message_equals_wechat_message(self):
        """B. 企业微信发送的 message 与 AlertLog 保存的 message 内容完全一致。"""
        from app.database.models import AlertLog

        self._clear_dedup()
        captured_send = {}

        def fake_send_message(msg):
            captured_send["msg"] = msg
            return True

        notifier = WeChatNotifier("https://mock.webhook/key=SECRET")
        notifier._send_message = fake_send_message

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(
                self.mock_fetcher,
                db=self.db,
                config=self._config("ndx_grid"),
            )

        # 从数据库读出 AlertLog
        log = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "NDX_GRID_DAILY_REPORT"
        ).first()
        self.assertIsNotNone(log, "AlertLog 应该存在")

        import json
        stored = json.loads(log.message)
        # AlertLog.message 经 json.dumps(alert_dict) 包装，stored["message"] 是完整文本
        alert_log_message = stored["message"]
        wechat_message = captured_send.get("msg", "")

        self.assertEqual(
            alert_log_message, wechat_message,
            "AlertLog.message['message'] 应与企业微信实际发送内容完全一致"
        )

    def test_C_alertlog_message_is_not_summary_line(self):
        """C. AlertLog.message 不再是单行摘要 'NDX Grid Daily Report YYYY-MM-DD: grid_status=...'"""
        from app.database.models import AlertLog
        import json

        self._clear_dedup()
        notifier_mock = MagicMock()
        notifier_mock.send_ndx_grid_report.return_value = True
        # format_ndx_grid_report 必须返回真实内容（不能被 MagicMock 掉）
        real_notifier = WeChatNotifier("")
        notifier_mock.format_ndx_grid_report.side_effect = real_notifier.format_ndx_grid_report

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier_mock):
            send_daily_report_job(
                self.mock_fetcher,
                db=self.db,
                config=self._config("ndx_grid"),
            )

        log = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "NDX_GRID_DAILY_REPORT"
        ).first()
        self.assertIsNotNone(log)
        stored = json.loads(log.message)
        alert_log_message = stored["message"]

        # 旧摘要格式不应存在
        self.assertNotRegex(
            alert_log_message,
            r"^NDX Grid Daily Report \d{4}-\d{2}-\d{2}: grid_status=\w+$",
            "AlertLog.message 不应再是单行摘要"
        )
        # 应包含多行
        self.assertGreater(alert_log_message.count("\n"), 5)

    def test_D_alertlog_message_contains_key_fields(self):
        """D. AlertLog.message 包含 纳斯达克100 / RSI(14) / Grid 状态 / Grid 入场信号 等关键字段。"""
        from app.database.models import AlertLog
        import json

        self._clear_dedup()
        notifier_real = WeChatNotifier("")
        notifier_mock = MagicMock()
        notifier_mock.send_ndx_grid_report.return_value = True
        notifier_mock.format_ndx_grid_report.side_effect = notifier_real.format_ndx_grid_report

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier_mock):
            send_daily_report_job(
                self.mock_fetcher,
                db=self.db,
                config=self._config("ndx_grid"),
            )

        log = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "NDX_GRID_DAILY_REPORT"
        ).first()
        stored = json.loads(log.message)
        msg = stored["message"]

        for keyword in ["纳斯达克100", "RSI(14)", "Grid 状态", "Grid 入场信号"]:
            self.assertIn(keyword, msg, f"AlertLog.message 应包含字段: {keyword}")

    def test_F_dedup_behavior_unchanged(self):
        """F. dedup 行为不变：NDX Grid 日报同一天最多发送一份。"""
        from app.database.models import AlertLog

        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = True
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(notifier.send_ndx_grid_report.call_count, 1)

        notifier.reset_mock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))

        notifier.send_ndx_grid_report.assert_not_called()
        notifier.format_ndx_grid_report.assert_not_called()

    def test_G_send_failure_sets_sent_successfully_false(self):
        """G. 推送失败时 sent_successfully=False 正确写入 AlertLog。"""
        from app.database.models import AlertLog
        import json

        self._clear_dedup()
        real_notifier = WeChatNotifier("")
        notifier_mock = MagicMock()
        notifier_mock.send_ndx_grid_report.return_value = False   # 发送失败
        notifier_mock.format_ndx_grid_report.side_effect = real_notifier.format_ndx_grid_report

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier_mock):
            res = send_daily_report_job(
                self.mock_fetcher,
                db=self.db,
                config=self._config("ndx_grid"),
            )

        self.assertEqual(res["status"], "OK")  # scheduler 不应崩溃
        log = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "NDX_GRID_DAILY_REPORT"
        ).first()
        self.assertIsNotNone(log)
        self.assertFalse(log.sent_successfully, "推送失败时 sent_successfully 应为 False")
        # message 仍然是完整内容，不是空字符串
        stored = json.loads(log.message)
        self.assertGreater(len(stored["message"]), 50)


class TestMarketClosedSkip(DailyReportTestBase):
    """美国市场全天休市日 -> 跳过日报 (复用 NYSE 交易日历; 不消耗 dedup; 提前收市日照发)"""

    def test_23_market_closed_skips_report(self):
        """23. 休市日 (周末/节假日) -> 不生成、不发送日报, 不写 AlertLog"""
        from app.database.models import AlertLog

        self._clear_dedup()
        notifier = MagicMock()
        with patch("app.scheduler.jobs.is_trading_day", return_value=False), \
             patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "MARKET_CLOSED")
        notifier.format_ndx_grid_report.assert_not_called()
        notifier.send_ndx_grid_report.assert_not_called()
        self.assertEqual(self.db.query(AlertLog).count(), 0)

    def test_24_market_closed_does_not_consume_daily_dedup(self):
        """24. 休市日跳过不消耗 DAILY_REPORT dedup: 下一个交易日仍能正常发送"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = True
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
        with patch("app.scheduler.jobs.is_trading_day", return_value=False):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        with patch("app.scheduler.jobs.is_trading_day", return_value=True), \
             patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(res["status"], "OK")
        notifier.send_ndx_grid_report.assert_called_once()

    def test_25_early_close_day_still_sends_report(self):
        """25. 提前收市日仍为有效交易日 -> 正常发送日报 (只判断交易日, 不判断时段)"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_ndx_grid_report.return_value = True
        notifier.format_ndx_grid_report.return_value = "mock ndx grid report"
        with patch("app.scheduler.jobs.is_trading_day", return_value=True), \
             patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        self.assertEqual(res["status"], "OK")
        notifier.send_ndx_grid_report.assert_called_once()


class TestTradingDayCalendar(unittest.TestCase):
    """is_trading_day 复用 pandas-market-calendars (XNYS) 真实日历: 周末/全天休市/提前收市"""

    def test_weekend_is_not_trading_day(self):
        from app.scheduler.trading_hours import is_trading_day
        self.assertFalse(is_trading_day(datetime(2026, 9, 5, 12, 0, tzinfo=et_tz)))   # 周六
        self.assertFalse(is_trading_day(datetime(2026, 9, 6, 12, 0, tzinfo=et_tz)))   # 周日

    def test_full_holiday_is_not_trading_day(self):
        from app.scheduler.trading_hours import is_trading_day
        # Labor Day 2026-09-07 / Independence Day observed 2026-07-03 / Thanksgiving 2026-11-26
        self.assertFalse(is_trading_day(datetime(2026, 9, 7, 12, 0, tzinfo=et_tz)))
        self.assertFalse(is_trading_day(datetime(2026, 7, 3, 12, 0, tzinfo=et_tz)))
        self.assertFalse(is_trading_day(datetime(2026, 11, 26, 12, 0, tzinfo=et_tz)))

    def test_early_close_day_is_trading_day(self):
        from app.scheduler.trading_hours import is_trading_day
        # 2026-11-27 (Thanksgiving 次日) 与 2026-12-24 (Christmas Eve) 均为提前收市日
        self.assertTrue(is_trading_day(datetime(2026, 11, 27, 12, 0, tzinfo=et_tz)))
        self.assertTrue(is_trading_day(datetime(2026, 12, 24, 12, 0, tzinfo=et_tz)))

    def test_normal_trading_day_is_trading_day(self):
        from app.scheduler.trading_hours import is_trading_day
        self.assertTrue(is_trading_day(datetime(2026, 9, 8, 12, 0, tzinfo=et_tz)))


class TestStoppedGridDailyReport(DailyReportTestBase):
    """STOPPED Grid 日报: 风险观察 / 已触发止损提醒 (jobs 全链路, 含 dedup 状态)"""

    STOPPED_SUGGESTED = {
        "base_price": 30000.0, "upper_price": 34500.0, "lower_price": 25500.0,
        "grid_count": 300, "leverage": 3.0,
    }
    STOPPED_ACTUAL = {
        "actual_base_price": 30000.0, "actual_upper_price": 34500.0,
        "actual_lower_price": 25500.0, "actual_grid_count": 300,
        "actual_leverage": 3.0, "actual_margin": 1000.0,
    }

    def _make_stopped_cycle(self, mark_stop_loss: bool):
        from app.alerts.grid_cycle import stop_grid_cycle
        from app.alerts import dedup

        cycle = create_waiting_grid_cycle(self.db, dict(self.STOPPED_SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(self.STOPPED_ACTUAL))
        stop_grid_cycle(self.db, cycle.id, reason="LOWER_BREACHED")
        if mark_stop_loss:
            dedup.should_alert_stop_loss(dedup.stop_loss_cycle_key(cycle.id))
        return cycle

    def _run_report(self):
        captured = {}
        notifier = WeChatNotifier("https://mock.webhook/key=SECRET")
        notifier._send_message = lambda msg: (captured.__setitem__("msg", msg) or True)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        return res, captured.get("msg", "")

    def test_26_stopped_grid_report_shows_risk_watch(self):
        """26. STOPPED Grid 日报: 风险观察 + 止损提醒线 + 实际参数"""
        self._clear_dedup()
        cycle = self._make_stopped_cycle(mark_stop_loss=False)
        res, msg = self._run_report()
        self.assertEqual(res["status"], "OK")
        self.assertIn("当前：🔴 STOPPED / 风险观察", msg)
        self.assertIn("止损提醒线：22,950.00", msg)  # 25500 × (1 - 0.10)
        self.assertIn("Base：30,000.00", msg)
        self.assertIn("Upper：34,500.00", msg)
        self.assertIn("Lower：25,500.00", msg)
        self.assertIn("网格：300格", msg)
        self.assertIn("杠杆：3.0x", msg)

    def test_27_triggered_stop_loss_report(self):
        """27. 已触发止损提醒 -> 日报明确显示 已触发止损提醒"""
        self._clear_dedup()
        cycle = self._make_stopped_cycle(mark_stop_loss=True)
        res, msg = self._run_report()
        self.assertEqual(res["status"], "OK")
        self.assertIn("当前：🛑 STOPPED / 已触发止损提醒", msg)
        self.assertIn("止损提醒线：22,950.00", msg)

    def test_28_no_active_grid_defaults_rendered(self):
        """28. 无 Active Grid -> 显示运行时配置默认 (±15% / 300格 / 3.0x)"""
        from app.config import Config

        self._clear_dedup()
        cfg = Config({
            "daily_report_mode": "ndx_grid",
            "wechat_webhook_url": "https://mock.webhook/key=SECRET",
            "default_grid_upper_pct": 0.15,
            "default_grid_lower_pct": 0.15,
            "default_grid_count": 300,
            "default_grid_leverage": 3.0,
            "default_grid_stop_loss_after_lower_pct": 0.10,
        })
        captured = {}
        notifier = WeChatNotifier("https://mock.webhook/key=SECRET")
        notifier._send_message = lambda msg: (captured.__setitem__("msg", msg) or True)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=cfg)
        self.assertEqual(res["status"], "OK")
        msg = captured.get("msg", "")
        self.assertIn("当前：⚪ 无运行中的 Grid", msg)
        self.assertIn("默认区间：±15%", msg)
        self.assertIn("网格：300格", msg)
        self.assertIn("杠杆：3.0x", msg)


if __name__ == "__main__":
    unittest.main()
