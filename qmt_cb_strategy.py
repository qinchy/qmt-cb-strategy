# -*- coding: utf-8 -*-
"""
国金QMT可转债交易策略（单文件优化版）
=====================================
直接复制本文件内容到国金QMT终端的策略编辑器即可运行，无需其他依赖文件。

功能：
- 每天多时段自动调仓
- 盘中实时行情监控，逐bar检查止损止盈
- ATR止损 + 固定比率止损 + 最小止损幅度保护
- 最高价跟踪止盈，盈利后不断抬高止盈线
- 组合级风控：单日最大亏损、最大回撤
- 策略重启后自动同步实际持仓
"""

import os
import json
import math
import datetime
import time

# ============================================================
# 1. 参数配置（使用前请修改 ACCOUNT_ID）
# ============================================================

ACCOUNT_ID = "88888888"          # 资金账号
ACCOUNT_TYPE = "STOCK"           # 国金QMT中一般传 "STOCK" 或 "CREDIT"

# 状态文件保存路径。QMT内置编辑器运行时 __file__ 可能不存在，建议写死为固定路径
STATE_FILE_PATH = "D:/QMT/userdata/log/qmt_cb_strategy_state.json"

REBALANCE_TIMES = ["09:35", "10:30", "11:20", "14:00", "14:45"]

POSITION_CONFIG = {
    "max_holding_count": 5,       # 最大持仓数量
    "single_position_ratio": 0.20,  # 单标仓位上限
    "total_position_ratio": 0.90,   # 总仓位上限
    "min_trade_amount": 1000,     # 最小可用现金要求
}

FILTER_CONFIG = {
    "sector": "沪深可转债",        # QMT板块名
    "custom_list": [],            # 自定义标的列表，为空则使用板块
    "max_count": 50,              # 板块最大取数
    "min_price": 100.0,
    "max_price": 150.0,
    "min_premium_ratio": -10.0,   # 最小转股溢价率(%)
    "max_premium_ratio": 50.0,    # 最大转股溢价率(%)
    "min_remaining_scale": 0.5,   # 最小剩余规模（亿元）
    "max_remaining_scale": 30.0,  # 最大剩余规模（亿元）
    "min_daily_amount": 500.0,    # 最小日成交额（万元）
    "exclude_new_days": 5,        # 排除上市前N天
    "exclude_expire_days": 30,    # 排除到期前N天
}

SIGNAL_CONFIG = {
    "lookback_days": 20,
    "ma_short": 5,
    "ma_long": 20,
    "momentum_days": 5,
    "momentum_threshold": 0.005,
    "rsi_period": 14,
    "rsi_low": 30,
    "rsi_high": 70,
}

RISK_CONFIG = {
    "atr_period": 14,
    "atr_stop_multiplier": 1.5,
    "fixed_stop_ratio": 0.03,
    "min_stop_ratio": 0.015,      # ATR止损不得低于此比例，防止ATR过窄被洗出
    "use_trailing_stop": True,
    "trailing_atr_multiplier": 2.0,
    "trailing_min_profit_ratio": 0.015,
    "max_daily_loss_ratio": 0.05,
    "max_drawdown_ratio": 0.10,
}

# 同一标的同一方向下单冷却时间（秒），防止重复下单
ORDER_COOLDOWN_SECONDS = 60

# QMT passorder 常量
OP_BUY = 23
OP_SELL = 24
PRICE_FIX = 5

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

        # 固定止损
        fixed_stop = entry_price * (1 - RISK_CONFIG["fixed_stop_ratio"])

        # ATR止损，且不低于最小止损幅度
        if self.atr_value and self.atr_value > 0:
            atr_stop = entry_price - RISK_CONFIG["atr_stop_multiplier"] * self.atr_value
            min_stop = entry_price * (1 - RISK_CONFIG["min_stop_ratio"])
            atr_stop = min(atr_stop, min_stop)  # 取更靠近入场价的止损（更严格）
        else:
            atr_stop = entry_price * 0.95

        # 实际止损取固定和ATR中较高者（更宽松，更稳健）
        self.stop_price = max(fixed_stop, atr_stop)
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
        # 跟踪止盈线只升不降
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
        self.prev_day_asset = 0.0

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

    def check_portfolio_risk(self, total_asset):
        if total_asset <= 0:
            return True, None

        # 当日最大亏损（基于昨日收盘总资产）
        if self.prev_day_asset > 0:
            daily_loss = self.prev_day_asset - total_asset
            daily_loss_ratio = daily_loss / self.prev_day_asset
            if daily_loss_ratio >= RISK_CONFIG["max_daily_loss_ratio"]:
                return False, "daily_loss_limit"

        # 最大回撤
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
        # 可转债1手=10张，每张价格即当前价格（每百元面额价格）
        volume = int(amount / price / 10) * 10
        return volume if volume >= 10 else 0


