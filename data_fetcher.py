# -*- coding: utf-8 -*-
"""数据获取层：基于 baostock 的 A 股日线 / 指数数据，以及股票池定义。

baostock 完全免费、免注册，比 akshare 的 stock_zh_a_hist 更稳定。
只提供日线级别数据，因此竞价/封单等盘中细节由 strategy 层用日线近似。
"""
import warnings
warnings.filterwarnings("ignore")

import baostock as bs
import pandas as pd
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# 股票池：聚焦策略重点主题（物理AI/具身智能、机器人、半导体设备、并购重组、低空经济）
# code 使用 baostock 格式（sh./sz.），limit 为当日涨跌停幅度阈值
# ---------------------------------------------------------------------------
WATCHLIST = [
    # 具身智能 / 物理AI
    {"code": "sz.002421", "name": "达实智能",   "theme": "具身智能",   "limit": 0.10},
    {"code": "sh.603859", "name": "能科科技",   "theme": "具身智能",   "limit": 0.10},
    {"code": "sz.300024", "name": "机器人",     "theme": "机器人",     "limit": 0.20},
    # 机器人产业链
    {"code": "sz.002527", "name": "新时达",     "theme": "机器人",     "limit": 0.10},
    {"code": "sz.300607", "name": "拓斯达",     "theme": "机器人",     "limit": 0.20},
    {"code": "sz.002896", "name": "中大力德",   "theme": "机器人",     "limit": 0.10},
    {"code": "sz.002009", "name": "天奇股份",   "theme": "机器人",     "limit": 0.10},
    {"code": "sz.300451", "name": "创业慧康",   "theme": "机器人",     "limit": 0.20},
    # 半导体设备
    {"code": "sh.688082", "name": "盛美上海",   "theme": "半导体设备", "limit": 0.20},
    {"code": "sh.688012", "name": "中微公司",   "theme": "半导体设备", "limit": 0.20},
    {"code": "sz.002371", "name": "北方华创",   "theme": "半导体设备", "limit": 0.10},
    {"code": "sh.688200", "name": "华峰测控",   "theme": "半导体设备", "limit": 0.20},
    {"code": "sh.688126", "name": "沪硅产业",   "theme": "半导体设备", "limit": 0.20},
    # 低空经济
    {"code": "sz.002025", "name": "航天电器",   "theme": "低空经济",   "limit": 0.10},
    {"code": "sh.600523", "name": "贵航股份",   "theme": "低空经济",   "limit": 0.10},
    {"code": "sh.600391", "name": "航发科技",   "theme": "低空经济",   "limit": 0.10},
    # 并购重组 / 题材
    {"code": "sz.000657", "name": "中钨高新",   "theme": "并购重组",   "limit": 0.10},
    {"code": "sh.600ythu", "name": "_placeholder", "theme": "并购重组", "limit": 0.10},
]
# 清理占位
WATCHLIST = [w for w in WATCHLIST if not w["name"].startswith("_")]

INDEX_CODE = "sh.000001"   # 上证指数，用于判断大盘当日是否低开
KCB50_CODE = "sh.000688"   # 科创50（备用）

FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg"


@contextmanager
def bs_session():
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        yield
    finally:
        bs.logout()


def _query(code, start, end, freq="d", adjust="2"):
    rs = bs.query_history_k_data_plus(
        code, FIELDS, start_date=start, end_date=end, frequency=freq, adjustflag=adjust
    )
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=FIELDS.split(","))
    df = pd.DataFrame(rows, columns=rs.fields)
    num_cols = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


def get_bars(code, start, end):
    """获取单只股票日线（不复权 adjust=2 -> 前复权? baostock: 1=后复权,2=前复权,3=不复权）。
    短线模拟用前复权(2)以保持价格连续。"""
    return _query(code, start, end, adjust="2")


def get_index(start, end, code=INDEX_CODE):
    return _query(code, start, end, adjust="3")


def get_trade_dates(start, end):
    rs = bs.query_trade_dates(start_date=start, end_date=end)
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        return []
    return df[df["is_trading_day"] == "1"]["calendar_date"].tolist()


def fetch_all(start, end):
    """一次性拉取股票池 + 指数所有日线，返回 (bars_dict, index_df, trade_dates)。"""
    with bs_session():
        bars = {}
        for w in WATCHLIST:
            df = get_bars(w["code"], start, end)
            if not df.empty:
                bars[w["code"]] = df
        index_df = get_index(start, end)
        dates = get_trade_dates(start, end)
    return bars, index_df, dates


if __name__ == "__main__":
    bars, idx, dates = fetch_all("2026-06-01", "2026-06-18")
    print("trade_dates:", dates)
    print("stocks fetched:", len(bars))
    for code, df in bars.items():
        print(code, len(df), "rows, last close", df["close"].iloc[-1])
    print("index rows:", len(idx))
