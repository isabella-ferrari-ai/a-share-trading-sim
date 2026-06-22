# -*- coding: utf-8 -*-
"""策略引擎——盘中实时选股，有机会即交易，遵守 T+1 卖出规则。

核心约束
========
- 盘中每 15 分钟用实时快照扫描全股票池，发现机会即以实时价成交（实时买/卖）；
- 实时买入：个股盘中冲向涨停（实时涨幅接近涨停阈值）+ 历史连板高度合适 + 流动性达标；
  若已封死涨停（实时价≈涨停价）则排队无法成交，标记"涨停无法买入"跳过；
- 严格 T+1：买入当日不可卖出，次日起方可卖；
- 跌停封死的持仓当日无法卖出（标记"跌停无法卖出"），顺延到可成交时；
- 收盘后（settle_close）仍用 baostock 完整日线跑一遍对账/结算。

收盘选股因子（settle / 回测，来自日线）
======================================
- 涨停收盘：pctChg >= 涨停阈值-缓冲 且 close≈high（收在最高即封板）
- 连板高度：连续涨停天数 / 放量比 / 换手率 / 全市场涨停数（情绪）

盘中实时因子
============
- 实时涨幅 pct：盘中冲板/封板判定（接近涨停阈值）
- 历史连板高度：从面板（前一交易日及以前）取连续涨停天数
- 实时量比 / 换手率 / 成交额：流动性与放量
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
import data_fetcher as dfetch

# ----------------------------- 资金/持仓约束 -----------------------------
MAX_POSITIONS = 3            # 同时最多持有
MAX_POSITION_PCT = 0.20      # 单票最大仓位

# ----------------------------- 选股因子阈值 -----------------------------
LIMIT_BUFFER = 0.005         # 涨停判定缓冲（接近阈值即视为涨停）
CLOSE_AT_HIGH_TOL = 0.003    # close 相对 high 的容差（收在最高=封板）
MIN_BOARDS = 1               # 最低连板高度（首板即可入选）
MAX_BOARDS = 4               # 连板过高(>4)风险大，不追
VOL_RATIO_MIN = 1.0          # 最低放量比（不缩量）
TURN_MIN = 2.0               # 最低换手率(%)，确保有承接与流动性
TURN_MAX = 50.0              # 换手过高(出货嫌疑)剔除
TOP_N_CANDIDATES = 20        # 候选池规模

# 流动性过滤（baostock 日线 amount 字段，单位：元）
MIN_AMOUNT = 1e8             # 日成交额 > 1亿

# ----------------------------- 卖出/风控阈值 -----------------------------
STOP_LOSS = -0.05            # 跌破买入价 -5% 止损（按收盘判定，收盘价执行）
STOP_OVERNIGHT_GAP = -0.03   # 次日低开 < -3% 开盘清仓（开盘价执行）
HOLD_MAX_DAYS = 7            # 最长持有自然日
STALL_DAYS = 3              # 持仓3日未启动(未达+8%)止损换股
STARTED_GAIN = 0.08          # "启动"定义：持仓期最高较成本 >= +8%
TARGET_PROFIT = 0.10         # 目标止盈中值(+8%~+15%)
TARGET_PROFIT_HI = 0.15
OPEN_BOARD_DROP = -0.03      # 封板后开板近似：持有期内某日大幅转弱
MAX_STOPS_PER_DAY = 2        # 当日止损≥2次收手

# ----------------------------- 市场情绪分级 -----------------------------
# 用全市场真实涨停家数分级
SENTIMENT_TIERS = [
    (120, "极强", 1),
    (80,  "强",   1),
    (40,  "中性", 1),
    (20,  "弱",   1),
    (0,   "极弱", 0),   # 极弱 -> 空仓不操作
]
INDEX_LOW_OPEN_LIMIT = -0.01  # 大盘当日低开 > 1% 不开新仓


# ==========================================================================
# 市场情绪
# ==========================================================================
def classify_sentiment(limit_up_count):
    """按真实全市场涨停家数分级，返回 (regime, tradable)。"""
    if limit_up_count is None:
        return "未知", 1
    for thr, name, tradable in SENTIMENT_TIERS:
        if limit_up_count >= thr:
            return name, tradable
    return "极弱", 0


# ==========================================================================
# 选股信号（T 日收盘）
# ==========================================================================
def is_limit_up(row, limit):
    """该日是否涨停收盘（封板）。row 需含 pctChg, close, high。"""
    pct = (row.get("pctChg") or 0) / 100.0
    if pct < limit - LIMIT_BUFFER:
        return False
    high = row.get("high") or 0
    close = row.get("close") or 0
    if high <= 0:
        return False
    return (high - close) / high <= CLOSE_AT_HIGH_TOL


def consecutive_boards(df, idx, limit):
    """计算截至第 idx 行（含）的连续涨停天数（连板高度）。df 按日期升序。"""
    n = 0
    i = idx
    while i >= 0:
        row = df.iloc[i]
        lp = dfetch.limit_pct(row["code"]) if "code" in row else limit
        if is_limit_up(row, lp):
            n += 1
            i -= 1
        else:
            break
    return n


def volume_ratio(df, idx, window=5):
    """当日成交量 / 过去 window 日均量（不含当日）。"""
    if idx < window:
        return None
    vols = df["volume"].iloc[idx - window:idx]
    avg = vols.mean()
    if not avg or avg <= 0:
        return None
    return float(df["volume"].iloc[idx] / avg)


def evaluate_candidate(df, idx):
    """对单只股票在第 idx 行(=T日)评估是否为候选。df 按日期升序，含 code 列。
    返回 dict(score, reason, factors) 或 None。"""
    row = df.iloc[idx]
    code = row["code"]
    if int(row.get("tradestatus", 1)) != 1:      # 停牌
        return None
    if int(row.get("isST", 0)) == 1:             # ST
        return None
    limit = dfetch.limit_pct(code)
    if not is_limit_up(row, limit):              # 必须涨停收盘
        return None
    boards = consecutive_boards(df, idx, limit)
    if boards < MIN_BOARDS or boards > MAX_BOARDS:
        return None
    amount = row.get("amount") or 0
    if amount < MIN_AMOUNT:                       # 流动性
        return None
    turn = row.get("turn") or 0
    if turn < TURN_MIN or turn > TURN_MAX:
        return None
    vr = volume_ratio(df, idx)
    if vr is not None and vr < VOL_RATIO_MIN:    # 不能缩量涨停（缩量一字除外，这里从严）
        # 一字板连板缩量是正常的（封死无量），放行连板>=2 的缩量
        if boards < 2:
            return None
    # 评分：连板高度为主，放量与换手为辅
    score = boards * 100
    if vr:
        score += min(vr, 5) * 10
    score += min(turn, 20)
    return {
        "code": code,
        "boards": boards,
        "vol_ratio": round(vr, 2) if vr else None,
        "turn": round(turn, 2),
        "amount": round(amount, 0),
        "pctChg": round(row.get("pctChg") or 0, 2),
        "close": row.get("close"),
        "score": round(score, 2),
        "reason": f"{boards}连板封涨停"
                  + (f" 放量{vr:.1f}x" if vr else "")
                  + f" 换手{turn:.1f}%",
    }


def select_candidates(panel, names, trade_date, sentiment_tradable, top_n=TOP_N_CANDIDATES):
    """对全市场面板在 trade_date(=T日) 选出候选池（按 score 降序，取 top_n）。
    panel: {code: df(升序)}。返回 list[dict]，每个含 name/theme 等。"""
    if not sentiment_tradable:
        return []
    cands = []
    for code, df in panel.items():
        nm = names.get(code, code) or ""
        if "ST" in nm.upper():          # isST 字段不可靠，用名称兜底过滤 ST/*ST
            continue
        # 定位 trade_date 行
        pos = df.index[df["date"] == trade_date]
        if len(pos) == 0:
            continue
        idx = int(pos[0])
        c = evaluate_candidate(df, idx)
        if c:
            c["name"] = nm
            cands.append(c)
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands[:top_n]


# ==========================================================================
# 盘中实时选股（每 15 分钟扫描）
# ==========================================================================
NEAR_LIMIT_MIN = 0.07        # 实时涨幅至少接近涨停的下限（主板>=7%才视为冲板候选）
SEALED_TOL = 0.002           # 实时价距涨停价 <=0.2% 视为已封死（排队无法买入）


def _prior_boards(df, limit, before_date=None):
    """用历史面板计算"截至最近一个交易日"的连续涨停天数（不含今日实时）。
    df 升序，含 code/pctChg/high/close。before_date 给定则只看 < before_date 的行。"""
    if df is None or df.empty:
        return 0
    sub = df if before_date is None else df[df["date"] < before_date]
    if sub.empty:
        return 0
    n = 0
    for i in range(len(sub) - 1, -1, -1):
        row = sub.iloc[i]
        if is_limit_up(row, limit):
            n += 1
        else:
            break
    return n


def evaluate_intraday_candidate(spot_row, hist_df, today=None):
    """盘中实时评估单只票是否为买入候选。
    spot_row: 实时快照行(dict)，含 code,name,price,pct,turn,amount,vol_ratio,limit_up,preclose。
    hist_df: 该票历史日线(升序，全部早于今日)，用于连板高度。
    返回 dict(score,reason,...) 或 None。"""
    code = spot_row.get("code")
    if not code:
        return None
    nm = (spot_row.get("name") or "")
    if "ST" in nm.upper() or "退" in nm:
        return None
    price = spot_row.get("price") or 0
    preclose = spot_row.get("preclose") or 0
    if price <= 0 or preclose <= 0:
        return None
    limit = dfetch.limit_pct(code)
    pct = (spot_row.get("pct") or 0) / 100.0
    # 1) 必须正在冲板：实时涨幅接近涨停
    if pct < max(NEAR_LIMIT_MIN, limit - 0.03):
        return None
    # 2) 流动性
    amount = spot_row.get("amount") or 0
    if amount < MIN_AMOUNT:
        return None
    turn = spot_row.get("turn") or 0
    if turn < TURN_MIN or turn > TURN_MAX:
        return None
    # 3) 连板高度（历史，剔除过高）
    boards = _prior_boards(hist_df, limit) if hist_df is not None else 0
    # 今日这一板尚未计入历史，实际"当前板高" = 历史连板 + (今日是否已涨停)
    cur_board = boards + 1
    if cur_board > MAX_BOARDS:
        return None
    vr = spot_row.get("vol_ratio")
    if vr is not None and vr < VOL_RATIO_MIN and cur_board < 2:
        return None
    score = cur_board * 100
    if vr:
        score += min(vr, 5) * 10
    score += min(turn, 20)
    limit_up_px = spot_row.get("limit_up") or (preclose * (1 + limit))
    sealed = limit_up_px > 0 and (limit_up_px - price) / limit_up_px <= SEALED_TOL
    return {
        "code": code, "name": nm, "boards": cur_board,
        "vol_ratio": round(vr, 2) if vr else None, "turn": round(turn, 2),
        "amount": round(amount, 0), "pctChg": round(spot_row.get("pct") or 0, 2),
        "price": price, "limit_up": limit_up_px, "sealed": sealed,
        "score": round(score, 2),
        "reason": f"盘中{cur_board}板冲涨停(实时{pct*100:.1f}%)"
                  + (f" 量比{vr:.1f}" if vr else "")
                  + f" 换手{turn:.1f}%",
    }


def select_intraday_candidates(spot_df, panel, sentiment_tradable, top_n=TOP_N_CANDIDATES):
    """对实时快照全股票池筛选盘中买入候选，按 score 降序取 top_n。
    spot_df: ak_spot 结果；panel: {code: 历史df(升序)}。返回 list[dict]。"""
    if not sentiment_tradable or spot_df is None or spot_df.empty:
        return []
    cands = []
    for _, sr in spot_df.iterrows():
        row = sr.to_dict()
        hist = panel.get(row.get("code")) or panel.get(dfetch.to_bs_code(row.get("code", "")))
        c = evaluate_intraday_candidate(row, hist)
        if c:
            cands.append(c)
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands[:top_n]


def count_limit_updown_spot(spot_df):
    """用实时快照统计全市场涨停/跌停家数。返回 (limit_up, limit_down, total_amount_yi)。"""
    if spot_df is None or spot_df.empty:
        return 0, 0, None
    up = dn = 0
    total_amount = 0.0
    for _, sr in spot_df.iterrows():
        code = str(sr.get("code") or "")
        price = sr.get("price") or 0
        if price <= 0:
            continue
        lu = sr.get("limit_up")
        ld = sr.get("limit_down")
        total_amount += sr.get("amount") or 0
        if lu and lu > 0 and (lu - price) / lu <= SEALED_TOL:
            up += 1
        elif ld and ld > 0 and (price - ld) / ld <= SEALED_TOL:
            dn += 1
    total_amount_yi = round(total_amount / 1e8, 1) if total_amount else None
    return up, dn, total_amount_yi


def can_buy_spot(spot_row):
    """实时能否买入：已封死涨停则排队无法成交。返回 (bool, reason)。"""
    code = spot_row.get("code")
    price = spot_row.get("price") or 0
    preclose = spot_row.get("preclose") or 0
    if price <= 0:
        return False, "无实时价"
    limit = dfetch.limit_pct(code)
    limit_up_px = spot_row.get("limit_up") or (preclose * (1 + limit) if preclose else 0)
    if limit_up_px > 0 and (limit_up_px - price) / limit_up_px <= SEALED_TOL:
        return False, f"涨停无法买入(封板{price})"
    return True, f"实时买入(现价{price})"


def can_sell_spot(spot_row):
    """实时能否卖出：封死跌停则无法成交。返回 (bool, reason)。"""
    code = spot_row.get("code")
    price = spot_row.get("price") or 0
    preclose = spot_row.get("preclose") or 0
    if price <= 0:
        return False, "无实时价"
    limit = dfetch.limit_pct(code)
    limit_dn_px = spot_row.get("limit_down") or (preclose * (1 - limit) if preclose else 0)
    if limit_dn_px > 0 and (price - limit_dn_px) / limit_dn_px <= SEALED_TOL:
        return False, f"跌停无法卖出(封板{price})"
    return True, ""


def evaluate_sell_intraday(pos, spot_row, current_date):
    """盘中实时卖出判定（用实时价）。返回 (do_sell, sell_price, reason)。严格 T+1。"""
    if pos["open_date"] == current_date:
        return False, None, "T+1当日不可卖"
    cost = pos["avg_cost"]
    price = spot_row.get("price") or 0
    preclose = spot_row.get("preclose") or cost
    if price <= 0:
        return False, None, "无实时价"
    days = _days_held(pos["open_date"], current_date)
    ret = price / cost - 1
    # 1) 跌破买价 -5% 止损
    if ret <= STOP_LOSS:
        return True, price, f"实时跌破买价{STOP_LOSS*100:.0f}%止损({ret*100:.1f}%)"
    # 2) 达到目标 +10% 止盈
    if ret >= TARGET_PROFIT:
        return True, price, f"实时触及+{TARGET_PROFIT*100:.0f}%止盈({ret*100:.1f}%)"
    # 3) 持仓未启动且转弱（当日实时跌幅大）
    pct = (spot_row.get("pct") or 0) / 100.0
    if days >= 1 and pct <= OPEN_BOARD_DROP and not pos.get("started"):
        return True, price, f"实时转弱(当日{pct*100:.1f}%)清仓"
    # 4) 持仓 STALL_DAYS 天未启动止损换股
    if days >= STALL_DAYS and not pos.get("started"):
        return True, price, f"持仓{days}天未启动止损换股"
    # 5) 最长持有到期
    if days >= HOLD_MAX_DAYS:
        return True, price, f"持有{days}天到期清仓"
    return False, None, "继续持有"


# ==========================================================================
# 成交可行性（T+1 开盘）——收盘对账/回测用
# ==========================================================================
def can_buy_at_open(next_row):
    """T+1 开盘能否买入：若开盘即涨停（一字/涨停开盘），无法买入。
    next_row 含 open, preclose, code。返回 (bool, reason)。"""
    code = next_row["code"]
    limit = dfetch.limit_pct(code)
    preclose = next_row.get("preclose") or 0
    if preclose <= 0:
        return False, "无昨收价"
    if int(next_row.get("tradestatus", 1)) != 1:
        return False, "停牌无法买入"
    open_gap = next_row["open"] / preclose - 1
    if open_gap >= limit - LIMIT_BUFFER:
        return False, f"涨停开盘({open_gap*100:.1f}%)无法买入"
    return True, f"开盘价买入(高开{open_gap*100:.1f}%)"


def can_sell_at(row, price_kind="close"):
    """能否卖出：跌停板无法卖出。返回 (bool, reason)。"""
    code = row["code"]
    limit = dfetch.limit_pct(code)
    preclose = row.get("preclose") or 0
    if preclose <= 0:
        return True, ""
    if int(row.get("tradestatus", 1)) != 1:
        return False, "停牌无法卖出"
    # 跌停判定：当日最低=收盘且跌幅达 -limit（封死跌停）
    pct = (row.get("pctChg") or 0) / 100.0
    low = row.get("low") or 0
    close = row.get("close") or 0
    if pct <= -(limit - LIMIT_BUFFER) and abs(close - low) / (close or 1) <= CLOSE_AT_HIGH_TOL:
        return False, f"跌停无法卖出({pct*100:.1f}%)"
    return True, ""


# ==========================================================================
# 持仓卖出决策（T+1 及以后）
# ==========================================================================
def _days_held(open_date, current_date):
    try:
        d0 = datetime.strptime(open_date, "%Y-%m-%d").date()
        d1 = datetime.strptime(current_date, "%Y-%m-%d").date()
        return (d1 - d0).days
    except Exception:
        return 0


def evaluate_sell(pos, row, current_date):
    """根据持仓与当日(current_date)日线决定是否卖出。
    返回 (do_sell, sell_price, reason, price_kind)。严格 T+1：买入当日不卖。"""
    if pos["open_date"] == current_date:
        return False, None, "T+1当日不可卖", None
    cost = pos["avg_cost"]
    o = row["open"]; h = row["high"]; low = row["low"]; c = row["close"]
    preclose = row.get("preclose") or cost
    days = _days_held(pos["open_date"], current_date)

    # 1) 次日跳空低开 < -3% -> 开盘清仓
    if preclose > 0:
        gap = o / preclose - 1
        if gap < STOP_OVERNIGHT_GAP:
            return True, o, f"低开{gap*100:.1f}%(<-3%)开盘清仓", "open"
    # 2) 跌破买入价 -5% -> 止损（收盘价执行）
    if c <= cost * (1 + STOP_LOSS):
        return True, c, f"收盘跌破买价{STOP_LOSS*100:.0f}%止损", "close"
    # 3) 封板后开板/转弱：持有中单日大幅下挫且未启动
    pct = (row.get("pctChg") or 0)
    if days >= 1 and pct / 100.0 <= OPEN_BOARD_DROP and not pos.get("started"):
        return True, c, f"开板转弱(当日{pct:.1f}%)清仓", "close"
    # 4) 达到目标止盈 +8%~+15%（盘中触及 +10% 即止盈）
    if h / cost - 1 >= TARGET_PROFIT:
        target_p = cost * (1 + TARGET_PROFIT)
        return True, target_p, f"触及目标+{TARGET_PROFIT*100:.0f}%止盈", "target"
    # 5) 持仓 STALL_DAYS 天未启动 -> 止损换股
    if days >= STALL_DAYS and not pos.get("started"):
        return True, c, f"持仓{days}天未启动止损换股", "close"
    # 6) 最长持有到期
    if days >= HOLD_MAX_DAYS:
        return True, c, f"持有{days}天到期清仓", "close"
    return False, None, "继续持有", None


# ==========================================================================
# 辅助
# ==========================================================================
def position_value(positions):
    return sum((p.get("last_price") or p["avg_cost"]) * p["shares"] for p in positions)


def lots(shares):
    return int(shares // 100 * 100)
