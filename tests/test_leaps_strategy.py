"""QQQ LEAPS 策略测试套件。

覆盖:
1. 入场信号两条件 (RSI / 52 周, 2026-10-11 起不再要求 SMA200) 与收盘确认制口径
2. 退出线优先级 (RSI_TP > TIME_STOP > DTE_FORCE) 与"先到先出"
3. 加仓判定 (档位顺序 / RSI 仍超卖 / 最大张数)
4. OptionPosition 状态机 (WAITING/HOLDING/CLOSED + DISMISSED/EXPIRED + 闸门唯一性)
5. 监控引擎 (数据有效性/新鲜度 fail-closed, 状态迁移, 提醒去重)
6. API 鉴权与生命周期端点
7. 通知格式化 (数据缺失渲染 N/A, 不误报, 不声称自动交易)
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SESSION_SECRET", "unit-test-session-secret-not-for-production")

from tests.support import login_client  # noqa: E402


def _make_qqq(**overrides):
    """构造一份"有效且新鲜"的 QQQ 数据 (默认全部条件满足入场)。

    data_timestamp 动态取最近一个交易日 (周一/周末回退), 保证 _is_data_fresh 不随真实日期漂移。
    """
    from app.scheduler.trading_hours import get_latest_trading_day
    try:
        ts = str(get_latest_trading_day())
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d")
    base = {
        "ticker": "QQQ",
        "is_data_valid": True,
        "is_data_fresh": True,
        "last_price": 480.0,
        "prev_close": 475.0,
        "data_timestamp": f"{ts} 00:00:00-04:00",
        "rsi": 30.0,
        "ma200": 450.0,
        "is_above_sma200_3d": True,
        "price_1y_ago": 420.0,
        "price_1y_ago_available": True,
        "closed_bar_date": "2026-09-29",
        "closed_price": 480.0,
        "closed_rsi": 30.0,
        "closed_ma200": 450.0,
        "closed_is_above_sma200_3d": True,
        "closed_price_1y_ago": 420.0,
        "closed_indicators_available": True,
    }
    base.update(overrides)
    return base


class _FakeConfig:
    """运行时配置替身 (与 Config 同接口)。"""

    def __init__(self, **kw):
        self.tp_rsi = kw.get("tp_rsi", 65.0)
        self.time_stop = kw.get("time_stop", 0)
        self.dte_force = kw.get("dte_force", 90)
        self.add_levels = kw.get("add_levels", [0.15, 0.25])
        self.max_qty = kw.get("max_qty", 3)
        self.entry_rsi = kw.get("entry_rsi", 35.0)
        self.delta = kw.get("delta", 0.65)
        self.tenor = kw.get("tenor", 365)
        self.waiting_ttl = kw.get("waiting_ttl", 3)
        self.half_tp = kw.get("half_tp", 0.5)
        self.capital_cap = kw.get("capital_cap", 100000.0)
        self.capital_mode = kw.get("capital_mode", "fixed")
        self.total_assets = kw.get("total_assets", 800000.0)
        self.capital_pct = kw.get("capital_pct", 0.125)
        self.iv_assumption = kw.get("iv_assumption", 0.22)

    def get_entry_rsi_threshold(self):
        return self.entry_rsi

    def get_tp_rsi(self):
        return self.tp_rsi

    def get_time_stop_trading_days(self):
        return self.time_stop

    def get_dte_force_days(self):
        return self.dte_force

    def get_add_levels(self):
        return self.add_levels

    def get_max_quantity(self):
        return self.max_qty

    def get_target_delta(self):
        return self.delta

    def get_target_tenor_days(self):
        return self.tenor

    def get_waiting_ttl_trading_days(self):
        return self.waiting_ttl

    def get_half_tp_pnl(self):
        return self.half_tp

    def get_capital_mode(self):
        return self.capital_mode

    def get_total_assets(self):
        return self.total_assets

    def get_capital_pct(self):
        return self.capital_pct

    def get_capital_cap(self):
        if self.capital_mode == "fixed":
            return self.capital_cap
        return round(self.total_assets * self.capital_pct, 2)

    def get_iv_assumption(self):
        return self.iv_assumption


# ===========================================================================
# 1. 入场信号
# ===========================================================================

class TestEntrySignal(unittest.TestCase):
    def test_1_all_conditions_met(self):
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        self.assertIsNotNone(sig)
        self.assertEqual(sig["signal_base_price"], 480.0)
        self.assertEqual(sig["signal_rsi"], 30.0)

    def test_2_rsi_not_below_threshold(self):
        from app.alerts.qqq_rules import check_entry_signal

        self.assertIsNone(check_entry_signal(_make_qqq(closed_rsi=40.0)))

    def test_3_sma200_not_required(self):
        """2026-10-11 修订: 入场不再要求站上 SMA200 (只保留 52 周趋势过滤)。"""
        from app.alerts.qqq_rules import check_entry_signal

        self.assertIsNotNone(check_entry_signal(_make_qqq(closed_is_above_sma200_3d=False)))

    def test_3f_entry_notification_without_suggestion_fields(self):
        """建议字段全 NULL (历史/异常记录): 入场提醒也必须能渲染, 不能因缺字段崩溃。"""
        from types import SimpleNamespace

        from app.notification.wechat import format_leaps_entry

        rec = SimpleNamespace(suggested_delta=None, suggested_tenor_days=None,
                              suggested_strike=None, suggested_premium=None,
                              suggested_quantity=None, suggested_expiry=None,
                              signal_base_price=480.0, signal_rsi=30.0)
        txt = format_leaps_entry(rec, _make_qqq(), _FakeConfig())
        self.assertIn("365 天", txt)                      # 期限退回默认
        self.assertIn("张数：按资金额度自行决定", txt)

    def test_3g_entry_notification_with_suggestion(self):
        """有建议字段时, 提醒里应出现行权价 / 权利金 / 张数与额度占用。"""
        from types import SimpleNamespace

        from app.notification.wechat import format_leaps_entry

        rec = SimpleNamespace(suggested_delta=0.70, suggested_tenor_days=365,
                              suggested_strike=710.0, suggested_premium=100.81,
                              suggested_quantity=9, suggested_expiry="2027-10-15",
                              signal_base_price=751.27, signal_rsi=30.0)
        txt = format_leaps_entry(rec, _make_qqq(), _FakeConfig())
        self.assertIn("行权价：≈ 710", txt)
        self.assertIn("权利金：≈ $100.81", txt)
        self.assertIn("张数：9 张", txt)
        self.assertIn("占用 91%", txt)

    def test_3b_option_math_delta_roundtrip(self):
        """按目标 delta 反解的行权价, 实际 delta 应回到目标值 (±0.01)。"""
        import math
        from statistics import NormalDist
        from app.market.option_math import strike_for_delta

        spot, iv, r, q = 751.0, 0.22, 0.042, 0.0055
        strike = strike_for_delta(spot, 365, 0.70, iv, r, q)
        d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv)) / (iv * math.sqrt(1.0))
        delta = math.exp(-q) * NormalDist().cdf(d1)
        self.assertAlmostEqual(delta, 0.70, delta=0.01)

    def test_3c2_capital_cap_modes(self):
        """额度口径: pct = 总资产 × 比例 (默认); fixed = 固定金额; 非法值回退不放大。"""
        from app.config import Config

        c = Config({"leaps_capital_mode": "pct", "leaps_total_assets": 800000,
                    "leaps_capital_pct": 0.125})
        self.assertEqual(c.get_capital_cap(), 100000.0)
        c2 = Config({"leaps_capital_mode": "pct", "leaps_total_assets": 1200000,
                     "leaps_capital_pct": 0.125})
        self.assertEqual(c2.get_capital_cap(), 150000.0)   # 资产增长 -> 额度自动增长
        c3 = Config({"leaps_capital_mode": "fixed", "leaps_capital_cap": 50000})
        self.assertEqual(c3.get_capital_cap(), 50000.0)
        c4 = Config({"leaps_capital_mode": "pct", "leaps_total_assets": 0,
                     "leaps_capital_pct": 0, "leaps_capital_cap": 70000})
        self.assertEqual(c4.get_capital_cap(), 70000.0)    # 非法 pct -> 回退固定值, 不会变"不限"
        c5 = Config({"leaps_capital_mode": "bogus"})
        self.assertEqual(c5.get_capital_mode(), "pct")     # 非法模式 -> 默认 pct

    def test_3c_contracts_for_capital(self):
        from app.market.option_math import contracts_for_capital, effective_max_contracts

        self.assertEqual(contracts_for_capital(100000, 87.0), 11)
        self.assertEqual(contracts_for_capital(100000, 0), 1)
        self.assertEqual(contracts_for_capital(0, 87.0), 0)
        self.assertEqual(effective_max_contracts(3, 100000, 87.0), 3)
        self.assertEqual(effective_max_contracts(0, 100000, 87.0), 11)
        self.assertEqual(effective_max_contracts(0, 0, 87.0), 0)

    def test_3d_standard_expiry_is_third_friday(self):
        import datetime
        from app.market.option_math import standard_expiry

        exp = standard_expiry(datetime.date(2026, 10, 12) + datetime.timedelta(days=365))
        self.assertEqual(exp.weekday(), 4)
        self.assertEqual(exp, datetime.date(2027, 10, 15))

    def test_4_below_1y_price_filter(self):
        from app.alerts.qqq_rules import check_entry_signal

        self.assertIsNone(check_entry_signal(_make_qqq(closed_price_1y_ago=500.0)))

    def test_5_missing_1y_data_is_no_signal(self):
        from app.alerts.qqq_rules import check_entry_signal

        self.assertIsNone(check_entry_signal(_make_qqq(closed_price_1y_ago=None)))

    def test_6_no_closed_bar_data(self):
        from app.alerts.qqq_rules import check_entry_signal

        self.assertIsNone(check_entry_signal(_make_qqq(closed_indicators_available=False)))


# ===========================================================================
# 2. 持仓退出线
# ===========================================================================

class _Pos:
    """OptionPosition 测试替身。"""

    def __init__(self, **kw):
        self.entry_date = kw.get("entry_date", date.today() - timedelta(days=30))
        self.expiration_date = kw.get("expiration_date", date.today() + timedelta(days=600))
        self.entry_price = kw.get("entry_price", 100.0)
        self.current_premium = kw.get("current_premium", 120.0)
        self.quantity = kw.get("quantity", 1)
        self.add_count = kw.get("add_count", 0)
        self.signal_base_price = kw.get("signal_base_price", 480.0)
        self.half_tp_alerted = kw.get("half_tp_alerted", False)
        self.realized_premium = kw.get("realized_premium", None)


class TestExitRules(unittest.TestCase):
    def _cfg(self, **kw):
        return _FakeConfig(**kw)

    def test_1_rsi_tp_has_priority(self):
        from app.alerts.qqq_rules import evaluate_position

        # RSI 超阈值 + 同时满足时间止损 -> 只报 RSI_TP (优先级最高)
        pos = _Pos(entry_date=date.today() - timedelta(days=200), current_premium=90.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=70.0), self._cfg())
        self.assertEqual(out["breach"]["type"], "RSI_TP")

    def test_2_time_stop_disabled_by_default(self):
        from app.alerts.qqq_rules import evaluate_position

        # 2026-10 复测: 默认配置禁用时间止损 (0) -> 超期且亏损不再触发 TIME_STOP
        pos = _Pos(entry_date=date.today() - timedelta(days=190), current_premium=90.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=40.0), self._cfg())
        self.assertIsNone(out["breach"])

    def test_2b_time_stop_when_enabled(self):
        from app.alerts.qqq_rules import evaluate_position

        # 显式开启 (126) 时: 超期且亏损 -> TIME_STOP
        pos = _Pos(entry_date=date.today() - timedelta(days=190), current_premium=90.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=40.0), self._cfg(time_stop=126))
        self.assertEqual(out["breach"]["type"], "TIME_STOP")

    def test_3_time_stop_skipped_when_in_profit(self):
        from app.alerts.qqq_rules import evaluate_position

        # 超期但在盈利 -> 不触发时间止损 (回测口径: 仍未回本才砍)
        pos = _Pos(entry_date=date.today() - timedelta(days=190), current_premium=150.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=40.0), self._cfg())
        self.assertIsNone(out["breach"])

    def test_4_dte_force(self):
        from app.alerts.qqq_rules import evaluate_position

        pos = _Pos(expiration_date=date.today() + timedelta(days=60), current_premium=300.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=40.0), self._cfg())
        self.assertEqual(out["breach"]["type"], "DTE_FORCE")

    def test_4b_dte_not_triggered_above_90(self):
        from app.alerts.qqq_rules import evaluate_position

        # 60 天后到期才触发; 100 天 (旧 180 档内的值) 不触发
        pos = _Pos(expiration_date=date.today() + timedelta(days=100), current_premium=300.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=40.0), self._cfg())
        self.assertIsNone(out["breach"])

    def test_5_no_breach_in_normal_holding(self):
        from app.alerts.qqq_rules import evaluate_position

        pos = _Pos(entry_date=date.today() - timedelta(days=30), current_premium=110.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=50.0), self._cfg())
        self.assertIsNone(out["breach"])


# ===========================================================================
# 2b. 分批止盈提醒 (HALF_TP)
# ===========================================================================

class TestHalfTpTrigger(unittest.TestCase):
    def _cfg(self, **kw):
        return _FakeConfig(**kw)

    def test_1_triggers_at_threshold(self):
        from app.alerts.qqq_rules import evaluate_position

        # +60% >= +50%, 2 张, 未提醒过 -> 触发, 卖 1 留 1
        pos = _Pos(quantity=2, current_premium=160.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=50.0), self._cfg())
        self.assertIsNotNone(out["half_tp_trigger"])
        self.assertEqual(out["half_tp_trigger"]["sell_qty"], 1)
        self.assertEqual(out["half_tp_trigger"]["quantity"], 2)
        # 仅提醒: 不产生 breach
        self.assertIsNone(out["breach"])

    def test_2_not_reached_or_single_lot(self):
        from app.alerts.qqq_rules import evaluate_position

        # +30% < +50% -> 不触发
        pos = _Pos(quantity=2, current_premium=130.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=50.0), self._cfg())
        self.assertIsNone(out["half_tp_trigger"])
        # 达标但只有 1 张 -> 无法卖半, 不触发
        pos = _Pos(quantity=1, current_premium=160.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=50.0), self._cfg())
        self.assertIsNone(out["half_tp_trigger"])

    def test_3_disabled_and_already_alerted(self):
        from app.alerts.qqq_rules import evaluate_position

        # half_tp=0 关闭
        pos = _Pos(quantity=2, current_premium=160.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=50.0), self._cfg(half_tp=0.0))
        self.assertIsNone(out["half_tp_trigger"])
        # 已提醒过 -> 不再触发
        pos = _Pos(quantity=2, current_premium=160.0, half_tp_alerted=True)
        out = evaluate_position(pos, _make_qqq(closed_rsi=50.0), self._cfg())
        self.assertIsNone(out["half_tp_trigger"])

    def test_4_rsi_tp_takes_priority(self):
        from app.alerts.qqq_rules import evaluate_position

        # RSI 70 + 盈利达标 -> 只报 RSI_TP breach, 不给 half_tp (先到先出)
        pos = _Pos(quantity=2, current_premium=160.0)
        out = evaluate_position(pos, _make_qqq(closed_rsi=70.0), self._cfg())
        self.assertEqual(out["breach"]["type"], "RSI_TP")
        self.assertIsNone(out["half_tp_trigger"])


# ===========================================================================
# 3. 加仓判定
# ===========================================================================

class TestAddTrigger(unittest.TestCase):
    def _evaluate(self, pos, cfg, **qqq_kw):
        from app.alerts.qqq_rules import evaluate_position

        return evaluate_position(pos, _make_qqq(**qqq_kw), cfg)

    def test_1_first_level_hit(self):
        # 跌 15% (第一档 0.15) + RSI 仍 < 35 -> 提示加到 2 张
        out = self._evaluate(_Pos(quantity=1), _FakeConfig(), closed_price=408.0, closed_rsi=30.0)
        self.assertIsNotNone(out["add_trigger"])
        self.assertEqual(out["add_trigger"]["next_count"], 2)

    def test_1b_no_add_between_levels(self):
        # 只跌 10%, 未到第一档 -15%
        out = self._evaluate(_Pos(quantity=1), _FakeConfig(), closed_price=430.0, closed_rsi=30.0)
        self.assertIsNone(out["add_trigger"])

    def test_2_no_add_when_rsi_recovered(self):
        # 跌幅够但 RSI 已修复 -> 不加
        out = self._evaluate(_Pos(quantity=1), _FakeConfig(), closed_price=430.0, closed_rsi=45.0)
        self.assertIsNone(out["add_trigger"])

    def test_3_no_add_above_level(self):
        # 只跌 5%, 未到第一档
        out = self._evaluate(_Pos(quantity=1), _FakeConfig(), closed_price=456.0, closed_rsi=30.0)
        self.assertIsNone(out["add_trigger"])

    def test_4_max_quantity_cap(self):
        out = self._evaluate(_Pos(quantity=3), _FakeConfig(), closed_price=350.0, closed_rsi=30.0)
        self.assertIsNone(out["add_trigger"])

    def test_5_second_level_requires_first_taken(self):
        # 已加过 1 次 (qty=2), 只认第二档 -25%
        out = self._evaluate(_Pos(quantity=2, add_count=1), _FakeConfig(), closed_price=430.0, closed_rsi=30.0)
        self.assertIsNone(out["add_trigger"])  # 未到 -25% 不再重复
        out = self._evaluate(_Pos(quantity=2, add_count=1), _FakeConfig(), closed_price=358.0, closed_rsi=30.0)
        self.assertIsNotNone(out["add_trigger"])
        self.assertEqual(out["add_trigger"]["next_count"], 3)


# ===========================================================================
# 4-6. 状态机 / 监控引擎 / API (共用 DB fixture)
# ===========================================================================

import tempfile  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from app.database.models import Base  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


class LeapsEndToEndTest(unittest.TestCase):
    """共用 app + 内存 DB 的端到端测试。"""

    def setUp(self):
        # 独立内存 DB (每用例一个, StaticPool 让同一 :memory: 跨 session 共享)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        # Configuration 行 (setup 流程会读)
        from app.database.models import Configuration
        self.db.add(Configuration(admin_password_hash=""))
        self.db.commit()

        # 注入测试 DB + 停用真实 scheduler
        import app.main as main_mod
        import app.database.init_db as init_db_mod

        self._orig_session_local = init_db_mod.SessionLocal
        init_db_mod.SessionLocal = self.Session
        main_mod.SessionLocal = self.Session

        self._orig_start = main_mod.start_scheduler
        self._orig_stop = main_mod.stop_scheduler
        main_mod.start_scheduler = lambda *a, **k: None
        main_mod.stop_scheduler = lambda *a, **k: None

        self.client = TestClient(main_mod.app)
        login_client(self.client)

    def test_dashboard_json_safe_with_date(self):
        """/admin 模板用 {{ leaps_dash|tojson }} 渲染: qqq 字典里的 date 必须转字符串,
        否则 TypeError: Object of type date is not JSON serializable -> 页面 500。"""
        import json

        from app.services.leaps_service import get_leaps_dashboard

        dash = get_leaps_dashboard(
            self.db,
            {"date": date(2026, 10, 11), "closed_price": 751.0, "last_price": 751.0,
             "closed_bar_date": "2026-10-09"},
        )
        json.dumps(dash)  # 不抛异常即通过
        self.assertEqual(dash["qqq"]["date"], "2026-10-11")

    def tearDown(self):
        import app.main as main_mod
        import app.database.init_db as init_db_mod

        init_db_mod.SessionLocal = self._orig_session_local
        main_mod.start_scheduler = self._orig_start
        main_mod.stop_scheduler = self._orig_stop
        self.db.close()

    # ---- 状态机 ----

    def test_1_position_lifecycle(self):
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())
        self.assertEqual(rec.status, "WAITING")

        # 闸门: 已有 WAITING 时不能再建
        with self.assertRaises(ValueError):
            leaps_service.create_waiting_position(self.db, sig, _FakeConfig())

        # 忽略 -> 释放闸门 -> 可再建
        leaps_service.dismiss_waiting(self.db, rec.id)
        rec2 = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())

        # 确认建仓
        held = leaps_service.confirm_entry(
            self.db, rec2.id, strike=450.0,
            expiration=date.today() + timedelta(days=700),
            entry_price=100.0, quantity=1,
        )
        self.assertEqual(held.status, "HOLDING")
        self.assertEqual(held.total_cost, 100.0)

        # 加仓
        added = leaps_service.add_lot(self.db, rec2.id, add_price=90.0, quantity=1)
        self.assertEqual(added.quantity, 2)
        self.assertEqual(added.add_count, 1)
        self.assertAlmostEqual(added.total_cost, 190.0)

        # 平仓
        closed = leaps_service.close_position(self.db, rec2.id, reason="RSI_TP", close_premium=160.0)
        self.assertEqual(closed.status, "CLOSED")
        self.assertEqual(closed.close_reason, "RSI_TP")

    def test_2_api_requires_auth(self):
        from tests.support import logout_client

        logout_client(self.client)
        self.assertEqual(self.client.get("/api/leaps/status").status_code, 401)
        self.assertEqual(
            self.client.post("/api/leaps/1/confirm", json={}).status_code, 401)

    def test_3_api_full_lifecycle(self):
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())

        # confirm
        res = self.client.post(f"/api/leaps/{rec.id}/confirm", json={
            "strike": 450.0,
            "expiration_date": (date.today() + timedelta(days=700)).isoformat(),
            "entry_price": 100.0,
            "quantity": 1,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["position_status"], "HOLDING")

        # add-lot
        res = self.client.post(f"/api/leaps/{rec.id}/add-lot", json={"add_price": 90.0, "quantity": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["quantity"], 2)

        # close
        res = self.client.post(f"/api/leaps/{rec.id}/close", json={"close_premium": 155.0})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["position_status"], "CLOSED")

        # 历史可查
        res = self.client.get("/api/leaps/history")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["positions"]), 1)

    def test_4_api_state_conflict_is_409(self):
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())

        # WAITING 状态不能直接 close (非法迁移 -> 409)
        res = self.client.post(f"/api/leaps/{rec.id}/close", json={})
        self.assertEqual(res.status_code, 409)

    def test_5_monitor_creates_waiting_and_dedups(self):
        from app.alerts.leaps_monitor import process_leaps_position
        from unittest.mock import patch

        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, _make_qqq(), notifier=None, config=_FakeConfig())
        self.assertEqual(out["action"], "ENTRY_SIGNAL")

        from app.services import leaps_service
        waiting = leaps_service.get_waiting_position(self.db)
        self.assertIsNotNone(waiting)

        # 再跑一轮: 已有 WAITING, 不会重复建
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, _make_qqq(), notifier=None, config=_FakeConfig())
        self.assertEqual(out["action"], "WAITING_QUEUED")

    def test_6_monitor_skips_invalid_and_stale_data(self):
        from app.alerts.leaps_monitor import process_leaps_position

        self.assertEqual(
            process_leaps_position(self.db, {}, notifier=None, config=_FakeConfig())["reason"],
            "NO_VALID_DATA")
        self.assertEqual(
            process_leaps_position(self.db, _make_qqq(last_price=0), notifier=None,
                                   config=_FakeConfig())["reason"],
            "NO_VALID_DATA")
        self.assertEqual(
            process_leaps_position(self.db, _make_qqq(last_price=-5), notifier=None,
                                   config=_FakeConfig())["reason"],
            "NON_POSITIVE_PRICE")
        self.assertEqual(
            process_leaps_position(
                self.db, _make_qqq(data_timestamp="2020-01-02"), notifier=None,
                config=_FakeConfig())["reason"],
            "STALE_DATA")
        # fail-closed: 没有创建任何仓位
        from app.services import leaps_service
        self.assertIsNone(leaps_service.get_waiting_position(self.db))

    def test_7_monitor_waiting_ttl_expiry(self):
        from app.alerts.leaps_monitor import process_leaps_position
        from app.database.models import OptionPosition
        from app.services import leaps_service

        rec = OptionPosition(
            status="WAITING",
            signal_base_price=480.0, signal_rsi=30.0,
            suggested_delta=0.65, suggested_tenor_days=365,
            created_at=datetime.utcnow() - timedelta(days=30),
        )
        self.db.add(rec)
        self.db.commit()

        from unittest.mock import patch
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, _make_qqq(), notifier=None,
                                         config=_FakeConfig(waiting_ttl=3))
        self.assertEqual(out["action"], "EXPIRED")

    def test_8_monitor_exit_flow(self):
        from app.alerts.leaps_monitor import process_leaps_position
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())
        leaps_service.confirm_entry(
            self.db, rec.id, strike=450.0,
            expiration=date.today() + timedelta(days=700),
            entry_price=100.0, quantity=1,
        )

        # RSI 70 -> RSI_TP 平仓
        from unittest.mock import patch
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, _make_qqq(closed_rsi=70.0), notifier=None,
                                         config=_FakeConfig())
        self.assertEqual(out["action"], "CLOSED")
        self.assertEqual(out["reason"], "RSI_TP")

        from app.database.models import OptionPosition
        held = self.db.query(OptionPosition).filter(OptionPosition.id == rec.id).first()
        self.assertEqual(held.status, "CLOSED")

    def test_8b_partial_close_flow(self):
        """部分平仓: 张数/成本按比例更新, realized_premium 累计, 不允许卖光。"""
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal
        from app.services.leaps_service import OptionParameterError

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())
        leaps_service.confirm_entry(
            self.db, rec.id, strike=450.0,
            expiration=date.today() + timedelta(days=700),
            entry_price=100.0, quantity=3,
        )
        out = leaps_service.partial_close(self.db, rec.id, sell_qty=1, sell_price=180.0)
        self.assertEqual(out.quantity, 2)
        self.assertAlmostEqual(out.total_cost, 200.0)   # 300 × 2/3
        self.assertAlmostEqual(out.realized_premium, 180.0)
        self.assertTrue(out.half_tp_alerted)
        self.assertEqual(out.status, "HOLDING")

        # 再加仓一次 (成本口径继续累加) 后尝试卖光 -> 400
        leaps_service.add_lot(self.db, rec.id, add_price=120.0, quantity=1)
        with self.assertRaises(OptionParameterError):
            leaps_service.partial_close(self.db, rec.id, sell_qty=3)

        # API 通道
        res = self.client.post(f"/api/leaps/{rec.id}/partial-close",
                               json={"sell_qty": 1, "sell_price": 150.0})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["quantity"], 2)
        self.assertAlmostEqual(body["realized_premium"], 330.0)  # 180 + 150

    def test_8c_monitor_half_tp_alert_once(self):
        """盈利达标 -> HALF_TP 提醒一次 (position 仍 HOLDING), 且不重复发。"""
        from app.alerts.leaps_monitor import process_leaps_position
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal
        from unittest.mock import patch
        from app.database.models import AlertLog

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())
        leaps_service.confirm_entry(
            self.db, rec.id, strike=450.0,
            expiration=date.today() + timedelta(days=700),
            entry_price=100.0, quantity=2,
        )
        from app.database.models import OptionPosition
        held = self.db.query(OptionPosition).filter(OptionPosition.id == rec.id).first()
        held.current_premium = 160.0   # +60% ≥ +50%
        self.db.commit()

        # 假 notifier: _notify_with_log 约定 notifier=None 不落 AlertLog
        class _FakeNotifier:
            def send_message(self, message):
                return True

        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, _make_qqq(), notifier=_FakeNotifier(),
                                         config=_FakeConfig())
        self.assertEqual(out["action"], "HALF_TP_ALERTED")

        held = self.db.query(OptionPosition).filter(OptionPosition.id == rec.id).first()
        self.assertEqual(held.status, "HOLDING")   # 仅提醒, 状态不变
        self.assertTrue(held.half_tp_alerted)

        alerts = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "LEAPS_HALF_TP",
            AlertLog.position_id == rec.id).all()
        self.assertEqual(len(alerts), 1)

        # 再跑一轮: 不重复提醒 (落库去重), 走 HOLDING_MONITORED
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out2 = process_leaps_position(self.db, _make_qqq(current_premium=170.0), notifier=None,
                                          config=_FakeConfig())
        self.assertEqual(out2["action"], "HOLDING_MONITORED")
        alerts2 = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "LEAPS_HALF_TP",
            AlertLog.position_id == rec.id).all()
        self.assertEqual(len(alerts2), 1)

    def test_9_dashboard_fresh_flag_injected(self):
        """P1 回归: 新鲜度由聚合层/监控判定注入后, 新鲜数据走正常信号路径 (不再恒 stale)"""
        from unittest.mock import patch
        from app.alerts.leaps_monitor import process_leaps_position, _is_data_fresh
        from app.services import leaps_service

        # 监控同款判定对当日数据返回 True (日报聚合层用的同一口径)
        self.assertTrue(_is_data_fresh(_make_qqq()))
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, _make_qqq(), notifier=None,
                                         config=_FakeConfig())
        self.assertIn(out["action"], ("ENTRY_SIGNAL", "WAITING_QUEUED"))
        dash = leaps_service.get_leaps_dashboard(self.db, _make_qqq())
        self.assertIsNotNone(dash["waiting_position"])

    def test_10_dashboard_aggregates_holding_summary(self):
        """P2/P3: get_leaps_dashboard 为 HOLDING 附加持仓评估摘要"""
        from app.services import leaps_service
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())
        leaps_service.confirm_entry(
            self.db, rec.id, strike=450.0,
            expiration=date.today() + timedelta(days=700),
            entry_price=100.0, quantity=1,
        )
        dash = leaps_service.get_leaps_dashboard(self.db, _make_qqq())
        s = dash.get("holding_summary") or {}
        self.assertIn("holding_days", s)
        self.assertIn("dte", s)
        self.assertAlmostEqual(s["dte"], 700, delta=2)
        # qty=1 < max 3, add_count=0 < len(levels): 下一档信息应存在
        self.assertAlmostEqual(s["next_add_level_pct"], 0.15)
        self.assertAlmostEqual(s["next_add_trigger_price"], 480.0 * 0.85, places=2)

    # ---- 2026-10-11 评审修复的回归 ----

    def test_11_config_dict_covers_capital_keys(self):
        """P1-1: /admin/rules 保存的 5 个新键必须进 _build_config_dict,
        否则调度器 (启动快照) 永远读代码默认值, 改总资产不生效。"""
        import app.main as main_mod
        from app.database.models import Configuration

        row = self.db.query(Configuration).first()
        row.leaps_capital_mode = "pct"
        row.leaps_total_assets = 1200000.0
        row.leaps_capital_pct = 0.10
        row.leaps_capital_cap = 999.0
        row.leaps_iv_assumption = 0.30
        self.db.commit()

        cfg = main_mod.get_config(main_mod._build_config_dict(row))
        self.assertEqual(cfg.get_capital_cap(), 120000.0)   # 1.2M × 10%
        self.assertEqual(cfg.get_iv_assumption(), 0.30)
        self.assertEqual(cfg.get_capital_mode(), "pct")

    def test_12_jobs_reload_config_from_db(self):
        """P1-2: 每轮任务从 DB 重载配置 -> 改完 /admin/rules 下一轮生效。"""
        from app.scheduler import jobs
        from app.database.models import Configuration

        row = self.db.query(Configuration).first()
        row.leaps_tp_rsi = 70.0
        row.leaps_capital_mode = "pct"
        row.leaps_total_assets = 2000000.0
        row.leaps_capital_pct = 0.05
        self.db.commit()

        fresh = jobs._fresh_config(self.db, _FakeConfig(tp_rsi=65.0))
        self.assertEqual(fresh.get_tp_rsi(), 70.0)          # 不再是启动快照的 65
        self.assertEqual(fresh.get_capital_cap(), 100000.0)  # 2M × 5%

        # DB 不可用时不得抛异常, 必须回退到一份可用配置 (env/默认或启动快照)
        fallback = jobs._fresh_config(None, _FakeConfig(tp_rsi=61.0))
        self.assertTrue(hasattr(fallback, "get_tp_rsi"))
        self.assertIsInstance(fallback.get_tp_rsi(), float)

    def test_13_add_alert_retries_after_send_failure(self):
        """P1-3: 推送失败不置去重 -> 下一轮重试; 只有成功才置位。"""
        from unittest.mock import patch

        from app.alerts import dedup
        from app.alerts.leaps_monitor import process_leaps_position
        from app.alerts.qqq_rules import check_entry_signal
        from app.services import leaps_service

        dedup.clear_dedup()
        sig = check_entry_signal(_make_qqq())
        rec = leaps_service.create_waiting_position(self.db, sig, _FakeConfig())
        leaps_service.confirm_entry(
            self.db, rec.id, strike=450.0,
            expiration=date.today() + timedelta(days=700),
            entry_price=100.0, quantity=1,
        )

        class _FailNotifier:
            def send_message(self, message):
                return False

        class _OkNotifier:
            def send_message(self, message):
                return True

        qqq = _make_qqq(closed_price=408.0, closed_rsi=30.0)   # 回撤 -15% 命中第一档
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out = process_leaps_position(self.db, qqq, notifier=_FailNotifier(), config=_FakeConfig())
        self.assertEqual(out["action"], "ADD_SEND_FAILED")

        # 失败记录不算已提醒 -> 下一轮用正常 notifier 必须还能发出来
        with patch("app.alerts.leaps_monitor._is_data_fresh", return_value=True):
            out2 = process_leaps_position(self.db, qqq, notifier=_OkNotifier(), config=_FakeConfig())
        self.assertEqual(out2["action"], "ADD_ALERTED")

        from app.database.models import AlertLog
        rows = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "LEAPS_ADD_LOT",
            AlertLog.position_id == rec.id).all()
        self.assertEqual(len(rows), 2)                          # 一失败一成功
        self.assertFalse(bool(rows[0].sent_successfully))
        self.assertTrue(bool(rows[1].sent_successfully))

    def test_14_alerted_within_success_only(self):
        """P1-3: 只统计成功送达的记录 (否则一次抖动就把提醒永久吞掉)。"""
        from app.alerts.alert_log import alerted_within, log_alert

        log_alert(self.db, "LEAPS_TEST_FAIL", "t", "msg", success=False)
        self.assertTrue(alerted_within(self.db, "LEAPS_TEST_FAIL", within_seconds=3600))
        self.assertFalse(alerted_within(self.db, "LEAPS_TEST_FAIL", within_seconds=3600,
                                        success_only=True))

        log_alert(self.db, "LEAPS_TEST_FAIL", "t", "msg", success=True)
        self.assertTrue(alerted_within(self.db, "LEAPS_TEST_FAIL", within_seconds=3600,
                                       success_only=True))

    def test_15_entry_signal_bar_date_is_date_only(self):
        """P3: 提醒文案里的判定基准不应带 " 00:00:00-04:00"。"""
        from app.alerts.qqq_rules import check_entry_signal

        sig = check_entry_signal(_make_qqq())
        self.assertIsNotNone(sig)
        self.assertRegex(sig["signal_bar_date"], r"^\d{4}-\d{2}-\d{2}$")


# ===========================================================================
# 7. 通知格式化
# ===========================================================================

class TestNotificationReliability(unittest.TestCase):
    """P1-3 / P2-8: 发送重试、长消息分段、去重查询与置位分离。"""

    def test_message_chunking_short_passthrough(self):
        from app.notification.wechat import split_message_chunks

        self.assertEqual(split_message_chunks("hello"), ["hello"])

    def test_message_chunking_long_message(self):
        from app.notification.wechat import split_message_chunks

        msg = "\n".join("行 %d %s" % (i, "x" * 120) for i in range(40))
        chunks = split_message_chunks(msg, 500)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.encode("utf-8")), 500)
        # 内容不丢 (去掉换行后逐字符一致)
        self.assertEqual("".join(chunks).replace("\n", ""), msg.replace("\n", ""))

    def test_wechat_retry_then_success(self):
        from unittest.mock import patch

        from app.notification.wechat import WeChatNotifier

        calls = {"n": 0}

        class _Resp:
            status_code = 200

            def json(self):
                return {"errcode": 0}

        def fake_post(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return _Resp()

        n = WeChatNotifier("https://example.invalid/hook")
        with patch("app.notification.wechat.requests.post", side_effect=fake_post), \
                patch("app.notification.wechat.time.sleep", return_value=None):
            self.assertTrue(n.send_message("hello"))
        self.assertEqual(calls["n"], 3)

    def test_wechat_retry_exhausted(self):
        from unittest.mock import patch

        from app.notification.wechat import WeChatNotifier

        class _Resp:
            status_code = 500

            def json(self):
                return {}

        n = WeChatNotifier("https://example.invalid/hook")
        with patch("app.notification.wechat.requests.post", return_value=_Resp()) as p, \
                patch("app.notification.wechat.time.sleep", return_value=None):
            self.assertFalse(n.send_message("hello"))
        self.assertEqual(p.call_count, n.SEND_ATTEMPTS)

    def test_telegram_notifier_retry_then_success(self):
        from unittest.mock import patch

        from app.notification.telegram import TelegramNotifier

        calls = {"n": 0}

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        def fake_post(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _Resp()

        n = TelegramNotifier("123:abc", "7474962046")
        with patch("app.notification.telegram.requests.post", side_effect=fake_post), \
                patch("app.notification.telegram.time.sleep", return_value=None):
            self.assertTrue(n.send_message("hi"))
        self.assertEqual(calls["n"], 2)

    def test_telegram_notifier_disabled_without_credentials(self):
        from app.notification.telegram import TelegramNotifier

        self.assertFalse(TelegramNotifier("", "").send_message("hi"))
        self.assertFalse(TelegramNotifier("123:abc", "").enabled)

    def test_composite_notifier_partial_delivery_counts_as_success(self):
        from app.notification.dispatch import CompositeNotifier

        class _Ok:
            def send_message(self, m):
                return True

        class _Bad:
            def send_message(self, m):
                return False

        self.assertTrue(CompositeNotifier([("a", _Bad()), ("b", _Ok())]).send_message("x"))
        self.assertFalse(CompositeNotifier([("a", _Bad())]).send_message("x"))
        self.assertFalse(CompositeNotifier([]).send_message("x"))

    def test_get_notifier_channel_selection(self):
        from app.notification.dispatch import get_notifier

        class _Cfg:
            def __init__(self, wh="", tok="", chat=""):
                self._wh, self._tok, self._chat = wh, tok, chat

            def get_wechat_webhook_url(self):
                return self._wh

            def get_telegram_bot_token(self):
                return self._tok

            def get_telegram_chat_id(self):
                return self._chat

        self.assertIsNone(get_notifier(_Cfg()))
        only_tg = get_notifier(_Cfg(tok="123:abc", chat="1"))
        self.assertEqual([name for name, _ in only_tg.channels], ["telegram"])
        both = get_notifier(_Cfg(wh="https://example.invalid/hook", tok="123:abc", chat="1"))
        self.assertEqual([name for name, _ in both.channels], ["wecom", "telegram"])
        # 日报便捷封装走同一 send_message
        self.assertTrue(hasattr(both, "send_leaps_report"))

    def test_dedup_check_then_mark(self):
        from app.alerts import dedup

        dedup.clear_dedup()
        self.assertFalse(dedup.was_alerted("KEY_X"))
        self.assertFalse(dedup.was_alerted("KEY_X"))   # 查询不置位
        dedup.mark_alerted("KEY_X")
        self.assertTrue(dedup.was_alerted("KEY_X"))
        dedup.clear_dedup()


class TestNotificationFormat(unittest.TestCase):
    def test_1_entry_message_contains_contract_suggestion(self):
        from app.notification.wechat import format_leaps_entry

        class _P:
            suggested_delta = 0.65
            suggested_tenor_days = 365

        class _C(_FakeConfig):
            def get_add_levels(self):
                return [0.10, 0.20]

        msg = format_leaps_entry(_P(), _make_qqq(), _C())
        self.assertIn("LEAPS 入场信号", msg)
        self.assertIn("0.65", msg)
        self.assertIn("不自动交易", msg)

    def test_2_exit_message_types(self):
        from app.notification.wechat import format_leaps_exit

        class _P:
            strike = 450.0
            expiration_date = "2028-09-01"
            quantity = 2
            add_count = 1
            entry_price = 100.0
            current_premium = 170.0
            total_cost = 190.0

        msg = format_leaps_exit(_P(), {"type": "RSI_TP", "reason": "RSI 66.1 > 止盈阈值 65"}, _make_qqq())
        self.assertIn("止盈", msg)
        msg = format_leaps_exit(_P(), {"type": "TIME_STOP", "reason": "持仓 130 个交易日"}, _make_qqq())
        self.assertIn("止损", msg)

    def test_3_daily_report_renders_na_not_crash(self):
        from app.notification.wechat import format_leaps_daily_report

        data = {
            "dashboard": {"qqq": {}, "holding_position": None, "waiting_position": None},
            "strategy": {"entry_rsi": 35.0, "tp_rsi": 65.0},
            "breadth": None,
        }
        msg = format_leaps_daily_report(data)
        self.assertIn("N/A", msg)
        self.assertIn("无持仓", msg)

    def test_4_daily_report_with_holding(self):
        from app.notification.wechat import format_leaps_daily_report

        data = {
            "dashboard": {
                "qqq": _make_qqq(),
                "holding_position": {
                    "id": 1, "status": "HOLDING", "strike": 450.0,
                    "expiration_date": "2028-09-01", "quantity": 2,
                    "entry_price": 100.0, "current_premium": 160.0, "add_count": 1,
                },
                "waiting_position": None,
            },
            "strategy": {"entry_rsi": 35.0, "tp_rsi": 65.0},
            "breadth": {"sp500": {"price": 6000.0, "change_pct": 0.5},
                        "vix": {"price": 15.0, "change_pct": -2.0}},
        }
        data["dashboard"]["holding_summary"] = {
            "holding_days": 45, "dte": 655,
            "next_add_level_pct": 0.10,
            "next_add_trigger_price": 432.0,
            "next_add_distance_pct": 11.18,
        }
        msg = format_leaps_daily_report(data)
        self.assertIn("HOLDING", msg)
        self.assertIn("标普500", msg)
        self.assertIn("VIX", msg)
        # P2/P3: 距离信息与持仓天数
        self.assertIn("已持仓：45 个交易日", msg)
        self.assertIn("距止盈", msg)
        self.assertIn("距加仓：下一档 -10%", msg)
        self.assertIn("距到期：655 天", msg)

    def test_4b_daily_report_waiting_shows_ttl(self):
        """P2: WAITING 时日报展示建议有效期 (TTL)"""
        from app.notification.wechat import format_leaps_daily_report

        data = {
            "dashboard": {
                "qqq": _make_qqq(),
                "holding_position": None,
                "waiting_position": {
                    "id": 7, "status": "WAITING",
                    "ttl": {"ttl_enabled": True, "ttl_trading_days": 3,
                            "remaining_trading_days": 2, "expired": False},
                },
            },
            "strategy": {"entry_rsi": 35.0, "tp_rsi": 65.0},
        }
        msg = format_leaps_daily_report(data)
        self.assertIn("WAITING", msg)
        self.assertIn("剩 2 个交易日", msg)



if __name__ == "__main__":
    unittest.main()
