# -*- coding: utf-8 -*-
"""
国金QMT可转债交易策略（单文件版）
=============================
直接复制本文件内容到国金QMT终端的策略编辑器即可运行，无需其他依赖文件。

功能：
- 每天多时段自动调仓
- 盘中实时行情监控，逐bar检查止损止盈
- ATR止损 + 固定比率止损
- 最高价跟踪止盈，盈利后抬高止盈线
- 组合级风控：单日最大亏损、最大回撤
"""

import os
import sys
import json
import math
import datetime

# ============================================================
# 1. 参数配置（使用前请修改 ACCOUNT_ID）
# ============================================================

ACCOUNT_ID = "88888888"          # 资金账号
ACCOUNT_TYPE = "STOCK"           # STOCK / CREDIT

REBALANCE_TIMES = ["09:35", "10:30", "11:20", "14:00", "14:45"]

POSITION_CONFIG = {
    "max_holding_count": 5,
    "single_position_ratio": 0.20,
    "total_position_ratio": 0.90,
    "min_trade_amount": 1000,
}

FILTER_CONFIG = {
    "sector": "沪深可转债",
    "custom_list": [],
    "max_count": 50,
    "min_price": 100.0,
    "max_price": 150.0,
    "min_daily_amount": 500.0,    # 万元
    "exclude_new_days": 5,
    "exclude_expire_days": 30,
}

SIGNAL_CONFIG = {
    "lookback_days": 20,
    "ma_short": 5,
    "ma_long": 20,
    "momentum_days": 5,
    "momentum_threshold": 0.01,
    "rsi_period": 14,
    "rsi_low": 30,
    "rsi_high": 70,
}

RISK_CONFIG = {
    "atr_period": 14,
    "atr_stop_multiplier": 1.5,
    "fixed_stop_ratio": 0.03,
    "use_trailing_stop": True,
    "trailing_atr_multiplier": 2.0,
    "trailing_min_profit_ratio": 0.015,
    "max_daily_loss_ratio": 0.05,
    "max_drawdown_ratio": 0.10,
}

# QMT passorder 常量
OP_BUY = 23
OP_SELL = 24
PRICE_FIX = 5
PRICE_LATEST = 11

# ============================================================
# 2. 技术指标（纯标准库实现）
# ============================================================

def _mean(values):
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values):
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    return math.sqrt(sum((x - avg) ** 2 for x in values) / (len(values) - 1))


def sma(values, period):
    if len(values) < period:
        return None
    return _mean(values[-period:])


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    tr_list = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i - 1])
        tr3 = abs(lows[i] - closes[i - 1])
        tr_list.append(max(tr1, tr2, tr3))
    if len(tr_list) < period:
        return None
    return _mean(tr_list[-period:])


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = _mean(gains[-period:])
    avg_loss = _mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + avg_gain / avg_loss)


def momentum(closes, period=5):
    if len(closes) < period:
        return None
    return (closes[-1] - closes[-period]) / closes[-period]


def volume_ma(volumes, period=20):
    if len(volumes) < period:
        return None
    return _mean(volumes[-period:])


# ============================================================
# 3. 风险管理
# ============================================================

class PositionState:
    def __init__(self, code, entry_price, entry_time, volume, atr_value):
        self.code = code
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.volume = volume
        self.atr_value = atr_value or 0.0
        self.highest_price = entry_price
        self.trailing_stop_price = None
        self.fixed_stop_price = entry_price * (1 - RISK_CONFIG["fixed_stop_ratio"])
        atr_stop = entry_price - RISK_CONFIG["atr_stop_multiplier"] * self.atr_value if self.atr_value else entry_price * 0.95
        self.stop_price = max(self.fixed_stop_price, atr_stop)
        self.status = "HOLDING"

    def update_highest(self, current_price):
        if current_price > self.highest_price:
            self.highest_price = current_price
            self._update_trailing_stop()

    def _update_trailing_stop(self):
        if not RISK_CONFIG["use_trailing_stop"]:
            return
        if self.highest_price < self.entry_price * (1 + RISK_CONFIG["trailing_min_profit_ratio"]):
            return
        candidate = self.highest_price - RISK_CONFIG["trailing_atr_multiplier"] * self.atr_value
        if self.trailing_stop_price is None or candidate > self.trailing_stop_price:
            self.trailing_stop_price = candidate

    def check_exit(self, current_price):
        if current_price <= self.stop_price:
            return "STOP_LOSS", "price_hit_stop_loss"
        if self.trailing_stop_price and current_price <= self.trailing_stop_price:
            return "TRAILING_STOP", "price_hit_trailing_stop"
        return None, None


