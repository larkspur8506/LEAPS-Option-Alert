"""
Phase 4B Tests: NDX Grid Web UI.

Covers:
- /admin/grid accessible with admin cookie (server-rendered data)
- /admin/positions redirects to /admin/grid
- WAITING rendering (suggested params, NO actual confusion)
- RUNNING rendering (actual params, frozen, theoretical position from actual params)
- No active grid: NO ACTIVE GRID + latest history shown
- History shows CLOSED / STOPPED with reasons
- Start/close buttons invoke correct Phase 4A API endpoints (via template markers
  + real API round-trip through the same routes the buttons call)
- API error prompts: 409 conflict message, 400/422 validation message (apiFetch mapping)
- Dashboard reflects NDX grid state
- Suggested vs actual params never mixed (post-start suggested intact on page data)
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pytz import timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, GridCycle
from app.alerts.grid_cycle import create_waiting_grid_cycle
from app.services import grid_service

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


def make_ndx_data(price=25000.0):
    return {
        "ticker": "^NDX",
        "is_data_valid": True,
        "last_price": price,
        "rsi": 30.0,
        "ma200": 19000.0,
        "is_above_sma200_3d": True,
        "price_1y_ago": 18000.0,
        "price_1y_ago_available": True,
        "data_timestamp": str(datetime.now(et_tz)),
    }


class GridWebUiTestBase(unittest.TestCase):
    """共享环境: 内存 DB + TestClient + admin cookie"""

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

        self.mock_fetcher = MagicMock()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data()

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _page(self, url):
        with patch("app.main.data_fetcher", self.mock_fetcher):
            return self.client.get(url)


class TestGridPages(GridWebUiTestBase):
    def test_1_admin_grid_accessible(self):
        """1. /admin/grid 可访问 (带认证), 返回 200 且含管理页面结构"""
        res = self._page("/admin/grid")
        self.assertEqual(res.status_code, 200)
        self.assertIn("NDX Grid 管理", res.text)

        # 未认证 -> 重定向登录页
        self.client.cookies.clear()
        res_no_auth = self.client.get("/admin/grid", follow_redirects=False)
        self.assertEqual(res_no_auth.status_code, 302)
        self.assertEqual(res_no_auth.headers["location"], "/admin/login")

    def test_2_positions_redirects_to_grid(self):
        """2. /admin/positions 正确重定向到 /admin/grid"""
        res = self._page("/admin/positions")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/admin/grid")

    def test_3_waiting_rendered_with_suggested_only(self):
        """3. WAITING 正确显示: suggested 参数可见, actual 参数不出现"""
        create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        res = self._page("/admin/grid")

        self.assertEqual(res.status_code, 200)
        self.assertIn("WAITING — ENTRY SIGNAL DETECTED", res.text)
        self.assertIn("25,000.00".replace(",", ""), res.text)  # 25000.00
        self.assertIn("30000.00", res.text)
        self.assertIn("20000.00", res.text)
        self.assertIn("确认启动 Grid", res.text)
        self.assertIn("50.00", res.text)  # grid step (30000-20000)/200

        # RUNNING 卡片与实际参数文案不得出现
        self.assertNotIn("MANUAL GRID ACTIVE", res.text)
        self.assertNotIn("Actual Base</h3>", res.text)

    def test_4_running_rendered_with_actual_params(self):
        """4. RUNNING 正确显示: actual 参数 + 理论状态 (基于实际参数)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        res = self._page("/admin/grid")

        self.assertEqual(res.status_code, 200)
        self.assertIn("RUNNING — MANUAL GRID ACTIVE", res.text)
        self.assertIn("25150.00", res.text)
        self.assertIn("30200.00", res.text)
        self.assertIn("20100.00", res.text)
        self.assertIn("手动关闭 Grid", res.text)
        self.assertIn("理论网格状态", res.text)
        self.assertIn("不代表交易所实际持仓或实际盈亏", res.text)

        # 理论格位基于实际参数: (25000-20100)/((30200-20100)/180)+1 = 88
        self.assertIn("88 / 180", res.text)
        # 不得出现假的真实交易数据
        self.assertNotIn("Liquidation", res.text)
        self.assertNotIn("Funding", res.text)

    def test_5_history_shows_closed_and_stopped(self):
        """5. CLOSED / STOPPED 历史记录正确显示 (状态徽章 + close reason)"""
        from app.alerts.grid_cycle import stop_grid_cycle

        c1 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, c1.id, dict(VALID_ACTUAL))
        grid_service.close_running_cycle(self.db, c1.id)

        c2 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, c2.id, dict(VALID_ACTUAL))
        stop_grid_cycle(self.db, c2.id, reason="LOWER_BREACHED")

        res = self._page("/admin/grid")
        self.assertEqual(res.status_code, 200)
        self.assertIn("CLOSED", res.text)
        self.assertIn("STOPPED", res.text)
        self.assertIn("MANUAL_CLOSE", res.text)
        self.assertIn("LOWER_BREACHED", res.text)
        self.assertIn("Grid 历史记录", res.text)

    def test_5b_waiting_not_shown_as_running(self):
        """5b. 无活动 Grid 时不得出现误导性运行中信息"""
        # 无任何周期
        res = self._page("/admin/grid")
        self.assertIn("NO ACTIVE GRID", res.text)
        self.assertNotIn("MANUAL GRID ACTIVE", res.text)
        self.assertNotIn("ENTRY SIGNAL DETECTED", res.text)

        # 有 CLOSED 历史时, 空状态应显示最近一次记录
        c1 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, c1.id, dict(VALID_ACTUAL))
        grid_service.close_running_cycle(self.db, c1.id)

        res2 = self._page("/admin/grid")
        self.assertIn("NO ACTIVE GRID", res2.text)
        self.assertIn("最近一次记录", res2.text)

    def test_5c_no_active_grid_explanation_text(self):
        """5c. 无活动 Grid 时根据数据状态显示三类精细化解释说明, 且无手动创建按钮"""
        # Case A: Stale data (is_data_fresh=False)
        stale_data = make_ndx_data()
        stale_data["is_data_fresh"] = False
        with patch("app.main.data_fetcher") as mock_f:
            mock_f.get_ndx_data.return_value = stale_data
            res_stale = self.client.get("/admin/grid")
        self.assertIn("NDX 当前数据不是最新交易日数据，暂不评估 Entry Signal", res_stale.text)
        self.assertNotIn("手动创建 Grid", res_stale.text)

        # Case B: Fresh data + Entry Signal not met
        fresh_data = make_ndx_data()
        fresh_data["is_data_fresh"] = True
        fresh_data["rsi"] = 50.0  # > 35, not met
        with patch("app.main.data_fetcher") as mock_f:
            mock_f.get_ndx_data.return_value = fresh_data
            res_fresh = self.client.get("/admin/grid")
        self.assertIn("当前 NDX Entry Signal 未满足，因此系统尚未创建 WAITING 周期", res_fresh.text)
        self.assertIn("届时这里会出现“确认启动 Grid (录入实际参数)”按钮", res_fresh.text)

        # Case C: Data unavailable
        with patch("app.main.data_fetcher") as mock_f:
            mock_f.get_ndx_data.return_value = None
            res_unavail = self.client.get("/admin/grid")
        self.assertIn("当前 NDX 数据不可用，暂时无法评估 Entry Signal", res_unavail.text)

    def test_5d_start_modal_margin_placeholder_and_validation(self):
        """5d. Start modal 中 sm-margin 包含 placeholder='请输入实际投入保证金', 且包含严格前端校验 JS 规则"""
        c1 = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        res = self._page("/admin/grid")
        html = res.text

        self.assertIn('id="sm-margin"', html)
        self.assertIn('placeholder="请输入实际投入保证金"', html)
        self.assertIn('value=""', html)

        # 前端 JS 校验规则检查
        self.assertIn('Actual Margin 保证金', html)
        self.assertIn('/^\\d+$/.test(rawCount)', html)
        self.assertIn('Actual Grid Count 必须为大于等于 1 的整数', html)
        self.assertIn('Actual Upper Price 必须大于 Actual Base Price', html)


