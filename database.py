# -*- coding: utf-8 -*-
"""SQLite 持久化层：持仓、交易明细、每日净值、市场情绪、扫描日志。"""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trading.db")

INITIAL_CAPITAL = 1_000_000.0


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    # 账户状态（单行）
    c.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            initial_capital REAL NOT NULL,
            updated_at TEXT
        )
    """)
    # 当前持仓
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            code TEXT PRIMARY KEY,
            name TEXT,
            theme TEXT,
            shares INTEGER NOT NULL,
            avg_cost REAL NOT NULL,
            open_date TEXT NOT NULL,      -- 买入日期（T+1 判定基准）
            last_price REAL,
            high_since_open REAL,         -- 持仓期最高价（用于跟踪止盈）
            started INTEGER DEFAULT 0     -- 是否已启动行情(用于持仓3天未启动止损)
        )
    """)
    # 交易明细
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,             -- 交易时间戳
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            theme TEXT,
            side TEXT NOT NULL,           -- BUY / SELL
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            amount REAL NOT NULL,
            pnl REAL DEFAULT 0,           -- 卖出时实现盈亏
            pnl_pct REAL DEFAULT 0,
            reason TEXT                   -- 买卖理由
        )
    """)
    # 每日净值
    c.execute("""
        CREATE TABLE IF NOT EXISTS equity_curve (
            trade_date TEXT PRIMARY KEY,
            cash REAL NOT NULL,
            market_value REAL NOT NULL,
            total_equity REAL NOT NULL,
            daily_return REAL DEFAULT 0,
            cum_return REAL DEFAULT 0
        )
    """)
    # 市场情绪
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_sentiment (
            trade_date TEXT PRIMARY KEY,
            limit_up_count INTEGER,       -- 涨停板数量
            total_amount REAL,            -- 全市场成交额(亿元)
            index_pct REAL,               -- 上证指数涨跌幅
            index_open_pct REAL,          -- 上证开盘相对昨收(判断低开)
            regime TEXT,                  -- 强市/中性/弱市
            tradable INTEGER,             -- 1=可操作 0=弱势空仓
            note TEXT
        )
    """)
    # 扫描日志（每15分钟一次的盘中扫描记录）
    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            trade_date TEXT,
            phase TEXT,                   -- 扫描阶段说明
            message TEXT,
            signals TEXT                  -- JSON: 本次扫描发现的信号
        )
    """)
    conn.commit()
    # 初始化账户
    row = c.execute("SELECT * FROM account WHERE id=1").fetchone()
    if row is None:
        c.execute(
            "INSERT INTO account (id, cash, initial_capital, updated_at) VALUES (1, ?, ?, ?)",
            (INITIAL_CAPITAL, INITIAL_CAPITAL, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    conn.close()


# --------------------------- 账户 ---------------------------
def get_account():
    conn = get_conn()
    row = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else None


def set_cash(cash):
    conn = get_conn()
    conn.execute(
        "UPDATE account SET cash=?, updated_at=? WHERE id=1",
        (cash, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


# --------------------------- 持仓 ---------------------------
def get_positions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM positions ORDER BY open_date").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_position(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM positions WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_position(pos):
    conn = get_conn()
    conn.execute("""
        INSERT INTO positions (code,name,theme,shares,avg_cost,open_date,last_price,high_since_open,started)
        VALUES (:code,:name,:theme,:shares,:avg_cost,:open_date,:last_price,:high_since_open,:started)
        ON CONFLICT(code) DO UPDATE SET
            shares=excluded.shares, avg_cost=excluded.avg_cost, last_price=excluded.last_price,
            high_since_open=excluded.high_since_open, started=excluded.started
    """, {
        "code": pos["code"], "name": pos.get("name"), "theme": pos.get("theme"),
        "shares": pos["shares"], "avg_cost": pos["avg_cost"], "open_date": pos["open_date"],
        "last_price": pos.get("last_price"), "high_since_open": pos.get("high_since_open"),
        "started": pos.get("started", 0),
    })
    conn.commit()
    conn.close()


def update_position_price(code, last_price, high_since_open=None, started=None):
    conn = get_conn()
    if high_since_open is not None and started is not None:
        conn.execute("UPDATE positions SET last_price=?, high_since_open=?, started=? WHERE code=?",
                     (last_price, high_since_open, started, code))
    else:
        conn.execute("UPDATE positions SET last_price=? WHERE code=?", (last_price, code))
    conn.commit()
    conn.close()


def remove_position(code):
    conn = get_conn()
    conn.execute("DELETE FROM positions WHERE code=?", (code,))
    conn.commit()
    conn.close()


# --------------------------- 交易 ---------------------------
def record_trade(t):
    conn = get_conn()
    conn.execute("""
        INSERT INTO trades (ts,trade_date,code,name,theme,side,price,shares,amount,pnl,pnl_pct,reason)
        VALUES (:ts,:trade_date,:code,:name,:theme,:side,:price,:shares,:amount,:pnl,:pnl_pct,:reason)
    """, {
        "ts": t.get("ts", datetime.now().isoformat(timespec="seconds")),
        "trade_date": t["trade_date"], "code": t["code"], "name": t.get("name"),
        "theme": t.get("theme"), "side": t["side"], "price": t["price"],
        "shares": t["shares"], "amount": t["amount"],
        "pnl": t.get("pnl", 0), "pnl_pct": t.get("pnl_pct", 0), "reason": t.get("reason", ""),
    })
    conn.commit()
    conn.close()


def get_trades(limit=200):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def trades_count_on(trade_date, side=None, reason_like=None):
    conn = get_conn()
    q = "SELECT COUNT(*) AS n FROM trades WHERE trade_date=?"
    args = [trade_date]
    if side:
        q += " AND side=?"
        args.append(side)
    if reason_like:
        q += " AND reason LIKE ?"
        args.append(f"%{reason_like}%")
    row = conn.execute(q, args).fetchone()
    conn.close()
    return row["n"]


# --------------------------- 净值 ---------------------------
def upsert_equity(rec):
    conn = get_conn()
    conn.execute("""
        INSERT INTO equity_curve (trade_date,cash,market_value,total_equity,daily_return,cum_return)
        VALUES (:trade_date,:cash,:market_value,:total_equity,:daily_return,:cum_return)
        ON CONFLICT(trade_date) DO UPDATE SET
            cash=excluded.cash, market_value=excluded.market_value,
            total_equity=excluded.total_equity, daily_return=excluded.daily_return,
            cum_return=excluded.cum_return
    """, rec)
    conn.commit()
    conn.close()


def get_equity_curve():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM equity_curve ORDER BY trade_date").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def last_equity():
    conn = get_conn()
    row = conn.execute("SELECT * FROM equity_curve ORDER BY trade_date DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


# --------------------------- 情绪 ---------------------------
def upsert_sentiment(rec):
    conn = get_conn()
    conn.execute("""
        INSERT INTO market_sentiment
            (trade_date,limit_up_count,total_amount,index_pct,index_open_pct,regime,tradable,note)
        VALUES (:trade_date,:limit_up_count,:total_amount,:index_pct,:index_open_pct,:regime,:tradable,:note)
        ON CONFLICT(trade_date) DO UPDATE SET
            limit_up_count=excluded.limit_up_count, total_amount=excluded.total_amount,
            index_pct=excluded.index_pct, index_open_pct=excluded.index_open_pct,
            regime=excluded.regime, tradable=excluded.tradable, note=excluded.note
    """, rec)
    conn.commit()
    conn.close()


def get_sentiment(trade_date=None):
    conn = get_conn()
    if trade_date:
        row = conn.execute("SELECT * FROM market_sentiment WHERE trade_date=?", (trade_date,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def get_sentiment_history(limit=30):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


# --------------------------- 扫描日志 ---------------------------
def log_scan(phase, message, signals=None, trade_date=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO scan_log (ts,trade_date,phase,message,signals) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), trade_date, phase, message,
         json.dumps(signals, ensure_ascii=False) if signals else None),
    )
    conn.commit()
    conn.close()


def get_scan_log(limit=50):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reset_all():
    """清库重置（重新种数据时用）。"""
    conn = get_conn()
    for t in ["positions", "trades", "equity_curve", "market_sentiment", "scan_log"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE account SET cash=?, initial_capital=? WHERE id=1",
                 (INITIAL_CAPITAL, INITIAL_CAPITAL))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
    print("account:", get_account())
