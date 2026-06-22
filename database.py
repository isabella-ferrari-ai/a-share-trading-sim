# -*- coding: utf-8 -*-
"""SQLite 持久化层（重构版）。

关键变化：
- trades 增加 signal_date（信号产生日=T）、execute_date（实际成交日=T+1）、status；
- 新增 candidate_pool（每日 T 收盘后动态生成的候选股池）；
- market_sentiment.limit_up_count 改为全市场真实涨停家数。

支持双库：默认 data/trading.db（实时/前向模拟）；回测用 data/backtest.db。
通过环境变量 TRADING_DB 切换，或调用 set_db_path()。
"""
import os
import sqlite3
import json
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "trading.db")
DB_PATH = os.environ.get("TRADING_DB", DEFAULT_DB)

INITIAL_CAPITAL = 1_000_000.0


def set_db_path(path):
    global DB_PATH
    DB_PATH = path


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            initial_capital REAL NOT NULL,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            code TEXT PRIMARY KEY,
            name TEXT,
            theme TEXT,
            shares INTEGER NOT NULL,
            avg_cost REAL NOT NULL,
            open_date TEXT NOT NULL,         -- 买入成交日(T+1)，T+1判定基准
            signal_date TEXT,                -- 产生买入信号的日(T)
            last_price REAL,
            high_since_open REAL,
            started INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            signal_date TEXT,                -- 信号日 T
            execute_date TEXT NOT NULL,      -- 成交日 T+1
            trade_date TEXT,                 -- = execute_date（兼容旧字段）
            code TEXT NOT NULL,
            name TEXT,
            theme TEXT,
            side TEXT NOT NULL,              -- BUY / SELL
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            amount REAL NOT NULL,
            pnl REAL DEFAULT 0,
            pnl_pct REAL DEFAULT 0,
            status TEXT DEFAULT 'FILLED',    -- FILLED / REJECTED
            reason TEXT
        )
    """)
    # 被拒绝的订单（涨停无法买入 / 跌停无法卖出 / 顺延）
    c.execute("""
        CREATE TABLE IF NOT EXISTS rejected_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            signal_date TEXT,
            attempt_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,
            reason TEXT
        )
    """)
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_sentiment (
            trade_date TEXT PRIMARY KEY,
            limit_up_count INTEGER,          -- 全市场真实涨停家数
            limit_down_count INTEGER,        -- 跌停家数
            total_amount REAL,               -- 两市成交额(亿元)
            index_pct REAL,
            index_open_pct REAL,
            regime TEXT,
            tradable INTEGER,
            note TEXT
        )
    """)
    # 每日动态候选池
    c.execute("""
        CREATE TABLE IF NOT EXISTS candidate_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,       -- T 日收盘生成
            rank INTEGER,
            code TEXT NOT NULL,
            name TEXT,
            boards INTEGER,                  -- 连板高度
            vol_ratio REAL,
            turn REAL,
            amount REAL,
            pct REAL,
            score REAL,
            reason TEXT,
            UNIQUE(signal_date, code)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            trade_date TEXT,
            phase TEXT,
            message TEXT,
            signals TEXT
        )
    """)
    conn.commit()
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
    conn.execute("UPDATE account SET cash=?, updated_at=? WHERE id=1",
                 (cash, datetime.now().isoformat(timespec="seconds")))
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
        INSERT INTO positions (code,name,theme,shares,avg_cost,open_date,signal_date,last_price,high_since_open,started)
        VALUES (:code,:name,:theme,:shares,:avg_cost,:open_date,:signal_date,:last_price,:high_since_open,:started)
        ON CONFLICT(code) DO UPDATE SET
            shares=excluded.shares, avg_cost=excluded.avg_cost, last_price=excluded.last_price,
            high_since_open=excluded.high_since_open, started=excluded.started
    """, {
        "code": pos["code"], "name": pos.get("name"), "theme": pos.get("theme"),
        "shares": pos["shares"], "avg_cost": pos["avg_cost"], "open_date": pos["open_date"],
        "signal_date": pos.get("signal_date"),
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
        INSERT INTO trades (ts,signal_date,execute_date,trade_date,code,name,theme,side,price,shares,amount,pnl,pnl_pct,status,reason)
        VALUES (:ts,:signal_date,:execute_date,:trade_date,:code,:name,:theme,:side,:price,:shares,:amount,:pnl,:pnl_pct,:status,:reason)
    """, {
        "ts": t.get("ts", datetime.now().isoformat(timespec="seconds")),
        "signal_date": t.get("signal_date"),
        "execute_date": t["execute_date"],
        "trade_date": t.get("trade_date", t["execute_date"]),
        "code": t["code"], "name": t.get("name"), "theme": t.get("theme"),
        "side": t["side"], "price": t["price"], "shares": t["shares"], "amount": t["amount"],
        "pnl": t.get("pnl", 0), "pnl_pct": t.get("pnl_pct", 0),
        "status": t.get("status", "FILLED"), "reason": t.get("reason", ""),
    })
    conn.commit()
    conn.close()


