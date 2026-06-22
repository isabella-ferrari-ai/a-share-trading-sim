# -*- coding: utf-8 -*-
"""Flask Web 服务：Dashboard 页面 + JSON API。生产用 waitress 提供。"""
import warnings
warnings.filterwarnings("ignore")

import os
from datetime import datetime
from flask import Flask, jsonify, render_template
from flask_cors import CORS

import database as db
import strategy as st
import data_fetcher as dfetch

app = Flask(__name__)
CORS(app)
db.init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    acct = db.get_account()
    positions = db.get_positions()
    mv = st.position_value(positions)
    total = acct["cash"] + mv
    init = acct["initial_capital"]
    eq = db.last_equity()
    # 今日盈亏
    curve = db.get_equity_curve()
    today_ret = curve[-1]["daily_return"] if curve else 0
    # 已实现盈亏（所有 SELL pnl 之和）
    realized = sum(t["pnl"] for t in db.get_trades(99999) if t["side"] == "SELL")
    # 浮动盈亏
    floating = sum(((p.get("last_price") or p["avg_cost"]) - p["avg_cost"]) * p["shares"] for p in positions)
    sells = [t for t in db.get_trades(99999) if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    win_rate = (len(wins) / len(sells) * 100) if sells else 0
    return jsonify({
        "cash": round(acct["cash"], 2),
        "market_value": round(mv, 2),
        "total_equity": round(total, 2),
        "initial_capital": init,
        "cum_return": round((total / init - 1) * 100, 2),
        "today_return": round(today_ret, 2),
        "realized_pnl": round(realized, 2),
        "floating_pnl": round(floating, 2),
        "position_count": len(positions),
        "max_positions": st.MAX_POSITIONS,
        "win_rate": round(win_rate, 1),
        "trade_count": len(sells),
        "updated_at": acct.get("updated_at"),
    })


@app.route("/api/equity")
def api_equity():
    return jsonify(db.get_equity_curve())


@app.route("/api/positions")
def api_positions():
    out = []
    for p in db.get_positions():
        last = p.get("last_price") or p["avg_cost"]
        cost_val = p["avg_cost"] * p["shares"]
        mkt_val = last * p["shares"]
        out.append({
            **p,
            "market_value": round(mkt_val, 2),
            "cost_value": round(cost_val, 2),
            "float_pnl": round(mkt_val - cost_val, 2),
            "float_pnl_pct": round((last / p["avg_cost"] - 1) * 100, 2),
            "days_held": st._days_held(p["open_date"], datetime.now().strftime("%Y-%m-%d")),
        })
    return jsonify(out)


@app.route("/api/trades")
def api_trades():
    return jsonify(db.get_trades(200))


@app.route("/api/sentiment")
def api_sentiment():
    return jsonify({
        "latest": db.get_sentiment(),
        "history": db.get_sentiment_history(30),
        "thresholds": {
            "limitup_strong": st.LIMITUP_STRONG, "limitup_weak": st.LIMITUP_WEAK,
            "amount_strong": st.AMOUNT_STRONG, "amount_weak": st.AMOUNT_WEAK,
        },
    })


@app.route("/api/scan_log")
def api_scan_log():
    return jsonify(db.get_scan_log(40))


@app.route("/api/watchlist")
def api_watchlist():
    return jsonify(dfetch.WATCHLIST)


@app.route("/api/strategy")
def api_strategy():
    return jsonify({
        "initial_capital": db.INITIAL_CAPITAL,
        "max_positions": st.MAX_POSITIONS,
        "max_position_pct": st.MAX_POSITION_PCT,
        "gap_up_range": [st.GAP_UP_MIN, st.GAP_UP_MAX],
        "stop_loss_intraday": st.STOP_LOSS_INTRADAY,
        "stop_overnight_gap": st.STOP_OVERNIGHT_GAP,
        "hold_max_days": st.HOLD_MAX_DAYS,
        "stall_days": st.STALL_DAYS,
        "target_profit": st.TARGET_PROFIT,
        "max_stops_per_day": st.MAX_STOPS_PER_DAY,
    })


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8888))
    app.run(host="0.0.0.0", port=port, debug=True)
