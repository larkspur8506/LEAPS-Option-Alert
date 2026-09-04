"""
Grid Core Algorithms for Nasdaq-100 (NDX) Long Grid Trading System.
Pure functions without side effects.
"""
from typing import Dict, List, Any
import math


def calculate_grid_parameters(
    base_price: float,
    upper_pct: float = 0.20,
    lower_pct: float = 0.20,
    grid_count: int = 200,
    leverage: float = 5.0
) -> Dict[str, Any]:
    """
    计算做多算术网格核心建议参数。
    
    参数约束:
        - base_price: 必须为有限正数 (> 0, 非 NaN / 非 inf)
        - upper_pct: 必须为有限正数 (> 0, 非 NaN / 非 inf)
        - lower_pct: 必须为有限正数 (0 < lower_pct < 1.0, 非 NaN / 非 inf)
        - grid_count: 必须为正整数 (>= 1)
        - leverage: 必须为有限正数 (> 0, 非 NaN / 非 inf)
        
    精度规则:
        - upper_price, lower_price 严格保留 2 位小数
        - grid_step 保留更高精度浮点数
    """
    if base_price is None or not isinstance(base_price, (int, float)) or math.isnan(base_price) or math.isinf(base_price) or base_price <= 0:
        raise ValueError(f"Invalid base_price: {base_price}. Must be a finite positive number.")
    
    if grid_count is None or not isinstance(grid_count, int) or grid_count <= 0:
        raise ValueError(f"Invalid grid_count: {grid_count}. Must be a positive integer.")
        
    if upper_pct is None or not isinstance(upper_pct, (int, float)) or math.isnan(upper_pct) or math.isinf(upper_pct) or upper_pct <= 0:
        raise ValueError(f"Invalid upper_pct: {upper_pct}. Must be a finite positive number.")

    if lower_pct is None or not isinstance(lower_pct, (int, float)) or math.isnan(lower_pct) or math.isinf(lower_pct) or lower_pct <= 0 or lower_pct >= 1.0:
        raise ValueError(f"Invalid lower_pct: {lower_pct}. Must be a finite positive number < 1.0.")

    if leverage is None or not isinstance(leverage, (int, float)) or math.isnan(leverage) or math.isinf(leverage) or leverage <= 0:
        raise ValueError(f"Invalid leverage: {leverage}. Must be a finite positive number.")

    upper_price = round(float(base_price) * (1.0 + float(upper_pct)), 2)
    lower_price = round(float(base_price) * (1.0 - float(lower_pct)), 2)
    
    if upper_price <= lower_price:
        raise ValueError(f"upper_price ({upper_price}) must be strictly greater than lower_price ({lower_price}).")
        
    grid_step = (upper_price - lower_price) / float(grid_count)
    
    return {
        "base_price": round(float(base_price), 2),
        "upper_price": upper_price,
        "lower_price": lower_price,
        "upper_pct": float(upper_pct),
        "lower_pct": float(lower_pct),
        "grid_count": grid_count,
        "total_nodes": grid_count + 1,  # 200格对应201个价格节点
        "grid_step": grid_step,         # 保留高精度浮点数
        "leverage": float(leverage)
    }


def calculate_grid_nodes(
    lower_price: float,
    upper_price: float,
    grid_count: int = 200
) -> List[float]:
    """
    计算算术网格的所有价格节点。
    
    要求:
        - len(nodes) == grid_count + 1
        - nodes[0] 严格等于 lower_price
        - nodes[-1] 严格等于 upper_price
        - 所有节点严格单调递增
    """
    if lower_price is None or not isinstance(lower_price, (int, float)) or math.isnan(lower_price) or math.isinf(lower_price):
        raise ValueError(f"Invalid lower_price: {lower_price}. Must be a finite number.")
    if upper_price is None or not isinstance(upper_price, (int, float)) or math.isnan(upper_price) or math.isinf(upper_price):
        raise ValueError(f"Invalid upper_price: {upper_price}. Must be a finite number.")
    if upper_price <= lower_price:
        raise ValueError(f"upper_price ({upper_price}) must be > lower_price ({lower_price}).")
    if grid_count is None or not isinstance(grid_count, int) or grid_count <= 0:
        raise ValueError(f"Invalid grid_count: {grid_count}. Must be a positive integer.")

    step = (float(upper_price) - float(lower_price)) / float(grid_count)
    nodes = [float(lower_price) + i * step for i in range(grid_count + 1)]
    # 首尾严格对齐，彻底消除浮点累加误差
    nodes[0] = float(lower_price)
    nodes[-1] = float(upper_price)
    return nodes