class RiskManager:
    def __init__(self):
        self.positions = {}
        self.daily_pnl = 0.0
        self.peak_asset = 0.0

    def add_position(self, code, entry_price, entry_time, volume, atr_value):
        if code in self.positions:
            return False
        self.positions[code] = PositionState(code, entry_price, entry_time, volume, atr_value)
        return True

    def remove_position(self, code):
        return self.positions.pop(code, None)

    def update_price(self, code, current_price):
        pos = self.positions.get(code)
        if not pos:
            return None, None
        pos.update_highest(current_price)
        return pos.check_exit(current_price)

    def check_portfolio_risk(self, total_asset, initial_asset):
        if initial_asset <= 0:
            return True, None
        daily_loss_ratio = -self.daily_pnl / initial_asset
        if daily_loss_ratio >= RISK_CONFIG["max_daily_loss_ratio"]:
            return False, "daily_loss_limit"
        if total_asset > self.peak_asset:
            self.peak_asset = total_asset
        drawdown = (self.peak_asset - total_asset) / self.peak_asset if self.peak_asset > 0 else 0
        if drawdown >= RISK_CONFIG["max_drawdown_ratio"]:
            return False, "max_drawdown_limit"
        return True, None

    def should_open_new(self, available_cash, total_asset):
        if len(self.positions) >= POSITION_CONFIG["max_holding_count"]:
            return False
        used = sum(p.volume * p.entry_price for p in self.positions.values())
        if used / total_asset >= POSITION_CONFIG["total_position_ratio"]:
            return False
        return available_cash >= POSITION_CONFIG["min_trade_amount"]

    def calc_target_volume(self, price, total_asset):
        amount = total_asset * POSITION_CONFIG["single_position_ratio"]
        volume = int(amount / price / 10) * 10
        return volume if volume >= 10 else 0


# ============================================================
# 4. 交易执行
# ============================================================

def _log(ContextInfo, level, msg):
    try:
        if hasattr(ContextInfo, "log"):
            log_func = getattr(ContextInfo.log, level, ContextInfo.log.info)
            log_func(msg)
        else:
            print("[%s] %s" % (level.upper(), msg))
    except Exception:
        print(msg)


def get_account_info(ContextInfo):
    try:
        data = ContextInfo.get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, "asset")
        if data:
            a = data[0]
            return {
                "total_asset": getattr(a, "m_dTotalAssets", 0),
                "available_cash": getattr(a, "m_dAvailable", 0),
                "market_value": getattr(a, "m_dMarketValue", 0),
            }
    except Exception as e:
        _log(ContextInfo, "error", "获取账户资产失败: %s" % str(e))
    return {"total_asset": 0, "available_cash": 0, "market_value": 0}


def get_positions(ContextInfo):
    positions = {}
    try:
        data = ContextInfo.get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, "position")
        for pos in data:
            code = getattr(pos, "m_strInstrumentID", "") + "." + getattr(pos, "m_strExchangeID", "")
            vol = getattr(pos, "m_nVolume", 0)
            if vol > 0:
                positions[code] = vol
    except Exception as e:
        _log(ContextInfo, "error", "获取持仓失败: %s" % str(e))
    return positions


