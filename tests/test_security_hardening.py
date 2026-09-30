"""
P0/P1 加固与策略修正的回归测试。

覆盖:
- 管理员会话: 伪造明文 Cookie 失效 / 签名会话有效 / 登出 / 登录限速
- /setup 初始化入口: 仅首次 + SETUP_TOKEN 或回环访问
- 口令散列: scrypt 新格式 + 旧 sha256 兼容
- WAITING 生命周期: TTL(交易日) 自动过期 -> EXPIRED, 可被人工忽略 -> DISMISSED
- 开仓判定基准: 收盘确认制 (closed_* 优先, 盘中噪声不触发/不解除)
- 风险/成本速算: 0 手续费平台下格距与资金费口径
- AlertLog 统一写入层: 今日计数与落库级去重
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pytz import timezone as pytz_timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, AlertLog, GridCycle
from app.alerts.grid_cycle import (
    create_waiting_grid_cycle,
    get_waiting_grid_cycle,
    dismiss_waiting_grid_cycle,
    expire_waiting_grid_cycle,
    waiting_ttl_state,
)
from app.alerts import dedup as dedup_mod
from app.alerts.ndx_rules import check_ndx_entry_conditions, check_entry_signals, _entry_reference
from app.alerts.alert_log import log_alert, count_alerts_today, alerted_within, utcnow_naive
from app.admin import auth as auth_mod
from app.admin import security as security_mod
from app.admin.security import SESSION_COOKIE_NAME, create_session_token, reset_login_limits
from app.config import Config, get_config
from app.services import grid_service
from tests.support import login_client, admin_session_token, set_setup_token

et_tz = pytz_timezone("America/New_York")

SUGGESTED = {
    "base_price": 25000.0,
    "upper_price": 30000.0,
    "lower_price": 20000.0,
    "grid_count": 200,
    "leverage": 5.0,
}

PASSWORD = "s3cret-pass"


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()
        reset_login_limits()
        dedup_mod.clear_dedup()

    def tearDown(self):
        set_setup_token("")
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        reset_login_limits()


class WebTestBase(BaseTest):
    def setUp(self):
        super().setUp()
        from app.main import app
        from app.database.init_db import get_db

        app.dependency_overrides[get_db] = lambda: self.db
        self.app = app
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        super().tearDown()

    def _set_admin_password(self, password: str = PASSWORD):
        auth_mod.set_admin_password(self.db, password)


# ---------------------------------------------------------------------------
# 1. 管理员会话
# ---------------------------------------------------------------------------

class TestAdminSessionHardening(WebTestBase):
    def test_1_forged_static_cookie_is_rejected(self):
        """1. 历史伪造方式 Cookie: admin_logged_in=true 不再授予访问权"""
        self._set_admin_password()
        self.client.cookies.set("admin_logged_in", "true")
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/login", res.headers.get("location", ""))

    def test_2_unsigned_or_tampered_session_cookie_rejected(self):
        """2. 未签名 / 被篡改 / 其他密钥签发的会话 Cookie 一律拒绝"""
        self._set_admin_password()

        self.client.cookies.set(SESSION_COOKIE_NAME, "admin")
        self.assertEqual(self.client.get("/admin").status_code, 302)

        token = create_session_token({"admin": True})
        self.client.cookies.set(SESSION_COOKIE_NAME, token[:-3] + "abc")
        self.assertEqual(self.client.get("/admin").status_code, 302)

        rogue = security_mod.URLSafeTimedSerializer("another-secret", salt=security_mod.SESSION_SALT)
        self.client.cookies.set(SESSION_COOKIE_NAME, rogue.dumps({"admin": True}))
        self.assertEqual(self.client.get("/admin").status_code, 302)

        # 载荷存在但 admin 不是 True -> 拒绝
        self.client.cookies.set(SESSION_COOKIE_NAME, create_session_token({"admin": 1}))
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_3_valid_signed_session_grants_access(self):
        """3. 真实登录路径: 登录 -> /admin 200 -> 登出 -> 立即失效"""
        self._set_admin_password()

        res = self.client.post("/admin/login", data={"password": PASSWORD})
        self.assertEqual(res.status_code, 303)
        self.assertEqual(self.client.get("/admin").status_code, 200)

        res = self.client.get("/admin/logout")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_3b_direct_token_cookie_also_works(self):
        """3b. 携带合法签名 token 的调用方 (API/脚本) 同样通过; 登出后 token 失效"""
        self._set_admin_password()
        login_client(self.client)
        self.assertEqual(self.client.get("/admin").status_code, 200)

        self.client.get("/admin/logout")          # 服务端清除会话
        self.client.cookies.clear()               # 浏览器会删掉被清除的 Cookie
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_4_login_sets_signed_cookie_not_plaintext(self):
        """4. 登录成功下发签名 Cookie (不再是明文 admin_logged_in)"""
        self._set_admin_password()
        res = self.client.post("/admin/login", data={"password": PASSWORD})
        self.assertEqual(res.status_code, 303)
        set_cookie = res.headers.get("set-cookie", "")
        self.assertIn(SESSION_COOKIE_NAME, set_cookie)
        self.assertNotIn("admin_logged_in", set_cookie)
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_5_login_failure_and_rate_limit(self):
        """5. 口令错误 401; 连续失败触发 429 冷却 (防爆破)"""
        self._set_admin_password()
        for _ in range(security_mod.LOGIN_MAX_FAILURES):
            res = self.client.post("/admin/login", data={"password": "wrong"})
            self.assertEqual(res.status_code, 401)

        res = self.client.post("/admin/login", data={"password": PASSWORD})
        self.assertEqual(res.status_code, 429)
        self.assertIn("尝试次数过多", res.text)

    def test_6_grid_api_requires_session(self):
        """6. /api/grid 接口同样受签名会话保护"""
        self._set_admin_password()
        self.client.cookies.set("admin_logged_in", "true")
        self.assertEqual(self.client.get("/api/grid/status").status_code, 401)
        login_client(self.client)
        res = self.client.get("/api/grid/status")
        self.assertNotEqual(res.status_code, 401)


# ---------------------------------------------------------------------------
# 2. /setup 初始化入口
# ---------------------------------------------------------------------------

class TestSetupEndpointHardening(WebTestBase):
    def test_1_setup_closed_after_password_set(self):
        """1. 已设置密码后 /setup 彻底关闭 (GET/POST 均重定向登录)"""
        self._set_admin_password()
        self.assertEqual(self.client.get("/setup").status_code, 302)
        res = self.client.post("/setup", data={"password": "new-pass", "setup_token": ""})
        self.assertEqual(res.status_code, 302)
        # 原密码仍然有效, 说明未被覆盖
        self.assertTrue(auth_mod.verify_admin_password(PASSWORD, self.db))
        self.assertFalse(auth_mod.verify_admin_password("new-pass", self.db))

    def test_2_setup_requires_matching_token(self):
        """2. 配置了 SETUP_TOKEN 时: 缺失/错误 403, 正确才放行"""
        set_setup_token("open-sesame")
        self.assertEqual(self.client.get("/setup").status_code, 403)
        self.assertEqual(self.client.get("/setup?token=nope").status_code, 403)
        res = self.client.get("/setup?token=open-sesame")
        self.assertEqual(res.status_code, 200)

        bad = self.client.post("/setup", data={"password": "abcdef", "setup_token": "nope"})
        self.assertEqual(bad.status_code, 403)
        self.assertFalse(auth_mod.is_first_time_setup(self.db) is False)

        ok = self.client.post("/setup", data={"password": "abcdef", "setup_token": "open-sesame"})
        self.assertEqual(ok.status_code, 302)
        self.assertFalse(auth_mod.is_first_time_setup(self.db))
        self.assertTrue(auth_mod.verify_admin_password("abcdef", self.db))

    def test_3_setup_denied_for_non_loopback_without_token(self):
        """3. 未配置 SETUP_TOKEN 时仅允许本机回环, 公网来源 403 (fail-closed)"""
        set_setup_token("")
        res = self.client.get("/setup")
        self.assertEqual(res.status_code, 403)
        self.assertIn("SETUP_TOKEN", res.text)

    def test_4_short_password_rejected(self):
        """4. 口令长度下限仍生效"""
        set_setup_token("open-sesame")
        res = self.client.post("/setup", data={"password": "123", "setup_token": "open-sesame"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("至少为 6 位", res.text)
        self.assertTrue(auth_mod.is_first_time_setup(self.db))


# ---------------------------------------------------------------------------
# 3. 口令散列
# ---------------------------------------------------------------------------

class TestPasswordHashing(BaseTest):
    def test_1_scrypt_format_and_verify(self):
        """1. 新口令使用 scrypt 加盐散列, 同口令两次散列不同但都能校验"""
        h1 = auth_mod.get_password_hash(PASSWORD)
        h2 = auth_mod.get_password_hash(PASSWORD)
        self.assertTrue(h1.startswith("scrypt$"))
        self.assertNotEqual(h1, h2)
        self.assertTrue(auth_mod.verify_password(PASSWORD, h1))
        self.assertFalse(auth_mod.verify_password(PASSWORD + "x", h1))

    def test_2_legacy_sha256_still_verifiable(self):
        """2. 旧版无盐 sha256(hex) 记录仍可校验 (不锁死存量部署)"""
        import hashlib

        legacy = hashlib.sha256(PASSWORD.encode("utf-8")).hexdigest()
        self.assertEqual(len(legacy), 64)
        self.assertTrue(auth_mod.verify_password(PASSWORD, legacy))
        self.assertFalse(auth_mod.verify_password("nope", legacy))

    def test_3_set_admin_password_persists_and_verifies(self):
        """3. set_admin_password 落库后可用 DB 校验"""
        auth_mod.set_admin_password(self.db, PASSWORD)
        self.assertFalse(auth_mod.is_first_time_setup(self.db))
        self.assertTrue(auth_mod.verify_admin_password(PASSWORD, self.db))
        self.assertFalse(auth_mod.verify_admin_password("nope", self.db))

    def test_4_empty_or_missing_hash_is_safe(self):
        """4. 空/缺失散列不崩溃且不通过校验"""
        self.assertFalse(auth_mod.verify_password(PASSWORD, ""))
        self.assertFalse(auth_mod.verify_password(PASSWORD, None))
        self.assertTrue(auth_mod.is_first_time_setup(self.db))


# ---------------------------------------------------------------------------
# 4. WAITING 生命周期 (TTL / DISMISS)
# ---------------------------------------------------------------------------

class TestWaitingLifecycle(BaseTest):
    def test_1_ttl_state_counts_trading_days(self):
        """1. TTL 按交易日计: 自然日跨周末时仍在期内"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        cycle.created_at = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)
        self.db.commit()

        state = waiting_ttl_state(cycle, 3)
        self.assertTrue(state["ttl_enabled"])
        self.assertGreaterEqual(state["elapsed_trading_days"], 3)
        self.assertTrue(state["expired"])
        self.assertEqual(state["remaining_trading_days"], 0)

        # TTL=0 (关闭) -> 永不过期
        disabled = waiting_ttl_state(cycle, 0)
        self.assertFalse(disabled["ttl_enabled"])
        self.assertFalse(disabled["expired"])

    def test_2_expire_and_dismiss_are_terminal(self):
        """2. EXPIRED / DISMISSED 均为终态, 不可重复操作, 且不再占用 WAITING 槽位"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        expire_waiting_grid_cycle(self.db, cycle.id, notes="ttl")
        self.assertIsNone(get_waiting_grid_cycle(self.db))

        self.db.expire_all()
        stored = self.db.query(GridCycle).filter(GridCycle.id == cycle.id).first()
        self.assertEqual(stored.status, "EXPIRED")

        with self.assertRaises(ValueError):
            expire_waiting_grid_cycle(self.db, cycle.id)
        with self.assertRaises(ValueError):
            dismiss_waiting_grid_cycle(self.db, cycle.id)

        # 释放槽位后可以创建新的 WAITING
        again = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        self.assertIsNotNone(get_waiting_grid_cycle(self.db))
        self.assertNotEqual(again.id, cycle.id)

    def test_3_dismiss_service_and_api(self):
        """3. 人工忽略: 服务层 DISMISSED; API 404/409/401 语义正确"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))

        from app.main import app
        from app.database.init_db import get_db

        app.dependency_overrides[get_db] = lambda: self.db
        client = TestClient(app, follow_redirects=False)
        try:
            self.assertEqual(client.post(f"/api/grid/{cycle.id}/dismiss").status_code, 401)

            login_client(client)
            res = client.post(f"/api/grid/{cycle.id}/dismiss")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "DISMISSED")

            # 重复忽略 -> 409 (非 WAITING)
            self.assertEqual(client.post(f"/api/grid/{cycle.id}/dismiss").status_code, 409)
            # 不存在 -> 404
            self.assertEqual(client.post("/api/grid/999999/dismiss").status_code, 404)
        finally:
            client.close()
            app.dependency_overrides.clear()

    def test_4_waiting_ttl_api_and_dashboard(self):
        """4. /api/grid/waiting 与 dashboard 暴露 TTL 状态"""
        create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        from app.main import app
        from app.database.init_db import get_db

        app.dependency_overrides[get_db] = lambda: self.db
        client = TestClient(app, follow_redirects=False)
        try:
            login_client(client)
            payload = client.get("/api/grid/waiting").json()
            self.assertIn("ttl", payload)
            self.assertIn("ttl_enabled", payload["ttl"])
        finally:
            client.close()
            app.dependency_overrides.clear()

        dash = grid_service.get_grid_dashboard(self.db, None)
        self.assertIn("waiting_ttl", dash)
        self.assertIn("risk_snapshot", dash)

    def test_5_monitor_expires_waiting_and_reevaluates(self):
        """5. 监控任务: WAITING 超过 TTL -> 自动 EXPIRED 并同一轮重新评估信号"""
        from app.alerts.grid_monitor import process_ndx_grid_cycle

        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        cycle.created_at = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)
        self.db.commit()

        config = Config({})
        indicators = {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": 25000.0,
            "rsi": 30.0,
            "ma200": 19000.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "data_timestamp": str(datetime.now(et_tz)),
            "closed_bar_date": str(datetime.now(et_tz).date()),
            "closed_price": 24900.0,
            "closed_rsi": 30.0,
            "closed_ma200": 19000.0,
            "closed_is_above_sma200_3d": True,
            "closed_price_1y_ago": 18000.0,
            "closed_indicators_available": True,
            "live_bar_is_provisional": False,
        }

        with patch("app.alerts.grid_monitor._is_data_fresh", return_value=True):
            result = process_ndx_grid_cycle(self.db, indicators, config=config, notifier=None)

        self.assertNotEqual(result.get("status"), "WAITING_EXISTS")
        self.db.expire_all()
        old = self.db.query(GridCycle).filter(GridCycle.id == cycle.id).first()
        self.assertEqual(old.status, "EXPIRED")
        # 同轮内已重新评估 -> 新 WAITING 建立 (30 天前创建, 说明未被永久阻塞)
        new_waiting = get_waiting_grid_cycle(self.db)
        self.assertIsNotNone(new_waiting)
        self.assertNotEqual(new_waiting.id, cycle.id)

    def test_6_monitor_keeps_recent_waiting(self):
        """6. 未超过 TTL 的 WAITING 不被过期 (行为向后兼容)"""
        from app.alerts.grid_monitor import process_ndx_grid_cycle

        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        config = Config({})
        indicators = {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": 25000.0,
            "rsi": 30.0,
            "ma200": 19000.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "data_timestamp": str(datetime.now(et_tz)),
        }
        with patch("app.alerts.grid_monitor._is_data_fresh", return_value=True):
            result = process_ndx_grid_cycle(self.db, indicators, config=config, notifier=None)

        self.assertIn(result.get("status"), ("WAITING_EXISTS", "WAITING_TTL_PENDING"))
        self.db.expire_all()
        stored = self.db.query(GridCycle).filter(GridCycle.id == cycle.id).first()
        self.assertEqual(stored.status, "WAITING")


