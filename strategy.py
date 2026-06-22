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

# 流通市值过滤（单位：元）——只做 5亿~100亿 的中小盘，过滤超大盘与壳股
MIN_FLOAT_MV = 5e8           # 5亿，过滤微盘/壳股
MAX_FLOAT_MV = 1e10          # 100亿，过滤超大盘（盘子太大难涨停）

# 题材板块联动加分（概念板块当日实时涨停家数；证监会行业无法捕捉跨行业题材）
SECTOR_LINK_MIN = 3          # 联动>=3只视为板块发酵
SECTOR_SCORE_W = 8           # 每只联动涨停的评分权重（封顶 10 只）
# 概念板块中的"持股结构/通用属性"标签——非题材，统计联动时剔除（否则人人联动失真）
CONCEPT_BLOCKLIST = {
    "保险重仓", "基金重仓", "社保重仓", "券商重仓", "QFII重仓", "信托重仓",
    "融资融券", "标准普尔", "MSCI中国", "富时罗素", "深股通", "沪股通",
    "含H股", "含B股", "AH股", "央企改革", "国企改革", "预盈预增", "预亏预减",
    "业绩预升", "业绩预降", "高送转", "次新股", "创业板综", "中证500", "沪深300",
    "上证180", "上证380", "深成500", "证金持股", "汇金概念", "百元股",
}

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


# ----------------------------- 策略2：缩量横盘突破 -----------------------------
RANGE_LOOKBACK = 20          # 横盘区间回看天数
RANGE_MAX_RATIO = 1.15       # 区间内最高/最低 <= 1.15（波动<15%即横盘）
SHRINK_RATIO = 0.8           # 前10日均量 < 前20日均量*0.8 视为缩量蓄势
BREAK_TOL = 0.99             # 收盘 > 区间上沿*0.99 视为突破
BREAK_VR_MIN = 2.0           # 突破须放量：量比>=2


def evaluate_breakout(df, idx):
    """缩量横盘突破：长期横盘缩量后放量突破区间上沿。
    df 按日期升序、含 code 列；idx=T日行。返回 dict 或 None。"""
    if idx < RANGE_LOOKBACK:
        return None
    row = df.iloc[idx]
    code = row["code"]
    if int(row.get("tradestatus", 1)) != 1 or int(row.get("isST", 0)) == 1:
        return None
    amount = row.get("amount") or 0
    if amount < MIN_AMOUNT:
        return None
    turn = row.get("turn") or 0
    if turn < TURN_MIN or turn > TURN_MAX:
        return None
    # 区间（前 RANGE_LOOKBACK 日，不含今日）
    win = df.iloc[idx - RANGE_LOOKBACK:idx]
    hi = float(win["high"].max())
    lo = float(win["low"].min())
    if lo <= 0 or hi <= 0:
        return None
    if hi / lo > RANGE_MAX_RATIO:                 # 波动过大，非横盘
        return None
    # 缩量蓄势：前10日均量 < 前20日均量*0.8
    v10 = float(df["volume"].iloc[idx - 10:idx].mean())
    v20 = float(win["volume"].mean())
    if v20 <= 0 or v10 >= v20 * SHRINK_RATIO:
        return None
    close = row.get("close") or 0
    if close <= hi * BREAK_TOL:                    # 未突破区间上沿
        return None
    # 突破放量：今日量 >= 前10日均量*2
    vr = float(row["volume"] / v10) if v10 > 0 else None
    if vr is None or vr < BREAK_VR_MIN:
        return None
    pct = row.get("pctChg") or 0
    score = 100 + min(vr, 5) * 15 + min(turn, 20) + (pct if pct > 5 else 0)
    return {
        "code": code, "strategy_type": "横盘突破",
        "boards": 0, "vol_ratio": round(vr, 2), "turn": round(turn, 2),
        "amount": round(amount, 0), "pctChg": round(pct, 2), "close": close,
        "score": round(score, 2),
        "reason": f"缩量横盘突破 放量{vr:.1f}x 换手{turn:.1f}%",
    }


# ----------------------------- 策略3：低位首阴反包 -----------------------------
REVERSAL_GAP = -0.03         # 今日低开 <= -3%


