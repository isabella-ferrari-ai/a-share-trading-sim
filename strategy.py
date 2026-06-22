# -*- coding: utf-8 -*-
"""策略引擎：情绪龙头法 + 连板战法（模拟盘）。

数据限制：baostock 仅日线，故用日线 OHLC 近似盘中行为：
- 高开幅度  = open / preclose - 1
- "开盘封板" 近似为：当日 pctChg >= 涨停阈值 且 (high≈close，即收在涨停附近)
- 封单>流通盘1% 无法从日线得到，用换手率/放量作为弱代理
- 卖出止损：用次日及之后日线的 open/low/close 判定

T+1：买入当日 open_date == 当前日 的持仓，当日不可卖出。
盘中扫描：scheduler 每 15 分钟调用 intraday_scan()，市场时段内基于最新可得日线/快照做信号判断。
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, date
import database as db

# ----------------------------- 参数 -----------------------------
MAX_POSITIONS = 3            # 同时最多持有
MAX_POSITION_PCT = 0.20      # 单票最大仓位
GAP_UP_MIN = 0.03            # 竞价高开下限 3%
GAP_UP_MAX = 0.08            # 竞价高开上限 8%（超过不追）
STOP_LOSS_INTRADAY = -0.05   # 当日跌破买入价 -5% 收盘清仓
STOP_OVERNIGHT_GAP = -0.03   # 隔夜次日低开 > -3%（即低于 -3%）开盘清仓
HOLD_MAX_DAYS = 7            # 最长持有
STALL_DAYS = 3               # 持仓3天未启动则止损换股
TARGET_PROFIT = 0.10         # 3天目标收益区间中值（用于跟踪止盈参考）
MAX_STOPS_PER_DAY = 2        # 当日被止损≥2次今日收手
INDEX_LOW_OPEN_LIMIT = -0.01 # 大盘当日低开 > -1% 则当日不参与

# 情绪阈值
LIMITUP_STRONG = 80
LIMITUP_WEAK = 40
AMOUNT_STRONG = 1.5e4        # 亿元 -> 1.5万亿
AMOUNT_WEAK = 1.0e4          # 1万亿


def classify_regime(limit_up_count, total_amount_yi):
    """根据涨停数与成交额判定市场强弱。total_amount_yi 单位：亿元。"""
    score = 0
    if limit_up_count is not None:
        if limit_up_count > LIMITUP_STRONG:
            score += 1
        elif limit_up_count < LIMITUP_WEAK:
            score -= 1
    if total_amount_yi is not None:
        if total_amount_yi > AMOUNT_STRONG:
            score += 1
        elif total_amount_yi < AMOUNT_WEAK:
            score -= 1
    if score >= 1:
        regime = "强市"
    elif score <= -1:
        regime = "弱市"
    else:
        regime = "中性"
    tradable = 0 if regime == "弱市" else 1
    return regime, tradable


def _days_held(open_date, current_date):
    try:
        d0 = datetime.strptime(open_date, "%Y-%m-%d").date()
        d1 = datetime.strptime(current_date, "%Y-%m-%d").date()
        return (d1 - d0).days
    except Exception:
        return 0


def is_buy_candidate(bar, index_open_pct, regime_tradable):
    """根据日线判断是否构成买入信号（近似情绪龙头/连板）。
    返回 (是否买入, 理由)。bar 为单只股票当日数据 dict，需含 open,high,low,close,preclose,pctChg,turn,limit。"""
    if not regime_tradable:
        return False, "弱势日空仓"
    # 大盘当日不低开 > 1%
    if index_open_pct is not None and index_open_pct < INDEX_LOW_OPEN_LIMIT:
        return False, f"大盘低开{index_open_pct*100:.1f}%超过-1%，不参与"
    preclose = bar.get("preclose") or 0
    if preclose <= 0:
        return False, "无昨收价"
    gap = bar["open"] / preclose - 1
    # 低开/平开 当日不参与
    if gap < GAP_UP_MIN:
        return False, f"高开{gap*100:.1f}%不足3%"
    # 高开 > 8% 不追
    if gap > GAP_UP_MAX:
        return False, f"高开{gap*100:.1f}%超过8%，不追等回踩"
    # 近似"开盘封板"：当日封涨停（收在涨停附近）。limit 为涨跌停幅度
    limit = bar.get("limit", 0.10)
    pct = (bar.get("pctChg") or 0) / 100.0
    closed_at_limit = pct >= (limit - 0.005) and (bar["high"] - bar["close"]) / bar["close"] < 0.005
    if not closed_at_limit:
        return False, f"未封板(涨幅{pct*100:.1f}%)"
    # 放量代理封单强度（换手率>3%视为有承接）
    turn = bar.get("turn") or 0
    if turn < 1.0:
        return False, f"成交清淡(换手{turn:.1f}%)"
    return True, f"高开{gap*100:.1f}%+开盘封板(涨{pct*100:.1f}%,换手{turn:.1f}%)"


def evaluate_sell(pos, bar, current_date):
    """根据持仓与当日日线判断是否卖出。返回 (是否卖出, 卖出价, 理由)。
    严格 T+1：buy 当日不卖。"""
    # T+1：当日买入不可卖
    if pos["open_date"] == current_date:
        return False, None, "T+1当日不可卖"
    cost = pos["avg_cost"]
    o, h, low, c = bar["open"], bar["high"], bar["low"], bar["close"]
    preclose = bar.get("preclose") or cost
    days = _days_held(pos["open_date"], current_date)

    # 1) 隔夜持仓次日低开 > -3% -> 开盘清仓（按开盘价）
    overnight_gap = o / preclose - 1
    if overnight_gap < STOP_OVERNIGHT_GAP:
        return True, o, f"次日低开{overnight_gap*100:.1f}%(>-3%)开盘清仓"
    # 2) 当日盘中跌破买入价 -5% -> 收盘前清仓（用收盘价近似）
    if low <= cost * (1 + STOP_LOSS_INTRADAY):
        # 若收盘仍低于阈值则按收盘价，否则视为触及止损线按止损价
        sell_p = c if c <= cost * (1 + STOP_LOSS_INTRADAY) else cost * (1 + STOP_LOSS_INTRADAY)
        return True, sell_p, f"跌破买价-5%收盘清仓"
    # 3) 封板后开板 -> 立即清仓（近似：持仓后某日未能维持强势，pctChg转负且放量）
    pct = (bar.get("pctChg") or 0)
    if days >= 1 and pct < -3 and not pos.get("started"):
        return True, c, f"未能延续涨势(当日{pct:.1f}%)，按开板清仓"
    # 4) 持仓3天未启动（未达+8%）-> 止损换股
    if days >= STALL_DAYS and not pos.get("started"):
        gain = c / cost - 1
        if gain < 0.08:
            return True, c, f"持仓{days}天未启动(收益{gain*100:.1f}%)止损换股"
    # 5) 达到目标收益 +8%~+15% 止盈
    gain = h / cost - 1
    if gain >= 0.08:
        # 在 +8%~+15% 区间内分批，模拟为触及+10%止盈
        target_p = cost * 1.10 if h >= cost * 1.10 else h
        return True, target_p, f"达到目标收益+{(target_p/cost-1)*100:.1f}%止盈"
    # 6) 最长持有7天到期
    if days >= HOLD_MAX_DAYS:
        return True, c, f"持有{days}天到期清仓"
    return False, None, "继续持有"


def position_value(positions):
    return sum((p.get("last_price") or p["avg_cost"]) * p["shares"] for p in positions)


def lots(shares):
    """A股按手(100股)取整。"""
    return int(shares // 100 * 100)
