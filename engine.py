# -*- coding: utf-8 -*-
"""执行引擎——T日收盘选股，T+1开盘买入，遵守T+1规则。

每个交易日 D 的处理顺序（process_day）：
1. 用 D 日全市场面板计算【真实情绪】（涨停/跌停家数、成交额）并落库；
2. 卖出：对现有持仓用 D 日行情判定（严格 T+1，跌停无法卖出则顺延）；
3. 买入执行：取【D 的上一交易日 prev】生成的候选池，在 D 日开盘价买入
   （prev=信号日 T，D=成交日 T+1）；涨停开盘无法买入则记 rejected；
4. 选股：用 D 日收盘数据生成【新候选池】（signal_date=D），供下一交易日开盘执行；
5. 更新净值。

成交价：买入=D开盘价；卖出=策略指定(开盘/收盘/止盈价)。
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
import database as db
import strategy as st
import data_fetcher as dfetch

COMMISSION = 0.0003     # 佣金万3
STAMP_TAX = 0.0005      # 卖出印花税万5
MIN_COMMISSION = 5.0


def _now_hm():
    """当前 HH:MM:SS（盘中成交时间戳用）。"""
    return datetime.now().strftime("%H:%M:%S")


def _buy_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION)


def _sell_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION) + amount * STAMP_TAX


def _row_at(panel, code, date):
    """取 code 在 date 的行(dict)；含 code 字段。无则 None。"""
    df = panel.get(code)
    if df is None:
        return None
    sub = df[df["date"] == date]
    if sub.empty:
        return None
    r = sub.iloc[0].to_dict()
    r["code"] = code
    return r


def _prev_date(dates, date):
    try:
        i = dates.index(date)
    except ValueError:
        return None
    return dates[i - 1] if i > 0 else None


# ==========================================================================
# 情绪（全市场真实统计）
# ==========================================================================
def compute_sentiment(panel, index_df, trade_date):
    """用全市场面板统计当日真实涨停/跌停家数与成交额。"""
    limit_up = limit_down = 0
    total_amount = 0.0
    counted = 0
    for code, df in panel.items():
        sub = df[df["date"] == trade_date]
        if sub.empty:
            continue
        r = sub.iloc[0]
        if int(r.get("tradestatus", 1)) != 1:
            continue
        counted += 1
        amt = r.get("amount") or 0
        total_amount += amt
        limit = dfetch.limit_pct(code)
        pct = (r.get("pctChg") or 0) / 100.0
        high = r.get("high") or 0
        low = r.get("low") or 0
        close = r.get("close") or 0
        if pct >= limit - st.LIMIT_BUFFER and high > 0 and (high - close) / high <= st.CLOSE_AT_HIGH_TOL:
            limit_up += 1
        elif pct <= -(limit - st.LIMIT_BUFFER) and close > 0 and abs(close - low) / close <= st.CLOSE_AT_HIGH_TOL:
            limit_down += 1
    total_amount_yi = round(total_amount / 1e8, 1) if total_amount else None
    regime, tradable = st.classify_sentiment(limit_up)

    index_pct = index_open_pct = None
    if index_df is not None and not index_df.empty:
        irow = index_df[index_df["date"] == trade_date]
        if not irow.empty:
            ir = irow.iloc[0]
            index_pct = round(float(ir["pctChg"]), 2)
            pc = float(ir["preclose"]) if ir["preclose"] == ir["preclose"] else 0
            if pc > 0:
                index_open_pct = round((float(ir["open"]) / pc - 1) * 100, 2)
    return {
        "trade_date": trade_date, "limit_up_count": limit_up, "limit_down_count": limit_down,
        "total_amount": total_amount_yi, "index_pct": index_pct, "index_open_pct": index_open_pct,
        "regime": regime, "tradable": tradable,
        "note": f"全市场涨停{limit_up}/跌停{limit_down}(样本{counted}只)",
    }, index_open_pct


# ==========================================================================
# 撮合
# ==========================================================================
def execute_buy(code, name, theme, price, signal_date, execute_date, reason=""):
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
        "avg_cost": price, "open_date": execute_date, "signal_date": signal_date,
        "last_price": price, "high_since_open": price, "started": 0,
    })
    t = {
        "ts": execute_date + "T09:30:00", "signal_date": signal_date,
        "execute_date": execute_date, "trade_date": execute_date,
        "code": code, "name": name, "theme": theme, "side": "BUY",
        "price": round(price, 3), "shares": shares, "amount": round(amount, 2),
        "pnl": 0, "pnl_pct": 0, "status": "FILLED", "reason": reason,
    }
    db.record_trade(t)
    return t


def execute_sell(pos, price, execute_date, ts=None, reason=""):
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
        "ts": ts or (execute_date + "T15:00:00"), "signal_date": pos.get("signal_date"),
        "execute_date": execute_date, "trade_date": execute_date,
        "code": pos["code"], "name": pos.get("name"), "theme": pos.get("theme"),
        "side": "SELL", "price": round(price, 3), "shares": shares,
        "amount": round(amount, 2), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        "status": "FILLED", "reason": reason,
    }
    db.record_trade(t)
    return t


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


# ==========================================================================
# 单交易日处理
# ==========================================================================
def _sell_price(price_kind, row, sell_p):
    """根据 price_kind 取实际成交价。"""
    if price_kind == "open":
        return row["open"]
    if price_kind == "close":
        return row["close"]
    if price_kind == "target":
        # 止盈价须在当日 [low, high] 内方可成交
        return min(max(sell_p, row["low"]), row["high"])
    return sell_p


def _spot_index(spot_df):
    """实时快照 -> {code: row_dict}，code 用六位。"""
    out = {}
    if spot_df is None or spot_df.empty:
        return out
    for _, sr in spot_df.iterrows():
        d = sr.to_dict()
        out[str(d.get("code"))] = d
    return out


def process_intraday(spot_df, panel, names, trade_date, theme_map=None, concept_map=None, log=True):
    """盘中实时撮合：用实时快照执行卖出（T+1）与买入，实时价成交。
    顺序：实时情绪 -> 卖出 -> 买入 -> 更新净值。收盘后 settle_close 再用日线对账。
    concept_map: {代码: [概念,...]}，用于题材概念板块联动加分。"""
    theme_map = theme_map or {}
    spot = _spot_index(spot_df)

    # 1) 实时情绪
    up, dn, amt_yi = st.count_limit_updown_spot(spot_df)
    regime, tradable = st.classify_sentiment(up)
    # 题材概念板块联动：各概念当日实时涨停家数
    concept_counts = st.count_concept_limitups_spot(spot_df, concept_map) if concept_map else {}
    # 上证指数实时涨跌幅（腾讯指数源；失败则留空）
    index_pct = index_open_pct = None
    try:
        for ix in dfetch.index_spot():
            if ix["code"] == dfetch.INDEX_CODE:
                index_pct = round(ix["pct"], 2) if ix.get("pct") is not None else None
                if ix.get("open") and ix.get("preclose"):
                    index_open_pct = round((ix["open"] / ix["preclose"] - 1) * 100, 2)
                break
    except Exception:
        pass
    sentiment = {
        "trade_date": trade_date, "limit_up_count": up, "limit_down_count": dn,
        "total_amount": amt_yi, "index_pct": index_pct, "index_open_pct": index_open_pct,
        "regime": regime, "tradable": tradable,
        "note": f"盘中实时涨停{up}/跌停{dn}(样本{len(spot)}只)",
    }
    db.upsert_sentiment(sentiment)

    daily_stops = db.trades_count_on(trade_date, side="SELL", reason_like="止损")
    sells, buys, rejected = [], [], []

    # 2) 卖出（实时价，严格 T+1，跌停无法卖顺延）
    for pos in db.get_positions():
        row = spot.get(pos["code"]) or spot.get(pos["code"].split(".")[-1])
        if not row:
            continue
        price = row.get("price") or 0
        if price > 0:
            high_since = max(pos.get("high_since_open") or pos["avg_cost"], price)
            started = 1 if (high_since / pos["avg_cost"] - 1) >= st.STARTED_GAIN else pos.get("started", 0)
            db.update_position_price(pos["code"], price, high_since, started)
            pos["high_since_open"] = high_since
            pos["started"] = started
        do_sell, sell_p, reason = st.evaluate_sell_intraday(pos, row, trade_date)
        if not do_sell:
            continue
        can, sreason = st.can_sell_spot(row)
        if not can:
            db.record_rejected({"signal_date": pos.get("signal_date"), "attempt_date": trade_date,
                                "code": pos["code"], "name": pos.get("name"), "side": "SELL",
                                "reason": f"{reason} 但{sreason}，顺延"})
            rejected.append({"code": pos["code"], "side": "SELL", "reason": sreason})
            continue
        ts = trade_date + "T" + _now_hm()
        t = execute_sell(pos, sell_p, trade_date, ts=ts, reason=reason)
        if t:
            sells.append(t)
            if "止损" in reason:
                daily_stops += 1

    # 3) 买入（实时价；情绪不可交易/止损过多则不开新仓）
    cands = []
    if tradable and daily_stops < st.MAX_STOPS_PER_DAY:
        cands = st.select_intraday_candidates(spot_df, panel, tradable,
                                              concept_map=concept_map, concept_counts=concept_counts)
        held = {p["code"] for p in db.get_positions()}
        for c in cands:
            if len(db.get_positions()) >= st.MAX_POSITIONS:
                break
            code = c["code"]
            bs_code = dfetch.to_bs_code(code)
            if code in held or bs_code in held:
                continue
            row = spot.get(code)
            if not row:
                continue
            can, breason = st.can_buy_spot(row)
            if not can:
                db.record_rejected({"signal_date": trade_date, "attempt_date": trade_date,
                                    "code": bs_code, "name": c.get("name"), "side": "BUY",
                                    "reason": breason})
                rejected.append({"code": code, "side": "BUY", "reason": breason})
                continue
            theme = theme_map.get(code) or c.get("sector") or c.get("strategy_type") or "题材"
            ts = trade_date + "T" + _now_hm()
            t = execute_buy(bs_code, c.get("name"), theme, row["price"], trade_date, trade_date,
                            reason=f"{c.get('reason','')}→{breason}")
            if t:
                # 实时买入即写入成交时间戳
                buys.append(t)
                held.add(bs_code)

    # 4) 候选池落库（盘中实时候选，signal_date=当日）+ 净值
    db.save_candidates(trade_date, cands)
    update_equity(trade_date)

    if log:
        db.log_scan("盘中实时撮合",
                    f"{trade_date} 情绪[{regime}]涨停{up}/跌停{dn} "
                    f"买{len(buys)}卖{len(sells)}拒{len(rejected)} 候选{len(cands)}",
                    signals={"buys": [b["code"] for b in buys], "sells": [s["code"] for s in sells],
                             "rejected": rejected}, trade_date=trade_date)
    return {"sentiment": sentiment, "buys": buys, "sells": sells,
            "rejected": rejected, "candidates": cands}


def process_day(panel, names, index_df, dates, trade_date, theme_map=None, log=True):
    """处理交易日 trade_date(=D)。"""
    theme_map = theme_map or {}
    # 1) 真实情绪
    sentiment, index_open_pct = compute_sentiment(panel, index_df, trade_date)
    db.upsert_sentiment(sentiment)

    daily_stops = db.trades_count_on(trade_date, side="SELL", reason_like="止损")
    sells, buys, rejected = [], [], []

    # 2) 卖出（先更新持仓状态，再判定）
    for pos in db.get_positions():
        row = _row_at(panel, pos["code"], trade_date)
        if row is None:
            continue
        high_since = max(pos.get("high_since_open") or pos["avg_cost"], row["high"])
        started = 1 if (high_since / pos["avg_cost"] - 1) >= st.STARTED_GAIN else pos.get("started", 0)
        db.update_position_price(pos["code"], row["close"], high_since, started)
        pos["high_since_open"] = high_since
        pos["started"] = started
        do_sell, sell_p, reason, price_kind = st.evaluate_sell(pos, row, trade_date)
        if not do_sell:
            continue
        can, sreason = st.can_sell_at(row, price_kind)
        if not can:
            db.record_rejected({"signal_date": pos.get("signal_date"), "attempt_date": trade_date,
                                 "code": pos["code"], "name": pos.get("name"), "side": "SELL",
                                 "reason": f"{reason} 但{sreason}，顺延"})
            rejected.append({"code": pos["code"], "side": "SELL", "reason": sreason})
            continue
        px = _sell_price(price_kind, row, sell_p)
        t = execute_sell(pos, px, trade_date, ts=trade_date + "T14:55:00", reason=reason)
        if t:
            sells.append(t)
            if "止损" in reason:
                daily_stops += 1

    # 3) 买入执行：用 prev(=信号日 T) 的候选池，在 D 开盘价买入
    prev = _prev_date(dates, trade_date)
    if prev and sentiment["tradable"] and daily_stops < st.MAX_STOPS_PER_DAY:
        # 大盘低开过多则不开新仓
        if index_open_pct is not None and index_open_pct < st.INDEX_LOW_OPEN_LIMIT * 100:
            if log:
                db.log_scan("买入暂停", f"{trade_date} 大盘低开{index_open_pct:.2f}%超过-1%，今日不开新仓", trade_date=trade_date)
        else:
            cands = db.get_candidates(prev)
            held = {p["code"] for p in db.get_positions()}
            for c in cands:
                if len(db.get_positions()) >= st.MAX_POSITIONS:
                    break
                code = c["code"]
                if code in held:
                    continue
                row = _row_at(panel, code, trade_date)
                if row is None:
                    continue
                can, breason = st.can_buy_at_open(row)
                if not can:
                    db.record_rejected({"signal_date": prev, "attempt_date": trade_date,
                                        "code": code, "name": c.get("name"), "side": "BUY",
                                        "reason": breason})
                    rejected.append({"code": code, "side": "BUY", "reason": breason})
                    continue
                theme = theme_map.get(code) or c.get("strategy_type") or "题材"
                t = execute_buy(code, c.get("name"), theme, row["open"], prev, trade_date,
                                reason=f"{c.get('reason','')}→{breason}")
                if t:
                    buys.append(t)
                    held.add(code)

    # 4) 选股：用 D 收盘生成新候选池（signal_date=D），供下一交易日执行
    cands_today = st.select_candidates(panel, names, trade_date, sentiment["tradable"])
    db.save_candidates(trade_date, cands_today)

    # 5) 净值
    update_equity(trade_date)

    if log:
        db.log_scan("收盘处理",
                    f"{trade_date} 情绪[{sentiment['regime']}]涨停{sentiment['limit_up_count']} "
                    f"买{len(buys)}卖{len(sells)}拒{len(rejected)} 新候选{len(cands_today)}",
                    signals={"buys": [b["code"] for b in buys], "sells": [s["code"] for s in sells],
                             "rejected": rejected}, trade_date=trade_date)
    return {"sentiment": sentiment, "buys": buys, "sells": sells,
            "rejected": rejected, "candidates": cands_today}


def settle_eod(panel, names, index_df, dates, trade_date, log=True):
    """收盘后日线对账结算——只做对账/统计，绝不再执行交易。

    盘中 process_intraday 已完成全部实时买卖，本函数仅用完整日线：
    1. 重新计算真实情绪（涨停/跌停家数、成交额）并落库；
    2. 用日线收盘价校正持仓 last_price / high_since_open / started；
    3. 用收盘日线生成次日候选池（signal_date=trade_date）；
    4. 重算净值。
    不执行买入、不执行卖出（避免与盘中重复成交）。
    """
    # 1) 真实情绪（覆盖盘中近似值）
    sentiment, _ = compute_sentiment(panel, index_df, trade_date)
    db.upsert_sentiment(sentiment)

    # 2) 用日线收盘价校正持仓状态（不卖出）
    for pos in db.get_positions():
        row = _row_at(panel, pos["code"], trade_date)
        if row is None:
            continue
        high_since = max(pos.get("high_since_open") or pos["avg_cost"], row["high"])
        started = 1 if (high_since / pos["avg_cost"] - 1) >= st.STARTED_GAIN else pos.get("started", 0)
        db.update_position_price(pos["code"], row["close"], high_since, started)

    # 3) 用收盘日线生成次日候选池
    cands_today = st.select_candidates(panel, names, trade_date, sentiment["tradable"])
    db.save_candidates(trade_date, cands_today)

    # 4) 净值
    update_equity(trade_date)

    if log:
        db.log_scan("收盘结算",
                    f"{trade_date} 日线对账 情绪[{sentiment['regime']}]涨停{sentiment['limit_up_count']}/"
                    f"跌停{sentiment['limit_down_count']} 持仓{len(db.get_positions())} "
                    f"明日候选{len(cands_today)}（无交易，盘中已成交）",
                    trade_date=trade_date)
    return {"sentiment": sentiment, "candidates": cands_today}
