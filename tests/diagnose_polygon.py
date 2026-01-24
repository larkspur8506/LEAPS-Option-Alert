import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from polygon import RESTClient
from datetime import datetime, timedelta
from pytz import timezone

et_tz = timezone("America/New_York")

def diagnose_polygon_api():
    """诊断 Polygon.io API 数据"""
    api_key = os.getenv("POLYGON_API_KEY", "")

    if not api_key:
        print("❌ 未配置 POLYGON_API_KEY")
        return

    print("=" * 60)
    print("🔍 Polygon.io API 诊断")
    print("=" * 60)

    print(f"\nAPI Key: {api_key[:8]}...{api_key[-4:]}")
    print(f"当前时间: {datetime.now(et_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    client = RESTClient(api_key)

    # 诊断 1：获取最近 5 天的数据
    print("\n【诊断 1】获取最近 5 个交易日的日线数据")
    end_date = datetime.now(et_tz).strftime("%Y-%m-%d")
    start_date = (datetime.now(et_tz) - timedelta(days=10)).strftime("%Y-%m-%d")

    print(f"  查询范围: {start_date} 到 {end_date}")

    try:
        aggs = client.get_aggs("QQQ", 1, "day", start_date, end_date, limit=5)

        print(f"  返回数据: {len(aggs)} 条")

        if aggs:
            print("  数据列表:")
            for i, agg in enumerate(aggs):
                dt = datetime.fromtimestamp(agg.timestamp / 1000, et_tz)
                print(f"    {i+1}. 日期: {dt.strftime('%Y-%m-%d (%A)')}")
                print(f"       开盘: ${agg.open:.2f}")
                print(f"       最高: ${agg.high:.2f}")
                print(f"       最低: ${agg.low:.2f}")
                print(f"       收盘: ${agg.close:.2f}")
                print()
        else:
            print("  ❌ 未获取到数据")
    except Exception as e:
        print(f"  ❌ 错误: {e}")

    # 诊断 2：获取最近 2 天的数据
    print("\n【诊断 2】获取最近 2 个交易日的日线数据")
    start_date = (datetime.now(et_tz) - timedelta(days=5)).strftime("%Y-%m-%d")

    print(f"  查询范围: {start_date} 到 {end_date}")

    try:
        aggs = client.get_aggs("QQQ", 1, "day", start_date, end_date, limit=2)

        print(f"  返回数据: {len(aggs)} 条")

        if aggs:
            print("  数据列表:")
            for i, agg in enumerate(aggs):
                dt = datetime.fromtimestamp(agg.timestamp / 1000, et_tz)
                print(f"    {i+1}. 日期: {dt.strftime('%Y-%m-%d (%A)')}")
                print(f"       收盘: ${agg.close:.2f}")
                print()
        else:
            print("  ❌ 未获取到数据")
    except Exception as e:
        print(f"  ❌ 错误: {e}")

    # 诊断 3：检查今天是否有数据
    print("\n【诊断 3】检查今天是否有数据")
    today = datetime.now(et_tz).strftime("%Y-%m-%d")

    try:
        aggs = client.get_aggs("QQQ", 1, "day", today, today, limit=1)

        print(f"  日期: {today}")
        print(f"  返回数据: {len(aggs)} 条")

        if aggs:
            dt = datetime.fromtimestamp(aggs[0].timestamp / 1000, et_tz)
            print(f"   日期: {dt.strftime('%Y-%m-%d (%A)')}")
            print(f"  收盘: ${aggs[0].close:.2f}")
        else:
            print(f"  ℹ️  今天没有数据（可能是周末或节假日）")
    except Exception as e:
        print(f"  ❌ 错误: {e}")

    print("\n" + "=" * 60)
    print("✅ 诊断完成")
    print("=" * 60)


if __name__ == "__main__":
    diagnose_polygon_api()
