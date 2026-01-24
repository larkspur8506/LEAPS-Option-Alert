from polygon import RESTClient
from datetime import datetime, timedelta
from pytz import timezone
import os
from dotenv import load_dotenv

load_dotenv()

et_tz = timezone("America/New_York")

print("=" * 60)
print("🔍 Polygon.io 数据诊断")
print("=" * 60)

api_key = os.getenv("POLYGON_API_KEY", "")
print(f"\nAPI Key: {api_key[:8]}...{api_key[-4:]}")
print(f"当前时间: {datetime.now(et_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")

client = RESTClient(api_key)

# 获取最近 5 天的数据
print("\n【诊断 1】最近 5 个交易日")
end_date = datetime.now(et_tz).strftime("%Y-%m-%d")
start_date = (datetime.now(et_tz) - timedelta(days=10)).strftime("%Y-%m-%d")

try:
    aggs = client.get_aggs("QQQ", 1, "day", start_date, end_date, limit=10)
    
    print(f"查询范围: {start_date} 到 {end_date}")
    print(f"返回数据: {len(aggs)} 条")
    
    if aggs:
        print("\n数据列表:")
        for i, agg in enumerate(aggs[-5:]):
            dt = datetime.fromtimestamp(agg.timestamp / 1000, et_tz)
            print(f"  {i+1}. 日期: {dt.strftime('%Y-%m-%d (%A)')}")
            print(f"     开盘: ${agg.open:.2f}")
            print(f"     最高: ${agg.high:.2f}")
            print(f"     最低: ${agg.low:.2f}")
            print(f"     收盘: ${agg.close:.2f}")
    else:
        print("  ❌ 未获取到数据")
except Exception as e:
    print(f"  ❌ 错误: {e}")

# 检查今天是否有数据
print("\n【诊断 2】检查今天是否有交易数据")
today = datetime.now(et_tz).strftime("%Y-%m-%d")

try:
    aggs_today = client.get_aggs("QQQ", 1, "day", today, today, limit=1)
    
    print(f"日期: {today}")
    print(f"返回数据: {len(aggs_today)} 条")
    
    if aggs_today:
        dt = datetime.fromtimestamp(aggs_today[0].timestamp / 1000, et_tz)
        print(f"  ✅ 有交易数据")
        print(f"     收盘价: ${aggs_today[0].close:.2f}")
    else:
        print(f"  ℹ️  无交易数据（可能是周末或节假日）")
except Exception as e:
    print(f"  ❌ 错误: {e}")

print("\n" + "=" * 60)
print("✅ 诊断完成")
print("=" * 60)

print("\n💡 说明:")
print("  - 免费版只能获取历史数据，无法获取当天实时数据")
print("  - 系统会使用前一天的数据作为'当前价格'")
print("  - 盘后（美国时间 23:59）会更新为当天收盘价")
print("  - 适合长期监控，不需要实时价格")
