# -*- coding: utf-8 -*-
"""盘中调度器：A股交易时段内每15分钟扫描一次交易信号。

市场时段：09:30-11:30, 13:00-15:00（北京时间）。
由于 baostock 仅提供日线（且当日数据收盘后才完整），盘中扫描行为如下：
- 在每个15分钟节点记录一次扫描日志（scan_log），说明当前阶段与是否有候选信号；
- 当日数据可得时（收盘后/次日），调用 engine.process_trading_day 落地真实交易；
- 这样 Dashboard 的"盘中扫描"面板能持续刷新，体现每15分钟的检查节奏。

实盘对接说明：若接入实时 L1 快照（如 akshare 实时行情），可在 _intraday_signals 中替换为
真实竞价/封板/封单判断，逻辑骨架已就绪。
"""
import warnings
warnings.filterwarnings("ignore")

import time
import json
from datetime import datetime

import database as db
import data_fetcher as dfetch
import engine
import strategy as st

SCAN_INTERVAL = 15 * 60   # 15分钟


def _now():
    return datetime.now()


def is_market_hours(now=None):
    now = now or _now()
    if now.weekday() >= 5:   # 周末
        return False, "周末休市"
    hm = now.hour * 100 + now.minute
    if 930 <= hm <= 1130:
        return True, "上午盘"
    if 1300 <= hm <= 1500:
        return True, "下午盘"
    if 915 <= hm < 930:
        return True, "集合竞价"
    return False, "非交易时段"


def _today():
    return _now().strftime("%Y-%m-%d")


def _try_settle_today(bars, index_df, trade_date):
    """若当日日线已可得（收盘后），跑一次结算落地交易。"""
    have = any((df["date"] == trade_date).any() for df in bars.values())
    if have:
        res = engine.process_trading_day(bars, index_df, trade_date, intraday_log=True)
        return res
    return None


def scan_once():
    now = _now()
    td = _today()
    in_market, phase = is_market_hours(now)
    if not in_market:
        # 非交易时段也记录一次心跳（每小时），避免日志爆炸：仅整点附近
        if now.minute < 15:
            db.log_scan("休市", f"{phase}，等待下一个交易时段", trade_date=td)
        return

    # 拉取近10天数据用于情绪与（若可得）当日结算
    start = (now.replace(day=max(1, now.day))).strftime("%Y-%m-%d")
    try:
        bars, index_df, dates = dfetch.fetch_all("2026-06-01", td)
    except Exception as e:
        db.log_scan("扫描异常", f"数据获取失败: {e}", trade_date=td)
        return

    has_today = engine._has_data_for(bars, td)

    # 当日数据是否已完整可得（收盘后 baostock 才有当日bar）
    if has_today:
        settle = _try_settle_today(bars, index_df, td)
        if settle and settle.get("sentiment"):
            sentiment = settle["sentiment"]
            msg = (f"{phase} 收盘数据已就绪，结算完成：买{len(settle['buys'])}/卖{len(settle['sells'])}，"
                   f"市场[{sentiment['regime']}]")
            db.log_scan(phase, msg, signals={"buys": settle["buys"], "sells": settle["sells"]}, trade_date=td)
            return

    # 盘中（当日数据未完整，baostock 当日盘中无数据）：基于已知持仓输出监控信号
    positions = db.get_positions()
    prev_sent = db.get_sentiment()
    regime_txt = prev_sent["regime"] if prev_sent else "待开盘"
    held = ",".join(p["name"] for p in positions) or "无"
    msg = (f"{phase} 扫描股票池{len(dfetch.WATCHLIST)}只寻找高开3-8%+开盘封板龙头；"
           f"参考上一交易日情绪[{regime_txt}]；当前持仓: {held}"
           f"（{len(positions)}/{st.MAX_POSITIONS}）。日线数据收盘后更新，届时自动结算交易")
    db.log_scan(phase, msg, trade_date=td)


def main():
    db.init_db()
    db.log_scan("启动", f"盘中调度器启动，每{SCAN_INTERVAL//60}分钟扫描一次", trade_date=_today())
    print("scheduler started")
    while True:
        try:
            scan_once()
        except Exception as e:
            try:
                db.log_scan("异常", f"扫描异常: {e}", trade_date=_today())
            except Exception:
                pass
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
