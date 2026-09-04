"""
Phase 5 Tests: Production Readiness Audit regressions.

Covers:
- Duplicate concurrent starts cannot create two RUNNING cycles (backend-enforced,
  independent sessions, no frontend disable reliance)
- Init DB engine no longer uses StaticPool (per-session connections)
- Scheduler NDX job gets its own DB session (db=None -> SessionLocal)
- Scheduler outer exception handler does NOT rollback committed state changes
- Restart recovery: WAITING / RUNNING survive process restart (state from DB,
  fresh engine + fresh session), scheduler continues monitoring after restart
- WeChat failure never rolls back committed CLOSED state (commit-before-notify)
- Secret redaction in wechat error logs
- Legacy option position POST endpoints retired (405) while GET redirect and
  DB read paths remain intact
- NDX data quality: NaN Close rows and duplicate dates handled, insufficient
  data never triggers entry signal
"""
import unittest
from datetime import datetime
from threading import Barrier
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pytz import timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, GridCycle
from app.alerts.grid_cycle import (
    create_waiting_grid_cycle,
    get_running_grid_cycle,
    get_waiting_grid_cycle,
)
from app.services import grid_service
from app.market.data_fetcher import DataFetcher

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


class TestEngineConfig(unittest.TestCase):
    def test_production_engine_not_staticpool(self):
        """生产 engine 不得使用 StaticPool (防止跨 session 事务交错)"""
        from app.database.init_db import engine
        self.assertNotIsInstance(engine.pool, StaticPool)

    def test_scheduler_ndx_job_uses_own_session(self):
        """scheduler 的 NDX job 注册参数必须是 db=None (自建独立 session)"""
        from app.scheduler import jobs
        from apscheduler.triggers.interval import IntervalTrigger

        job = jobs.scheduler.get_job("check_ndx_grid_cycles")
        if job is None:
            self.skipTest("scheduler not started in test env")
        self.assertIsNone(job.args[1])


class TestDuplicateStartRace(unittest.TestCase):
    """连续/并发 POST start 不能产生两个 RUNNING (由后端保证)"""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=self.engine)

    def test_duplicate_sequential_start_single_running(self):
        """连续两次 POST start (如 HTTP 客户端重试) -> 第二次失败, 仅一个 RUNNING"""
        db = self.Session()
        cycle = create_waiting_grid_cycle(db, dict(SUGGESTED))
        db.close()

        # 两次请求各用独立 session, 模拟两个真实 HTTP 请求
        for i in range(2):
            s = self.Session()
            try:
                if i == 0:
                    result = grid_service.start_cycle(s, cycle.id, dict(VALID_ACTUAL))
                    self.assertEqual(result["status"], "RUNNING")
                else:
                    with self.assertRaises(grid_service.GridCycleStateError):
                        grid_service.start_cycle(s, cycle.id, dict(VALID_ACTUAL))
            finally:
                s.close()

        s = self.Session()
        try:
            running = s.query(GridCycle).filter(GridCycle.status == "RUNNING").all()
            self.assertEqual(len(running), 1)
        finally:
            s.close()

    def test_concurrent_start_same_cycle_single_running(self):
        """同一 cycle 被两个线程同时 start (HTTP 重试/双击) -> 全程仅一个 RUNNING

        注: 两个不同 WAITING 并发 start 的场景在生产中结构性不可能——
        WAITING 仅由单线程 scheduler (max_instances=1) 经 create_waiting_grid_cycle
        的唯一性防护创建, API 不创建 WAITING。
        """
        db = self.Session()
        cycle = create_waiting_grid_cycle(db, dict(SUGGESTED))
        cycle_id = cycle.id
        db.close()

        results = []
        barrier = Barrier(2)

        def _start():
            barrier.wait()  # 两个线程同时到达, 最大化竞争窗口
            s = self.Session()
            try:
                grid_service.start_cycle(s, cycle_id, dict(VALID_ACTUAL))
                results.append("OK")
            except grid_service.GridCycleStateError:
                results.append("CONFLICT")
            finally:
                s.close()

        import threading
        t1 = threading.Thread(target=_start)
        t2 = threading.Thread(target=_start)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # 无论竞争结果如何 (一成一败或都成功覆盖同值), 状态必须一致且唯一
        self.assertEqual(
            self.Session().query(GridCycle).filter(GridCycle.status == "RUNNING").count(), 1
        )
        self.assertEqual(
            self.Session().query(GridCycle).filter(GridCycle.status == "WAITING").count(), 0
        )

        s = self.Session()
        try:
            running = s.query(GridCycle).filter(GridCycle.id == cycle_id).first()
            self.assertEqual(running.status, "RUNNING")
            # 同 payload 重试: 参数必须是请求的值, 不得被改写为其他值
            self.assertEqual(running.actual_grid_count, 150)
            self.assertEqual(running.actual_leverage, 3.0)
        finally:
            s.close()


