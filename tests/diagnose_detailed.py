from polygon import RESTClient
from datetime import datetime, timedelta
from pytz import timezone
import os
from dotenv import load_dotenv

load_dotenv()

et_tz = timezone("America/New_York")

print("=" * 60)
print("🔍 详细诊断 Polygon.io API 调用")
print("=" * 60)

api_key = os.getenv("POLYGON_API_KEY", "")
print(f"\nAPI Key: {api_key[:8]}...{api_key[-4:]}")
print(f"当前时间: {datetime.now(et_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")

client = RESTClient(api_key)

today = datetime.now(et_tz).strftime("%Y-%m-%d")
yesterday = (datetime.now(et_tz) - timedelta(days=1)).strftime("%Y-%m-%d")
two_days_ago = (datetime.now(et_tz) - timedelta(days=2)).strftime("%Y-%m-%d")

print(f"\n日期信息:")
print(f"  今天: {today} ({datetime.now(et_tz).strftime('%A)')}")
print(f"  昨天: {yesterday}")
print(f"  前天: {two_days_ago}")

# 测试 1：获取昨天的数据
print("\n" + "=" * 60)
print("【测试 1】获取昨天的数据（昨日收盘价）")
print("=" * 60)

try:
    aggs = client.get_aggs("QQQ", 1, "day", yesterday, yesterday, limit=1)

    if aggs:
        print(f"✅ 成功获取 {len(aggs)} 条数据")
        print(f"  日期: {yesterday}")
        print(f"  收盘价: ${aggs[0].close:.2f}")
    else:
        print("❌ 未获取到数据")
except Exception as e:
    print(f"❌ 错误: {e}")

# 测试 2：获取最近 2 天的数据（用于"当前价格"）
print("\n" + "=" * 60)
print("【测试 2】获取最近 2 天的数据（用作当前价格）")
print("=" * 60)

try:
    start_date = two_days_ago
    end_date = today

    print(f"查询范围: {start_date} 到 {end_date}")

    aggs = client.get_aggs("QQQ", 1, "day", start_date, end_date, limit=10)

    print(f"✅ 成功获取 {len(aggs)} 条数据")

    print("\n数据列表:")
    for i, agg in enumerate(reversed(aggs)):
        dt = datetime.fromtimestamp(agg.timestamp / 1000, et_tz)
        print(f"  {i+1}. 日期: {dt.strftime('%Y-%m-%d (%A)')}")
        print(f"     收盘: ${agg.close:.2f}")
        print(f"     最高: ${agg.high:.2f}")
        print(f"     最低: ${agg.low:.2f}")

    # 使用最新的一条数据（应该是昨天的）
    if len(aggs) >= 1:
        last_agg = aggs[-1]
        dt = datetime.fromtimestamp(last_agg.timestamp / 1000, et_tz)
        print(f"\n最新数据: {dt.strftime('%Y-%m-%d (%A)')}")
        print(f"收盘价: ${last_agg.close:.2f}")
except Exception as e:
    print(f"❌ 错误: {e}")

# 测试 3：检查今天的数据
print("\n" + "=" * 60)
print("【测试 3】检查今天的数据")
print("=" * 60)

try:
    aggs_today = client.get_aggs("QQQ", 1, "day", today, today, limit=1)

    print(f"日期: {today}")

    if aggs_today:
        dt = datetime.fromtimestamp(aggs_today[0].timestamp / 1000, et_tz)
        print(f"✅ 有交易数据")
        print(f"  时间: {dt.strftime('%H:%M:%S %Z')}")
        print(f"  收盘价: ${aggs_today[0].close:.2f}")
    else:
        print(f"❌ 无交易数据（可能是周末或节假日）")
except Exception as e:
    print(f"❌ 错误: {e}")

print("\n" + "=" * 60)
print("💡 分析")
print("=" * 60)

print("\n系统逻辑:")
print("  - 昨日收盘价: 前一天（昨天）的收盘价")
print("  - 当前价格: 使用最新的一条数据（通常是昨天）")
print("  - 如果是周末，'当前价格'可能是上周五的数据")

print("\n免费版限制:")
print("  - 无法获取当天实时数据（只能获取历史数据）")
print("  - 工作日早上，'当前价格'会更新为昨天收盘价")
print("  - 系统每 5 分钟检查一次")
print("  - 适合长期监控，不适合短线交易")

print("\n" + "=" * 60)
print("✅ 诊断完成")
print("=" * 60)