# ============================================================
# 4. 工具函数
# ============================================================

def _log(ContextInfo, level, msg):
    try:
        if hasattr(ContextInfo, "log") and ContextInfo.log:
            log_func = getattr(ContextInfo.log, level, ContextInfo.log.info)
            log_func(msg)
        else:
            print("[%s] %s" % (level.upper(), msg))
    except Exception:
        print(msg)


def _state_file_path():
    """状态文件路径，优先使用配置的固定路径"""
    if STATE_FILE_PATH:
        return STATE_FILE_PATH
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "qmt_cb_strategy_state.json")


def _get_field(obj, candidates, default=None):
    """尝试从对象或字典中获取多个候选字段之一"""
    for name in candidates:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    return default


def _extract_price(tick_data, field="lastPrice"):
    """
    兼容不同QMT版本的tick数据结构
    tick_data 可能是 dict 或 XtTick 对象
    """
    if tick_data is None:
        return 0.0

    # 如果是字典
    if isinstance(tick_data, dict):
        # 常见字段名
        for f in [field, "lastPrice", "close", "last", "now", "price"]:
            if f in tick_data:
                val = tick_data[f]
                if val is not None and val > 0:
                    return float(val)
        return 0.0

    # 如果是对象，尝试常见属性
    for attr in [field, "lastPrice", "close", "last", "now", "price"]:
        if hasattr(tick_data, attr):
            val = getattr(tick_data, attr)
            if val is not None and val > 0:
                return float(val)
    return 0.0


def _extract_amount(tick_data):
    """提取成交额"""
    if tick_data is None:
        return 0.0
    if isinstance(tick_data, dict):
        for f in ["amount", "amt", "totalAmount"]:
            if f in tick_data:
                return float(tick_data[f] or 0)
    for attr in ["amount", "amt", "totalAmount"]:
        if hasattr(tick_data, attr):
            return float(getattr(tick_data, attr) or 0)
    return 0.0


# ============================================================
# 5. 交易执行
# ============================================================