def evaluate_reversal(df, idx):
    """低位首阴反包：昨日涨停封板，今日大幅低开后收红（低开高走反包）。
    df 按日期升序、含 code 列；idx=T日行。返回 dict 或 None。"""
    if idx < 1:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]
    code = row["code"]
    if int(row.get("tradestatus", 1)) != 1 or int(row.get("isST", 0)) == 1:
        return None
    limit = dfetch.limit_pct(code)
    if not is_limit_up(prev, limit):              # 昨日须涨停封板
        return None
    o = row.get("open") or 0
    c = row.get("close") or 0
    preclose = row.get("preclose") or 0
    if o <= 0 or c <= 0 or preclose <= 0:
        return None
    gap = o / preclose - 1
    if gap > REVERSAL_GAP:                         # 低开不足 3%
        return None
    if c < o:                                      # 必须收红（低开高走）
        return None
    amount = row.get("amount") or 0
    if amount < MIN_AMOUNT:
        return None
    close_vs_open = c / o - 1
    pct = row.get("pctChg") or 0
    # 低开越深、反包越强 -> 加分；当日涨幅再加分
    score = 150 + min(abs(gap), 0.10) * 200 + close_vs_open * 100 + (pct if pct > 0 else 0)
    return {
        "code": code, "strategy_type": "首阴反包",
        "boards": 0, "vol_ratio": None, "turn": round(row.get("turn") or 0, 2),
        "amount": round(amount, 0), "pctChg": round(pct, 2), "close": c,
        "score": round(score, 2),
        "reason": f"首阴反包 低开{gap*100:.1f}% 收{close_vs_open*100:.1f}%",
    }


def select_candidates_limitup(panel, names, trade_date, sentiment_tradable, top_n=TOP_N_CANDIDATES):
    """连板涨停策略：对全市场面板在 trade_date(=T日) 选出候选池。"""
    if not sentiment_tradable:
        return []
    cands = []
    for code, df in panel.items():
        nm = names.get(code, code) or ""
        if "ST" in nm.upper():          # isST 字段不可靠，用名称兜底过滤 ST/*ST
            continue
        pos = df.index[df["date"] == trade_date]
        if len(pos) == 0:
            continue
        idx = int(pos[0])
        c = evaluate_candidate(df, idx)
        if c:
            c["name"] = nm
            c["strategy_type"] = "连板涨停"
            cands.append(c)
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands[:top_n]


def select_candidates(panel, names, trade_date, sentiment_tradable, top_n=TOP_N_CANDIDATES):
    """统一入口：连板涨停 + 缩量横盘突破 + 低位首阴反包 三策略并行，合并按 score 排序。
    同一股票若被多策略命中，保留 score 最高者。panel: {code: df(升序)}。"""
    if not sentiment_tradable:
        return []
    best = {}   # code -> 最优候选
    for code, df in panel.items():
        nm = names.get(code, code) or ""
        if "ST" in nm.upper():
            continue
        pos = df.index[df["date"] == trade_date]
        if len(pos) == 0:
            continue
        idx = int(pos[0])
        for fn, stype in ((evaluate_candidate, "连板涨停"),
                          (evaluate_breakout, "横盘突破"),
                          (evaluate_reversal, "首阴反包")):
            c = fn(df, idx)
            if not c:
                continue
            c["name"] = nm
            c.setdefault("strategy_type", stype)
            if code not in best or c["score"] > best[code]["score"]:
                best[code] = c
    cands = sorted(best.values(), key=lambda x: x["score"], reverse=True)
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


