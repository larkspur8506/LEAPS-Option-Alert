"""
Phase 4A Tests: GridCycle Backend Management API / Service Layer.

Covers:
- Start: WAITING -> RUNNING, actual params saved verbatim, suggested untouched,
  started_at set, freeze after RUNNING
- Validation: base/upper/lower/grid_count/leverage/margin rules + NaN rejection
- State: re-start RUNNING/CLOSED/STOPPED fails, uniqueness when another RUNNING
- Close: RUNNING -> CLOSED/MANUAL_CLOSE, illegal source states fail,
  scheduler does NOT re-send UPPER notification after manual close
- History: latest first, limit respected, limit clamped
- Dashboard: uses ACTUAL params for theoretical position, nulls without RUNNING,
  entry signal is read-only
- HTTP API: auth 401, 404/409/400/422 mapping, null bodies, status endpoint
"""
import unittest
import math
from datetime import datetime
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
from app.alerts.grid_monitor import process_ndx_grid_cycle
from app.services import grid_service
from app.services.grid_service import (
    GridCycleNotFoundError,
    GridCycleStateError,
    GridParameterError,
    start_cycle,
    close_running_cycle,
    get_cycle_history,
    get_grid_dashboard,
    validate_actual_params,
)

et_tz = timezone("America/New_York")

VALID_ACTUAL = {
    "actual_base_price": 25150.0,
    "actual_upper_price": 30200.0,
    "actual_lower_price": 20100.0,
    "actual_grid_count": 180,
    "actual_leverage": 4.0,
    "actual_margin": 1000.0,
}

SUGGESTED = {
    "base_price": 25000.0,
    "upper_price": 30000.0,
    "lower_price": 20000.0,
    "grid_count": 200,
    "leverage": 5.0,
}


class GridServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()
        self.cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _assert_grid_parameter_error(self, params_overrides: dict):
        params = dict(VALID_ACTUAL)
        params.update(params_overrides)
        with self.assertRaises(GridParameterError):
            start_cycle(self.db, self.cycle.id, params)


class TestGridStartService(GridServiceTestBase):
    def test_1_start_waiting_to_running_success(self):
        """1. 正常 WAITING -> RUNNING"""
        result = start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(get_running_grid_cycle(self.db).id, self.cycle.id)

    def test_2_actual_params_saved_verbatim(self):
        """2. actual 参数原样保存 (grid_count=180, leverage=4 不得被自动修正)"""
        result = start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        self.assertEqual(result["actual_base_price"], 25150.0)
        self.assertEqual(result["actual_upper_price"], 30200.0)
        self.assertEqual(result["actual_lower_price"], 20100.0)
        self.assertEqual(result["actual_grid_count"], 180)
        self.assertEqual(result["actual_leverage"], 4.0)
        self.assertEqual(result["actual_margin"], 1000.0)

    def test_3_suggested_params_unchanged_after_start(self):
        """3. suggested 参数保持原样, 不被 actual 覆盖"""
        result = start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        self.assertEqual(result["suggested_base_price"], 25000.0)
        self.assertEqual(result["suggested_upper_price"], 30000.0)
        self.assertEqual(result["suggested_lower_price"], 20000.0)
        self.assertEqual(result["suggested_grid_count"], 200)
        self.assertEqual(result["suggested_leverage"], 5.0)

    def test_4_started_at_set(self):
        """4. started_at 在启动后被设置"""
        result = start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        self.assertIsNotNone(result["started_at"])
        self.assertIsNone(result["closed_at"])
        self.assertIsNone(result["close_reason"])

    def test_5_actual_frozen_after_running(self):
        """5. actual 参数冻结: RUNNING 后再次 start 失败, actual 不变"""
        result = start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

        with self.assertRaises(GridCycleStateError):
            start_cycle(self.db, self.cycle.id, {
                "actual_base_price": 26000.0,
                "actual_upper_price": 31000.0,
                "actual_lower_price": 21000.0,
                "actual_grid_count": 100,
                "actual_leverage": 3.0,
                "actual_margin": 500.0,
            })

        self.db.expire_all()
        frozen = self.db.query(GridCycle).filter(GridCycle.id == self.cycle.id).first()
        self.assertEqual(frozen.actual_base_price, result["actual_base_price"])
        self.assertEqual(frozen.actual_grid_count, 180)
        self.assertEqual(frozen.actual_leverage, 4.0)


