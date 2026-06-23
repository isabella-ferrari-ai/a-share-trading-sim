# -*- coding: utf-8 -*-
"""专业 A 股数据工具（financial-tool）HTTP 客户端 —— MCP-over-HTTP(streamable)。

振威 2026-06-23 要求：策略程序应直接用专业 A 股数据查询工具（或其背后 API）取数。
该工具是 VisionClaw 的 financial-tool 服务，走 MCP streamable-HTTP 协议：
  initialize -> notifications/initialized -> tools/call。每会话需 Mcp-Session-Id。

本模块把它包成普通函数，供独立 pm2 python 进程（scheduler/engine）调用，无需 MCP SDK：
  - get_bars(symbol, start, end)        日线 OHLCV（实盘增量/校验用）
  - get_snapshots(symbols)              实时快照（盘中风控主源，替代单一脆弱的腾讯源）
  - search_instrument(query)            名称->代码

数据口径注意（诚实声明）：
  - 该工具日线仅 OHLCV+volume，缺 preclose/amount/turn/tradestatus/pctChg/isST/industry，
    故日线面板仍以 baostock 为准；本工具用于①实时快照主源 ②交叉校验。
  - A 股 symbol 为六位（如 600549），本模块自动在 bs代码(sh.600549) 与六位间转换。

配置来自 ~/.visionclaw/profiles/default/config.json 的 financialToolBaseUrl/financialToolApiKey，
也可用环境变量 FINANCIAL_TOOL_BASE_URL / FINANCIAL_TOOL_API_KEY 覆盖。失败安全降级（返回空）。
"""
import os
import json
import ssl
import time
import urllib.request

_CFG_PATH = os.path.expanduser("~/.visionclaw/profiles/default/config.json")
_PROTOCOL = "2025-06-18"
_CTX = ssl.create_default_context()


def _load_cfg():
    base = os.environ.get("FINANCIAL_TOOL_BASE_URL")
    key = os.environ.get("FINANCIAL_TOOL_API_KEY")
    if base and key:
        return base, key
    try:
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            c = json.load(f)
        base = base or c.get("financialToolBaseUrl")
        key = key or c.get("financialToolApiKey")
    except Exception:
        pass
    return base, key


_BASE, _KEY = _load_cfg()
_MCP_URL = (_BASE.rstrip("/") + "/mcp") if _BASE else None


# --------------------------- 代码格式互转 ---------------------------
def to_six(code):
    """sh.600549 / sz.000657 / 600549 -> 600549（六位）。"""
    if not code:
        return code
    if "." in code:
        return code.split(".", 1)[1]
    return code


def to_bs(six, ref=None):
    """六位 -> bs代码。优先用 ref（原 bs 代码）保前缀；否则按 6/(0/3) 猜 sh/sz。"""
    if ref and "." in ref:
        return ref
    if "." in six:
        return six
    return ("sh." if six.startswith("6") else "sz.") + six


# --------------------------- MCP 会话 ---------------------------
def _headers(sid=None):
    h = {"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": _PROTOCOL}
    if sid:
        h["Mcp-Session-Id"] = sid
    return h


def _post(body, sid=None, timeout=20):
    req = urllib.request.Request(_MCP_URL, data=json.dumps(body).encode(),
                                 headers=_headers(sid), method="POST")
    r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
    return r.headers.get("Mcp-Session-Id"), r.read().decode()


def _parse_payload(raw):
    """MCP 响应可能是纯 JSON 或 SSE(data: {...})。解析出 tools/call 的结果 JSON。"""
    txt = raw
    if "data:" in raw and "{" in raw:
        # SSE：取最后一个 data: 行
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                txt = line[5:].strip()
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    # tools/call 结果在 result.content[0].text（又是一层 JSON 字符串）
    res = obj.get("result", {})
    content = res.get("content")
    if isinstance(content, list) and content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except Exception:
            return content[0]["text"]
    return res or obj