def calculate_theoretical_grid_position(
    current_price: float,
    lower_price: float,
    upper_price: float,
    grid_count: int = 200
) -> Dict[str, Any]:
    """
    计算当前价格的理论网格位置 (仅供界面与提醒展示，不代表真实成交或真实持仓)。
    
    区间与节点语义规范:
        - grid_count = 200 表示 200 个区间
        - 对应价格节点为 node_0, node_1, ..., node_200 (共 201 个节点)
        - 第 1 格: [node_0, node_1)
        - 第 200 格: (node_199, node_200]
        - grid_interval_index 为 1-based 的区间编号:
            * current < lower_price: 0 (BELOW_LOWER)
            * current == lower_price: 1 (AT_LOWER, 属于第 1 格下边界)
            * lower_price < current < upper_price: 1 ~ grid_count (IN_RANGE)
            * current == upper_price: grid_count (AT_UPPER, 属于最后一格上边界)
            * current > upper_price: grid_count + 1 (ABOVE_UPPER)
    """
    if current_price is None or not isinstance(current_price, (int, float)) or math.isnan(current_price) or math.isinf(current_price):
        raise ValueError(f"Invalid current_price: {current_price}. Must be a finite number.")
    if lower_price is None or not isinstance(lower_price, (int, float)) or math.isnan(lower_price) or math.isinf(lower_price):
        raise ValueError(f"Invalid lower_price: {lower_price}. Must be a finite number.")
    if upper_price is None or not isinstance(upper_price, (int, float)) or math.isnan(upper_price) or math.isinf(upper_price):
        raise ValueError(f"Invalid upper_price: {upper_price}. Must be a finite number.")
    if upper_price <= lower_price:
        raise ValueError(f"upper_price ({upper_price}) must be > lower_price ({lower_price}).")
    if grid_count is None or not isinstance(grid_count, int) or grid_count <= 0:
        raise ValueError(f"Invalid grid_count: {grid_count}. Must be a positive integer.")

    span = float(upper_price) - float(lower_price)
    step = span / float(grid_count)
    ratio = (float(current_price) - float(lower_price)) / span

    if current_price < lower_price:
        return {
            "current_price": round(float(current_price), 2),
            "grid_interval_index": 0,
            "grid_count": grid_count,
            "position_ratio": round(ratio, 4),
            "is_in_range": False,
            "status": "BELOW_LOWER",
            "description": "低于网格下限"
        }
    elif current_price == lower_price:
        return {
            "current_price": round(float(current_price), 2),
            "grid_interval_index": 1,
            "grid_count": grid_count,
            "position_ratio": 0.0,
            "is_in_range": True,
            "status": "AT_LOWER",
            "description": "触及网格下限"
        }
    elif current_price > upper_price:
        return {
            "current_price": round(float(current_price), 2),
            "grid_interval_index": grid_count + 1,
            "grid_count": grid_count,
            "position_ratio": round(ratio, 4),
            "is_in_range": False,
            "status": "ABOVE_UPPER",
            "description": "高于网格上限"
        }
    elif current_price == upper_price:
        return {
            "current_price": round(float(current_price), 2),
            "grid_interval_index": grid_count,
            "grid_count": grid_count,
            "position_ratio": 1.0,
            "is_in_range": True,
            "status": "AT_UPPER",
            "description": "触及网格上限"
        }
    else:
        # 严格区间定位: 对于落在内部的点，使用 floor 计算其 1-based 区间
        interval_index = math.floor((float(current_price) - float(lower_price)) / step) + 1
        # 防浮点误差越界 (极贴近 upper_price 时)
        interval_index = min(max(1, interval_index), grid_count)
        return {
            "current_price": round(float(current_price), 2),
            "grid_interval_index": interval_index,
            "grid_count": grid_count,
            "position_ratio": round(ratio, 4),
            "is_in_range": True,
            "status": "IN_RANGE",
            "description": f"第 {interval_index} / {grid_count} 格"
        }