def place_order(ContextInfo, code, operation, volume, price=None, remark=""):
    if volume <= 0:
        return None
    volume = int(volume / 10) * 10
    if volume < 10:
        return None
    price_type = PRICE_FIX if price else PRICE_LATEST
    if price is None:
        price = 0
    try:
        _log(ContextInfo, "info", "下单 %s %s vol=%d price=%s remark=%s" % (
            code, "BUY" if operation == OP_BUY else "SELL", volume, price, remark))
        return ContextInfo.passorder(operation, price_type, ACCOUNT_ID, code, volume, price, "可转债策略", remark, ContextInfo)
    except Exception as e:
        _log(ContextInfo, "error", "下单失败 %s: %s" % (code, str(e)))
        return None


def liquidate_all(ContextInfo, positions, remark="risk_control"):
    for code, volume in positions.items():
        place_order(ContextInfo, code, OP_SELL, volume, remark=remark)


# ============================================================
# 5. 可转债筛选
# ============================================================

def get_bond_pool(ContextInfo):
    custom = FILTER_CONFIG["custom_list"]
    if custom:
        return custom
    try:
        sector = FILTER_CONFIG["sector"]
        all_bonds = ContextInfo.get_stock_list_in_sector(sector)
        return all_bonds[:FILTER_CONFIG["max_count"]]
    except Exception as e:
        _log(ContextInfo, "error", "获取可转债板块失败: %s" % str(e))
        return []


def get_history_data(ContextInfo, code, period="1d", count=60):
    try:
        data = ContextInfo.get_market_data(["open", "high", "low", "close", "volume"], [code], period, count, fill_up=False)
        if data and code in data:
            return data[code]
    except Exception as e:
        _log(ContextInfo, "error", "获取历史行情失败 %s: %s" % (code, str(e)))
    return None


def get_realtime_data(ContextInfo, code):
    try:
        data = ContextInfo.get_full_tick([code])
        if data and code in data:
            return data[code]
    except Exception:
        pass
    try:
        data = ContextInfo.get_market_data(["close", "high", "low", "volume", "amount"], [code], "1d", 1, fill_up=False)
        if data and code in data:
            d = data[code]
            return {
                "lastPrice": d["close"][-1],
                "high": d["high"][-1],
                "low": d["low"][-1],
                "volume": d["volume"][-1],
                "amount": d["amount"][-1] if "amount" in d else 0,
            }
    except Exception as e:
        _log(ContextInfo, "error", "获取实时行情失败 %s: %s" % (code, str(e)))
    return None


def filter_bonds(ContextInfo, bond_list):
    result = []
    for code in bond_list:
        hist = get_history_data(ContextInfo, code, "1d", 30)
        rt = get_realtime_data(ContextInfo, code)
        if hist is None or rt is None:
            continue
        closes = list(hist.get("close", []))
        volumes = list(hist.get("volume", []))
        if len(closes) < 20:
            continue
        price = rt.get("lastPrice", closes[-1])
        if price <= 0:
            continue
        if not (FILTER_CONFIG["min_price"] <= price <= FILTER_CONFIG["max_price"]):
            continue
        if rt.get("amount", 0) < FILTER_CONFIG["min_daily_amount"] * 10000:
            continue
        avg_vol = volume_ma(volumes, 20)
        if avg_vol and volumes[-1] < avg_vol * 0.5:
            continue
        if len(closes) < FILTER_CONFIG["exclude_new_days"] + 20:
            continue
        result.append(code)
    return result


def score_bond(ContextInfo, code):
    hist = get_history_data(ContextInfo, code, "1d", SIGNAL_CONFIG["lookback_days"] + 5)
    if hist is None:
        return -999
    closes = list(hist.get("close", []))
    highs = list(hist.get("high", []))
    lows = list(hist.get("low", []))
    if len(closes) < SIGNAL_CONFIG["ma_long"] + 5:
        return -999
    score = 0.0
    ma_s = sma(closes, SIGNAL_CONFIG["ma_short"])
    ma_l = sma(closes, SIGNAL_CONFIG["ma_long"])
    if ma_s and ma_l and ma_s > ma_l:
        score += 30
    mom = momentum(closes, SIGNAL_CONFIG["momentum_days"])
    if mom and mom > SIGNAL_CONFIG["momentum_threshold"]:
        score += 20 + mom * 1000
    rsi_val = rsi(closes, SIGNAL_CONFIG["rsi_period"])
    if rsi_val is not None:
        if rsi_val < SIGNAL_CONFIG["rsi_low"]:
            score += 20
        elif rsi_val > SIGNAL_CONFIG["rsi_high"]:
            score -= 30
    atr_val = atr(highs, lows, closes, RISK_CONFIG["atr_period"])
    if atr_val:
        atr_ratio = atr_val / closes[-1]
        if 0.005 <= atr_ratio <= 0.03:
            score += 10
        elif atr_ratio > 0.05:
            score -= 10
    return score