class TestGridStartValidation(GridServiceTestBase):
    def test_6_base_not_positive(self):
        """6. actual_base_price <= 0 拒绝"""
        self._assert_grid_parameter_error({"actual_base_price": 0})
        self._assert_grid_parameter_error({"actual_base_price": -1})

    def test_7_upper_not_above_base(self):
        """7. actual_upper_price <= actual_base_price 拒绝"""
        self._assert_grid_parameter_error({"actual_upper_price": 25150.0})
        self._assert_grid_parameter_error({"actual_upper_price": 20000.0})

    def test_8_lower_not_below_base(self):
        """8. actual_lower_price >= actual_base_price 拒绝"""
        self._assert_grid_parameter_error({"actual_lower_price": 25150.0})
        self._assert_grid_parameter_error({"actual_lower_price": 26000.0})

    def test_9_lower_not_positive(self):
        """9. actual_lower_price <= 0 拒绝"""
        self._assert_grid_parameter_error({"actual_lower_price": 0})
        self._assert_grid_parameter_error({"actual_lower_price": -100})

    def test_10_grid_count_invalid(self):
        """10. actual_grid_count < 1 拒绝"""
        self._assert_grid_parameter_error({"actual_grid_count": 0})
        self._assert_grid_parameter_error({"actual_grid_count": -5})

    def test_11_leverage_not_positive(self):
        """11. actual_leverage <= 0 拒绝 (不强制等于 5)"""
        self._assert_grid_parameter_error({"actual_leverage": 0})
        self._assert_grid_parameter_error({"actual_leverage": -1})

    def test_12_margin_not_positive(self):
        """12. actual_margin <= 0 拒绝"""
        self._assert_grid_parameter_error({"actual_margin": 0})
        self._assert_grid_parameter_error({"actual_margin": -100})

    def test_12b_nan_inf_rejected(self):
        """补充: NaN / Inf 数值拒绝 (防止污染 RUNNING 边界监控)"""
        self._assert_grid_parameter_error({"actual_base_price": float("nan")})
        self._assert_grid_parameter_error({"actual_upper_price": float("inf")})

    def test_12c_leverage_differs_from_suggested_allowed(self):
        """补充: leverage=3 与建议 5 不同但 > 0, 必须允许"""
        params = dict(VALID_ACTUAL)
        params["actual_leverage"] = 3.0
        result = start_cycle(self.db, self.cycle.id, params)
        self.assertEqual(result["actual_leverage"], 3.0)
        self.assertEqual(result["suggested_leverage"], 5.0)


class TestGridStartState(GridServiceTestBase):
    def test_13_start_running_fails(self):
        """13. 已 RUNNING 再 start -> 失败"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        with self.assertRaises(GridCycleStateError):
            start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

    def test_14_start_closed_fails(self):
        """14. CLOSED 再 start -> 失败"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        close_running_cycle(self.db, self.cycle.id)
        with self.assertRaises(GridCycleStateError):
            start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

    def test_15_start_stopped_fails(self):
        """15. STOPPED 再 start -> 失败"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        from app.alerts.grid_cycle import stop_grid_cycle
        stop_grid_cycle(self.db, self.cycle.id, reason="LOWER_BREACHED")
        with self.assertRaises(GridCycleStateError):
            start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

    def test_16_start_blocked_when_another_running(self):
        """16. 已存在其他 RUNNING -> 必须失败 (唯一性)"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

        # RUNNING A 存在时 create_waiting 会被阻断, 手动插入 WAITING B 以测试防线
        cycle_b = GridCycle(
            status="WAITING",
            suggested_base_price=25000.0,
            suggested_upper_price=30000.0,
            suggested_lower_price=20000.0,
            suggested_grid_count=200,
            suggested_leverage=5.0
        )
        self.db.add(cycle_b)
        self.db.commit()

        with self.assertRaises(GridCycleStateError):
            start_cycle(self.db, cycle_b.id, dict(VALID_ACTUAL))

    def test_16b_start_not_found(self):
        """补充: 不存在的 cycle_id -> NotFound"""
        with self.assertRaises(GridCycleNotFoundError):
            start_cycle(self.db, 9999, dict(VALID_ACTUAL))


