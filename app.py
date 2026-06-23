# -*- coding: utf-8 -*-
"""Flask Web 服务：Dashboard + JSON API。生产用 waitress。

支持通过 ?db=backtest 查看回测库，默认实时库 data/trading.db。
"""
import warnings
warnings.filterwarnings("ignore")

import os
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import database as db
import strategy as st

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DB = os.path.join(BASE_DIR, "data", "trading.db")
BACKTEST_DB = os.path.join(BASE_DIR, "data", "backtest.db")

app = Flask(__name__)
CORS(app)


def _select_db():
    """根据查询参数切换库（live/backtest）。"""
    which = request.args.get("db", "live")
    db.set_db_path(BACKTEST_DB if which == "backtest" else LIVE_DB)
    db.init_db()
    return which


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    which = _select_db()
    acct = db.get_account()
    positions = db.get_positions()
    mv = st.position_value(positions)
    total = acct["cash"] + mv
    init = acct["initial_capital"]
    curve = db.get_equity_curve()
    today_ret = curve[-1]["daily_return"] if curve else 0
    sells = [t for t in db.get_trades(99999) if t["side"] == "SELL" and t["status"] == "FILLED"]
    realized = sum(t["pnl"] for t in sells)
    floating = sum(((p.get("last_price") or p["avg_cost"]) - p["avg_cost"]) * p["shares"] for p in positions)
    wins = [t for t in sells if t["pnl"] > 0]
    win_rate = (len(wins) / len(sells) * 100) if sells else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in sells if t["pnl"] <= 0]
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    # 最大回撤
    peak = init; mdd = 0
    for e in curve:
        peak = max(peak, e["total_equity"])
        mdd = min(mdd, e["total_equity"] / peak - 1)
    return jsonify({
        "db": which,
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
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "max_drawdown": round(mdd * 100, 2),
        "updated_at": acct.get("updated_at"),
    })


@app.route("/api/equity")
def api_equity():
    _select_db()
    return jsonify(db.get_equity_curve())


@app.route("/api/positions")
def api_positions():
    _select_db()
    out = []
    today = datetime.now().strftime("%Y-%m-%d")
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
            "days_held": st._days_held(p["open_date"], today),
            "can_sell": p["open_date"] != today,
        })
    return jsonify(out)


@app.route("/api/trades")
def api_trades():
    _select_db()
    return jsonify(db.get_trades(300))


@app.route("/api/rejected")
def api_rejected():
    _select_db()
    return jsonify(db.get_rejected(100))


@app.route("/api/candidates")
def api_candidates():
    _select_db()
    sd = request.args.get("date")
    return jsonify({"signal_date": sd, "items": db.get_candidates(sd, limit=20)})


@app.route("/api/sentiment")
def api_sentiment():
    _select_db()
    return jsonify({
        "latest": db.get_sentiment(),
        "history": db.get_sentiment_history(60),
        "tiers": [{"threshold": t, "name": n, "tradable": tr} for t, n, tr in st.SENTIMENT_TIERS],
    })


@app.route("/api/scan_log")
def api_scan_log():
    _select_db()
    return jsonify(db.get_scan_log(40))


@app.route("/api/indices")
def api_indices():
    """参考情绪的 A 股宽基指数实时快照（腾讯源）。"""
    import data_fetcher as dfetch
    try:
        items = dfetch.index_spot()
    except Exception:
        items = []
    return jsonify({"items": items, "as_of": datetime.now().isoformat(timespec="seconds")})


@app.route("/api/strategy")
def api_strategy():
    _select_db()
    return jsonify({
        "model": "盘中实时选股，有机会即交易，遵守T+1卖出规则",
        "initial_capital": db.INITIAL_CAPITAL,
        "max_positions": st.MAX_POSITIONS,
        "max_position_pct": st.MAX_POSITION_PCT,
        "min_boards": st.MIN_BOARDS,
        "max_boards": st.MAX_BOARDS,
        "vol_ratio_min": st.VOL_RATIO_MIN,
        "turn_range": [st.TURN_MIN, st.TURN_MAX],
        "min_amount": st.MIN_AMOUNT,
        "stop_loss": st.STOP_LOSS,
        "stop_overnight_gap": st.STOP_OVERNIGHT_GAP,
        "hold_max_days": st.HOLD_MAX_DAYS,
        "stall_days": st.STALL_DAYS,
        "target_profit": st.TARGET_PROFIT,
        "max_stops_per_day": st.MAX_STOPS_PER_DAY,
        "sentiment_tiers": [{"threshold": t, "name": n, "tradable": tr} for t, n, tr in st.SENTIMENT_TIERS],
    })


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8888))
    db.set_db_path(LIVE_DB)
    db.init_db()
    app.run(host="0.0.0.0", port=port, debug=True)
