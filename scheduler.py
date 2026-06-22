# -*- coding: utf-8 -*-
"""前向模拟调度器——盘中实时选股，有机会即交易，遵守 T+1 卖出规则。

交易模型：
- 盘中（09:30-11:30, 13:00-15:00）每 15 分钟用实时快照扫描全股票池，
  发现机会即以实时价成交（engine.process_intraday）。买入当日不可卖（严格 T+1）。
- 收盘后（约 15:05 起）用 baostock 完整日线跑 engine.process_day 做最终对账/结算，
  并把当日并入面板库（供次日连板高度计算）。

数据：
- 实时：data_fetcher.ak_spot() 三级 fallback（akshare→东财→腾讯）。
- 历史/连板：panel.db，启动时与每日收盘后增量刷新到最近交易日。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import time
from datetime import datetime, timedelta

import database as db
import data_fetcher as dfetch
import engine
import strategy as st

SCAN_INTERVAL = 15 * 60   # 15分钟
# 前向模拟起始日：今天即可建仓
SIM_START = os.environ.get("SIM_START", "2026-06-22")  # 今天即开始盘中实时建仓
PANEL_LOOKBACK_DAYS = 30  # 连板高度所需的近端历史天数


def _now():
    return datetime.now()


def is_market_hours(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return False, "周末休市"
    hm = now.hour * 100 + now.minute
    if 930 <= hm <= 1130:
        return True, "上午盘"
    if 1300 <= hm <= 1500:
        return True, "下午盘"
    if 915 <= hm < 930:
        return False, "集合竞价"
    return False, "非交易时段"


def _today():
    return _now().strftime("%Y-%m-%d")


_panel_cache = {"date": None, "panel": None, "names": None}
_industry_cache = {"map": None}


def _load_industry_cached():
    """行业分类映射缓存（题材板块联动用，盘中不变）。"""
    if _industry_cache["map"] is None:
        _industry_cache["map"] = dfetch.load_industry()
    return _industry_cache["map"]


def _panel_is_fresh(td, max_age_days=5):
    """面板库最新日期距 td 在 max_age_days 内则视为新鲜（连板高度用，无需当日盘中 bar）。"""
    try:
        pdates = dfetch.panel_dates()
        if not pdates:
            return False
        last = datetime.strptime(pdates[-1], "%Y-%m-%d")
        return (datetime.strptime(td, "%Y-%m-%d") - last).days <= max_age_days
    except Exception:
        return False


def _ensure_recent_panel(td, force=False):
    """确保面板库含最近交易日数据（用于连板高度）。
    若面板已新鲜（最新日期距今≤5天）则跳过，避免重复全量抓取（约 20 分钟）。
    盘中 baostock 无当日 bar，连板高度用到昨日为止即可。"""
    if not force and _panel_is_fresh(td):
        return
    start = (datetime.strptime(td, "%Y-%m-%d") - timedelta(days=PANEL_LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")
    try:
        dfetch.build_panel(start, td, lookback_days=PANEL_LOOKBACK_DAYS)
    except Exception as e:
        db.log_scan("面板刷新异常", f"{repr(e)[:120]}", trade_date=td)


def _load_panel_cached(td):
    if _panel_cache["date"] == td and _panel_cache["panel"] is not None:
        return _panel_cache["panel"], _panel_cache["names"]
    panel, names = dfetch.load_panel()
    _panel_cache.update({"date": td, "panel": panel, "names": names})
    return panel, names


def _is_trade_day(td):
    try:
        with dfetch.bs_session():
            tds = dfetch.get_trade_dates((datetime.strptime(td, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d"), td)
        return td in tds
    except Exception:
        # 网络异常时退化为"工作日即交易日"
        return datetime.strptime(td, "%Y-%m-%d").weekday() < 5


def scan_intraday(td):
    """盘中一次实时扫描+撮合。"""
    if td < SIM_START:
        db.log_scan("等待建仓", f"{td} 模拟起始{SIM_START}前，盘中观察不建仓", trade_date=td)
        return
    panel, names = _load_panel_cached(td)
    spot_df, src = dfetch.ak_spot()
    if spot_df is None or spot_df.empty:
        db.log_scan("实时行情失败", f"{td} 实时快照为空(源{src})，本次跳过", trade_date=td)
        return
    industry_map = _load_industry_cached()
    res = engine.process_intraday(spot_df, panel, names, td, industry_map=industry_map, log=False)
    s = res["sentiment"]
    db.log_scan("盘中扫描",
                f"{td} [实时源:{src}] 情绪[{s['regime']}]涨停{s['limit_up_count']}/跌停{s['limit_down_count']} "
                f"成交{len(res['buys'])}买/{len(res['sells'])}卖 拒{len(res['rejected'])} "
                f"候选{len(res['candidates'])} 持仓{len(db.get_positions())}/{st.MAX_POSITIONS}",
                signals={"buys": [b["code"] for b in res["buys"]],
                         "sells": [s2["code"] for s2 in res["sells"]],
                         "rejected": res["rejected"]},
                trade_date=td)


def settle_close(td):
    """收盘后用 baostock 完整日线对账/结算（并入当日面板，计算次日连板基准）。"""
    if td < SIM_START:
        return
    # 已用日线结算过则跳过（用 scan_log 标记位判断）
    logs = db.get_scan_log(20)
    if any(l["phase"] == "收盘结算" and l["trade_date"] == td for l in logs):
        return
    _ensure_recent_panel(td, force=True)
    if td not in set(dfetch.panel_dates()):
        db.log_scan("等待数据", f"{td} 日线未就绪(收盘后约15:30更新)，稍后重试", trade_date=td)
        return
    panel, names = dfetch.load_panel()
    _panel_cache.update({"date": None})  # 失效缓存
    with dfetch.bs_session():
        index_df = dfetch.get_index(SIM_START, td)
        dates = [d for d in dfetch.get_trade_dates(SIM_START, td) if d in set(dfetch.panel_dates())]
    if td not in dates:
        return
    res = engine.process_day(panel, names, index_df, dates, td, log=False)
    s = res["sentiment"]
    db.log_scan("收盘结算",
                f"{td} 日线对账 情绪[{s['regime']}]涨停{s['limit_up_count']} "
                f"今日累计买{len([t for t in db.get_trades(999) if t['side']=='BUY' and t['execute_date']==td])} "
                f"明日候选{len(res['candidates'])}",
                trade_date=td)


def scan_once():
    now = _now()
    td = _today()
    in_market, phase = is_market_hours(now)

    if in_market:
        # 交易日校验（每个交易日首次时刷新近端面板）
        if _panel_cache["date"] != td:
            if not _is_trade_day(td):
                db.log_scan("非交易日", f"{td} 非交易日，今日不扫描", trade_date=td)
                _panel_cache.update({"date": td, "panel": {}, "names": {}})
                return
            _ensure_recent_panel(td)
            _panel_cache.update({"date": None})  # 强制重载最新面板
            _industry_cache["map"] = None        # 行业映射随面板刷新
        scan_intraday(td)
        return

    # 非交易时段：收盘后(15:05之后)做日线对账结算
    hm = now.hour * 100 + now.minute
    if now.weekday() < 5 and 1505 <= hm <= 2359:
        settle_close(td)
    elif now.minute < 15:
        db.log_scan("休市", f"{phase}", trade_date=td)


def main():
    db.init_db()
    db.log_scan("启动",
                f"调度器启动：盘中每{SCAN_INTERVAL//60}分钟实时扫描撮合，遵守T+1卖出规则，"
                f"收盘后日线对账，模拟起始{SIM_START}", trade_date=_today())
    print("scheduler started")
    while True:
        try:
            scan_once()
        except Exception as e:
            try:
                db.log_scan("异常", f"{repr(e)[:120]}", trade_date=_today())
            except Exception:
                pass
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