# ---------------------------------------------------------------------------
# 5. 开仓判定基准 (收盘确认制)
# ---------------------------------------------------------------------------

class TestClosedBarEntryBasis(unittest.TestCase):
    def _indicators(self, **overrides):
        data = {
            "is_data_valid": True,
            "last_price": 25000.0,
            "rsi": 30.0,
            "ma200": 19000.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "closed_indicators_available": True,
            "closed_bar_date": "2026-09-29",
            "closed_price": 24900.0,
            "closed_rsi": 30.0,
            "closed_ma200": 19000.0,
            "closed_is_above_sma200_3d": True,
            "closed_price_1y_ago": 18000.0,
        }
        data.update(overrides)
        return data

    def test_1_reference_prefers_closed_bar(self):
        """1. 参考基准优先取已收盘 bar"""
        ref = _entry_reference(25000.0, self._indicators())
        self.assertEqual(ref["basis"], "CLOSED_BAR")
        self.assertEqual(ref["price"], 24900.0)

    def test_2_live_noise_does_not_trigger_entry(self):
        """2. 盘中 RSI 短暂破位但收盘未破 -> 不产生开仓信号 (修掉噪声周期)"""
        indicators = self._indicators(rsi=20.0, is_above_sma200_3d=False, closed_rsi=40.0)
        self.assertFalse(check_ndx_entry_conditions(25000.0, indicators, 35.0))
        self.assertEqual(check_entry_signals(25000.0, indicators), [])

    def test_3_closed_bar_conditions_trigger_entry(self):
        """3. 收盘基准满足三条件 -> 触发信号, 并标注基准来源"""
        signals = check_entry_signals(25000.0, self._indicators())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["entry_basis"], "CLOSED_BAR")
        self.assertEqual(signals[0]["reference_price"], 24900.0)

    def test_4_falls_back_to_live_bar_without_closed_fields(self):
        """4. 无 closed_* 字段 (旧数据/回测) 时退回实时基准, 行为向后兼容"""
        indicators = self._indicators(closed_indicators_available=False)
        ref = _entry_reference(25000.0, indicators)
        self.assertEqual(ref["basis"], "LIVE_BAR")
        self.assertEqual(ref["price"], 25000.0)
        self.assertTrue(check_ndx_entry_conditions(25000.0, indicators, 35.0))

    def test_5_closed_bar_must_be_above_sma200_and_1y_ago(self):
        """5. 收盘基准本身必须站上 SMA200 且高于 1 年前"""
        self.assertFalse(check_ndx_entry_conditions(
            25000.0, self._indicators(closed_is_above_sma200_3d=False), 35.0))
        self.assertFalse(check_ndx_entry_conditions(
            25000.0, self._indicators(closed_price_1y_ago=26000.0), 35.0))


