# -*- coding: utf-8 -*-
"""前向模拟调度器。

交易模型：T日收盘选股，T+1开盘买入，遵守T+1规则。
- 选股只用 T 日收盘可见的日线数据；不使用盘中实时数据。
- 买入在 T+1 日开盘价执行（T+1 开盘价是已发生的真实价格，非未来数据）。
- 卖出在持仓 T+1 日起判定（买入当日不可卖，严格 T+1）。
- 真正的"结算"发生在每个交易日收盘后：用当日完整日线跑 process_day。

盘中（09:30-15:00）：每 15 分钟记录一次监控心跳（scan_log），仅展示状态；
  不做任何盘中成交。日线收盘后（约 15:05 起）才结算。

数据：
- 收盘结算：增量刷新本地面板库（panel.db）到今日 -> load_panel -> process_day。
- panel.db 随实盘自然增长，无需重复全量抓取。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import time
from datetime import datetime

import database as db
import data_fetcher as dfetch
import engine
import strategy as st

SCAN_INTERVAL = 15 * 60   # 15分钟
# 前向模拟起始日：今天即可建仓
SIM_START = os.environ.get("SIM_START", "2026-06-22")


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
        return True, "集合竞价"
    return False, "非交易时段"


def _today():
    return _now().strftime("%Y-%m-%d")


def _refresh_panel_today(td):
    """收盘后增量刷新面板库到今日（仅抓尚无 td 行的股票），返回是否已有今日数据。"""
    # 用 build_panel 的增量能力：抓取 [td, td] 但保留 lookback。简单起见复用 build_panel。
    try:
        dfetch.build_panel(td, td, lookback_days=40)
    except Exception as e:
        db.log_scan("数据刷新异常", f"{repr(e)[:120]}", trade_date=td)
    pdates = set(dfetch.panel_dates())
    return td in pdates


def _collect_market_data_live(td):
    """SIM_START 前：用 akshare 涨停池 + baostock 上证指数收集市场数据。
    结果落库到 market_sentiment，不执行任何交易。
    """
    import akshare as ak

    # 1. 确认是否交易日
    with dfetch.bs_session():
        trade_dates = dfetch.get_trade_dates("2026-06-01", td)
        if td not in trade_dates:
            db.log_scan("非交易日", f"{td} 非交易日", trade_date=td)
            return

    # 2. akshare 获取今日涨停/跌停家数
    ak_date = td.replace("-", "")   # YYYYMMDD
    limit_up = limit_down = 0
    total_amount_yi = None
    note_extra = ""
    try:
        zt_df = ak.stock_zt_pool_em(date=ak_date)
        limit_up = len(zt_df)
        note_extra = f"akshare涨停池{limit_up}只"
    except Exception as e:
        # 收盘后数据才可用；盘中用东财快照估算
        try:
            spot = dfetch.em_spot()
            if not spot.empty:
                def _is_zt(row):
                    code = str(row.get("code", ""))
                    pct = row.get("pct") or 0
                    return pct >= (19.8 if code.startswith(("68", "30")) else 9.8)
                def _is_dt(row):
                    code = str(row.get("code", ""))
                    pct = row.get("pct") or 0
                    return pct <= -(19.8 if code.startswith(("68", "30")) else 9.8)
                limit_up = int(spot.apply(_is_zt, axis=1).sum())
                limit_down = int(spot.apply(_is_dt, axis=1).sum())
                total_amount = spot["amount"].sum() if "amount" in spot.columns else 0
                total_amount_yi = round(total_amount / 1e8, 1) if total_amount else None
                note_extra = f"东财实时快照(盘中){len(spot)}只"
        except Exception as e2:
            note_extra = f"快照失败:{str(e2)[:40]}"

    try:
        dt_df = ak.stock_dt_pool_em(date=ak_date)
        limit_down = len(dt_df)
    except Exception:
        pass

    # 3. baostock 获取上证指数
    index_pct = index_open_pct = None
    try:
        with dfetch.bs_session():
            index_df = dfetch.get_index("2026-06-01", td)
        if index_df is not None and not index_df.empty:
            irow = index_df[index_df["date"] == td]
            if not irow.empty:
                ir = irow.iloc[0]
                index_pct = round(float(ir["pctChg"]), 2)
                pc = float(ir["preclose"]) if ir["preclose"] == ir["preclose"] else 0
                if pc > 0:
                    index_open_pct = round((float(ir["open"]) / pc - 1) * 100, 2)
    except Exception as e:
        note_extra += f" 指数失败:{str(e)[:40]}"

    regime, tradable = st.classify_sentiment(limit_up)
    sentiment = {
        "trade_date": td, "limit_up_count": limit_up, "limit_down_count": limit_down,
        "total_amount": total_amount_yi, "index_pct": index_pct, "index_open_pct": index_open_pct,
        "regime": regime, "tradable": tradable,
        "note": f"全市场涨停{limit_up}/跌停{limit_down} {note_extra}",
    }
    db.upsert_sentiment(sentiment)
    db.log_scan("市场数据收集",
                f"{td} [等待建仓] 情绪[{regime}]涨停{limit_up}/跌停{limit_down} "
                f"指数{index_pct}% (模拟起始{SIM_START}前空仓)",
                trade_date=td)


def settle_close(td):
    """收盘后结算：收集市场数据 -> process_day（SIM_START前只收集数据不建仓）。"""
    # 已结算过则跳过
    if db.get_sentiment(td):
        return

    if td < SIM_START:
        # SIM_START 前：用东财实时快照+baostock指数，不做交易
        _collect_market_data_live(td)
        return

    # SIM_START 后：刷新面板 -> process_day
    have = _refresh_panel_today(td)
    if not have:
        db.log_scan("等待数据", f"{td} 日线数据未就绪(收盘后约15:30更新)，稍后重试", trade_date=td)
        return

    panel, names = dfetch.load_panel()
    with dfetch.bs_session():
        index_df = dfetch.get_index(SIM_START, td)
        dates = [d for d in dfetch.get_trade_dates(SIM_START, td) if d in set(dfetch.panel_dates())]

    if td not in dates:
        db.log_scan("非交易日", f"{td} 非交易日或无数据", trade_date=td)
        return

    res = engine.process_day(panel, names, index_df, dates, td, log=True)
    s = res["sentiment"]
    db.log_scan("收盘结算完成",
                f"{td} 情绪[{s['regime']}]涨停{s['limit_up_count']} "
                f"买{len(res['buys'])}卖{len(res['sells'])}拒{len(res['rejected'])} 明日候选{len(res['candidates'])}",
                trade_date=td)


def scan_once():
    now = _now()
    td = _today()
    in_market, phase = is_market_hours(now)

    if in_market:
        positions = db.get_positions()
        held = ",".join(f"{p['name']}({p.get('float_pnl_pct','')})" for p in positions) or "空仓"
        prev_sent = db.get_sentiment()
        regime = prev_sent["regime"] if prev_sent else "待结算"
        cands = db.get_candidates()
        db.log_scan(phase,
                    f"{phase} 监控心跳：持仓{len(positions)}/{st.MAX_POSITIONS}[{held}]；"
                    f"昨日候选{len(cands)}只，T+1开盘执行；参考情绪[{regime}]。"
                    f"T日收盘选股/T+1开盘买入模型，收盘后结算",
                    trade_date=td)
        return

    # 非交易时段：收盘后(15:05之后)尝试结算今日
    hm = now.hour * 100 + now.minute
    if now.weekday() < 5 and 1505 <= hm <= 2359:
        settle_close(td)
    elif now.minute < 15:
        db.log_scan("休市", f"{phase}", trade_date=td)


def main():
    db.init_db()
    db.log_scan("启动", f"调度器启动：T日收盘选股/T+1开盘买入，遵守T+1规则，每{SCAN_INTERVAL//60}分钟一次，模拟起始{SIM_START}", trade_date=_today())
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
