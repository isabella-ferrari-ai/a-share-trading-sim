# -*- coding: utf-8 -*-
"""执行引擎：把策略信号落地为模拟买卖，更新账户/持仓/净值/情绪。

提供：
- execute_buy / execute_sell：撮合并记账
- process_trading_day：对某一交易日跑完整流程（先卖后买，更新净值）——用于种历史数据和每日收盘结算
- intraday_scan：盘中每15分钟调用，基于当日已知行情产出信号并（在收盘扫描时）落地交易
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
import database as db
import strategy as st
import data_fetcher as dfetch

COMMISSION = 0.0003     # 佣金万3
STAMP_TAX = 0.0005      # 卖出印花税万5(已下调)
MIN_COMMISSION = 5.0


def _buy_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION)


def _sell_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION) + amount * STAMP_TAX


def execute_buy(code, name, theme, price, trade_date, ts=None, reason=""):
    """按可用现金与单票20%上限买入。返回交易记录或 None。"""
    acct = db.get_account()
    cash = acct["cash"]
    total_equity = cash + st.position_value(db.get_positions())
    budget = min(total_equity * st.MAX_POSITION_PCT, cash * 0.98)
    shares = st.lots(budget / price)
    if shares < 100:
        return None
    amount = shares * price
    fee = _buy_cost(amount)
    if amount + fee > cash:
        shares = st.lots((cash * 0.98) / price)
        if shares < 100:
            return None
        amount = shares * price
        fee = _buy_cost(amount)
    db.set_cash(cash - amount - fee)
    db.upsert_position({
        "code": code, "name": name, "theme": theme, "shares": shares,
        "avg_cost": price, "open_date": trade_date, "last_price": price,
        "high_since_open": price, "started": 0,
    })
    t = {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date, "code": code, "name": name, "theme": theme,
        "side": "BUY", "price": round(price, 3), "shares": shares,
        "amount": round(amount, 2), "pnl": 0, "pnl_pct": 0, "reason": reason,
    }
    db.record_trade(t)
    return t


def execute_sell(pos, price, trade_date, ts=None, reason=""):
    acct = db.get_account()
    shares = pos["shares"]
    amount = shares * price
    fee = _sell_cost(amount)
    cost_amount = shares * pos["avg_cost"]
    pnl = amount - cost_amount - fee
    pnl_pct = (price / pos["avg_cost"] - 1) * 100
    db.set_cash(acct["cash"] + amount - fee)
    db.remove_position(pos["code"])
    t = {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date, "code": pos["code"], "name": pos.get("name"),
        "theme": pos.get("theme"), "side": "SELL", "price": round(price, 3),
        "shares": shares, "amount": round(amount, 2),
        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "reason": reason,
    }
    db.record_trade(t)
    return t


def _bar_for(bars, code, trade_date, watch):
    df = bars.get(code)
    if df is None:
        return None
    row = df[df["date"] == trade_date]
    if row.empty:
        return None
    r = row.iloc[0]
    return {
        "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
        "close": float(r["close"]), "preclose": float(r["preclose"]),
        "pctChg": float(r["pctChg"]), "turn": float(r["turn"]) if r["turn"] == r["turn"] else 0,
        "limit": watch["limit"],
    }


def compute_sentiment(bars, index_df, trade_date):
    """用股票池作为市场情绪的代理样本计算涨停数/情绪（模拟盘近似）。"""
    limit_up = 0
    sample = 0
    for w in dfetch.WATCHLIST:
        df = bars.get(w["code"])
        if df is None:
            continue
        row = df[df["date"] == trade_date]
        if row.empty:
            continue
        sample += 1
        pct = float(row.iloc[0]["pctChg"]) / 100.0
        if pct >= w["limit"] - 0.005:
            limit_up += 1
    # 用样本涨停比例放大到全市场近似（样本~17只 -> 全市场约5000只，比例放大 + 基准噪声）
    ratio = (limit_up / sample) if sample else 0
    est_limit_up = int(ratio * 120 + 30)   # 经验映射：让数值落在合理区间
    # 指数
    index_pct = None
    index_open_pct = None
    if index_df is not None and not index_df.empty:
        irow = index_df[index_df["date"] == trade_date]
        if not irow.empty:
            ir = irow.iloc[0]
            index_pct = float(ir["pctChg"])
            pc = float(ir["preclose"]) if ir["preclose"] == ir["preclose"] else 0
            if pc > 0:
                index_open_pct = float(ir["open"]) / pc - 1
    # 全市场成交额代理：用上证成交额(亿)粗略放大
    total_amount_yi = None
    if index_df is not None and not index_df.empty:
        irow = index_df[index_df["date"] == trade_date]
        if not irow.empty and irow.iloc[0]["amount"] == irow.iloc[0]["amount"]:
            # 上证指数成分成交额(元)->亿元，再粗略放大到沪深两市
            sh_amount_yi = float(irow.iloc[0]["amount"]) / 1e8
            total_amount_yi = sh_amount_yi * 2.3
    regime, tradable = st.classify_regime(est_limit_up, total_amount_yi)
    note = f"样本涨停{limit_up}/{sample}"
    return {
        "trade_date": trade_date, "limit_up_count": est_limit_up,
        "total_amount": round(total_amount_yi, 1) if total_amount_yi else None,
        "index_pct": round(index_pct, 2) if index_pct is not None else None,
        "index_open_pct": round(index_open_pct * 100, 2) if index_open_pct is not None else None,
        "regime": regime, "tradable": tradable, "note": note,
    }, index_open_pct


def update_equity(trade_date):
    acct = db.get_account()
    positions = db.get_positions()
    mv = st.position_value(positions)
    total = acct["cash"] + mv
    prev = db.last_equity()
    prev_total = prev["total_equity"] if prev else acct["initial_capital"]
    daily_ret = (total / prev_total - 1) * 100 if prev_total else 0
    cum_ret = (total / acct["initial_capital"] - 1) * 100
    db.upsert_equity({
        "trade_date": trade_date, "cash": round(acct["cash"], 2),
        "market_value": round(mv, 2), "total_equity": round(total, 2),
        "daily_return": round(daily_ret, 3), "cum_return": round(cum_ret, 3),
    })


def _has_data_for(bars, trade_date):
    return any((df["date"] == trade_date).any() for df in bars.values())


def process_trading_day(bars, index_df, trade_date, intraday_log=False):
    """处理某一交易日：更新情绪 -> 先卖(止损止盈) -> 再买(新信号) -> 更新净值。
    若该日尚无行情数据（如当日盘中、数据未更新），跳过，不写入虚假情绪。"""
    if not _has_data_for(bars, trade_date):
        return {"sentiment": None, "sells": [], "buys": [], "skipped": "无当日行情数据"}
    sentiment, index_open_pct = compute_sentiment(bars, index_df, trade_date)
    db.upsert_sentiment(sentiment)

    daily_stops = db.trades_count_on(trade_date, side="SELL", reason_like="止损")
    sells, buys = [], []

    # ---- 先处理卖出 ----
    for pos in db.get_positions():
        watch = next((w for w in dfetch.WATCHLIST if w["code"] == pos["code"]), {"limit": 0.10})
        bar = _bar_for(bars, pos["code"], trade_date, watch)
        if bar is None:
            continue
        # 更新持仓最新价/最高价/启动状态
        high_since = max(pos.get("high_since_open") or pos["avg_cost"], bar["high"])
        started = 1 if (high_since / pos["avg_cost"] - 1) >= 0.08 else pos.get("started", 0)
        db.update_position_price(pos["code"], bar["close"], high_since, started)
        pos["last_price"] = bar["close"]
        pos["high_since_open"] = high_since
        pos["started"] = started
        do_sell, sell_p, reason = st.evaluate_sell(pos, bar, trade_date)
        if do_sell:
            t = execute_sell(pos, sell_p, trade_date, reason=reason)
            if t:
                sells.append(t)
                if "止损" in reason:
                    daily_stops += 1

    # ---- 再处理买入 ----
    held_codes = {p["code"] for p in db.get_positions()}
    if sentiment["tradable"] and daily_stops < st.MAX_STOPS_PER_DAY:
        for w in dfetch.WATCHLIST:
            if len(db.get_positions()) >= st.MAX_POSITIONS:
                break
            if w["code"] in held_codes:
                continue
            bar = _bar_for(bars, w["code"], trade_date, w)
            if bar is None:
                continue
            ok, reason = st.is_buy_candidate(bar, index_open_pct, sentiment["tradable"])
            if ok:
                # 模拟以接近开盘封板价买入（用开盘价上浮，封板很难买到，取 open 与 close 间）
                buy_price = round((bar["open"] + bar["close"]) / 2, 3)
                t = execute_buy(w["code"], w["name"], w["theme"], buy_price, trade_date, reason=reason)
                if t:
                    buys.append(t)
                    held_codes.add(w["code"])

    update_equity(trade_date)
    if intraday_log:
        db.log_scan("收盘结算", f"{trade_date} 卖出{len(sells)}笔 买入{len(buys)}笔 情绪[{sentiment['regime']}]",
                    signals={"sells": sells, "buys": buys}, trade_date=trade_date)
    return {"sentiment": sentiment, "sells": sells, "buys": buys}