# ---------------------------------------------------------------------------
# 6. 风险/成本速算 (0 手续费平台)
# ---------------------------------------------------------------------------

class TestRiskSnapshot(unittest.TestCase):
    def test_1_zero_fee_keeps_full_grid_edge(self):
        """1. 0 手续费 (MEXC): 每格净毛利 == 格距, 手续费占比 0"""
        config = Config({})
        snap = grid_service.get_risk_snapshot(config, ndx_data={"last_price": 25000.0})
        self.assertAlmostEqual(snap["fee_pct_per_side"], 0.0)
        self.assertAlmostEqual(snap["round_trip_fee_pct"], 0.0)
        self.assertAlmostEqual(snap["net_edge_pct_per_grid"], snap["grid_step_pct"])
        self.assertAlmostEqual(snap["fee_ratio_of_step_pct"], 0.0)

    def test_2_fees_can_consume_the_whole_edge(self):
        """2. 线上配置 (10 倍杠杆区间 40%/200 格 -> 0.2% 格距) 下 0.1% 往返手续费占半;
        而 ±15%/300 格 (格距 0.1%) 会被 0.1% 往返手续费吃光 (正是线上经济性问题)"""
        with patch.dict("os.environ", {"GRID_TAKER_FEE_PCT": "0.05", "GRID_MAKER_FEE_PCT": "0.05"}):
            snap = grid_service.get_risk_snapshot(get_config(), ndx_data={"last_price": 25000.0})
        self.assertAlmostEqual(snap["grid_step_pct"], 0.2, places=3)
        self.assertAlmostEqual(snap["round_trip_fee_pct"], 0.1, places=3)
        self.assertAlmostEqual(snap["fee_ratio_of_step_pct"], 50.0, places=1)
        self.assertAlmostEqual(snap["net_edge_pct_per_grid"], 0.1, places=3)

        # 线上 ±15% / 300 格: 格距 0.1% == 往返手续费 -> 每格毛利归零
        with patch.dict("os.environ", {
            "GRID_TAKER_FEE_PCT": "0.05", "GRID_MAKER_FEE_PCT": "0.05",
            "DEFAULT_GRID_UPPER_PCT": "0.15", "DEFAULT_GRID_LOWER_PCT": "0.15",
            "DEFAULT_GRID_COUNT": "300", "DEFAULT_GRID_LEVERAGE": "3",
        }):
            snap = grid_service.get_risk_snapshot(get_config(), ndx_data={"last_price": 25000.0})
        self.assertAlmostEqual(snap["grid_step_pct"], 0.1, places=3)
        self.assertAlmostEqual(snap["fee_ratio_of_step_pct"], 100.0, places=1)
        self.assertAlmostEqual(snap["net_edge_pct_per_grid"], 0.0, places=3)

        # 0 手续费 (MEXC) 下同一配置: 手续费占比 0, 净毛利 == 格距
        with patch.dict("os.environ", {
            "DEFAULT_GRID_UPPER_PCT": "0.15", "DEFAULT_GRID_LOWER_PCT": "0.15",
            "DEFAULT_GRID_COUNT": "300", "DEFAULT_GRID_LEVERAGE": "3",
        }):
            zero_fee = grid_service.get_risk_snapshot(get_config(), ndx_data={"last_price": 25000.0})
        self.assertAlmostEqual(zero_fee["fee_ratio_of_step_pct"], 0.0)
        self.assertAlmostEqual(zero_fee["net_edge_pct_per_grid"], zero_fee["grid_step_pct"], places=4)

    def test_3_margin_and_liquidation_estimate(self):
        """3. 满仓浮亏按杠杆折算, 并给出估算强平价与距离"""
        config = Config({})
        with patch.dict("os.environ",
                        {"DEFAULT_GRID_LEVERAGE": "3", "DEFAULT_GRID_UPPER_PCT": "0.15",
                         "DEFAULT_GRID_LOWER_PCT": "0.15", "DEFAULT_GRID_COUNT": "300"}):
            snap = grid_service.get_risk_snapshot(get_config(), ndx_data={"last_price": 25000.0})

        self.assertAlmostEqual(snap["leverage"], 3.0)
        self.assertAlmostEqual(snap["avg_entry_at_full_load"], 23125.0, places=2)
        loss = snap["loss_at_lower_pct_of_notional"]
        self.assertAlmostEqual(loss, -8.11, delta=0.05)
        self.assertAlmostEqual(snap["margin_loss_at_lower_pct"], loss * 3.0, delta=0.05)
        self.assertLess(snap["est_liquidation_price"], snap["lower_price"])
        self.assertLess(snap["distance_to_liquidation_pct"], snap["lower_vs_liquidation_pct"])

    def test_4_funding_cost_per_day(self):
        """4. 资金费按 3 次/天折算为日拖累与 30 天拖累"""
        with patch.dict("os.environ", {"FUNDING_RATE_PCT_8H": "0.01"}):
            snap = grid_service.get_risk_snapshot(get_config(), ndx_data={"last_price": 25000.0})
        self.assertAlmostEqual(snap["funding_cost_pct_per_day"], 0.03, places=4)
        self.assertAlmostEqual(snap["funding_cost_pct_30d"], 0.9, places=3)

    def test_5_uses_actual_params_for_running_cycle(self):
        """5. RUNNING 周期按实际参数速算 (而非默认参数)"""
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            cycle = create_waiting_grid_cycle(db, dict(SUGGESTED))
            grid_service.start_cycle(db, cycle.id, {
                "actual_base_price": 25000.0, "actual_upper_price": 30000.0,
                "actual_lower_price": 20000.0, "actual_grid_count": 100,
                "actual_leverage": 2.0, "actual_margin": 500.0,
            })
            snap = grid_service.get_risk_snapshot(Config({}), cycle=cycle, ndx_data=None)
            self.assertEqual(snap["basis"], "ACTUAL_PARAMS")
            # 按实际参数 (100 格 / 2 倍杠杆) 速算, 而非建议参数
            self.assertEqual(snap["grid_count"], 100)
            self.assertAlmostEqual(snap["leverage"], 2.0)
            self.assertAlmostEqual(snap["grid_step_pct"], 0.4, places=3)
        finally:
            db.close()
            Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# 7. AlertLog 统一写入层