class TestGridClose(GridServiceTestBase):
    def test_17_manual_close_success(self):
        """17. RUNNING -> CLOSED / MANUAL_CLOSE, closed_at 设置"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        result = close_running_cycle(self.db, self.cycle.id)
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["close_reason"], "MANUAL_CLOSE")
        self.assertIsNotNone(result["closed_at"])
        # actual 参数在关闭后保持不变
        self.assertEqual(result["actual_upper_price"], 30200.0)

    def test_17b_manual_close_no_upper_renotification(self):
        """17b. 手动关闭后 scheduler 不得再触发 UPPER notification (幂等)"""
        mock_notifier = MagicMock()
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        close_running_cycle(self.db, self.cycle.id)

        # 价格远超 actual_upper, 但 cycle 已 CLOSED -> 无 RUNNING -> 不得触发
        ndx_data = {
            "is_data_valid": True,
            "last_price": 31000.0,
            "rsi": 50.0,
            "data_timestamp": str(datetime.now(et_tz))
        }
        res = process_ndx_grid_cycle(self.db, ndx_data, notifier=mock_notifier, config=None)

        self.assertEqual(res["status"], "NO_SIGNAL")
        mock_notifier.send_message.assert_not_called()
        self.assertEqual(mock_notifier.send_message.call_count, 0)

        self.db.expire_all()
        closed = self.db.query(GridCycle).filter(GridCycle.id == self.cycle.id).first()
        self.assertEqual(closed.status, "CLOSED")
        self.assertEqual(closed.close_reason, "MANUAL_CLOSE")

    def test_18_close_waiting_fails(self):
        """18. WAITING close -> 失败"""
        with self.assertRaises(GridCycleStateError):
            close_running_cycle(self.db, self.cycle.id)

    def test_19_close_closed_fails(self):
        """19. CLOSED close -> 失败"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        close_running_cycle(self.db, self.cycle.id)
        with self.assertRaises(GridCycleStateError):
            close_running_cycle(self.db, self.cycle.id)

    def test_20_close_stopped_fails(self):
        """20. STOPPED close -> 失败"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        from app.alerts.grid_cycle import stop_grid_cycle
        stop_grid_cycle(self.db, self.cycle.id, reason="LOWER_BREACHED")
        with self.assertRaises(GridCycleStateError):
            close_running_cycle(self.db, self.cycle.id)


class TestGridHistory(GridServiceTestBase):
    def _build_three_cycles(self):
        """构造 3 个不同状态的周期: CLOSED / STOPPED / WAITING"""
        from app.alerts.grid_cycle import stop_grid_cycle
        c1 = self.cycle
        start_cycle(self.db, c1.id, dict(VALID_ACTUAL))
        close_running_cycle(self.db, c1.id)

        c2 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_cycle(self.db, c2.id, dict(VALID_ACTUAL))
        stop_grid_cycle(self.db, c2.id, reason="LOWER_BREACHED")

        c3 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        return c1, c2, c3

    def test_21_history_latest_first(self):
        """21. history 按最新优先排序"""
        c1, c2, c3 = self._build_three_cycles()
        result = get_cycle_history(self.db, 50)
        ids = [c["id"] for c in result["cycles"]]
        self.assertEqual(ids, [c3.id, c2.id, c1.id])
        self.assertEqual(result["cycles"][0]["status"], "WAITING")
        self.assertEqual(result["cycles"][1]["status"], "STOPPED")
        self.assertEqual(result["cycles"][2]["status"], "CLOSED")

    def test_22_history_limit(self):
        """22. limit 生效"""
        c1, c2, c3 = self._build_three_cycles()
        result = get_cycle_history(self.db, 2)
        self.assertEqual(result["count"], 2)
        self.assertEqual([c["id"] for c in result["cycles"]], [c3.id, c2.id])
        self.assertEqual(result["limit"], 2)

    def test_23_history_limit_clamped(self):
        """23. limit 上限生效 (100000 -> 200), 下限钳制 (0 -> 1)"""
        self._build_three_cycles()

        result = get_cycle_history(self.db, 100000000)
        self.assertEqual(result["limit"], grid_service.MAX_HISTORY_LIMIT)
        self.assertEqual(result["count"], 3)

        result_min = get_cycle_history(self.db, 0)
        self.assertEqual(result_min["limit"], 1)
        self.assertEqual(result_min["count"], 1)


class TestGridDashboard(GridServiceTestBase):
    def _ndx_data(self, price=25000.0):
        return {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": price,
            "rsi": 30.0,
            "ma200": 19000.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "price_1y_ago_available": True,
            "data_timestamp": str(datetime.now(et_tz))
        }

    def test_24_dashboard_with_running_uses_actual_params(self):
        """24. 有 RUNNING 时数据正确; theoretical position 必须基于 actual 参数"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

        dash = get_grid_dashboard(self.db, self._ndx_data(25000.0))

        self.assertIsNotNone(dash["running_cycle"])
        self.assertEqual(dash["running_cycle"]["id"], self.cycle.id)
        self.assertEqual(dash["ndx"]["last_price"], 25000.0)
        self.assertEqual(dash["ndx"]["rsi"], 30.0)
        self.assertEqual(dash["ndx"]["ma200"], 19000.0)
        # rsi=30 < 35 且连续3天站上SMA200 且价格高于一年前 -> entry_signal=True
        self.assertTrue(dash["ndx"]["entry_signal"])

        pos = dash["theoretical_grid_position"]
        self.assertIsNotNone(pos)
        # 基于实际参数: lower=20100, upper=30200, count=180
        # step = 10100/180 = 56.111.., (25000-20100)/56.111 = 87.3 -> 第 88 格
        expected_interval = math.floor((25000.0 - 20100.0) / ((30200.0 - 20100.0) / 180)) + 1
        self.assertEqual(expected_interval, 88)
        self.assertEqual(pos["grid_interval_index"], expected_interval)
        self.assertEqual(pos["grid_count"], 180)  # 若误用 suggested 200 会得到 200
        self.assertEqual(pos["status"], "IN_RANGE")
        self.assertEqual(pos["distance_to_upper"], 5200.0)   # 30200 - 25000
        self.assertEqual(pos["distance_to_lower"], 4900.0)   # 25000 - 20100
        self.assertEqual(pos["basis"], "ACTUAL_PARAMS")

    def test_25_dashboard_no_running_nulls(self):
        """25. 无 RUNNING 时 theoretical position / running 为 null"""
        dash = get_grid_dashboard(self.db, self._ndx_data(25000.0))
        self.assertIsNone(dash["running_cycle"])
        self.assertIsNone(dash["theoretical_grid_position"])
        self.assertIsNotNone(dash["waiting_cycle"])  # setUp 创建了 WAITING

        # 连数据都没有时 ndx 全空
        dash_no_data = get_grid_dashboard(self.db, None)
        self.assertIsNone(dash_no_data["ndx"]["last_price"])
        self.assertIsNone(dash_no_data["ndx"]["entry_signal"])
        self.assertIsNone(dash_no_data["running_cycle"])
        self.assertIsNone(dash_no_data["theoretical_grid_position"])

    def test_26_dashboard_entry_signal_readonly(self):
        """26. entry signal 只读判定, 绝不创建 WAITING"""
        before = self.db.query(GridCycle).count()
        dash = get_grid_dashboard(self.db, self._ndx_data(20000.0))  # 满足全部开仓条件
        self.assertTrue(dash["ndx"]["entry_signal"])
        self.assertEqual(self.db.query(GridCycle).count(), before)