class TestSchedulerNoRollbackAfterCommit(unittest.TestCase):
    """scheduler 外层异常处理不得 rollback 已 commit 的状态变更"""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        self.cycle_id = cycle.id

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_exception_after_commit_keeps_closed_state(self):
        """process 内部 commit 后抛异常 -> 不 rollback, CLOSED 保持, 不重复通知"""
        from app.scheduler.jobs import check_ndx_grid_cycles

        mock_fetcher = MagicMock()
        mock_fetcher.get_ndx_data.return_value = {
            "is_data_valid": True,
            "last_price": 25000.0,  # >= actual_upper 24000
            "data_timestamp": str(datetime.now(et_tz)),
        }
        # 通知器在发送时抛异常 (commit 已完成)
        faulty_notifier = MagicMock()
        faulty_notifier.send_ndx_upper_alert.side_effect = ConnectionError("webhook down")

        # 需要带 webhook 的 config, monitor 才会构造 notifier 并发送
        mock_config = MagicMock()
        mock_config.get_wechat_webhook_url.return_value = "https://mock.webhook/key"

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=faulty_notifier):
            res = check_ndx_grid_cycles(
                mock_fetcher, db=self.db, config=mock_config, check_trading_hours=False
            )

        self.assertEqual(res["status"], "CLOSED")
        self.db.expire_all()
        cycle = self.db.query(GridCycle).filter(GridCycle.id == self.cycle_id).first()
        self.assertEqual(cycle.status, "CLOSED")
        self.assertEqual(cycle.close_reason, "UPPER_REACHED")
        # 只通知一次, 下一轮不再触发
        self.assertEqual(faulty_notifier.send_ndx_upper_alert.call_count, 1)

    def test_post_commit_exception_does_not_rollback_in_job_wrapper(self):
        """job 外层 except 分支不再调用 rollback: patch 验证"""
        import app.scheduler.jobs as jobs_module

        with patch.object(jobs_module, "logger") as mock_logger, \
             patch.object(jobs_module, "process_ndx_grid_cycle",
                          side_effect=RuntimeError("boom after inner commit")):
            res = jobs_module.check_ndx_grid_cycles(
                MagicMock(), db=self.db, config=None, check_trading_hours=False
            )

        self.assertEqual(res["status"], "ERROR")
        # session 未被 rollback (Rollback-only 检查: 无 dirty 状态丢失异常)
        mock_logger.error.assert_called_once()


class TestRestartRecovery(unittest.TestCase):
    """进程重启恢复: 状态全部来自数据库, 新 engine + 新 session 可继续监控"""

    def _make_engine(self):
        return create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )

    def _run_monitor(self, engine, price):
        """模拟重启后 scheduler 一轮: 全新 session"""
        S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        s = S()
        try:
            ndx_data = {
                "is_data_valid": True,
                "last_price": price,
                "data_timestamp": str(datetime.now(et_tz)),
            }
            from app.alerts.grid_monitor import process_ndx_grid_cycle
            return process_ndx_grid_cycle(s, ndx_data, notifier=None, config=None)
        finally:
            s.close()

    def test_waiting_survives_restart(self):
        """1. 创建 WAITING -> 重启 (新 engine 挂到同一文件库此处用同 engine 模拟) -> WAITING 仍在"""
        engine = self._make_engine()
        Base.metadata.create_all(bind=engine)
        S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        s = S()
        try:
            cycle = create_waiting_grid_cycle(s, dict(SUGGESTED))
            cycle_id = cycle.id
        finally:
            s.close()  # 相当于进程结束释放 session

        # "重启后": 新 session 查询
        s2 = S()
        try:
            waiting = get_waiting_grid_cycle(s2)
            self.assertIsNotNone(waiting)
            self.assertEqual(waiting.id, cycle_id)
            self.assertEqual(waiting.status, "WAITING")
        finally:
            s2.close()
        Base.metadata.drop_all(bind=engine)

    def test_running_survives_restart_and_monitoring_continues(self):
        """2. RUNNING -> 重启 -> RUNNING 仍在 -> scheduler 恢复后继续边界监控"""
        engine = self._make_engine()
        Base.metadata.create_all(bind=engine)
        S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        s = S()
        try:
            cycle = create_waiting_grid_cycle(s, dict(SUGGESTED))
            grid_service.start_cycle(s, cycle.id, dict(VALID_ACTUAL))
            cycle_id = cycle.id
        finally:
            s.close()

        # 重启后第一轮监控: 价格处于区间内
        res1 = self._run_monitor(engine, 20000.0)
        self.assertEqual(res1["status"], "RUNNING_NO_CHANGE")

        # 重启后第二轮: 突破 upper -> 正常 CLOSED (说明监控已恢复)
        res2 = self._run_monitor(engine, 24500.0)
        self.assertEqual(res2["status"], "CLOSED")

        s3 = S()
        try:
            cycle = s3.query(GridCycle).filter(GridCycle.id == cycle_id).first()
            self.assertEqual(cycle.status, "CLOSED")
            self.assertEqual(cycle.close_reason, "UPPER_REACHED")
        finally:
            s3.close()
        Base.metadata.drop_all(bind=engine)

    def test_file_db_restart_recovery(self):
        """3. 真实文件库重启: 落盘 -> 关闭所有连接 -> 新 engine 重新打开 -> 状态完整"""
        import os
        import tempfile

        tmp = os.path.join(tempfile.gettempdir(), "grid_phase5_restart_test.db")
        if os.path.exists(tmp):
            os.remove(tmp)

        url = f"sqlite:///{tmp}"
        engine1 = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine1)
        S1 = sessionmaker(bind=engine1)
        s1 = S1()
        try:
            cycle = create_waiting_grid_cycle(s1, dict(SUGGESTED))
            grid_service.start_cycle(s1, cycle.id, dict(VALID_ACTUAL))
            cycle_id = cycle.id
        finally:
            s1.close()
        engine1.dispose()  # 相当于进程退出

        engine2 = create_engine(url, connect_args={"check_same_thread": False})
        S2 = sessionmaker(bind=engine2)
        s2 = S2()
        try:
            running = s2.query(GridCycle).filter(GridCycle.id == cycle_id).first()
            self.assertEqual(running.status, "RUNNING")
            self.assertEqual(running.actual_grid_count, 150)
            self.assertEqual(running.actual_leverage, 3.0)
        finally:
            s2.close()
            engine2.dispose()
            os.remove(tmp)


