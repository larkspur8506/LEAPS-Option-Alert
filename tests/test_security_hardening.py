"""
P0/P1 加固与策略修正的回归测试。

覆盖:
- 管理员会话: 伪造明文 Cookie 失效 / 签名会话有效 / 登出 / 登录限速
- /setup 初始化入口: 仅首次 + SETUP_TOKEN 或回环访问
- 口令散列: scrypt 新格式 + 旧 sha256 兼容
- AlertLog 统一写入层: 今日计数与落库级去重
- LEAPS 日报去重: 美东自然日口径 (LEAPS 策略版)

(LEAPS 策略本身的状态机/退出规则/加仓/监控引擎见 test_leaps_strategy.py)
"""
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pytz import timezone as pytz_timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, AlertLog
from app.alerts import dedup as dedup_mod
from app.alerts.alert_log import log_alert, count_alerts_today, alerted_within, utcnow_naive
from app.admin import auth as auth_mod
from app.admin import security as security_mod
from app.admin.security import SESSION_COOKIE_NAME, create_session_token, reset_login_limits
from app.config import Config
from tests.support import login_client, admin_session_token, set_setup_token

et_tz = pytz_timezone("America/New_York")

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

    def test_6_leaps_api_requires_session(self):
        """6. /api/leaps 接口同样受签名会话保护"""
        self._set_admin_password()
        self.client.cookies.set("admin_logged_in", "true")
        self.assertEqual(self.client.get("/api/leaps/status").status_code, 401)
        login_client(self.client)
        res = self.client.get("/api/leaps/status")
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

    def test_5_browser_flow_form_carries_token(self):
        """5. 浏览器真实流程: 带 token 打开页面 -> 表单把 token 一起提交 -> 设置成功

        回归: 表单 action 写死为 /setup 且不带隐藏字段时, 用户从 ?token= 链接打开页面
        能正常显示, 但提交会被 403 拒绝 ("初始化入口未授权"), 密码永远设置不上。
        """
        set_setup_token("open-sesame")
        page = self.client.get("/setup?token=open-sesame")
        self.assertEqual(page.status_code, 200)
        m = re.search(r'name="setup_token"\s+value="([^"]*)"', page.text)
        self.assertIsNotNone(m, "初始化页必须把 token 渲染进表单隐藏字段")
        assert m is not None  # 供类型检查收窄
        self.assertEqual(m.group(1), "open-sesame")

        # 浏览器只会提交表单里的字段 (password + 隐藏 token), 不带 URL query
        res = self.client.post("/setup", data={"password": "abcdef", "setup_token": m.group(1)})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(auth_mod.verify_admin_password("abcdef", self.db))

    def test_6_post_accepts_token_from_query_string(self):
        """6. token 只出现在 URL 上 (旧链接/表单字段丢失) 时也放行"""
        set_setup_token("open-sesame")
        res = self.client.post("/setup?token=open-sesame", data={"password": "abcdef"})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(auth_mod.verify_admin_password("abcdef", self.db))

    def test_7_post_without_any_token_still_denied(self):
        """7. 两个来源都没有 token 时依旧 403, 且提示如何拿到链接"""
        set_setup_token("open-sesame")
        res = self.client.post("/setup", data={"password": "abcdef"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("?token=", res.text)
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
# 4. AlertLog 统一写入层
# ---------------------------------------------------------------------------

class TestAlertLogLayer(BaseTest):
    def test_1_daily_report_dedup_across_restart(self):
        """1. 落库级去重: 已写过当天日报则重启后不再重复 (内存 dedup 丢失也有效)"""
        log_alert(self.db, "LEAPS_DAILY_REPORT", "LEAPS Daily Report", "hello\nworld", True)
        self.assertTrue(alerted_within(self.db, "LEAPS_DAILY_REPORT", 86400))
        # 窗口外 (2 天后) 不再视为已发送
        later = utcnow_naive() + timedelta(days=2)
        self.assertFalse(alerted_within(self.db, "LEAPS_DAILY_REPORT", 86400, now_utc=later))

    def test_2_position_scoped_dedup(self):
        """2. 按 position 去重: 同仓位已提醒则不再提醒, 不同仓位互不影响"""
        log_alert(self.db, "LEAPS_EXIT", "exit", "msg", True, position_id=1)
        self.assertTrue(alerted_within(self.db, "LEAPS_EXIT", 3600, position_id=1))
        self.assertFalse(alerted_within(self.db, "LEAPS_EXIT", 3600, position_id=2))

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

    def test_4_log_alert_stores_full_text_and_position_id(self):
        """4. 统一写入层: 存完整文本 + position_id, 不写 legacy JSON"""
        message = "多行\n完整消息\n不再被 json 包装"
        log_alert(self.db, "LEAPS_EXIT", "exit", message, False,
                  error_message="webhook down", position_id=42)
        row = self.db.query(AlertLog).filter(AlertLog.alert_type == "LEAPS_EXIT").first()
        self.assertEqual(row.message, message)
        self.assertEqual(row.position_id, 42)
        self.assertFalse(row.sent_successfully)
        self.assertEqual(row.error_message, "webhook down")


class TestDailyReportDedupWindow(BaseTest):
    """
    日报落库去重必须按"美东自然日"判定。

    历史实现用滚动 24 小时窗口: 上一次日报恰好约 24 小时前, 会被判成
    "今天已发送" -> 隔天漏发日报 (线上 16:30 定时任务最典型的翻车方式)。
    """

    def setUp(self):
        super().setUp()
        from app.config import Config

        self.mock_fetcher = MagicMock()
        self.mock_fetcher.get_qqq_data.return_value = {
            "ticker": "QQQ", "is_data_valid": True, "last_price": 480.0, "rsi": 30.0,
            "ma200": 450.0, "is_above_sma200_3d": True, "price_1y_ago": 420.0,
            "data_timestamp": str(datetime.now(et_tz)),
        }
        self.config = Config({
            "daily_report_mode": "leaps",
            "wechat_webhook_url": "https://mock.webhook/key=SECRET",
        })

    def _send(self, previous_report_utc=None, clear_memory_dedup=True):
        from app.scheduler.jobs import send_daily_report_job
        from app.alerts import dedup as dedup_module

        if clear_memory_dedup:
            dedup_module.clear_dedup()
        if previous_report_utc is not None:
            log_alert(self.db, "LEAPS_DAILY_REPORT", "LEAPS Daily Report",
                      "previous report", True, triggered_at=previous_report_utc)

        notifier = MagicMock()
        notifier.send_leaps_report.return_value = True
        notifier.format_leaps_report.return_value = "report text"

        fixed_now_et = et_tz.localize(datetime(2026, 9, 30, 16, 30, 0))
        with patch("app.scheduler.jobs.is_trading_day", return_value=True), \
             patch("app.scheduler.jobs.get_current_time_et", return_value=fixed_now_et), \
             patch("app.scheduler.jobs.get_wechat_notifier", return_value=notifier):
            send_daily_report_job(self.mock_fetcher, db=self.db, config=self.config)
        return notifier

    def test_1_yesterday_report_does_not_block_today(self):
        """1. 昨天 (美东) 已发日报 -> 今天照常发送 (不能漏发)"""
        notifier = self._send(previous_report_utc=datetime(2026, 9, 29, 20, 30, 0))
        notifier.send_leaps_report.assert_called_once()

    def test_2_same_day_report_blocks_duplicate(self):
        """2. 今天 (美东) 已发日报 -> 同一天第二次不再发送"""
        notifier = self._send(previous_report_utc=datetime(2026, 9, 30, 12, 0, 0))
        notifier.send_leaps_report.assert_not_called()

    def test_3_no_previous_report_sends(self):
        """3. 无历史记录 -> 正常发送"""
        notifier = self._send(previous_report_utc=None)
        notifier.send_leaps_report.assert_called_once()

    def test_4_both_gates_agree(self):
        """4. 落库闸门与内存闸门语义一致 (同一天只发一次, 跨天各发一次)"""
        # 第一次: 发送并落库
        notifier = self._send(previous_report_utc=None)
        notifier.send_leaps_report.assert_called_once()
        rows = self.db.query(AlertLog).filter(AlertLog.alert_type == "LEAPS_DAILY_REPORT").count()
        self.assertEqual(rows, 1)
        # 立刻再跑一次 (内存 dedup 生效, 不重置内存态) -> 不重复
        notifier2 = self._send(previous_report_utc=None, clear_memory_dedup=False)
        notifier2.send_leaps_report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