def get_account_info(ContextInfo):
    try:
        data = ContextInfo.get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, "asset")
        if data:
            a = data[0]
            return {
                "total_asset": float(getattr(a, "m_dTotalAssets", 0) or 0),
                "available_cash": float(getattr(a, "m_dAvailable", 0) or 0),
                "market_value": float(getattr(a, "m_dMarketValue", 0) or 0),
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
                positions[code] = int(vol)
    except Exception as e:
        _log(ContextInfo, "error", "获取持仓失败: %s" % str(e))
    return positions


def can_place_order(ContextInfo, code, operation):
    """检查是否已过下单冷却时间"""
    key = (code, operation)
    now = time.time()
    last = getattr(ContextInfo, "_order_cooldown", {}).get(key, 0)
    if now - last < ORDER_COOLDOWN_SECONDS:
        return False
    return True


def record_order_time(ContextInfo, code, operation):
    if not hasattr(ContextInfo, "_order_cooldown"):
        ContextInfo._order_cooldown = {}
    ContextInfo._order_cooldown[(code, operation)] = time.time()


def place_order(ContextInfo, code, operation, volume, price=None, remark=""):
    if volume <= 0:
        return None
    volume = int(volume / 10) * 10
    if volume < 10:
        return None

    if not can_place_order(ContextInfo, code, operation):
        _log(ContextInfo, "info", "订单冷却中，忽略: %s %s" % (code, operation))
        return None

    # 默认使用限价，价格为最新价；如未提供价格，尝试获取实时价
    if price is None or price <= 0:
        rt = get_realtime_data(ContextInfo, code)
        price = _extract_price(rt) if rt else 0
    if price <= 0:
        _log(ContextInfo, "warning", "无法获取有效价格，取消下单: %s" % code)
        return None

    try:
        _log(ContextInfo, "info", "下单 %s %s vol=%d price=%.3f remark=%s" % (
            code, "BUY" if operation == OP_BUY else "SELL", volume, price, remark))
        order_id = ContextInfo.passorder(
            operation, PRICE_FIX, ACCOUNT_ID, code, volume, price,
            "可转债策略", remark, ContextInfo
        )
        if order_id:
            record_order_time(ContextInfo, code, operation)
        return order_id
    except Exception as e:
        _log(ContextInfo, "error", "下单失败 %s: %s" % (code, str(e)))
        return None


def liquidate_all(ContextInfo, positions, remark="risk_control"):
    for code, volume in positions.items():
        place_order(ContextInfo, code, OP_SELL, volume, remark=remark)


# ============================================================
# 6. 可转债筛选
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


def get_instrument_info(ContextInfo, code):
    try:
        return ContextInfo.get_instrumentdetail(code)
    except Exception:
        return {}


def get_history_data(ContextInfo, code, period="1d", count=60):
    try:
        data = ContextInfo.get_market_data(
            ["open", "high", "low", "close", "volume"],
            [code], period, count, fill_up=False
        )
        if data and code in data:
            return data[code]
    except Exception as e:
        _log(ContextInfo, "error", "获取历史行情失败 %s: %s" % (code, str(e)))
    return None


def get_realtime_data(ContextInfo, code):
    """优先使用 get_full_tick，失败则回退到日K最新数据"""
    try:
        data = ContextInfo.get_full_tick([code])
        if data and code in data:
            return data[code]
    except Exception:
        pass
    try:
        data = ContextInfo.get_market_data(
            ["close", "high", "low", "volume", "amount"],
            [code], "1d", 1, fill_up=False
        )
        if data and code in data:
            d = data[code]
            return {
                "lastPrice": d["close"][-1] if "close" in d else 0,
                "high": d["high"][-1] if "high" in d else 0,
                "low": d["low"][-1] if "low" in d else 0,
                "volume": d["volume"][-1] if "volume" in d else 0,
                "amount": d["amount"][-1] if "amount" in d else 0,
            }
    except Exception as e:
        _log(ContextInfo, "error", "获取实时行情失败 %s: %s" % (code, str(e)))
    return None


def _safe_float(val):
    try:
        return float(val)
    except Exception:
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

        price = _extract_price(rt)
        if price <= 0:
            price = closes[-1]
        if price <= 0:
            continue

        # 价格过滤
        if not (FILTER_CONFIG["min_price"] <= price <= FILTER_CONFIG["max_price"]):
            continue

        # 成交额过滤
        amount = _extract_amount(rt)
        if amount < FILTER_CONFIG["min_daily_amount"] * 10000:
            continue

        # 成交量过滤
        avg_vol = volume_ma(volumes, 20)
        if avg_vol and volumes[-1] < avg_vol * 0.3:
            continue

        # 上市时间过滤（历史数据长度近似）
        if len(closes) < FILTER_CONFIG["exclude_new_days"] + 20:
            continue

        # 读取合约详情，尝试过滤溢价率、剩余规模、到期日等
        info = get_instrument_info(ContextInfo, code)
        if info:
            # 转股溢价率：不同QMT版本字段名可能不同
            premium = _safe_float(_get_field(info, [
                "conversion_premium", "premium_ratio", "m_dPremiumRate",
                "m_dConversionPremium", "m_fPremium"
            ]))
            if premium is not None:
                if not (FILTER_CONFIG["min_premium_ratio"] <= premium <= FILTER_CONFIG["max_premium_ratio"]):
                    continue

            # 剩余规模（亿元）
            scale = _safe_float(_get_field(info, [
                "remaining_scale", "m_dRemainingScale", "m_fRemainingScale",
                "balance", "m_dBalance"
            ]))
            if scale is not None:
                if not (FILTER_CONFIG["min_remaining_scale"] <= scale <= FILTER_CONFIG["max_remaining_scale"]):
                    continue

            # 到期日过滤
            expire = _get_field(info, ["expire_date", "m_strExpireDate", "maturity_date", "m_strMaturityDate"])
            if expire:
                try:
                    expire_str = str(expire)
                    if len(expire_str) == 8:
                        expire_date = datetime.datetime.strptime(expire_str, "%Y%m%d").date()
                        today = datetime.datetime.now().date()
                        if (expire_date - today).days < FILTER_CONFIG["exclude_expire_days"]:
                            continue
                except Exception:
                    pass

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

    # 均线趋势
    ma_s = sma(closes, SIGNAL_CONFIG["ma_short"])
    ma_l = sma(closes, SIGNAL_CONFIG["ma_long"])
    if ma_s and ma_l and ma_s > ma_l:
        score += 30

    # 动量
    mom = momentum(closes, SIGNAL_CONFIG["momentum_days"])
    if mom and mom > SIGNAL_CONFIG["momentum_threshold"]:
        score += 20 + mom * 1000

    # RSI 不在超买区
    rsi_val = rsi(closes, SIGNAL_CONFIG["rsi_period"])
    if rsi_val is not None:
        if rsi_val < SIGNAL_CONFIG["rsi_low"]:
            score += 20
        elif rsi_val > SIGNAL_CONFIG["rsi_high"]:
            score -= 30

    # 波动率适中
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
# 7. 持仓同步与再评估
# ============================================================

def _sync_positions(ContextInfo, positions):
    """
    将实际持仓同步到本地风控器，主要用于策略重启后恢复状态
    """
    for code, volume in positions.items():
        if code in ContextInfo.risk_manager.positions:
            # 更新数量
            ContextInfo.risk_manager.positions[code].volume = volume
            continue

        rt = get_realtime_data(ContextInfo, code)
        price = _extract_price(rt)
        if price <= 0:
            continue

        hist = get_history_data(ContextInfo, code, "1d", 30)
        atr_val = None
        if hist:
            atr_val = atr(
                list(hist.get("high", [])),
                list(hist.get("low", [])),
                list(hist.get("close", [])),
                RISK_CONFIG["atr_period"]
            )

        now_str = datetime.datetime.now().strftime("%H:%M")
        ContextInfo.risk_manager.add_position(code, price, now_str, volume, atr_val)
        _log(ContextInfo, "info", "同步持仓 %s 成本=%.3f 数量=%d" % (code, price, volume))


def _should_sell_existing(ContextInfo, code):
    """
    持仓再评估：跌破均线或打分过低则卖出
    """
    hist = get_history_data(ContextInfo, code, "1d", SIGNAL_CONFIG["lookback_days"] + 5)
    if hist is None:
        return False
    closes = list(hist.get("close", []))
    if len(closes) < SIGNAL_CONFIG["ma_long"]:
        return False
    ma_l = sma(closes, SIGNAL_CONFIG["ma_long"])
    if ma_l is None:
        return False
    rt = get_realtime_data(ContextInfo, code)
    price = _extract_price(rt) if rt else closes[-1]
    # 价格跌破长期均线
    if price < ma_l * 0.98:
        return True
    # 打分过低
    if score_bond(ContextInfo, code) < -20:
        return True
    return False


# ============================================================
# 8. 状态持久化
# ============================================================

def _load_state(ContextInfo):
    path = _state_file_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            ContextInfo.initial_asset = state.get("initial_asset", 0)
            ContextInfo.risk_manager.prev_day_asset = state.get("prev_day_asset", 0)
            ContextInfo.risk_manager.peak_asset = state.get("peak_asset", 0)
            ContextInfo.last_trade_date = state.get("last_trade_date")
            ContextInfo.rebalanced_today = set(state.get("rebalanced_today", []))
            _log(ContextInfo, "info", "加载历史状态成功")
    except Exception as e:
        _log(ContextInfo, "error", "加载状态失败: %s" % str(e))


def _save_state(ContextInfo):
    path = _state_file_path()
    try:
        # 确保目录存在
        dir_path = os.path.dirname(path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)

        state = {
            "initial_asset": getattr(ContextInfo, "initial_asset", 0),
            "prev_day_asset": ContextInfo.risk_manager.prev_day_asset,
            "peak_asset": ContextInfo.risk_manager.peak_asset,
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
# 9. QMT策略入口
# ============================================================

def init(ContextInfo):
    ContextInfo.accountid = ACCOUNT_ID
    ContextInfo.accounttype = ACCOUNT_TYPE
    ContextInfo.risk_manager = RiskManager()
    ContextInfo.rebalanced_today = set()
    ContextInfo.last_trade_date = None
    ContextInfo.initial_asset = 0
    ContextInfo._order_cooldown = {}
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
    is_new_day = ContextInfo.last_trade_date != today
    if is_new_day:
        # 交易日切换时，记录昨日总资产用于当日盈亏计算
        asset_info = get_account_info(ContextInfo)
        total_asset = asset_info.get("total_asset", 0)
        if total_asset > 0:
            ContextInfo.risk_manager.prev_day_asset = total_asset
            ContextInfo.risk_manager.peak_asset = total_asset
        ContextInfo.rebalanced_today.clear()
        ContextInfo.last_trade_date = today
        _log(ContextInfo, "info", "新的交易日: %s" % today)

    asset_info = get_account_info(ContextInfo)
    total_asset = asset_info.get("total_asset", 0)
    available_cash = asset_info.get("available_cash", 0)

    if total_asset > 0 and ContextInfo.initial_asset == 0:
        ContextInfo.initial_asset = total_asset

    # 组合级风控检查
    is_safe, risk_reason = ContextInfo.risk_manager.check_portfolio_risk(total_asset)
    if not is_safe:
        _log(ContextInfo, "warning", "触发组合风险，原因=%s，执行清仓" % risk_reason)
        liquidate_all(ContextInfo, get_positions(ContextInfo), remark=risk_reason)
        _save_state(ContextInfo)
        return

    # 同步实际持仓到本地风控器
    positions = get_positions(ContextInfo)
    _sync_positions(ContextInfo, positions)

    # 检查止损止盈
    for code in list(ContextInfo.risk_manager.positions.keys()):
        if code not in positions:
            ContextInfo.risk_manager.remove_position(code)
            continue

        rt = get_realtime_data(ContextInfo, code)
        price = _extract_price(rt)
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

    is_safe, risk_reason = ContextInfo.risk_manager.check_portfolio_risk(total_asset)
    if not is_safe:
        _log(ContextInfo, "warning", "组合风险未解除，跳过调仓")
        return

    positions = get_positions(ContextInfo)
    _sync_positions(ContextInfo, positions)

    # 持仓再评估：主动卖出趋势走坏的标的
    for code in list(positions.keys()):
        if _should_sell_existing(ContextInfo, code):
            _log(ContextInfo, "info", "持仓再评估卖出 %s" % code)
            place_order(ContextInfo, code, OP_SELL, positions.get(code, 0), remark="rebalance_sell")
            ContextInfo.risk_manager.remove_position(code)
            if code in positions:
                del positions[code]

    # 生成候选池
    bond_list = get_bond_pool(ContextInfo)
    candidates = select_candidates(ContextInfo, bond_list, top_n=POSITION_CONFIG["max_holding_count"] * 2)
    _log(ContextInfo, "info", "候选标的: %s" % candidates)
    _log(ContextInfo, "info", "当前持仓: %s" % list(positions.keys()))

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
        current_price = _extract_price(rt)
        if current_price <= 0:
            continue

        hist = get_history_data(ContextInfo, code, "1d", 30)
        atr_val = None
        if hist:
            atr_val = atr(
                list(hist.get("high", [])),
                list(hist.get("low", [])),
                list(hist.get("close", [])),
                RISK_CONFIG["atr_period"]
            )

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
