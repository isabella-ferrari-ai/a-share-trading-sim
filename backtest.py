# -*- coding: utf-8 -*-
"""回测驱动：用本地面板库(data/panel.db)逐交易日跑 engine.process_day，验证策略逻辑。

无未来函数：第 D 日只用截至 D 收盘可见数据选股，T+1 开盘执行。
用法：
    python3 -W ignore backtest.py 2025-07-01 2025-08-31
默认写入 data/backtest.db（与实时盘 data/trading.db 隔离）。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import sys

os.environ.setdefault("TRADING_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest.db"))

import database as db
import data_fetcher as dfetch
import engine


def run(start, end):
    db.init_db()
    db.reset_all()
    print(f"[backtest] DB={db.DB_PATH} reset. range {start}~{end}")

    panel, names = dfetch.load_panel()
    print(f"[backtest] panel loaded: {len(panel)} stocks")
    if not panel:
        print("[backtest] empty panel — run: python3 data_fetcher.py panel <start> <end>")
        return

    # 指数
    with dfetch.bs_session():
        index_df = dfetch.get_index(start, end)
        all_dates = dfetch.get_trade_dates(start, end)

    # 仅在面板覆盖范围内的交易日
    pdates = set(dfetch.panel_dates())
    dates = [d for d in all_dates if d in pdates]
    print(f"[backtest] trade days in range with data: {len(dates)} ({dates[0]}~{dates[-1]})" if dates else "no dates")
    if not dates:
        return

    theme_map = {}
    for i, d in enumerate(dates):
        res = engine.process_day(panel, names, index_df, dates, d, theme_map=theme_map, log=True)
        s = res["sentiment"]
        print(f"{d} [{s['regime']}] 涨停{s['limit_up_count']} 跌停{s['limit_down_count']} "
              f"额{s['total_amount']}亿 | 买{len(res['buys'])} 卖{len(res['sells'])} "
              f"拒{len(res['rejected'])} 候选{len(res['candidates'])}")

    acct = db.get_account()
    eq = db.last_equity()
    sells = [t for t in db.get_trades(99999) if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    print("\n=== 回测结果 ===")
    print("现金:", round(acct["cash"], 2))
    print("持仓:", [(p["name"], p["shares"], round(p["avg_cost"], 2)) for p in db.get_positions()])
    if eq:
        print(f"总资产: {eq['total_equity']:.2f}  累计收益: {eq['cum_return']:.2f}%")
    print(f"完成交易(卖出): {len(sells)} 笔, 胜率: {len(wins)/len(sells)*100:.1f}%" if sells else "无完成交易")
    buys = [t for t in db.get_trades(99999) if t["side"] == "BUY"]
    print(f"买入: {len(buys)} 笔, 拒单: {len(db.get_rejected(99999))} 笔")


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "2025-07-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2025-08-31"
    run(s, e)