def evaluate_intraday_candidate(spot_row, hist_df, today=None, concept_map=None, concept_counts=None):
    """盘中实时评估单只票是否为买入候选。
    spot_row: 实时快照行(dict)，含 code,name,price,pct,turn,amount,vol_ratio,limit_up,preclose,float_mv。
    hist_df: 该票历史日线(升序，全部早于今日)，用于连板高度。
    concept_map/concept_counts: 概念板块联动（同概念当日实时涨停家数）映射与计数。
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
    # 3) 流通市值过滤：只做 5亿~100亿 中小盘（float_mv 缺失则放行，不误杀）
    fmv = spot_row.get("float_mv")
    if fmv and fmv > 0 and not (MIN_FLOAT_MV <= fmv <= MAX_FLOAT_MV):
        return None
    # 4) 连板高度（历史，剔除过高）
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
    # 5) 题材板块联动加分：所属概念中最热概念的涨停家数越多，板块越活跃、持续性越强
    sector = ""
    sector_link = 0
    if concept_map is not None and concept_counts:
        sector, sector_link = best_concept_link(code, concept_map, concept_counts)
        if sector_link:
            score += min(sector_link, 10) * SECTOR_SCORE_W
    limit_up_px = spot_row.get("limit_up") or (preclose * (1 + limit))
    sealed = limit_up_px > 0 and (limit_up_px - price) / limit_up_px <= SEALED_TOL
    return {
        "code": code, "name": nm, "strategy_type": "连板涨停", "boards": cur_board,
        "vol_ratio": round(vr, 2) if vr else None, "turn": round(turn, 2),
        "amount": round(amount, 0), "pctChg": round(spot_row.get("pct") or 0, 2),
        "price": price, "limit_up": limit_up_px, "sealed": sealed,
        "float_mv": round(fmv, 0) if fmv else None,
        "sector": sector or "", "sector_link": sector_link,
        "score": round(score, 2),
        "reason": f"盘中{cur_board}板冲涨停(实时{pct*100:.1f}%)"
                  + (f" 量比{vr:.1f}" if vr else "")
                  + f" 换手{turn:.1f}%"
                  + (f" 板块联动{sector_link}板" if sector_link >= SECTOR_LINK_MIN else ""),
    }


def _passes_liquidity_mv(spot_row):
    """盘中通用过滤：非ST/退、流动性、换手、流通市值。通过返回 (price, turn, amount, fmv)，否则 None。"""
    nm = (spot_row.get("name") or "")
    if "ST" in nm.upper() or "退" in nm:
        return None
    price = spot_row.get("price") or 0
    preclose = spot_row.get("preclose") or 0
    if price <= 0 or preclose <= 0:
        return None
    amount = spot_row.get("amount") or 0
    if amount < MIN_AMOUNT:
        return None
    turn = spot_row.get("turn") or 0
    if turn < TURN_MIN or turn > TURN_MAX:
        return None
    fmv = spot_row.get("float_mv")
    if fmv and fmv > 0 and not (MIN_FLOAT_MV <= fmv <= MAX_FLOAT_MV):
        return None
    return price, turn, amount, fmv


def evaluate_intraday_breakout(spot_row, hist_df):
    """盘中缩量横盘突破：历史长期横盘缩量，实时价突破前20日高点且放量。
    hist_df: 该票历史日线(升序，全部早于今日)。返回 dict 或 None。"""
    if hist_df is None or len(hist_df) < RANGE_LOOKBACK:
        return None
    pf = _passes_liquidity_mv(spot_row)
    if pf is None:
        return None
    price, turn, amount, fmv = pf
    code = spot_row.get("code")
    nm = spot_row.get("name") or ""
    win = hist_df.iloc[-RANGE_LOOKBACK:]
    hi = float(win["high"].max())
    lo = float(win["low"].min())
    if lo <= 0 or hi <= 0 or hi / lo > RANGE_MAX_RATIO:
        return None
    v10 = float(hist_df["volume"].iloc[-10:].mean())
    v20 = float(win["volume"].mean())
    if v20 <= 0 or v10 >= v20 * SHRINK_RATIO:        # 须前期缩量蓄势
        return None
    if price <= hi * BREAK_TOL:                       # 实时价未突破区间上沿
        return None
    vr = spot_row.get("vol_ratio")
    if vr is not None and vr < BREAK_VR_MIN:          # 须放量突破
        return None
    pct = spot_row.get("pct") or 0
    score = 100 + (min(vr, 5) * 15 if vr else 0) + min(turn, 20) + (pct if pct > 5 else 0)
    return {
        "code": code, "name": nm, "strategy_type": "横盘突破", "boards": 0,
        "vol_ratio": round(vr, 2) if vr else None, "turn": round(turn, 2),
        "amount": round(amount, 0), "pctChg": round(pct, 2),
        "price": price, "limit_up": spot_row.get("limit_up"), "sealed": False,
        "float_mv": round(fmv, 0) if fmv else None, "sector": "", "sector_link": 0,
        "score": round(score, 2),
        "reason": f"盘中横盘突破(破{hi:.2f})" + (f" 量比{vr:.1f}" if vr else "") + f" 换手{turn:.1f}%",
    }


def evaluate_intraday_reversal(spot_row, hist_df):
    """盘中低位首阴反包：昨日涨停封板，今日大幅低开后实时价已回升过今开（低开高走）。
    hist_df: 该票历史日线(升序)。返回 dict 或 None。"""
    if hist_df is None or hist_df.empty:
        return None
    pf = _passes_liquidity_mv(spot_row)
    if pf is None:
        return None
    price, turn, amount, fmv = pf
    code = spot_row.get("code")
    nm = spot_row.get("name") or ""
    limit = dfetch.limit_pct(code)
    prev = hist_df.iloc[-1]                            # 昨日（早于今日）
    if not is_limit_up(prev, limit):                   # 昨日须涨停封板
        return None
    o = spot_row.get("open") or 0
    preclose = spot_row.get("preclose") or 0
    if o <= 0 or preclose <= 0:
        return None
    gap = o / preclose - 1
    if gap > REVERSAL_GAP:                             # 低开不足 3%
        return None
    if price < o:                                      # 实时价须已回升过今开（低开高走）
        return None
    cur_vs_open = price / o - 1
    pct = spot_row.get("pct") or 0
    score = 150 + min(abs(gap), 0.10) * 200 + cur_vs_open * 100 + (pct if pct > 0 else 0)
    return {
        "code": code, "name": nm, "strategy_type": "首阴反包", "boards": 0,
        "vol_ratio": round(spot_row.get("vol_ratio"), 2) if spot_row.get("vol_ratio") else None,
        "turn": round(turn, 2), "amount": round(amount, 0), "pctChg": round(pct, 2),
        "price": price, "limit_up": spot_row.get("limit_up"), "sealed": False,
        "float_mv": round(fmv, 0) if fmv else None, "sector": "", "sector_link": 0,
        "score": round(score, 2),
        "reason": f"盘中首阴反包 低开{gap*100:.1f}% 现回升{cur_vs_open*100:.1f}%",
    }


def select_intraday_candidates(spot_df, panel, sentiment_tradable, top_n=TOP_N_CANDIDATES,
                               concept_map=None, concept_counts=None):
    """对实时快照全股票池筛选盘中买入候选（连板涨停+横盘突破+首阴反包），按 score 降序取 top_n。
    spot_df: ak_spot 结果；panel: {code: 历史df(升序)}。
    concept_map/concept_counts: 概念板块联动映射与各概念实时涨停计数。返回 list[dict]。
    同股多策略命中保留 score 最高者。"""
    if not sentiment_tradable or spot_df is None or spot_df.empty:
        return []
    best = {}
    for _, sr in spot_df.iterrows():
        row = sr.to_dict()
        code = row.get("code")
        hist = panel.get(code) or panel.get(dfetch.to_bs_code(code or ""))
        for c in (evaluate_intraday_candidate(row, hist, concept_map=concept_map, concept_counts=concept_counts),
                  evaluate_intraday_breakout(row, hist),
                  evaluate_intraday_reversal(row, hist)):
            if not c:
                continue
            if code not in best or c["score"] > best[code]["score"]:
                best[code] = c
    cands = sorted(best.values(), key=lambda x: x["score"], reverse=True)
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


def count_sector_limitups_spot(spot_df, industry_map):
    """统计每个证监会行业当日实时涨停家数（题材板块联动数）。
    industry_map: {六位或bs代码: 行业名}。返回 {行业名: 涨停家数}。"""
    counts = {}
    if spot_df is None or spot_df.empty or not industry_map:
        return counts
    for _, sr in spot_df.iterrows():
        code = str(sr.get("code") or "")
        price = sr.get("price") or 0
        lu = sr.get("limit_up")
        if price <= 0 or not lu or lu <= 0:
            continue
        if (lu - price) / lu <= SEALED_TOL:   # 封死涨停
            ind = industry_map.get(code) or industry_map.get(dfetch.to_bs_code(code))
            if ind:
                counts[ind] = counts.get(ind, 0) + 1
    return counts


def count_concept_limitups_spot(spot_df, concept_map):
    """统计每个概念板块当日实时涨停家数（剔除持股结构类通用标签）。
    concept_map: {六位或bs代码: [概念,...]}。返回 {概念名: 涨停家数}。"""
    counts = {}
    if spot_df is None or spot_df.empty or not concept_map:
        return counts
    for _, sr in spot_df.iterrows():
        code = str(sr.get("code") or "")
        price = sr.get("price") or 0
        lu = sr.get("limit_up")
        if price <= 0 or not lu or lu <= 0:
            continue
        if (lu - price) / lu <= SEALED_TOL:   # 封死涨停
            concepts = concept_map.get(code) or concept_map.get(dfetch.to_bs_code(code)) or []
            for cpt in concepts:
                if cpt in CONCEPT_BLOCKLIST:
                    continue
                counts[cpt] = counts.get(cpt, 0) + 1
    return counts


def best_concept_link(code, concept_map, concept_counts):
    """返回该票所属概念中联动涨停家数最多的 (概念名, 家数)；无则 ('', 0)。"""
    if not concept_map or not concept_counts:
        return "", 0
    concepts = concept_map.get(code) or concept_map.get(dfetch.to_bs_code(code)) or []
    best_name, best_n = "", 0
    for cpt in concepts:
        if cpt in CONCEPT_BLOCKLIST:
            continue
        n = concept_counts.get(cpt, 0)
        if n > best_n:
            best_name, best_n = cpt, n
    return best_name, best_n


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