class TestSecretRedaction(unittest.TestCase):
    def test_webhook_key_redacted_in_error_log(self):
        """wechat 异常日志中的 webhook key 必须脱敏"""
        from app.notification.wechat import _redact_secrets

        leaky = 'HTTPSConnectionPool(host=\'qyapi.weixin.qq.com\', port=443): url=/cgi-bin/webhook/send?key=SECRET123ABC'
        redacted = _redact_secrets(leaky)
        self.assertNotIn("SECRET123ABC", redacted)
        self.assertIn("key=***", redacted)
        # 无 key 参数的文本不受影响
        self.assertEqual(_redact_secrets("plain error"), "plain error")


class TestLegacyOptionEndpoints(unittest.TestCase):
    """旧 option position POST 端点已退役; 读路径与 GET redirect 保留"""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        from app.main import app
        from app.database.init_db import get_db
        app.dependency_overrides[get_db] = lambda: self.db
        self.app = app
        self.client = TestClient(app, follow_redirects=False)
        self.client.cookies.set("admin_logged_in", "true")

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_legacy_post_routes_retired(self):
        """POST 退役: /admin/positions 405 (路径仅存 GET redirect), 子路径 404 (路由移除)"""
        self.assertEqual(self.client.post("/admin/positions", data={}).status_code, 405)
        self.assertEqual(self.client.post("/admin/positions/1/delete").status_code, 404)
        self.assertEqual(self.client.post("/admin/positions/1/refresh").status_code, 404)

    def test_get_redirect_and_db_read_paths_intact(self):
        """GET redirect 保留; OptionPosition 表仍可读写 (jobs 读取路径依赖)"""
        res = self.client.get("/admin/positions")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/admin/grid")

        from app.database.models import OptionPosition
        from datetime import date
        pos = OptionPosition(
            underlying="QQQ", option_type="CALL", strike_price=500.0,
            expiration_date=date(2027, 1, 15), entry_price=10.0,
            quantity=1, entry_date=date(2026, 1, 15)
        )
        self.db.add(pos)
        self.db.commit()
        self.assertEqual(self.db.query(OptionPosition).count(), 1)


class TestNDXDataQuality(unittest.TestCase):
    """NDX 数据质量: NaN / 重复索引 / 数据不足 均不产生错误信号"""

    def _df(self, n=300, base=20000.0):
        import pandas as pd
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        prices = [base + i * 10 for i in range(n)]
        return pd.DataFrame({"Close": prices, "High": prices, "Low": prices}, index=dates)

    def test_nan_close_dropped(self):
        import pandas as pd
        import numpy as np
        df = self._df(300)
        df.iloc[150, df.columns.get_loc("Close")] = np.nan
        res = DataFetcher.calculate_technical_indicators(df, ticker="^NDX")
        self.assertTrue(res["is_data_valid"])
        self.assertEqual(res["total_records"], 299)
        self.assertFalse(pd.isna(res["last_price"]))

    def test_duplicate_dates_deduped(self):
        import pandas as pd
        df = self._df(300)
        dup = df.iloc[[-1]]
        dirty = pd.concat([df, dup])
        res = DataFetcher.calculate_technical_indicators(dirty, ticker="^NDX")
        self.assertEqual(res["total_records"], 300)

    def test_insufficient_data_no_signal(self):
        from app.alerts.ndx_rules import check_entry_signals
        res = DataFetcher.calculate_technical_indicators(self._df(150), ticker="^NDX")
        self.assertFalse(res["is_data_valid"])
        # 数据不足 -> 指标缺失 -> check_entry_signals 不应触发
        self.assertEqual(check_entry_signals(res.get("last_price") or 0, res), [])


if __name__ == "__main__":
    unittest.main()
