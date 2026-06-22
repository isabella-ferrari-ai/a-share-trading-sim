# -*- coding: utf-8 -*-
"""数据获取层。

交易模型：T日收盘选股，T+1开盘买入，遵守T+1规则。
关键原则：买入决策只使用 T 日收盘可见的日线数据；买入成交在 T+1 开盘价执行。

数据源：
1. baostock 日线（免费、稳定、含 isST/tradestatus/turn 字段）：
   - 日线面板缓存到本地 SQLite（data/panel.db），可断点续传。
   - 股票池：沪深300 + 中证500 + 中证1000 成分股并集（约 1800 只）。
2. 东方财富 push2 实时快照（直连，分页+重试）：
   - 用于 SIM_START 前收集市场情绪数据（涨停数）。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import json
import time
import sqlite3
import urllib.request
from contextlib import contextmanager

import baostock as bs
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DB = os.path.join(BASE_DIR, "data", "panel.db")

INDEX_CODE = "sh.000001"   # 上证指数
KCB50_CODE = "sh.000688"   # 科创50（备用）

# baostock 日线字段
K_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"
NUM_COLS = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]


# --------------------------------------------------------------------------
# 涨跌停阈值 / 板块判定
# --------------------------------------------------------------------------
def limit_pct(code):
    """根据代码返回当日涨停幅度阈值（剔除 ST 后）。
    主板(60/00) 10%；创业板(30)/科创板(68) 20%；北交所(8/4) 30%(本系统不纳入)。"""
    c = code.split(".")[-1] if "." in code else code
    if c.startswith("68") or c.startswith("30"):
        return 0.20
    if c.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def board_name(code):
    c = code.split(".")[-1] if "." in code else code
    if c.startswith("68"):
        return "科创板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


def to_bs_code(code):
    """六位代码 -> baostock 格式 sh./sz.。"""
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return "sh." + code
    if code.startswith(("0", "3", "2")):
        return "sz." + code
    if code.startswith(("8", "4")):
        return "bj." + code
    return "sz." + code


def to_em_secid(code):
    """baostock/六位代码 -> 东方财富 secid（1.沪 / 0.深）。"""
    c = code.split(".")[-1] if "." in code else code
    pre = code.split(".")[0] if "." in code else None
    if pre == "sh" or c.startswith(("6", "9")):
        return "1." + c
    return "0." + c


# --------------------------------------------------------------------------
# baostock 会话
# --------------------------------------------------------------------------
@contextmanager
def bs_session():
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        yield
    finally:
        bs.logout()


def _kdata(code, start, end, adjust="2"):
    rs = bs.query_history_k_data_plus(
        code, K_FIELDS, start_date=start, end_date=end, frequency="d", adjustflag=adjust
    )
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=K_FIELDS.split(","))
    df = pd.DataFrame(rows, columns=rs.fields)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


def get_bars(code, start, end, adjust="2"):
    """单只股票日线（adjust: 1后复权 2前复权 3不复权）。短线模拟用不复权(3)以贴合真实涨跌停价。"""
    return _kdata(to_bs_code(code), start, end, adjust=adjust)


def get_index(start, end, code=INDEX_CODE):
    return _kdata(code, start, end, adjust="3")


def get_trade_dates(start, end):
    rs = bs.query_trade_dates(start_date=start, end_date=end)
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        return []
    return df[df["is_trading_day"] == "1"]["calendar_date"].tolist()


def get_all_basics():
    """全市场证券基础信息（含 ipoDate / type / status）。type=1 股票, status=1 上市。"""
    rs = bs.query_stock_basic()
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    return df


def _bs_index_codes(fn):
    """调用 baostock 指数成分函数，返回 baostock 代码集合(sh./sz.)。"""
    rs = getattr(bs, fn)()
    codes = set()
    while (rs.error_code == "0") and rs.next():
        row = rs.get_row_data()
        # 字段含 code(如 sh.600000)
        for v in row:
            if isinstance(v, str) and (v.startswith("sh.") or v.startswith("sz.")):
                codes.add(v)
                break
    return codes


def index_universe():
    """沪深300 + 中证500 + 中证1000 成分股并集（baostock 代码）。约 1800 只。
    HS300/ZZ500 用 baostock；CSI1000 用 akshare index_stock_cons_csindex('000852')。"""
    codes = set()
    # baostock 在 bs_session 内调用
    try:
        codes |= _bs_index_codes("query_hs300_stocks")
    except Exception as e:
        print(f"[universe] hs300 ERR {repr(e)[:80]}")
    try:
        codes |= _bs_index_codes("query_zz500_stocks")
    except Exception as e:
        print(f"[universe] zz500 ERR {repr(e)[:80]}")
    # CSI1000 via akshare
    try:
        import akshare as ak
        df1000 = ak.index_stock_cons_csindex(symbol="000852")
        for c in df1000["成分券代码"].astype(str):
            codes.add(to_bs_code(c.zfill(6)))
    except Exception as e:
        print(f"[universe] csi1000 ERR {repr(e)[:80]}")
    return codes


def tradable_universe(basics, as_of_date, min_listed_days=180):
    """从全市场基础信息中筛出可交易股票池（剔除指数/B股/北交所/新股/退市）。
    返回 list[dict(code,name,ipoDate)]。ST 在日线层用 isST 字段过滤。"""
    out = []
    cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=min_listed_days)
    for _, r in basics.iterrows():
        if str(r.get("type")) != "1":          # 仅股票
            continue
        if str(r.get("status")) != "1":         # 仅在市
            continue
        code = r["code"]
        c = code.split(".")[-1]
        if code.startswith("bj.") or c.startswith(("8", "4", "92")):  # 不纳入北交所
            continue
        if not c.startswith(("60", "00", "30", "68")):
            continue
        ipo = str(r.get("ipoDate") or "")
        if ipo:
            try:
                if pd.Timestamp(ipo) > cutoff:   # 新股 < min_listed_days 天剔除
                    continue
            except Exception:
                pass
        out.append({"code": code, "name": r.get("code_name"), "ipoDate": ipo})
    return out


# --------------------------------------------------------------------------
# 全 A 日线面板缓存（回测用，可断点续传）
# --------------------------------------------------------------------------
def _panel_conn(path=PANEL_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _panel_init(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            preclose REAL, volume REAL, amount REAL, turn REAL,
            tradestatus INTEGER, pctChg REAL, isST INTEGER,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS basics (
            code TEXT PRIMARY KEY, name TEXT, ipoDate TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_meta (
            code TEXT, start TEXT, end TEXT, fetched_at TEXT,
            PRIMARY KEY (code, start, end)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date)")
    conn.commit()


def build_panel(start, end, lookback_days=40, path=PANEL_DB, progress_every=200):
    """抓取指数成分股日线到本地面板库（沪深300+中证500+中证1000）。
    lookback_days 为信号回溯（连板/均量）所需的前置历史。
    断点续传：已抓取过相同 (code,start,end) 的股票跳过。"""
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
    conn = _panel_conn(path)
    _panel_init(conn)
    with bs_session():
        basics = get_all_basics()
        uni = tradable_universe(basics, start)
        # 限定为沪深300 + 中证500 + 中证1000 成分股并集
        idx_codes = index_universe()
        if idx_codes:
            uni = [u for u in uni if u["code"] in idx_codes]
            print(f"[panel] 沪深300+中证500+中证1000 共{len(idx_codes)}只 -> 可交易{len(uni)}只")
        # 存基础信息
        conn.executemany(
            "INSERT OR REPLACE INTO basics(code,name,ipoDate) VALUES(?,?,?)",
            [(u["code"], u["name"], u["ipoDate"]) for u in uni],
        )
        conn.commit()
        done = {r[0] for r in conn.execute(
            "SELECT code FROM fetch_meta WHERE start=? AND end=?", (fetch_start, end)
        ).fetchall()}
        todo = [u for u in uni if u["code"] not in done]
        print(f"[panel] universe={len(uni)} done={len(done)} todo={len(todo)} range={fetch_start}~{end}")
        n = 0
        for u in todo:
            code = u["code"]
            try:
                df = _kdata(code, fetch_start, end, adjust="3")  # 不复权，贴合真实涨跌停
            except Exception as e:
                print(f"[panel] {code} ERR {repr(e)[:80]}")
                time.sleep(0.5)
                continue
            if not df.empty:
                recs = [
                    (code, r["date"], r["open"], r["high"], r["low"], r["close"],
                     r["preclose"], r["volume"], r["amount"], r["turn"],
                     int(float(r["tradestatus"])) if str(r["tradestatus"]).strip() not in ("", "nan") else 1,
                     r["pctChg"],
                     int(float(r["isST"])) if str(r["isST"]).strip() not in ("", "nan") else 0)
                    for _, r in df.iterrows()
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", recs
                )
            conn.execute(
                "INSERT OR REPLACE INTO fetch_meta(code,start,end,fetched_at) VALUES(?,?,?,?)",
                (code, fetch_start, end, pd.Timestamp("now").isoformat()),
            )
            n += 1
            if n % progress_every == 0:
                conn.commit()
                print(f"[panel] {n}/{len(todo)} fetched ...")
        conn.commit()
    conn.close()
    print(f"[panel] done, fetched {n} new stocks")


def load_panel(path=PANEL_DB):
    """读出面板库为 {code: DataFrame(按date升序)} 与 basics dict。"""
    conn = _panel_conn(path)
    bars = pd.read_sql_query("SELECT * FROM bars ORDER BY code,date", conn)
    basics = pd.read_sql_query("SELECT * FROM basics", conn)
    conn.close()
    bmap = {c: g.reset_index(drop=True) for c, g in bars.groupby("code")}
    nmap = {r["code"]: r["name"] for _, r in basics.iterrows()}
    return bmap, nmap


def panel_dates(path=PANEL_DB):
    conn = _panel_conn(path)
    rows = conn.execute("SELECT DISTINCT date FROM bars ORDER BY date").fetchall()
    conn.close()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# 东方财富全市场实时快照（前向实时模拟用）
# --------------------------------------------------------------------------
_EM_FIELDS = "f12,f14,f2,f3,f5,f6,f8,f20,f21,f15,f16,f17,f18"
# f12代码 f14名称 f2现价 f3涨跌幅 f5成交量(手) f6成交额(元) f8换手率
# f20总市值 f21流通市值 f15最高 f16最低 f17今开 f18昨收


def em_spot(max_pages=60, page_size=200, retries=4, pause=0.2):
    """直连东方财富 push2 分页抓取沪深 A 股全市场快照。返回 DataFrame。"""
    base = ("https://82.push2.eastmoney.com/api/qt/clist/get?"
            "pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f3&"
            "fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=" + _EM_FIELDS)
    rows, total = [], None
    for pn in range(1, max_pages + 1):
        ok = False
        for attempt in range(retries):
            try:
                url = base.format(pn=pn, pz=page_size)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                d = json.loads(urllib.request.urlopen(req, timeout=20).read())
                data = d.get("data")
                if not data:
                    ok = True
                    break
                total = data.get("total", total)
                diff = data.get("diff") or []
                rows.extend(diff)
                ok = True
                break
            except Exception:
                time.sleep(pause * (attempt + 1) * 3)
        if not ok:
            time.sleep(1.0)
        if total and len(rows) >= total:
            break
        time.sleep(pause)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct", "f5": "volume",
        "f6": "amount", "f8": "turn", "f20": "total_mv", "f21": "float_mv",
        "f15": "high", "f16": "low", "f17": "open", "f18": "preclose",
    })
    for c in ["price", "pct", "volume", "amount", "turn", "total_mv", "float_mv", "high", "low", "open", "preclose"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 加 baostock 风格 code
    df["bs_code"] = df["code"].apply(to_bs_code)
    return df


# --------------------------------------------------------------------------
# 腾讯实时行情（qt.gtimg.cn）——本环境东财/akshare 被限流时的主用实时源
# --------------------------------------------------------------------------
# 腾讯返回 GBK 文本，每只股票一行 v_XXX="字段~分隔..."。关键字段下标：
#  1名称 2代码 3现价 4昨收 5今开 6成交量(手) 30时间戳 32涨跌幅% 33最高 34最低
#  36成交量(手) 37成交额(万元) 38换手率% 44流通市值(亿) 45总市值(亿) 47涨停价 48跌停价 49量比
def _tx_code(code):
    """六位/baostock 代码 -> 腾讯代码 sh600000 / sz000001。"""
    c = code.split(".")[-1] if "." in code else code
    pre = code.split(".")[0] if "." in code else None
    if pre == "sh" or c.startswith(("6", "9")):
        return "sh" + c
    if pre == "bj" or c.startswith(("8", "4", "92")):
        return "bj" + c
    return "sz" + c


def _tx_fetch_batch(tx_codes, retries=3, timeout=15):
    q = ",".join(tx_codes)
    url = "https://qt.gtimg.cn/q=" + q
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
            )
            return urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    return ""


def tx_spot(codes, batch=60, pause=0.05):
    """腾讯批量实时快照。codes: 六位或 baostock 代码列表。返回 DataFrame，
    列与 em_spot 对齐：code,name,price,pct,volume,amount,turn,total_mv,float_mv,
    high,low,open,preclose,limit_up,limit_down,vol_ratio,ts,bs_code。"""
    rows = []
    codes = list(codes)
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        txc = [_tx_code(c) for c in chunk]
        raw = _tx_fetch_batch(txc)
        if not raw:
            continue
        for line in raw.strip().split("\n"):
            if "=" not in line or '"' not in line:
                continue
            body = line.split('"', 1)[1].rsplit('"', 1)[0]
            p = body.split("~")
            if len(p) < 50 or not p[2]:
                continue
            def _f(idx):
                try:
                    return float(p[idx])
                except Exception:
                    return None
            code6 = p[2]
            rows.append({
                "code": code6, "name": p[1], "price": _f(3), "preclose": _f(4),
                "open": _f(5), "high": _f(33), "low": _f(34), "pct": _f(32),
                "volume": _f(6), "amount": (_f(37) * 1e4) if _f(37) is not None else None,
                "turn": _f(38), "float_mv": (_f(44) * 1e8) if _f(44) is not None else None,
                "total_mv": (_f(45) * 1e8) if _f(45) is not None else None,
                "limit_up": _f(47), "limit_down": _f(48), "vol_ratio": _f(49),
                "ts": p[30] if len(p) > 30 else None,
            })
        time.sleep(pause)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["bs_code"] = df["code"].apply(to_bs_code)
    return df


def universe_codes(path=PANEL_DB):
    """股票池六位代码列表（指数成分并集）。优先取实际有日线的代码（bars 表，
    即真实抓取过的指数成分约 1760 只），回退到 basics 全表。"""
    try:
        conn = _panel_conn(path)
        rows = conn.execute("SELECT DISTINCT code FROM bars").fetchall()
        if not rows:
            rows = conn.execute("SELECT code FROM basics").fetchall()
        conn.close()
        return [r[0].split(".")[-1] for r in rows]
    except Exception:
        return []


# 源健康缓存：长跑调度器里某源连续失败后跳过，避免每 15 分钟都白等 akshare/东财超时。
_SRC_DEAD = {"akshare": False, "eastmoney": False}


def _ak_full():
    """akshare 全市场快照（成功返回统一列 DataFrame，失败抛异常）。"""
    import akshare as ak
    raw = ak.stock_zh_a_spot_em()
    if raw is None or raw.empty:
        raise RuntimeError("akshare empty")
    df = raw.rename(columns={
        "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "pct",
        "成交量": "volume", "成交额": "amount", "换手率": "turn",
        "总市值": "total_mv", "流通市值": "float_mv", "最高": "high",
        "最低": "low", "今开": "open", "昨收": "preclose",
    })
    keep = [c for c in ["code", "name", "price", "pct", "volume", "amount", "turn",
                        "total_mv", "float_mv", "high", "low", "open", "preclose"] if c in df.columns]
    df = df[keep].copy()
    for c in keep:
        if c not in ("code", "name"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["bs_code"] = df["code"].astype(str).apply(to_bs_code)
    # 补涨停/跌停价（akshare 无，用昨收×阈值近似）
    df["limit_up"] = df.apply(lambda r: round((r["preclose"] or 0) * (1 + limit_pct(str(r["code"]))), 2)
                              if r.get("preclose") else None, axis=1)
    df["limit_down"] = df.apply(lambda r: round((r["preclose"] or 0) * (1 - limit_pct(str(r["code"]))), 2)
                                if r.get("preclose") else None, axis=1)
    df["vol_ratio"] = None
    return df


def ak_spot(codes=None, prefer="tencent"):
    """实时股票池快照。默认优先腾讯（qt.gtimg.cn，本环境唯一稳定源），
    再回退 akshare 全市场 / 东财直连。
    统一列：code,name,price,pct,volume,amount,turn,total_mv,float_mv,
            high,low,open,preclose,limit_up,limit_down,vol_ratio,bs_code。"""
    if codes is None:
        codes = universe_codes()

    def _try_tencent():
        df = tx_spot(codes)
        if df is not None and not df.empty:
            return df, "tencent"
        return None

    def _try_akshare():
        if _SRC_DEAD["akshare"]:
            return None
        try:
            return _ak_full(), "akshare"
        except Exception as e:
            _SRC_DEAD["akshare"] = True
            print(f"[ak_spot] akshare 标记不可用 {repr(e)[:50]}")
            return None

    def _try_eastmoney():
        if _SRC_DEAD["eastmoney"]:
            return None
        try:
            df = em_spot(max_pages=30)
            if df is not None and not df.empty:
                # em_spot 无 limit_up/down/vol_ratio，补齐
                if "limit_up" not in df.columns:
                    df["limit_up"] = df.apply(lambda r: round((r.get("preclose") or 0) * (1 + limit_pct(str(r["code"]))), 2)
                                              if r.get("preclose") else None, axis=1)
                    df["limit_down"] = df.apply(lambda r: round((r.get("preclose") or 0) * (1 - limit_pct(str(r["code"]))), 2)
                                                if r.get("preclose") else None, axis=1)
                    df["vol_ratio"] = None
                return df, "eastmoney"
        except Exception as e:
            _SRC_DEAD["eastmoney"] = True
            print(f"[ak_spot] eastmoney 标记不可用 {repr(e)[:50]}")
        return None

    order = {
        "tencent": [_try_tencent, _try_akshare, _try_eastmoney],
        "akshare": [_try_akshare, _try_eastmoney, _try_tencent],
    }.get(prefer, [_try_tencent, _try_akshare, _try_eastmoney])

    for fn in order:
        r = fn()
        if r is not None:
            return r
    return pd.DataFrame(), "none"


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "panel":
        s = sys.argv[2] if len(sys.argv) > 2 else "2025-07-01"
        e = sys.argv[3] if len(sys.argv) > 3 else "2025-08-31"
        build_panel(s, e)
    elif len(sys.argv) >= 2 and sys.argv[1] == "spot":
        df, src = ak_spot()
        print("spot source:", src, "rows:", len(df))
        if not df.empty:
            print(df.head(5).to_string())
    else:
        with bs_session():
            print("trade dates sample:", get_trade_dates("2025-07-01", "2025-07-10"))