class TestGridPageActions(GridWebUiTestBase):
    def test_6_start_button_calls_correct_api(self):
        """6. 启动按钮调用正确的 API 端点与请求体 (模板 marker + 真实 API 往返)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        res = self._page("/admin/grid")
        html = res.text

        # 模板生成的启动按钮必须指向 Phase 4A start API
        # (JS 模板字符串与 onclick 参数均可静态验证)
        self.assertIn(f"submitStart({cycle.id})", html)
        self.assertIn("/api/grid/${cycleId}/start", html)
        self.assertIn("method: \"POST\"", html)

        # 模拟按钮触发的真实调用: 与 grid.html submitStart 完全相同的请求
        api_res = self.client.post(f"/api/grid/{cycle.id}/start", json=dict(VALID_ACTUAL))
        self.assertEqual(api_res.status_code, 200)
        self.assertEqual(api_res.json()["status"], "RUNNING")
        self.assertEqual(api_res.json()["actual_grid_count"], 180)

        # 页面数据: actual 已录入, suggested 未变 (无混淆)
        res2 = self._page("/admin/grid")
        self.assertIn("25150.00", res2.text)
        self.assertIn("RUNNING — MANUAL GRID ACTIVE", res2.text)

    def test_7_close_button_calls_correct_api(self):
        """7. 关闭按钮调用正确的 API 端点"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))

        res = self._page("/admin/grid")
        self.assertIn(f"submitClose({cycle.id})", res.text)

        # 与 grid.html submitClose 完全相同的调用
        api_res = self.client.post(f"/api/grid/{cycle.id}/close")
        self.assertEqual(api_res.status_code, 200)
        body = api_res.json()
        self.assertEqual(body["status"], "CLOSED")
        self.assertEqual(body["close_reason"], "MANUAL_CLOSE")

    def test_8_api_409_prompts_conflict_message(self):
        """8. API 409 -> 前端映射为状态冲突提示 (验证 apiFetch 分支对应的响应体)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))

        res = self.client.post(f"/api/grid/{cycle.id}/start", json=dict(VALID_ACTUAL))
        self.assertEqual(res.status_code, 409)
        detail = res.json()["detail"]
        # grid.html 中 409 分支直接显示 body.detail
        self.assertIn("must be 'WAITING'", detail)

    def test_9_api_400_422_prompts_validation_message(self):
        """9. API 400/422 -> 前端显示后端参数错误 (验证两个分支响应体)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))

        # 400: 服务层参数校验
        res400 = self.client.post(f"/api/grid/{cycle.id}/start", json={
            **dict(VALID_ACTUAL), "actual_base_price": 0
        })
        self.assertEqual(res400.status_code, 400)
        self.assertIn("actual_base_price", res400.json()["detail"])

        # 422: pydantic 缺字段
        payload = dict(VALID_ACTUAL)
        del payload["actual_margin"]
        res422 = self.client.post(f"/api/grid/{cycle.id}/start", json=payload)
        self.assertEqual(res422.status_code, 422)
        errors = res422.json()["detail"]
        self.assertTrue(any(e["loc"][-1] == "actual_margin" for e in errors))