def get_trades(limit=200):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def trades_count_on(execute_date, side=None, reason_like=None):
    conn = get_conn()
    q = "SELECT COUNT(*) AS n FROM trades WHERE execute_date=? AND status='FILLED'"
    args = [execute_date]
    if side:
        q += " AND side=?"
        args.append(side)
    if reason_like:
        q += " AND reason LIKE ?"
        args.append(f"%{reason_like}%")
    row = conn.execute(q, args).fetchone()
    conn.close()
    return row["n"]


def record_rejected(r):
    conn = get_conn()
    conn.execute("""
        INSERT INTO rejected_orders (ts,signal_date,attempt_date,code,name,side,reason)
        VALUES (?,?,?,?,?,?,?)
    """, (r.get("ts", datetime.now().isoformat(timespec="seconds")),
          r.get("signal_date"), r["attempt_date"], r["code"], r.get("name"),
          r["side"], r.get("reason", "")))
    conn.commit()
    conn.close()


def get_rejected(limit=100):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM rejected_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
            (trade_date,limit_up_count,limit_down_count,total_amount,index_pct,index_open_pct,regime,tradable,note)
        VALUES (:trade_date,:limit_up_count,:limit_down_count,:total_amount,:index_pct,:index_open_pct,:regime,:tradable,:note)
        ON CONFLICT(trade_date) DO UPDATE SET
            limit_up_count=excluded.limit_up_count, limit_down_count=excluded.limit_down_count,
            total_amount=excluded.total_amount, index_pct=excluded.index_pct,
            index_open_pct=excluded.index_open_pct, regime=excluded.regime,
            tradable=excluded.tradable, note=excluded.note
    """, {
        "trade_date": rec["trade_date"], "limit_up_count": rec.get("limit_up_count"),
        "limit_down_count": rec.get("limit_down_count"), "total_amount": rec.get("total_amount"),
        "index_pct": rec.get("index_pct"), "index_open_pct": rec.get("index_open_pct"),
        "regime": rec.get("regime"), "tradable": rec.get("tradable"), "note": rec.get("note"),
    })
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


def get_sentiment_history(limit=60):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


# --------------------------- 候选池 ---------------------------
def save_candidates(signal_date, cands):
    conn = get_conn()
    conn.execute("DELETE FROM candidate_pool WHERE signal_date=?", (signal_date,))
    for i, c in enumerate(cands):
        conn.execute("""
            INSERT OR REPLACE INTO candidate_pool
                (signal_date,rank,code,name,boards,vol_ratio,turn,amount,pct,score,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (signal_date, i + 1, c["code"], c.get("name"), c.get("boards"),
              c.get("vol_ratio"), c.get("turn"), c.get("amount"), c.get("pctChg"),
              c.get("score"), c.get("reason")))
    conn.commit()
    conn.close()


def get_candidates(signal_date=None, limit=20):
    conn = get_conn()
    if signal_date is None:
        row = conn.execute("SELECT signal_date FROM candidate_pool ORDER BY signal_date DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return []
        signal_date = row["signal_date"]
    rows = conn.execute(
        "SELECT * FROM candidate_pool WHERE signal_date=? ORDER BY rank LIMIT ?", (signal_date, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    conn = get_conn()
    for t in ["positions", "trades", "rejected_orders", "equity_curve",
              "market_sentiment", "candidate_pool", "scan_log"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE account SET cash=?, initial_capital=? WHERE id=1",
                 (INITIAL_CAPITAL, INITIAL_CAPITAL))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
    print("account:", get_account())
