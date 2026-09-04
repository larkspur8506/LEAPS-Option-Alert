import unittest
import math
import pandas as pd
import numpy as np
from datetime import datetime, date

from app.alerts.grid_math import (
    calculate_grid_parameters,
    calculate_grid_nodes,
    calculate_theoretical_grid_position
)
from app.alerts.ndx_rules import (
    check_ndx_entry_conditions,
    generate_suggested_grid,
    check_entry_signals
)
from app.market.data_fetcher import DataFetcher
from app.config import Config


class TestNDXPhase1(unittest.TestCase):
    # -------------------------------------------------------------
    # 1. 网格核心参数与步长计算测试
    # -------------------------------------------------------------
    def test_grid_parameters_default(self):
        """测试基准点位 25000 下的默认网格计算"""
        params = calculate_grid_parameters(base_price=25000.0)
        
        self.assertEqual(params["base_price"], 25000.0)
        self.assertEqual(params["upper_price"], 30000.0)  # 25000 * 1.20
        self.assertEqual(params["lower_price"], 20000.0)  # 25000 * 0.80
        self.assertEqual(params["grid_count"], 200)
        self.assertEqual(params["total_nodes"], 201)       # 200 个区间对应 201 个节点
        self.assertEqual(params["grid_step"], 50.0)        # (30000 - 20000) / 200 = 50.0
        self.assertEqual(params["leverage"], 5.0)

    def test_grid_nodes_generation(self):
        """测试 200 格对应 201 个价格节点及单调性"""
        nodes = calculate_grid_nodes(lower_price=20000.0, upper_price=30000.0, grid_count=200)
        
        self.assertEqual(len(nodes), 201)
        self.assertEqual(nodes[0], 20000.0)
        self.assertEqual(nodes[-1], 30000.0)
        self.assertAlmostEqual(nodes[1], 20050.0)
        self.assertAlmostEqual(nodes[100], 25000.0)
        
        # 验证严格单调递增
        for i in range(len(nodes) - 1):
            self.assertLess(nodes[i], nodes[i + 1])

    def test_grid_invalid_inputs(self):
        """测试网格参数异常输入边界处理 (包括 NaN, Inf, 负数, 零, 类型等)"""
        # base_price
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=-100)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=0)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=float('nan'))
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=float('inf'))

        # grid_count
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, grid_count=0)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, grid_count=-10)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, grid_count=200.5)  # non-int

        # upper_pct / lower_pct
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, upper_pct=-0.1)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, upper_pct=float('nan'))
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, lower_pct=1.0)     # lower_pct >= 1
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, lower_pct=float('inf'))

        # leverage
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, leverage=0)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, leverage=-5)
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, leverage=float('nan'))
        with self.assertRaises(ValueError):
            calculate_grid_parameters(base_price=25000, leverage=float('inf'))

        # nodes
        with self.assertRaises(ValueError):
            calculate_grid_nodes(lower_price=30000, upper_price=20000, grid_count=200)
        with self.assertRaises(ValueError):
            calculate_grid_nodes(lower_price=float('nan'), upper_price=30000, grid_count=200)

    # -------------------------------------------------------------
    # 2. 理论网格位置算法测试
    # -------------------------------------------------------------
    def test_theoretical_grid_position_in_range(self):
        """测试区间内各点位的理论网格定位与准确性"""
        # lower=20000, upper=30000, step=50, count=200
        # 1. 正中心 25000: (25000 - 20000) / 50 = 100. 落在第 101 格
        pos_mid = calculate_theoretical_grid_position(25000, 20000, 30000, 200)
        self.assertEqual(pos_mid["grid_interval_index"], 101)
        self.assertTrue(pos_mid["is_in_range"])
        self.assertEqual(pos_mid["status"], "IN_RANGE")

        # 2. 略高于 lower: 20025 -> (25/50)=0 -> 第 1 格 [20000, 20050)
        pos_low = calculate_theoretical_grid_position(20025, 20000, 30000, 200)
        self.assertEqual(pos_low["grid_interval_index"], 1)
        self.assertTrue(pos_low["is_in_range"])

        # 3. 第 2 个区间的下边界点: current = lower + step = 20050 -> 应该落在第 2 格 [20050, 20100)
        pos_step2 = calculate_theoretical_grid_position(20050, 20000, 30000, 200)
        self.assertEqual(pos_step2["grid_interval_index"], 2)
        self.assertTrue(pos_step2["is_in_range"])

        # 4. 略低于 upper: 29975 -> 应该落在最后一格（第 200 格）
        pos_high = calculate_theoretical_grid_position(29975, 20000, 30000, 200)
        self.assertEqual(pos_high["grid_interval_index"], 200)
        self.assertTrue(pos_high["is_in_range"])

        # 5. upper - step: 29950 -> 第 200 格
        pos_upper_step = calculate_theoretical_grid_position(29950, 20000, 30000, 200)
        self.assertEqual(pos_upper_step["grid_interval_index"], 200)
        self.assertTrue(pos_upper_step["is_in_range"])

    def test_theoretical_grid_position_boundaries(self):
        """测试边界点 (等于 lower, 等于 upper, 低于 lower, 高于 upper, NaN/Inf)"""
        # 1. 刚好等于下限: current = lower
        pos_at_lower = calculate_theoretical_grid_position(20000, 20000, 30000, 200)
        self.assertEqual(pos_at_lower["status"], "AT_LOWER")
        self.assertEqual(pos_at_lower["grid_interval_index"], 1)
        self.assertTrue(pos_at_lower["is_in_range"])

        # 2. 刚好等于上限: current = upper
        pos_at_upper = calculate_theoretical_grid_position(30000, 20000, 30000, 200)
        self.assertEqual(pos_at_upper["status"], "AT_UPPER")
        self.assertEqual(pos_at_upper["grid_interval_index"], 200)
        self.assertTrue(pos_at_upper["is_in_range"])

        # 3. 跌破下限
        pos_below = calculate_theoretical_grid_position(19950, 20000, 30000, 200)
        self.assertEqual(pos_below["status"], "BELOW_LOWER")
        self.assertEqual(pos_below["grid_interval_index"], 0)
        self.assertFalse(pos_below["is_in_range"])

        # 4. 突破上限
        pos_above = calculate_theoretical_grid_position(30050, 20000, 30000, 200)
        self.assertEqual(pos_above["status"], "ABOVE_UPPER")
        self.assertEqual(pos_above["grid_interval_index"], 201)
        self.assertFalse(pos_above["is_in_range"])

        # 5. 非法值
        with self.assertRaises(ValueError):
            calculate_theoretical_grid_position(float('nan'), 20000, 30000, 200)
        with self.assertRaises(ValueError):
            calculate_theoretical_grid_position(25000, 20000, 30000, 0)

    # -------------------------------------------------------------
    # 3. 技术指标与一年前价格计算测试
    # -------------------------------------------------------------
    def test_indicators_calculation_with_sufficient_data(self):
        """测试数据充足 (300条K线) 时的 SMA200, RSI, 一年前价格"""
        dates = pd.date_range("2024-01-01", periods=300, freq="B")
        prices = [20000.0 + i * 15.0 for i in range(300)]
        df = pd.DataFrame({
            "Close": prices,
            "High": [p + 20 for p in prices],
            "Low": [p - 20 for p in prices],
            "Volume": 1000000
        }, index=dates)

        res = DataFetcher.calculate_technical_indicators(df, ticker="^NDX")
        
        self.assertTrue(res["is_data_valid"])
        self.assertIsNotNone(res["ma200"])
        self.assertIsNotNone(res["rsi"])
        self.assertTrue(res["is_above_sma200_3d"])
        
        # 300天历史 >= 253天，一年前价格必须可用
        self.assertTrue(res["price_1y_ago_available"])
        self.assertIsNotNone(res["price_1y_ago"])
        # 倒数第253天的价格索引为 300 - 253 = 47
        self.assertAlmostEqual(res["price_1y_ago"], prices[47])

    def test_indicators_data_cleaning_and_deduplication(self):
        """测试含有重复日期与 NaN Close 时的健壮性清洗"""
        dates = pd.date_range("2024-01-01", periods=300, freq="B")
        prices = [20000.0 + i * 10.0 for i in range(300)]
        df = pd.DataFrame({"Close": prices, "High": prices, "Low": prices}, index=dates)
        
        # 插入重复行和带 NaN 的行
        dup_row = pd.DataFrame({"Close": [25000.0], "High": [25000.0], "Low": [25000.0]}, index=[dates[-1]])
        nan_row = pd.DataFrame({"Close": [np.nan], "High": [25000.0], "Low": [25000.0]}, index=[pd.Timestamp("2025-06-01")])
        dirty_df = pd.concat([df, dup_row, nan_row])

        res = DataFetcher.calculate_technical_indicators(dirty_df, ticker="^NDX")
        self.assertTrue(res["is_data_valid"])
        self.assertEqual(res["total_records"], 300)

    def test_indicators_insufficient_data(self):
        """测试数据长度不足 200 天和不足 253 天的边界"""
        # 1. 只有 150 条K线
        dates = pd.date_range("2024-01-01", periods=150, freq="B")
        df_short = pd.DataFrame({"Close": range(150), "High": range(150), "Low": range(150)}, index=dates)
        res_short = DataFetcher.calculate_technical_indicators(df_short)
        
        self.assertFalse(res_short["is_data_valid"])
        self.assertIsNone(res_short["ma200"])
        self.assertFalse(res_short["price_1y_ago_available"])
        self.assertIsNone(res_short["price_1y_ago"])

        # 2. 有 220 条K线 (足以计算 SMA200，但不足以计算 252天前的一年前价格)
        dates_220 = pd.date_range("2024-01-01", periods=220, freq="B")
        df_220 = pd.DataFrame({"Close": [100.0] * 220, "High": [105.0] * 220, "Low": [95.0] * 220}, index=dates_220)
        res_220 = DataFetcher.calculate_technical_indicators(df_220)
        
        self.assertTrue(res_220["is_data_valid"])
        self.assertIsNotNone(res_220["ma200"])
        # 一年前价格应明确为不可用，绝不抛出 IndexError
        self.assertFalse(res_220["price_1y_ago_available"])
        self.assertIsNone(res_220["price_1y_ago"])

    def test_indicators_empty_dataframe(self):
        """测试空 DataFrame 防御性处理"""
        empty_df = pd.DataFrame()
        res = DataFetcher.calculate_technical_indicators(empty_df)
        self.assertFalse(res["is_data_valid"])
        self.assertIsNone(res["last_price"])

    # -------------------------------------------------------------
    # 4. NDX 开仓信号判定测试
    # -------------------------------------------------------------
    def test_entry_signal_all_conditions_met(self):
        """场景 1：三个条件全部满足 -> 触发信号，生成建议网格"""
        current_price = 25000.0
        indicators = {
            "rsi": 32.5,                # < 35
            "is_above_sma200_3d": True, # 连续3天 > SMA200
            "price_1y_ago": 21000.0     # 现价高于1年前
        }
        
        # 1. 纯布尔函数校验
        self.assertTrue(check_ndx_entry_conditions(current_price, indicators))
        
        # 2. 信号列表生成校验
        alerts = check_entry_signals(current_price, indicators)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        
        self.assertEqual(alert["alert_type"], "NDX_GRID_ENTRY")
        self.assertEqual(alert["ticker"], "^NDX")
        self.assertIn("suggested_grid", alert)
        
        grid = alert["suggested_grid"]
        self.assertEqual(grid["base_price"], 25000.0)
        self.assertEqual(grid["upper_price"], 30000.0)
        self.assertEqual(grid["lower_price"], 20000.0)
        self.assertEqual(grid["grid_count"], 200)
        self.assertEqual(grid["grid_step"], 50.0)
        self.assertEqual(grid["leverage"], 5.0)

        # 确保彻底剥离了期权字段
        self.assertNotIn("delta_recommendation", alert)
        self.assertNotIn("option_type", alert)
        self.assertNotIn("strike_price", alert)

    def test_entry_signal_with_custom_config(self):
        """验证 Config 配置覆盖真正生效"""
        custom_config = Config({
            "rsi_threshold": 40.0,
            "default_grid_upper_pct": 0.15,
            "default_grid_lower_pct": 0.25,
            "default_grid_count": 100,
            "default_grid_leverage": 3.0
        })

        current_price = 20000.0
        indicators = {
            "rsi": 38.0,  # 介于 35 到 40 之间，默认配置不触发，自定义配置触发
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0
        }

        alerts = check_entry_signals(current_price, indicators, config=custom_config)
        self.assertEqual(len(alerts), 1)
        grid = alerts[0]["suggested_grid"]
        self.assertEqual(grid["upper_price"], 23000.0)  # 20000 * 1.15
        self.assertEqual(grid["lower_price"], 15000.0)  # 20000 * 0.75
        self.assertEqual(grid["grid_count"], 100)
        self.assertEqual(grid["leverage"], 3.0)

    def test_entry_signal_rsi_not_met(self):
        """场景 2：RSI >= 35 -> 不触发"""
        indicators = {
            "rsi": 35.0,  # 未跌破
            "is_above_sma200_3d": True,
            "price_1y_ago": 21000.0
        }
        self.assertFalse(check_ndx_entry_conditions(25000.0, indicators))
        alerts = check_entry_signals(25000.0, indicators)
        self.assertEqual(len(alerts), 0)

    def test_entry_signal_sma200_not_met(self):
        """场景 3：未连续3天站上 SMA200 -> 不触发"""
        indicators = {
            "rsi": 30.0,
            "is_above_sma200_3d": False,
            "price_1y_ago": 21000.0
        }
        self.assertFalse(check_ndx_entry_conditions(25000.0, indicators))
        alerts = check_entry_signals(25000.0, indicators)
        self.assertEqual(len(alerts), 0)

    def test_entry_signal_price_1y_not_met(self):
        """场景 4：当前价格 <= 一年前价格 -> 不触发"""
        # 1. 现价低于一年前
        indicators_lower = {
            "rsi": 30.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 26000.0  # 比现价25000高
        }
        self.assertFalse(check_ndx_entry_conditions(25000.0, indicators_lower))
        
        # 2. 现价等于一年前
        indicators_equal = {
            "rsi": 30.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 25000.0
        }
        self.assertFalse(check_ndx_entry_conditions(25000.0, indicators_equal))

        # 3. 一年前价格缺失 (None)
        indicators_none = {
            "rsi": 30.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": None
        }
        self.assertFalse(check_ndx_entry_conditions(25000.0, indicators_none))


if __name__ == "__main__":
    unittest.main()