# ---------------------------------------------------------------------------

class TestAlertLogLayer(BaseTest):
    def test_1_daily_report_dedup_across_restart(self):
        """1. 落库级去重: 已写过当天日报则重启后不再重复 (内存 dedup 丢失也有效)"""
        log_alert(self.db, "NDX_GRID_DAILY_REPORT", "NDX Grid Daily Report", "hello\nworld", True)
        self.assertTrue(alerted_within(self.db, "NDX_GRID_DAILY_REPORT", 86400))
        # 窗口外 (2 天后) 不再视为已发送
        later = utcnow_naive() + timedelta(days=2)
        self.assertFalse(alerted_within(self.db, "NDX_GRID_DAILY_REPORT", 86400, now_utc=later))

    def test_2_cycle_scoped_dedup(self):
        """2. 按 cycle 去重: 同 cycle 已提醒则不再提醒, 不同 cycle 互不影响"""
        log_alert(self.db, "NDX_GRID_STOP_LOSS", "stop", "msg", True, cycle_id=1)
        self.assertTrue(alerted_within(self.db, "NDX_GRID_STOP_LOSS", 3600, cycle_id=1))
        self.assertFalse(alerted_within(self.db, "NDX_GRID_STOP_LOSS", 3600, cycle_id=2))

    def test_3_count_alerts_today_uses_utc_naive(self):
        """3. 今日计数: 只统计当天 (UTC) 记录, 昨日不计"""
        log_alert(self.db, "A", "r", "m", True)
        log_alert(self.db, "B", "r", "m", False)
        self.assertEqual(count_alerts_today(self.db), 2)
        self.assertEqual(count_alerts_today(self.db, alert_type="A"), 1)

        yesterday = utcnow_naive() - timedelta(days=1)
        self.db.query(AlertLog).update({"triggered_at": yesterday})
        self.db.commit()
        self.assertEqual(count_alerts_today(self.db), 0)

    def test_4_log_alert_stores_full_text_and_cycle_id(self):
        """4. 统一写入层: 存完整文本 + cycle_id, 不写 legacy JSON"""
        message = "多行\n完整消息\n不再被 json 包装"
        log_alert(self.db, "NDX_GRID_STOP_LOSS", "stop", message, False,
                  error_message="webhook down", cycle_id=42)
        row = self.db.query(AlertLog).filter(AlertLog.alert_type == "NDX_GRID_STOP_LOSS").first()
        self.assertEqual(row.message, message)
        self.assertEqual(row.cycle_id, 42)
        self.assertFalse(row.sent_successfully)
        self.assertEqual(row.error_message, "webhook down")


if __name__ == "__main__":
    unittest.main()
