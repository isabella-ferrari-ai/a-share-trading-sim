# -*- coding: utf-8 -*-
"""种入历史模拟交易数据：从 2026-06-01 起逐个交易日跑策略，填充净值/交易/情绪。"""
import warnings
warnings.filterwarnings("ignore")

import sys
from datetime import datetime
import database as db
import data_fetcher as dfetch
import engine

START = "2026-06-22"
END = datetime.now().strftime("%Y-%m-%d")


def main(reset=True):
    db.init_db()
    if reset:
        db.reset_all()
        print("DB reset.")
    print(f"Fetching market data {START} ~ {END} ...")
    bars, index_df, dates = dfetch.fetch_all(START, END)
    print(f"Got {len(bars)} stocks, {len(dates)} trade days: {dates}")
    if not dates:
        print("No trade dates, abort.")
        return
    for d in dates:
        res = engine.process_trading_day(bars, index_df, d, intraday_log=True)
        s = res["sentiment"]
        if s is None:
            print(f"{d} 跳过（{res.get('skipped','无数据')}）")
            continue
        print(f"{d} [{s['regime']}] limitup~{s['limit_up_count']} amt~{s['total_amount']}亿 "
              f"| buy {len(res['buys'])} sell {len(res['sells'])}")
    acct = db.get_account()
    eq = db.last_equity()
    print("\n=== 结果 ===")
    print("现金:", round(acct["cash"], 2))
    print("持仓:", [(p["name"], p["shares"], p["avg_cost"]) for p in db.get_positions()])
    if eq:
        print(f"总资产: {eq['total_equity']:.2f}  累计收益: {eq['cum_return']:.2f}%")
    print("交易笔数:", len(db.get_trades(9999)))


if __name__ == "__main__":
    reset = "--no-reset" not in sys.argv
    main(reset=reset)
