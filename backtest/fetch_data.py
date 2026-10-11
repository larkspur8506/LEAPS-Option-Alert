#!/usr/bin/env python3
"""重新下载回测数据 (FRED 公开序列, 免 key)。用法:

    python3 fetch_data.py            # 下载到 ./data/
    python3 fetch_data.py --check    # 只检查本地数据是否为最新

序列:
  NASDAQ100 纳斯达克100 指数 (1986→)     —— 标的 (QQQ 代理)
  VXNCLS    纳指隐含波动率指数 (2001-02→) —— 期权 IV
  DGS3MO    3 个月国库券收益率 (1981→)    —— 无风险利率
"""
import argparse
import csv
import os
import sys
import urllib.request
from datetime import date

SERIES = {
    "NASDAQ100": "NASDAQ100",
    "VXNCLS": "VXNCLS",
    "DGS3MO": "DGS3MO",
}
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")


def fetch(sid: str) -> str:
    req = urllib.request.Request(URL.format(sid=sid), headers={"User-Agent": "leaps-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def last_date(text: str) -> str:
    rows = [r for r in csv.reader(text.splitlines()) if r and r[0] and r[0][0].isdigit()]
    return rows[-1][0] if rows else "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对本地与远端最后日期, 不写文件")
    args = ap.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    rc = 0
    for sid, name in SERIES.items():
        path = os.path.join(DATA_DIR, f"{name}.csv")
        try:
            text = fetch(sid)
        except Exception as e:
            print(f"[ERR] {name}: 下载失败 {type(e).__name__}: {e}")
            rc = 1
            continue
        remote_last = last_date(text)
        local_last = "?"
        if os.path.exists(path):
            with open(path) as f:
                local_last = last_date(f.read())
        if args.check:
            flag = "OK" if local_last == remote_last else "STALE"
            print(f"[{flag}] {name}: 本地 {local_last} / 远端 {remote_last}")
            if flag == "STALE":
                rc = 1
            continue
        with open(path, "w") as f:
            f.write(text)
        print(f"[ok] {name}.csv 写入 {len(text.splitlines()) - 1} 行, 最后 {remote_last}")
    if not args.check:
        print(f"\n完成 ({date.today()})。回测: python3 leaps_engine.py / leaps_sweep1.py ...")
    return rc


if __name__ == "__main__":
    sys.exit(main())
