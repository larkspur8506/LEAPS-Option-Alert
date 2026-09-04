import unittest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, GridCycle
from app.alerts.grid_cycle import (
    create_waiting_grid_cycle,
    start_grid_cycle,
    close_grid_cycle,
    stop_grid_cycle,
    get_waiting_grid_cycle,
    has_waiting_grid_cycle,
    get_running_grid_cycle,
    has_running_grid_cycle
)
from app.config import Config


class TestGridCyclePhase2(unittest.TestCase):
    def setUp(self):
        """每个测试用例使用独立的 SQLite 内存数据库"""
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    # -------------------------------------------------------------
    # 1. 创建 WAITING 状态测试
    # -------------------------------------------------------------
    def test_create_waiting_cycle(self):
        """测试创建 WAITING 周期，建议参数正确保存，实际参数初始为空"""
        suggested = {
            "base_price": 25000.0,
            "upper_price": 30000.0,
            "lower_price": 20000.0,
            "grid_count": 200,
            "leverage": 5.0
        }

        cycle = create_waiting_grid_cycle(self.db, suggested)

        self.assertIsNotNone(cycle.id)
        self.assertEqual(cycle.status, "WAITING")
        self.assertEqual(cycle.suggested_base_price, 25000.0)
        self.assertEqual(cycle.suggested_upper_price, 30000.0)
        self.assertEqual(cycle.suggested_lower_price, 20000.0)
        self.assertEqual(cycle.suggested_grid_count, 200)
        self.assertEqual(cycle.suggested_leverage, 5.0)

        # actual 参数必须显式为空
        self.assertIsNone(cycle.actual_base_price)
        self.assertIsNone(cycle.actual_upper_price)
        self.assertIsNone(cycle.actual_lower_price)
        self.assertIsNone(cycle.actual_grid_count)
        self.assertIsNone(cycle.actual_leverage)
        self.assertIsNone(cycle.actual_margin)

        # 时间戳验证
        self.assertIsNotNone(cycle.created_at)
        self.assertIsNone(cycle.started_at)
        self.assertIsNone(cycle.closed_at)

        # 查询辅助函数验证
        self.assertTrue(has_waiting_grid_cycle(self.db))
        self.assertFalse(has_running_grid_cycle(self.db))

    # -------------------------------------------------------------
    # 2. 启动 WAITING -> RUNNING 与实际参数写入
    # -------------------------------------------------------------
    def test_start_waiting_to_running(self):
        """测试 WAITING -> RUNNING，实际参数独立保存"""
        suggested = {
            "base_price": 25000.0,
            "upper_price": 30000.0,
            "lower_price": 20000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)

        # 用户根据实际行情在交易所下单，录入实际参数 (可能微调)
        running_cycle = start_grid_cycle(
            self.db,
            cycle_id=cycle.id,
            actual_base_price=25150.0,
            actual_upper_price=30500.0,
            actual_lower_price=20500.0,
            actual_grid_count=180,
            actual_leverage=4.0,
            actual_margin=10000.0,
            notes="MEXC 5x 永续做多网格已开"
        )

        self.assertEqual(running_cycle.status, "RUNNING")
        # 建议参数保持原样
        self.assertEqual(running_cycle.suggested_base_price, 25000.0)
        # 实际参数严格写入
        self.assertEqual(running_cycle.actual_base_price, 25150.0)
        self.assertEqual(running_cycle.actual_upper_price, 30500.0)
        self.assertEqual(running_cycle.actual_lower_price, 20500.0)
        self.assertEqual(running_cycle.actual_grid_count, 180)
        self.assertEqual(running_cycle.actual_leverage, 4.0)
        self.assertEqual(running_cycle.actual_margin, 10000.0)
        self.assertIsNotNone(running_cycle.started_at)
        self.assertIn("MEXC", running_cycle.notes)

        # 查询辅助函数验证
        self.assertFalse(has_waiting_grid_cycle(self.db))
        self.assertTrue(has_running_grid_cycle(self.db))
        self.assertEqual(get_running_grid_cycle(self.db).id, cycle.id)

    # -------------------------------------------------------------
    # 3. 参数冻结特性测试
    # -------------------------------------------------------------
    def test_parameter_freezing(self):
        """测试 RUNNING 之后即使系统策略默认参数发生改变，实际参数完全不改变"""
        suggested = {
            "base_price": 25000.0,
            "upper_price": 30000.0,
            "lower_price": 20000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle_id=cycle.id,
            actual_base_price=25100.0,
            actual_upper_price=30200.0,
            actual_lower_price=20100.0,
            actual_grid_count=200,
            actual_leverage=5.0,
            actual_margin=5000.0
        )

        # 模拟外部管理员或系统修改了策略默认参数
        changed_config = Config({
            "default_grid_upper_pct": 0.10,  # 改成了 +10%
            "default_grid_lower_pct": 0.30,  # 改成了 -30%
            "default_grid_count": 500,       # 改成了 500格
            "default_grid_leverage": 2.0     # 改成了 2x
        })

        # 重新从数据库拉取周期记录
        fresh_cycle = self.db.query(GridCycle).filter(GridCycle.id == cycle.id).first()
        self.assertEqual(fresh_cycle.actual_base_price, 25100.0)
        self.assertEqual(fresh_cycle.actual_upper_price, 30200.0)
        self.assertEqual(fresh_cycle.actual_lower_price, 20100.0)
        self.assertEqual(fresh_cycle.actual_grid_count, 200)
        self.assertEqual(fresh_cycle.actual_leverage, 5.0)
        self.assertEqual(fresh_cycle.actual_margin, 5000.0)

    # -------------------------------------------------------------
    # 4. 正常状态转换测试
    # -------------------------------------------------------------
    def test_valid_state_transitions(self):
        """测试合法状态转换: WAITING -> RUNNING -> CLOSED 和 WAITING -> RUNNING -> STOPPED"""
        suggested = {
            "base_price": 25000.0, "upper_price": 30000.0, "lower_price": 20000.0,
            "grid_count": 200, "leverage": 5.0
        }

        # 链路 1: WAITING -> RUNNING -> CLOSED
        c1 = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(self.db, c1.id, 25000.0, 30000.0, 20000.0, 200, 5.0)
        closed_c1 = close_grid_cycle(self.db, c1.id, reason="UPPER_REACHED")
        self.assertEqual(closed_c1.status, "CLOSED")
        self.assertEqual(closed_c1.close_reason, "UPPER_REACHED")
        self.assertIsNotNone(closed_c1.closed_at)

        # 链路 2: WAITING -> RUNNING -> STOPPED
        c2 = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(self.db, c2.id, 25000.0, 30000.0, 20000.0, 200, 5.0)
        stopped_c2 = stop_grid_cycle(self.db, c2.id, reason="LOWER_BREACHED")
        self.assertEqual(stopped_c2.status, "STOPPED")
        self.assertEqual(stopped_c2.close_reason, "LOWER_BREACHED")
        self.assertIsNotNone(stopped_c2.closed_at)

    # -------------------------------------------------------------
    # 5. 非法状态转换测试
    # -------------------------------------------------------------
    def test_illegal_state_transitions(self):
        """测试所有非法状态转换必须抛出 ValueError"""
        suggested = {
            "base_price": 25000.0, "upper_price": 30000.0, "lower_price": 20000.0,
            "grid_count": 200, "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)

        # 1. WAITING 不能直接 CLOSED
        with self.assertRaises(ValueError):
            close_grid_cycle(self.db, cycle.id, reason="TEST")

        # 2. WAITING 不能直接 STOPPED
        with self.assertRaises(ValueError):
            stop_grid_cycle(self.db, cycle.id, reason="TEST")

        # 转为 RUNNING
        start_grid_cycle(self.db, cycle.id, 25000.0, 30000.0, 20000.0, 200, 5.0)

        # 3. RUNNING 不能再被 start
        with self.assertRaises(ValueError):
            start_grid_cycle(self.db, cycle.id, 25000.0, 30000.0, 20000.0, 200, 5.0)

        # 关闭周期
        close_grid_cycle(self.db, cycle.id, reason="MANUAL_CLOSE")

        # 4. CLOSED 不能再转 RUNNING
        with self.assertRaises(ValueError):
            start_grid_cycle(self.db, cycle.id, 25000.0, 30000.0, 20000.0, 200, 5.0)

        # 5. CLOSED 不能再转 STOPPED
        with self.assertRaises(ValueError):
            stop_grid_cycle(self.db, cycle.id, reason="TEST")

        # 6. CLOSED 不能再重复 close
        with self.assertRaises(ValueError):
            close_grid_cycle(self.db, cycle.id, reason="TEST")

    # -------------------------------------------------------------
    # 6. WAITING 唯一性测试
    # -------------------------------------------------------------
    def test_waiting_uniqueness(self):
        """测试已有 WAITING 周期时，禁止创建第二个 WAITING"""
        suggested = {
            "base_price": 25000.0, "upper_price": 30000.0, "lower_price": 20000.0,
            "grid_count": 200, "leverage": 5.0
        }
        create_waiting_grid_cycle(self.db, suggested)

        # 尝试创建第二个 WAITING 周期 -> 必须失败
        with self.assertRaises(ValueError) as ctx:
            create_waiting_grid_cycle(self.db, suggested)
        self.assertIn("already exists", str(ctx.exception))

    # -------------------------------------------------------------
    # 7. RUNNING 唯一性测试
    # -------------------------------------------------------------
    def test_running_uniqueness(self):
        """测试已有 RUNNING 周期时，禁止将第二个周期转为 RUNNING"""
        suggested = {
            "base_price": 25000.0, "upper_price": 30000.0, "lower_price": 20000.0,
            "grid_count": 200, "leverage": 5.0
        }
        # 创建并启动周期 A
        cycle_a = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(self.db, cycle_a.id, 25000.0, 30000.0, 20000.0, 200, 5.0)

        # 在 RUNNING A 存在时，创建 WAITING B 也会被阻断
        with self.assertRaises(ValueError):
            create_waiting_grid_cycle(self.db, suggested)

        # 手动向数据库强行插入一条假 WAITING B 记录以检验 start_grid_cycle 的双重防线
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

        # 尝试将 B 激活为 RUNNING -> 必须被严格阻断
        with self.assertRaises(ValueError) as ctx:
            start_grid_cycle(self.db, cycle_b.id, 25000.0, 30000.0, 20000.0, 200, 5.0)
        self.assertIn("already RUNNING", str(ctx.exception))

        # 将 A 关闭之后，B 才能启动
        close_grid_cycle(self.db, cycle_a.id, reason="MANUAL_CLOSE")
        started_b = start_grid_cycle(self.db, cycle_b.id, 25000.0, 30000.0, 20000.0, 200, 5.0)
        self.assertEqual(started_b.status, "RUNNING")


if __name__ == "__main__":
    unittest.main()