class TestGridApi(unittest.TestCase):
    """HTTP API 级测试 (TestClient, 覆盖认证与错误码映射)"""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()
        self.cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))

        from app.main import app
        from app.database.init_db import get_db

        app.dependency_overrides[get_db] = lambda: self.db
        self.app = app
        self.client = TestClient(app)
        self.client.cookies.set("admin_logged_in", "true")

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_api_401_without_cookie(self):
        """认证: 未认证访问 API -> 401"""
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/grid/running").status_code, 401)
        self.assertEqual(self.client.get("/api/grid/waiting").status_code, 401)
        self.assertEqual(self.client.get("/api/grid/status").status_code, 401)
        self.assertEqual(self.client.get("/api/grid/history").status_code, 401)
        self.assertEqual(
            self.client.post("/api/grid/1/start", json=dict(VALID_ACTUAL)).status_code,
            401
        )
        self.assertEqual(self.client.post("/api/grid/1/close").status_code, 401)

    def test_api_waiting_running_null(self):
        """WAITING/RUNNING 不存在时返回 null"""
        self.assertIsNone(self.client.get("/api/grid/running").json())
        self.assertIsNotNone(self.client.get("/api/grid/waiting").json())

        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        running = self.client.get("/api/grid/running").json()
        self.assertEqual(running["status"], "RUNNING")
        self.assertEqual(running["actual_grid_count"], 180)
        self.assertIsNone(self.client.get("/api/grid/waiting").json())

    def test_api_start_success(self):
        """POST start: 200 + RUNNING + actual/suggested 正确"""
        res = self.client.post(f"/api/grid/{self.cycle.id}/start", json=dict(VALID_ACTUAL))
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "RUNNING")
        self.assertEqual(body["actual_base_price"], 25150.0)
        self.assertEqual(body["actual_grid_count"], 180)
        self.assertEqual(body["suggested_base_price"], 25000.0)
        self.assertIsNotNone(body["started_at"])

    def test_api_start_validation_400(self):
        """POST start: 参数校验失败 -> 400"""
        bad_cases = [
            {"actual_base_price": 0},
            {"actual_upper_price": 25150.0},
            {"actual_lower_price": 25150.0},
            {"actual_lower_price": -5},
            {"actual_grid_count": 0},
            {"actual_leverage": 0},
            {"actual_margin": 0},
        ]
        for override in bad_cases:
            payload = dict(VALID_ACTUAL)
            payload.update(override)
            res = self.client.post(f"/api/grid/{self.cycle.id}/start", json=payload)
            self.assertEqual(res.status_code, 400, f"payload={override}")

        # 校验失败不得改变状态
        self.db.expire_all()
        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.id == self.cycle.id).first().status,
            "WAITING"
        )

    def test_api_start_missing_field_422(self):
        """POST start: 缺字段 -> 422"""
        payload = dict(VALID_ACTUAL)
        del payload["actual_margin"]
        res = self.client.post(f"/api/grid/{self.cycle.id}/start", json=payload)
        self.assertEqual(res.status_code, 422)

    def test_api_start_state_conflict_409(self):
        """POST start: 非 WAITING / 已有 RUNNING -> 409"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        res = self.client.post(f"/api/grid/{self.cycle.id}/start", json=dict(VALID_ACTUAL))
        self.assertEqual(res.status_code, 409)

        cycle_b = GridCycle(
            status="WAITING",
            suggested_base_price=25000.0,
            suggested_upper_price=30000.0,
            suggested_lower_price=20000.0,
            suggested_grid_count=200,
            suggested_leverage=5.0
        )
        self.db.add(cycle_b)
        self.db.commit()
        res_b = self.client.post(f"/api/grid/{cycle_b.id}/start", json=dict(VALID_ACTUAL))
        self.assertEqual(res_b.status_code, 409)

    def test_api_start_not_found_404(self):
        """POST start: 不存在的 cycle -> 404"""
        res = self.client.post("/api/grid/9999/start", json=dict(VALID_ACTUAL))
        self.assertEqual(res.status_code, 404)

    def test_api_close_flows(self):
        """POST close: RUNNING 200/MANUAL_CLOSE; WAITING/CLOSED 409; 未知 404"""
        res_wait = self.client.post(f"/api/grid/{self.cycle.id}/close")
        self.assertEqual(res_wait.status_code, 409)

        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        res_ok = self.client.post(f"/api/grid/{self.cycle.id}/close")
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json()["close_reason"], "MANUAL_CLOSE")
        self.assertEqual(res_ok.json()["status"], "CLOSED")

        res_again = self.client.post(f"/api/grid/{self.cycle.id}/close")
        self.assertEqual(res_again.status_code, 409)

        res_missing = self.client.post("/api/grid/9999/close")
        self.assertEqual(res_missing.status_code, 404)

    def test_api_history_endpoint(self):
        """GET history: limit 参数生效"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))
        close_running_cycle(self.db, self.cycle.id)
        c2 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))

        res = self.client.get("/api/grid/history", params={"limit": 1})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["cycles"][0]["id"], c2.id)

    def test_api_status_with_patched_fetcher(self):
        """GET status: 使用 mock data_fetcher, 有 RUNNING 时返回完整数据"""
        start_cycle(self.db, self.cycle.id, dict(VALID_ACTUAL))

        mock_fetcher = MagicMock()
        mock_fetcher.get_ndx_data.return_value = {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": 25000.0,
            "rsi": 30.0,
            "ma200": 19000.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "price_1y_ago_available": True,
            "data_timestamp": str(datetime.now(et_tz))
        }

        with patch("app.main.data_fetcher", mock_fetcher):
            res = self.client.get("/api/grid/status")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["ndx"]["last_price"], 25000.0)
        self.assertEqual(body["running_cycle"]["id"], self.cycle.id)
        pos = body["theoretical_grid_position"]
        self.assertEqual(pos["grid_count"], 180)  # 基于实际参数
        self.assertEqual(pos["distance_to_upper"], 5200.0)

        # dashboard 调用不得产生副作用 (不创建/不修改周期)
        self.db.expire_all()
        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.id == self.cycle.id).first().status,
            "RUNNING"
        )


if __name__ == "__main__":
    unittest.main()