class TestDashboardPage(GridWebUiTestBase):
    def test_10_no_active_grid_dashboard(self):
        """10. 无活动 Grid 时 dashboard 显示 NO ACTIVE GRID + 最近历史"""
        res = self._page("/admin")
        self.assertEqual(res.status_code, 200)
        self.assertIn("NDX Grid Dashboard", res.text)
        self.assertIn("NO ACTIVE GRID", res.text)
        self.assertNotIn("MANUAL GRID ACTIVE", res.text)

    def test_10b_dashboard_waiting_and_running(self):
        """10b. dashboard 在 WAITING/RUNNING 状态下正确显示"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        res_wait = self._page("/admin")
        self.assertIn("WAITING — ENTRY SIGNAL DETECTED", res_wait.text)

        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))
        res_run = self._page("/admin")
        self.assertIn("RUNNING — MANUAL GRID ACTIVE", res_run.text)
        self.assertIn("25150.00", res_run.text)
        # WAITING 横幅不再出现
        self.assertNotIn("ENTRY SIGNAL DETECTED", res_run.text)

    def test_11_suggested_and_actual_not_mixed(self):
        """11. Actual 与 Suggested 参数视觉与数据分离:
        启动后页面数据中 suggested 保持系统建议值, actual 为用户输入值"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))

        res = self._page("/admin/grid")
        html = res.text

        # actual 值存在 (用户输入 25150/30200/20100/180/4x)
        self.assertIn("25150.00", html)
        self.assertIn("30200.00", html)
        self.assertIn("20100.00", html)
        self.assertIn("180", html)
        self.assertIn("4.0x", html)
        self.assertIn("1000.00", html)

        # RUNNING 视图基于 actual; 建议值不得出现在实际参数区域。
        # (历史表格按规格只显示建议 Base 列, 因此 RUNNING 页面上
        #  suggested_upper/lower 不以两位小数格式出现, 嵌入 JSON 中为原始值)
        actual_section = html.split("实际 Grid 参数 (已冻结)")[1].split("理论网格状态")[0]
        self.assertIn("25150.00", actual_section)
        self.assertNotIn("25000.00", actual_section)
        self.assertNotIn("30000.00", actual_section)
        self.assertNotIn("20000.00", actual_section)
        self.assertNotIn("5.0x", actual_section)

        # 历史表格中 suggested 列仍保留系统建议值 (数据分离的证明)
        self.assertIn("25000.00", html)

        # 启动弹窗中推荐参数与实际参数分区明确
        self.assertIn("推荐参数 (系统建议, 只读)", html)
        self.assertIn("实际启动参数 (以交易所实际网格为准", html)

    def test_11b_dashboard_price_precision_and_leverage_format(self):
        """11b. 价格 2 位小数 / Leverage x.x 格式 / 格数整数"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        grid_service.start_cycle(self.db, cycle.id, dict(VALID_ACTUAL))

        res = self._page("/admin")
        self.assertIn("25000.00", res.text)   # NDX 现价 2 位小数
        self.assertIn("30.00", res.text)      # RSI 2 位小数
        self.assertIn("4.0x", res.text)       # leverage 格式
        self.assertNotIn("4.00x", res.text)   # 不得出现两位小数杠杆


if __name__ == "__main__":
    unittest.main()