def select_candidates(ContextInfo, bond_list, top_n=None):
    filtered = filter_bonds(ContextInfo, bond_list)
    scored = [(code, score_bond(ContextInfo, code)) for code in filtered]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    if top_n:
        scored = scored[:top_n]
    return [code for code, _ in scored]


# ============================================================
# 6. 状态持久化
# ============================================================

def _state_file(ContextInfo):
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "qmt_cb_strategy_state.json")


def _load_state(ContextInfo):
    path = _state_file(ContextInfo)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            ContextInfo.initial_asset = state.get("initial_asset", 0)
            ContextInfo.last_trade_date = state.get("last_trade_date")
            ContextInfo.rebalanced_today = set(state.get("rebalanced_today", []))
            _log(ContextInfo, "info", "加载历史状态成功")
    except Exception as e:
        _log(ContextInfo, "error", "加载状态失败: %s" % str(e))


def _save_state(ContextInfo):
    path = _state_file(ContextInfo)
    try:
        state = {
            "initial_asset": getattr(ContextInfo, "initial_asset", 0),
            "last_trade_date": getattr(ContextInfo, "last_trade_date", None),
            "rebalanced_today": list(getattr(ContextInfo, "rebalanced_today", set())),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(ContextInfo, "error", "保存状态失败: %s" % str(e))


def get_today(ContextInfo):
    try:
        return ContextInfo.get_trade_time()[0].strftime("%Y%m%d")
    except Exception:
        return datetime.datetime.now().strftime("%Y%m%d")


# ============================================================
# 7. QMT策略入口
# ============================================================

def init(ContextInfo):
    ContextInfo.accountid = ACCOUNT_ID
    ContextInfo.accounttype = ACCOUNT_TYPE
    ContextInfo.risk_manager = RiskManager()
    ContextInfo.rebalanced_today = set()
    ContextInfo.last_trade_date = None
    ContextInfo.initial_asset = 0
    _load_state(ContextInfo)

    for t in REBALANCE_TIMES:
        try:
            ContextInfo.run_time("rebalance_task", "1n", t, "SH")
        except Exception as e:
            _log(ContextInfo, "error", "注册定时任务失败 %s: %s" % (t, str(e)))

    try:
        ContextInfo.run_time("daily_close_task", "1n", "14:55", "SH")
    except Exception as e:
        _log(ContextInfo, "error", "注册日终任务失败: %s" % str(e))

    _log(ContextInfo, "info", "可转债策略初始化完成，账号=%s" % ACCOUNT_ID)


def handlebar(ContextInfo):
    today = get_today(ContextInfo)
    if ContextInfo.last_trade_date != today:
        ContextInfo.rebalanced_today.clear()
        ContextInfo.last_trade_date = today
        ContextInfo.risk_manager.daily_pnl = 0.0
        _log(ContextInfo, "info", "新的交易日: %s" % today)

    asset_info = get_account_info(ContextInfo)
    total_asset = asset_info.get("total_asset", 0)
    if total_asset > 0 and ContextInfo.initial_asset == 0:
        ContextInfo.initial_asset = total_asset

    is_safe, risk_reason = ContextInfo.risk_manager.check_portfolio_risk(total_asset, ContextInfo.initial_asset)
    if not is_safe:
        _log(ContextInfo, "warning", "触发组合风险，原因=%s，执行清仓" % risk_reason)
        liquidate_all(ContextInfo, get_positions(ContextInfo), remark=risk_reason)
        _save_state(ContextInfo)
        return

    positions = get_positions(ContextInfo)
    for code in list(ContextInfo.risk_manager.positions.keys()):
        if code not in positions:
            ContextInfo.risk_manager.remove_position(code)
            continue
        rt = get_realtime_data(ContextInfo, code)
        if rt is None:
            continue
        price = rt.get("lastPrice", 0)
        if price <= 0:
            continue
        action, reason = ContextInfo.risk_manager.update_price(code, price)
        if action:
            _log(ContextInfo, "info", "触发%s %s 当前价=%.3f 原因=%s" % (
                "止损" if action == "STOP_LOSS" else "跟踪止盈", code, price, reason))
            place_order(ContextInfo, code, OP_SELL, positions.get(code, 0), remark=reason)
            ContextInfo.risk_manager.remove_position(code)

    _save_state(ContextInfo)


def rebalance_task(ContextInfo):
    now_str = datetime.datetime.now().strftime("%H:%M")
    if now_str in ContextInfo.rebalanced_today:
        return
    _log(ContextInfo, "info", "===== 开始调仓 %s =====" % now_str)

    asset_info = get_account_info(ContextInfo)
    total_asset = asset_info.get("total_asset", 0)
    available_cash = asset_info.get("available_cash", 0)
    if total_asset <= 0:
        _log(ContextInfo, "warning", "未能获取账户资产，跳过调仓")
        return
    if ContextInfo.initial_asset == 0:
        ContextInfo.initial_asset = total_asset

    is_safe, risk_reason = ContextInfo.risk_manager.check_portfolio_risk(total_asset, ContextInfo.initial_asset)
    if not is_safe:
        _log(ContextInfo, "warning", "组合风险未解除，跳过调仓")
        return

    positions = get_positions(ContextInfo)
    bond_list = get_bond_pool(ContextInfo)
    candidates = select_candidates(ContextInfo, bond_list, top_n=POSITION_CONFIG["max_holding_count"] * 2)
    _log(ContextInfo, "info", "候选标的: %s" % candidates)
    _log(ContextInfo, "info", "当前持仓: %s" % list(positions.keys()))

    # 同步持仓状态
    for code, volume in positions.items():
        if code not in ContextInfo.risk_manager.positions:
            rt = get_realtime_data(ContextInfo, code)
            price = rt.get("lastPrice", 0) if rt else 0
            hist = get_history_data(ContextInfo, code, "1d", 30)
            atr_val = None
            if hist:
                atr_val = atr(list(hist["high"]), list(hist["low"]), list(hist["close"]), RISK_CONFIG["atr_period"])
            ContextInfo.risk_manager.add_position(code, price, now_str, volume, atr_val)

    # 买入新标的
    open_slots = POSITION_CONFIG["max_holding_count"] - len(positions)
    for code in candidates:
        if open_slots <= 0:
            break
        if code in positions:
            continue
        if not ContextInfo.risk_manager.should_open_new(available_cash, total_asset):
            break
        rt = get_realtime_data(ContextInfo, code)
        if rt is None:
            continue
        current_price = rt.get("lastPrice", 0)
        if current_price <= 0:
            continue
        hist = get_history_data(ContextInfo, code, "1d", 30)
        atr_val = None
        if hist:
            atr_val = atr(list(hist["high"]), list(hist["low"]), list(hist["close"]), RISK_CONFIG["atr_period"])
        volume = ContextInfo.risk_manager.calc_target_volume(current_price, total_asset)
        if volume < 10:
            continue
        order_id = place_order(ContextInfo, code, OP_BUY, volume, remark="rebalance_%s" % now_str)
        if order_id:
            ContextInfo.risk_manager.add_position(code, current_price, now_str, volume, atr_val)
            available_cash -= volume * current_price
            open_slots -= 1

    ContextInfo.rebalanced_today.add(now_str)
    _save_state(ContextInfo)
    _log(ContextInfo, "info", "===== 调仓结束 %s =====" % now_str)


def daily_close_task(ContextInfo):
    _log(ContextInfo, "info", "日终任务：保存状态")
    _save_state(ContextInfo)