def _session():
    """建立 MCP 会话，返回 sid；失败返回 None。"""
    if not _MCP_URL or not _KEY:
        return None
    try:
        sid, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "a-share-sim", "version": "1.0"}}})
        if not sid:
            return None
        # initialized 通知（无响应体）
        req = urllib.request.Request(
            _MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
            headers=_headers(sid), method="POST")
        try:
            urllib.request.urlopen(req, timeout=20, context=_CTX)
        except Exception:
            pass
        return sid
    except Exception:
        return None


def _call(tool, args, sid=None, retries=2):
    """调用一个工具，自动建会话+重试。返回解析后的结果（dict/list）或 None。"""
    for attempt in range(retries):
        s = sid or _session()
        if not s:
            time.sleep(0.4)
            continue
        try:
            _, raw = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": tool, "arguments": args}}, sid=s)
            return _parse_payload(raw)
        except Exception:
            sid = None
            time.sleep(0.4)
    return None


# --------------------------- 对外 API ---------------------------
def available():
    """配置是否就绪（base+key）。"""
    return bool(_MCP_URL and _KEY)


def get_bars(symbol, start, end, unit="day", multiplier=1):
    """日线 OHLCV。symbol 接受 bs代码或六位；返回 [{date,open,high,low,close,volume}]。"""
    res = _call("financial_get_bars", {
        "symbol": to_six(symbol), "market": "A", "from": start, "to": end,
        "timeframe": {"multiplier": multiplier, "unit": unit}})
    rows = (res or {}).get("results") if isinstance(res, dict) else None
    out = []
    for r in rows or []:
        ts = r.get("ts", "")
        date = ts[:10] if ts else None
        out.append({"date": date, "open": r.get("open"), "high": r.get("high"),
                    "low": r.get("low"), "close": r.get("close"),
                    "volume": r.get("volume")})
    return out


def get_snapshots(symbols):
    """实时快照。symbols: bs代码或六位列表。
    返回 {bs代码: {price,preclose,high,low,close,pct,volume,ts}}。失败/缺配置返回 {}。
    pct 由 (price/previousDay.close-1) 计算（工具未直接给涨跌幅）。"""
    if not symbols:
        return {}
    six_list = [to_six(c) for c in symbols]
    ref = {to_six(c): c for c in symbols}   # 六位 -> 原代码，保前缀
    out = {}
    # 工具单次最多 50 只
    sid = _session()
    if not sid:
        return {}
    for i in range(0, len(six_list), 50):
        chunk = six_list[i:i + 50]
        res = _call("financial_get_snapshots", {"symbols": chunk, "market": "A"}, sid=sid)
        rows = (res or {}).get("results") if isinstance(res, dict) else None
        for r in rows or []:
            six = str(r.get("symbol"))
            day = r.get("day") or {}
            prev = r.get("previousDay") or {}
            last = r.get("lastTrade") or {}
            price = last.get("price") if last.get("price") is not None else day.get("close")
            preclose = prev.get("close")
            pct = None
            if price is not None and preclose:
                try:
                    pct = round((price / preclose - 1) * 100, 2)
                except Exception:
                    pct = None
            bs = to_bs(six, ref.get(six))
            out[bs] = {"code": bs, "price": price, "preclose": preclose,
                       "high": day.get("high"), "low": day.get("low"),
                       "close": day.get("close"), "pct": pct,
                       "volume": day.get("volume"),
                       "ts": (last.get("ts") or r.get("updatedAt") or "")}
    return out


def search_instrument(query):
    """名称/代码搜索，返回 [{symbol,name}]。"""
    res = _call("financial_search_instruments",
                {"query": query, "market": "A", "limit": 10})
    return (res or {}).get("results", []) if isinstance(res, dict) else []


if __name__ == "__main__":
    import sys
    print("available:", available(), "base:", _BASE)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snap"
    if cmd == "snap":
        print(get_snapshots(["sh.600549", "sz.000657", "601958"]))
    elif cmd == "bars":
        for r in get_bars(sys.argv[2] if len(sys.argv) > 2 else "600549",
                          "2026-06-15", "2026-06-20"):
            print(r)
    elif cmd == "search":
        print(search_instrument(sys.argv[2] if len(sys.argv) > 2 else "厦门钨业"))
