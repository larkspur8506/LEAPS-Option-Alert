"""
Phase 6 Tests: Configurable Daily Report.

Covers:
- Default mode = legacy (backward compatible: NULL config -> legacy)
- mode = off / legacy / ndx_grid dispatching in scheduler job
- Rules page renders radio controls, POST saves to DB (real runtime config)
- legacy path calls original report logic unchanged
- ndx_grid generates NDX Grid daily report (reuses grid_service dashboard)
- WAITING / RUNNING / CLOSED / STOPPED / NO ACTIVE GRID report content
- stale / unavailable NDX -> N/A + NOT EVALUATED, never fake "NO" signal
- suggested_*/actual_* strictly separated in RUNNING report
- dedup: one report per day, mode switch does not resend
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
        self.mock_fetcher.get_qqq_data.return_value = {
            "last_price": 500.0, "rsi": 30.0, "ma200": 480.0,
            "consec_above": 3, "consec_below": 0,
            "is_above_sma200_3d": True, "is_below_sma200_3d": False,
            "price_1y_ago": 450.0,
        }
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data()

        # Configuration 行 (daily_report_mode 列)
        self.config_row = Configuration(
            admin_password_hash="x", daily_report_mode=None  # NULL -> legacy 默认
        )
        self.db.add(self.config_row)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _config(self, mode=None):
        """构造运行时 config: mode=None 表示 DB 中无配置 (走 legacy 默认)"""
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
        })
        return cfg

    def _clear_dedup(self):
        from app.alerts.dedup import clear_dedup
        clear_dedup()


class TestConfigDefaults(DailyReportTestBase):
    def test_1_default_mode_is_legacy(self):
        """1. 默认 mode = legacy (DB NULL / 缺失 / 非法值)"""
        self.assertEqual(self._config(None).get_daily_report_mode(), "legacy")  # DB NULL
        from app.config import Config
        self.assertEqual(Config({}).get_daily_report_mode(), "legacy")           # 完全缺失
        self.assertEqual(Config({"daily_report_mode": "bogus"}).get_daily_report_mode(), "legacy")  # 非法
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
        notifier.send_daily_report.assert_not_called()
        notifier.send_ndx_grid_report.assert_not_called()

    def test_3_mode_legacy_calls_original_logic(self):
        """3/7. mode=legacy -> 调用原有日报逻辑 (send_daily_report + QQQ 数据 + alert log)"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_daily_report.return_value = True
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            res = send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("legacy"))
        self.assertEqual(res["status"], "OK")
        self.mock_fetcher.get_qqq_data.assert_called_once()
        notifier.send_daily_report.assert_called_once()
        report = notifier.send_daily_report.call_args[0][0]
        self.assertEqual(report["qqq_price"], 500.0)   # 原内容结构不变
        self.assertEqual(report["entry_met"], True)
        notifier.send_ndx_grid_report.assert_not_called()
        # 原日报仍写 alert_logs
        from app.database.models import AlertLog
        self.assertEqual(
            self.db.query(AlertLog).filter(AlertLog.alert_type == "DAILY_REPORT").count(), 1
        )

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
        notifier.send_daily_report.assert_not_called()
        report = notifier.send_ndx_grid_report.call_args[0][0]
        self.assertIn("dashboard", report)
        self.assertIn("strategy", report)

    def test_6_mode_read_at_execution_time(self):
        """6. scheduler 每次执行时读取当前配置 (运行时配置, 非启动时快照)"""
        self._clear_dedup()
        # 第一次: legacy
        notifier = MagicMock()
        notifier.send_daily_report.return_value = True
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("legacy"))
        notifier.send_daily_report.assert_called_once()
        # 同一天第二次执行 (任何 mode) -> dedup, 不再发送
        notifier.reset_mock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        notifier.send_ndx_grid_report.assert_not_called()
        notifier.send_daily_report.assert_not_called()

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
        """21. 日报内部异常 -> job 返回 ERROR, 不向上抛, 不影响其他 job"""
        self._clear_dedup()
        bad_fetcher = MagicMock()
        bad_fetcher.get_qqq_data.side_effect = RuntimeError("boom")
        res = send_daily_report_job(bad_fetcher, db=self.db, config=self._config("legacy"))
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

            # 4. 非法值 fallback legacy
            res_bogus = client.post("/admin/rules/daily-report-mode", data={"daily_report_mode": "invalid_mode"})
            self.assertEqual(res_bogus.status_code, 200)
            self.assertIn('value="legacy"\n                        checked', res_bogus.text)
        finally:
            client.close()
            app.dependency_overrides.clear()

    def test_5c_db_overrides_env_var_priority(self):
        """5c. Priority test: DB Configuration.daily_report_mode > DAILY_REPORT_MODE env > 'legacy'"""
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
        self.assertIn("Grid Status\nCLOSED\nReason: UPPER_REACHED", msg)
        self.assertNotIn("Base:", msg)
        self.assertNotIn("Suggested", msg)

    def test_9_waiting_report_suggested_only(self):
        """9. WAITING 日报只使用 suggested_*"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        ser = grid_service.serialize_cycle(cycle)
        msg = self._format(self._dashboard(waiting=ser))
        self.assertIn("Grid Status\nWAITING — 等待用户确认并在交易所启动 Grid", msg)
        self.assertIn("Suggested Base: 20,000.00", msg)
        self.assertIn("Suggested Upper: 24,000.00", msg)
        self.assertIn("Suggested Lower: 16,000.00", msg)
        self.assertIn("Grid Count: 200", msg)
        self.assertIn("Leverage: 5.0x", msg)
        self.assertNotIn("actual_", msg)
        self.assertNotIn("Actual", msg)

    def test_10_running_report_actual_only(self):
        """10/16. RUNNING 日报只使用 actual_*; suggested 不混淆"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        running = grid_service.get_running_cycle(self.db)

        msg = self._format(self._dashboard(running=running))
        self.assertIn("Grid Status\nRUNNING", msg)
        self.assertIn("Base: 20,000.00", msg)
        self.assertIn("Grid Count: 150", msg)     # actual 150, 不是 suggested 200
        self.assertIn("Leverage: 3.0x", msg)      # actual 3x, 不是 suggested 5x
        self.assertIn("Margin: 1,000.00", msg)
        # Grid Status 段内不得出现 suggested 值 (Grid Strategy 段的默认值 200/5x 属正常展示)
        grid_status_section = msg.split("Grid Status\nRUNNING")[1]
        self.assertIn("Grid Count: 150", grid_status_section)
        self.assertNotIn("Grid Count: 200", grid_status_section)
        self.assertNotIn("Leverage: 5.0x", grid_status_section)

    def test_10b_running_report_actual_survives_strategy_change(self):
        """10b. 默认策略变化后 RUNNING 日报仍显示用户实际参数 (150/3x)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        running = grid_service.get_running_cycle(self.db)

        # 策略"变成" 200格/5x —— 传给 report 的 strategy 变了, 但 running cycle 不变
        msg = self._format(self._dashboard(running=running))
        self.assertIn("Grid Count: 150", msg)
        self.assertIn("Leverage: 3.0x", msg)

    def test_11_closed_report(self):
        """11. CLOSED -> Grid Status CLOSED + Reason"""
        msg = self._format(self._dashboard(), latest_cycle={"status": "CLOSED", "close_reason": "UPPER_REACHED", "id": 1})
        self.assertIn("Grid Status\nCLOSED\nReason: UPPER_REACHED", msg)

    def test_12_stopped_report(self):
        """12. STOPPED -> Grid Status STOPPED + Reason"""
        msg = self._format(self._dashboard(), latest_cycle={"status": "STOPPED", "close_reason": "LOWER_BREACHED", "id": 2})
        self.assertIn("Grid Status\nSTOPPED\nReason: LOWER_BREACHED", msg)

    def test_14_stale_ndx_not_fake_signal(self):
        """14. NDX 数据 stale -> Data: STALE, Entry Signal: NOT EVALUATED (不是 NO)"""
        dash = self._dashboard()
        dash["ndx"] = {
            "last_price": 22000.0, "rsi": 30.0, "ma200": 19000.0,
            "price_1y_ago": 18000.0, "entry_signal": None,
            "is_data_valid": True, "is_data_fresh": False,
            "data_timestamp": "2026-08-25 00:00:00-04:00",
        }
        msg = self._format(dash)
        self.assertIn("Data: STALE", msg)
        self.assertIn("Entry Signal\nNOT EVALUATED", msg)

    def test_15_unavailable_ndx(self):
        """15. NDX 数据完全不可用 -> N/A + NOT EVALUATED, 日报仍发送"""
        dash = self._dashboard()
        dash["ndx"] = {
            "last_price": None, "rsi": None, "ma200": None, "price_1y_ago": None,
            "entry_signal": None, "is_data_valid": False, "is_data_fresh": False,
            "data_timestamp": None,
        }
        msg = self._format(dash)
        self.assertIn("Price: N/A", msg)
        self.assertIn("RSI14: N/A", msg)
        self.assertIn("Data: UNAVAILABLE", msg)
        self.assertIn("Entry Signal\nNOT EVALUATED", msg)

    def test_17_18_dedup_single_report_per_day(self):
        """17/18. 同一天最多一份; legacy 发送后切换 ndx_grid 不再发第二份"""
        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_daily_report.return_value = True
        notifier.send_ndx_grid_report.return_value = True
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("legacy"))
        self.assertEqual(notifier.send_daily_report.call_count, 1)

        # 16:35 用户改成 ndx_grid -> 不自动再发第二份
        notifier.reset_mock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))
        notifier.send_ndx_grid_report.assert_not_called()
        notifier.send_daily_report.assert_not_called()

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
        self.assertIn("NDX Market", msg)
        self.assertIn("RSI14", msg)
        self.assertIn("Grid Status", msg)
        self.assertIn("Grid Strategy", msg)
        self.assertIn("Entry Signal", msg)
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
        """D. AlertLog.message 包含 NDX Market / RSI14 / Grid Status / Grid Strategy 等关键字段。"""
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

        for keyword in ["NDX Market", "RSI14", "Grid Status", "Grid Strategy"]:
            self.assertIn(keyword, msg, f"AlertLog.message 应包含字段: {keyword}")

    def test_E_legacy_alertlog_behavior_unchanged(self):
        """E. Legacy 日报 AlertLog 行为保持不变（单行摘要形式，alert_type=DAILY_REPORT）。"""
        from app.database.models import AlertLog
        import json

        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_daily_report.return_value = True

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(
                self.mock_fetcher,
                db=self.db,
                config=self._config("legacy"),
            )

        log = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "DAILY_REPORT"
        ).first()
        self.assertIsNotNone(log, "Legacy AlertLog 应存在")
        stored = json.loads(log.message)
        self.assertEqual(stored["alert_type"], "DAILY_REPORT")
        self.assertEqual(stored["rule_name"], "盘后交易日报")
        # Legacy message 字段是单行摘要 (原有行为不变)
        self.assertIn("QQQ收盘价", stored["message"])

    def test_F_dedup_behavior_unchanged(self):
        """F. dedup 行为不变：同一天 legacy 发送后，ndx_grid 不再发第二份。"""
        from app.database.models import AlertLog

        self._clear_dedup()
        notifier = MagicMock()
        notifier.send_daily_report.return_value = True
        notifier.send_ndx_grid_report.return_value = True

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("legacy"))
        self.assertEqual(notifier.send_daily_report.call_count, 1)

        notifier.reset_mock()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self._config("ndx_grid"))

        notifier.send_ndx_grid_report.assert_not_called()
        notifier.format_ndx_grid_report.assert_not_called()

        # 数据库中不应多出 NDX_GRID_DAILY_REPORT 记录
        ndx_count = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "NDX_GRID_DAILY_REPORT"
        ).count()
        self.assertEqual(ndx_count, 0, "dedup 生效后不应写入 NDX_GRID_DAILY_REPORT AlertLog")

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


if __name__ == "__main__":
    unittest.main()
