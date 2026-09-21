# -*- coding: utf-8 -*-
"""
国金QMT可转债交易策略（单文件优化版 v2）
=====================================
直接复制本文件内容到国金QMT终端的策略编辑器即可运行，无需其他依赖文件。

优化重点：
- 风控：组合级锁定机制、当日盈亏基于日初资产、ATR止损更稳健、跟踪止盈更敏感
- 选股：双低因子、正股趋势、量能突破、价格分位、强赎风险过滤
- 交易：买入成功后加入风控、卖出限价更保守、状态文件原子写入
- 性能：降低bar内重复数据调用频率
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
    "single_position_ratio": 0.18,  # 单标仓位上限
    "total_position_ratio": 0.80,   # 总仓位上限
    "min_trade_amount": 1000,     # 最小可用现金要求
}

FILTER_CONFIG = {
    "sector": "沪深可转债",        # QMT板块名
    "custom_list": [],            # 自定义标的列表，为空则使用板块
    "max_count": 80,              # 板块最大取数
    "min_price": 100.0,
    "max_price": 145.0,           # 排除高价妖债
    "min_premium_ratio": -10.0,   # 最小转股溢价率(%)
    "max_premium_ratio": 35.0,    # 最大转股溢价率(%)
    "min_remaining_scale": 0.5,   # 最小剩余规模（亿元）
    "max_remaining_scale": 25.0,  # 最大剩余规模（亿元）
    "min_daily_amount": 300.0,    # 最小日成交额（万元）
    "exclude_new_days": 5,        # 排除上市前N天
    "exclude_expire_days": 30,    # 排除到期前N天
    "max_strong_redemption_price": 125.0,  # 价格超过此值且溢价率高时视为强赎风险
    "max_strong_redemption_premium": 15.0, # 强赎风险溢价率阈值
    "min_double_low_rank": 40,    # 双低得分排名前N%才进入候选池
}

SIGNAL_CONFIG = {
    "lookback_days": 60,          # 历史数据长度
    "ma_short": 5,
    "ma_long": 20,
    "momentum_days": 5,
    "momentum_threshold": 0.003,
    "rsi_period": 14,
    "rsi_low": 35,
    "rsi_high": 70,
    "volume_breakout_ratio": 1.2, # 成交量突破均线倍数
    "price_percentile_window": 60,# 价格分位计算窗口
    "price_percentile_low": 0.7,  # 价格分位不高于70%（避免追高）
}

RISK_CONFIG = {
    "atr_period": 14,
    "atr_stop_multiplier": 2.5,   # ATR倍数（可转债波动大，适当放宽避免洗盘）
    "fixed_stop_ratio": 0.025,
    "min_stop_ratio": 0.020,      # ATR止损不得低于2%
    "use_trailing_stop": True,
    "trailing_atr_multiplier": 1.5,
    "trailing_min_profit_ratio": 0.020,
    "max_daily_loss_ratio": 0.04, # 日最大亏损4%
    "max_drawdown_ratio": 0.08,   # 最大回撤8%
    "max_single_loss_ratio": 0.05,# 单票最大亏损5%
}

# 同一标的同一方向下单冷却时间（秒），防止重复下单
ORDER_COOLDOWN_SECONDS = 60

# 每隔多少根K线同步一次持仓（建议1分钟周期设5，即5分钟同步一次）
SYNC_INTERVAL_BARS = 5

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


def ema(values, period):
    """指数移动平均"""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result


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


def price_percentile(closes, window=60):
    """当前价格在过去N日高低点中的分位，0~1之间"""
    if len(closes) < window:
        return None
    window_closes = closes[-window:]
    lo, hi = min(window_closes), max(window_closes)
    if hi <= lo:
        return 0.5
    return (closes[-1] - lo) / (hi - lo)


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
        self.max_single_loss_price = entry_price * (1 - RISK_CONFIG["max_single_loss_ratio"])

        # 固定止损
        fixed_stop = entry_price * (1 - RISK_CONFIG["fixed_stop_ratio"])

        # ATR止损，且不低于最小止损幅度
        if self.atr_value and self.atr_value > 0:
            atr_stop = entry_price - RISK_CONFIG["atr_stop_multiplier"] * self.atr_value
            min_stop = entry_price * (1 - RISK_CONFIG["min_stop_ratio"])
            atr_stop = min(atr_stop, min_stop)  # 取更靠近入场价的止损（更严格）
        else:
            atr_stop = entry_price * 0.95

        # 实际止损取固定、ATR、单票最大亏损中最高者（最宽松，最稳健）
        self.stop_price = max(fixed_stop, atr_stop, self.max_single_loss_price)
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
        # 跟踪止盈线只升不降，且不低于成本价（保本）
        candidate = max(candidate, self.entry_price)
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
        self.day_start_asset = 0.0
        self.peak_asset = 0.0
        self.locked = False
        self.lock_reason = None
        self.lock_date = None

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

    def lock(self, reason, date_str):
        self.locked = True
        self.lock_reason = reason
        self.lock_date = date_str

    def unlock_if_new_day(self, date_str):
        if self.locked and self.lock_date != date_str:
            self.locked = False
            self.lock_reason = None
            self.lock_date = None

    def is_locked(self):
        return self.locked

    def check_portfolio_risk(self, total_asset, date_str):
        """返回 (is_safe, reason)。触发风控时同时锁定。"""
        if total_asset <= 0:
            return True, None

        self.unlock_if_new_day(date_str)

        # 更新峰值
        if total_asset > self.peak_asset:
            self.peak_asset = total_asset

        # 当日最大亏损（基于日初总资产）
        if self.day_start_asset > 0:
            daily_loss_ratio = (self.day_start_asset - total_asset) / self.day_start_asset
            if daily_loss_ratio >= RISK_CONFIG["max_daily_loss_ratio"]:
                self.lock("daily_loss_limit", date_str)
                return False, "daily_loss_limit"

        # 最大回撤（基于历史峰值）
        drawdown = (self.peak_asset - total_asset) / self.peak_asset if self.peak_asset > 0 else 0
        if drawdown >= RISK_CONFIG["max_drawdown_ratio"]:
            self.lock("max_drawdown_limit", date_str)
            return False, "max_drawdown_limit"

        return True, None

    def should_open_new(self, available_cash, total_asset):
        if self.locked:
            return False
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
    if STATE_FILE_PATH:
        return STATE_FILE_PATH
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "qmt_cb_strategy_state.json")


def _atomic_write(path, content):
    """原子写入状态文件，避免中途损坏"""
    try:
        dir_path = os.path.dirname(path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        if os.path.exists(path):
            os.replace(tmp_path, path)
        else:
            os.rename(tmp_path, path)
    except Exception:
        # 原子写入失败时尝试直接写入
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def _get_field(obj, candidates, default=None):
    for name in candidates:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    return default


def _safe_float(val):
    try:
        return float(val)
    except Exception:
        return None


def _extract_price(tick_data, field="lastPrice"):
    if tick_data is None:
        return 0.0
    if isinstance(tick_data, dict):
        for f in [field, "lastPrice", "close", "last", "now", "price"]:
            if f in tick_data:
                val = tick_data[f]
                if val is not None and val > 0:
                    return float(val)
        return 0.0
    for attr in [field, "lastPrice", "close", "last", "now", "price"]:
        if hasattr(tick_data, attr):
            val = getattr(tick_data, attr)
            if val is not None and val > 0:
                return float(val)
    return 0.0


def _extract_amount(tick_data):
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


def _get_underlying_stock(code, info=None):
    """尝试获取可转债对应的正股代码"""
    if info:
        stock = _get_field(info, [
            "underlying_code", "stock_code", "m_strUnderlyingCode",
            "m_strStockCode", "underlying", "正股代码"
        ])
        if stock:
            return str(stock)
    # 简单推断（不可靠，仅作为fallback）
    prefix = code[:3]
    if prefix in ("110", "113", "118", "111"):
        return None  # 沪市，无法简单推断
    if prefix in ("123", "127", "128", "129"):
        return None  # 深市，无法简单推断
    return None


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


def get_order_price(ContextInfo, code, operation, default_price=0.0):
    """
    为限价单获取委托价格。
    买入：默认使用最新价（稳健）；
    卖出：使用最新价与跌停价之间偏保守的价格，确保止损止盈更快成交。
    """
    price = default_price
    if price <= 0:
        rt = get_realtime_data(ContextInfo, code)
        price = _extract_price(rt)
    if price <= 0:
        return 0.0

    if operation == OP_SELL:
        # 卖出时稍微压低价格（但不超过跌停限制），提升成交概率
        # A股可转债跌停约 -20%，这里用 0.5% 让利
        return round(price * 0.995, 3)
    return round(price, 3)


def place_order(ContextInfo, code, operation, volume, price=None, remark=""):
    if volume <= 0:
        return None
    volume = int(volume / 10) * 10
    if volume < 10:
        return None

    if not can_place_order(ContextInfo, code, operation):
        return None

    order_price = get_order_price(ContextInfo, code, operation, price)
    if order_price <= 0:
        _log(ContextInfo, "warning", "无法获取有效价格，取消下单: %s" % code)
        return None

    try:
        _log(ContextInfo, "info", "下单 %s %s vol=%d price=%.3f remark=%s" % (
            code, "BUY" if operation == OP_BUY else "SELL", volume, order_price, remark))
        order_id = ContextInfo.passorder(
            operation, PRICE_FIX, ACCOUNT_ID, code, volume, order_price,
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
# 6. 可转债筛选与评分
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


def _parse_expire_date(expire):
    expire_str = str(expire)
    formats = ["%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(expire_str, fmt).date()
        except Exception:
            continue
    return None


def filter_bonds(ContextInfo, bond_list):
    """可转债初步过滤：价格、成交额、规模、溢价率、到期日、强赎风险"""
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

        # 成交量过滤：当日不低于20日均量30%（避免流动性枯竭）
        avg_vol = volume_ma(volumes, 20)
        if avg_vol and volumes[-1] < avg_vol * 0.3:
            continue

        # 上市时间过滤
        if len(closes) < FILTER_CONFIG["exclude_new_days"] + 20:
            continue

        info = get_instrument_info(ContextInfo, code)
        premium = None
        scale = None
        if info:
            premium = _safe_float(_get_field(info, [
                "conversion_premium", "premium_ratio", "m_dPremiumRate",
                "m_dConversionPremium", "m_fPremium"
            ]))
            scale = _safe_float(_get_field(info, [
                "remaining_scale", "m_dRemainingScale", "m_fRemainingScale",
                "balance", "m_dBalance"
            ]))

            # 溢价率过滤
            if premium is not None:
                if not (FILTER_CONFIG["min_premium_ratio"] <= premium <= FILTER_CONFIG["max_premium_ratio"]):
                    continue

            # 剩余规模过滤
            if scale is not None:
                if not (FILTER_CONFIG["min_remaining_scale"] <= scale <= FILTER_CONFIG["max_remaining_scale"]):
                    continue

            # 到期日过滤
            expire = _get_field(info, ["expire_date", "m_strExpireDate", "maturity_date", "m_strMaturityDate"])
            if expire:
                expire_date = _parse_expire_date(expire)
                if expire_date:
                    today = datetime.datetime.now().date()
                    if (expire_date - today).days < FILTER_CONFIG["exclude_expire_days"]:
                        continue

        # 强赎风险：价格过高且溢价率不低，排除
        if premium is not None:
            if (price >= FILTER_CONFIG["max_strong_redemption_price"] and
                    premium >= FILTER_CONFIG["max_strong_redemption_premium"]):
                continue

        result.append((code, price, premium, scale))
    return result


def score_bond(ContextInfo, code, price=None, premium=None, scale=None):
    """
    可转债综合打分，越高越好。
    核心逻辑：
    - 双低得分（低价+低溢价）加分
    - 均线多头排列加分
    - 动量为正加分
    - 量能突破加分
    - RSI 不超买加分
    - 价格分位适中加分
    - 正股趋势向上加分（如果可获取）
    """
    hist = get_history_data(ContextInfo, code, "1d", SIGNAL_CONFIG["lookback_days"] + 5)
    if hist is None:
        return -999

    closes = list(hist.get("close", []))
    highs = list(hist.get("high", []))
    lows = list(hist.get("low", []))
    volumes = list(hist.get("volume", []))
    if len(closes) < SIGNAL_CONFIG["ma_long"] + 5:
        return -999

    if price is None or price <= 0:
        price = closes[-1]

    score = 0.0

    # 1. 双低因子：价格+溢价率*10，越低越好
    if premium is not None:
        double_low = price + premium * 10
        # 双低得分映射到 0~30 分
        if double_low <= 130:
            score += 30
        elif double_low <= 140:
            score += 20
        elif double_low <= 150:
            score += 10
        else:
            score -= 10

    # 2. 均线趋势
    ma_s = sma(closes, SIGNAL_CONFIG["ma_short"])
    ma_l = sma(closes, SIGNAL_CONFIG["ma_long"])
    if ma_s and ma_l:
        if ma_s > ma_l:
            score += 25
        if closes[-1] > ma_s:
            score += 10

    # 3. 动量
    mom = momentum(closes, SIGNAL_CONFIG["momentum_days"])
    if mom is not None:
        if mom > SIGNAL_CONFIG["momentum_threshold"]:
            score += 15 + min(mom * 800, 10)
        elif mom < -0.02:
            score -= 15

    # 4. 量能突破
    avg_vol = volume_ma(volumes, 20)
    if avg_vol and volumes[-1] > avg_vol * SIGNAL_CONFIG["volume_breakout_ratio"]:
        score += 10

    # 5. RSI 不在超买区
    rsi_val = rsi(closes, SIGNAL_CONFIG["rsi_period"])
    if rsi_val is not None:
        if rsi_val < SIGNAL_CONFIG["rsi_low"]:
            score += 10
        elif rsi_val > SIGNAL_CONFIG["rsi_high"]:
            score -= 25

    # 6. 价格分位：避免追高
    pct = price_percentile(closes, SIGNAL_CONFIG["price_percentile_window"])
    if pct is not None:
        if pct <= SIGNAL_CONFIG["price_percentile_low"]:
            score += 10
        elif pct > 0.9:
            score -= 15

    # 7. 波动率适中
    atr_val = atr(highs, lows, closes, RISK_CONFIG["atr_period"])
    if atr_val:
        atr_ratio = atr_val / closes[-1]
        if 0.004 <= atr_ratio <= 0.025:
            score += 5
        elif atr_ratio > 0.04:
            score -= 5

    # 8. 正股趋势（可选）
    info = get_instrument_info(ContextInfo, code)
    stock_code = _get_underlying_stock(code, info)
    if stock_code:
        stock_hist = get_history_data(ContextInfo, stock_code, "1d", 30)
        if stock_hist:
            stock_closes = list(stock_hist.get("close", []))
            if len(stock_closes) >= 20:
                stock_ma20 = sma(stock_closes, 20)
                stock_ma5 = sma(stock_closes, 5)
                if stock_ma20 and stock_ma5 and stock_ma5 > stock_ma20:
                    score += 10

    return score


def select_candidates(ContextInfo, bond_list, top_n=None):
    """先过滤，再打分排序，返回候选标的"""
    filtered = filter_bonds(ContextInfo, bond_list)
    scored = []
    for code, price, premium, scale in filtered:
        s = score_bond(ContextInfo, code, price, premium, scale)
        if s > 0:
            scored.append((code, s))
    scored.sort(key=lambda x: x[1], reverse=True)

    # 双低排名前N%也保留，确保不全是趋势票
    if FILTER_CONFIG["min_double_low_rank"] < 100:
        double_low = [(code, price, premium) for code, price, premium, _ in filtered if premium is not None]
        double_low.sort(key=lambda x: x[1] + x[2] * 10)
        keep_top = max(1, int(len(double_low) * FILTER_CONFIG["min_double_low_rank"] / 100.0))
        top_codes = set(item[0] for item in double_low[:keep_top])
        # 合并：在打分前列或双低前列
        scored_codes = set(item[0] for item in scored)
        extra = [(code, 0.1) for code in top_codes if code not in scored_codes]
        scored = scored + extra
        scored.sort(key=lambda x: x[1], reverse=True)

    if top_n:
        scored = scored[:top_n]
    return [code for code, _ in scored]


# ============================================================
# 7. 持仓同步与再评估
# ============================================================

def _sync_positions(ContextInfo, positions):
    """将实际持仓同步到本地风控器"""
    for code, volume in positions.items():
        if code in ContextInfo.risk_manager.positions:
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


def _should_sell_existing(ContextInfo, code):
    """持仓再评估：跌破长期均线或打分过低则卖出"""
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
    if price < ma_l * 0.98:
        return True
    if score_bond(ContextInfo, code, price) < -20:
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
            ContextInfo.risk_manager.day_start_asset = state.get("day_start_asset", 0)
            ContextInfo.risk_manager.peak_asset = state.get("peak_asset", 0)
            ContextInfo.risk_manager.locked = state.get("locked", False)
            ContextInfo.risk_manager.lock_reason = state.get("lock_reason")
            ContextInfo.risk_manager.lock_date = state.get("lock_date")
            ContextInfo.last_trade_date = state.get("last_trade_date")
            ContextInfo.rebalanced_today = set(state.get("rebalanced_today", []))
            _log(ContextInfo, "info", "加载历史状态成功，锁定状态=%s" % ContextInfo.risk_manager.locked)
    except Exception as e:
        _log(ContextInfo, "error", "加载状态失败: %s" % str(e))


def _save_state(ContextInfo):
    path = _state_file_path()
    try:
        state = {
            "initial_asset": getattr(ContextInfo, "initial_asset", 0),
            "day_start_asset": ContextInfo.risk_manager.day_start_asset,
            "peak_asset": ContextInfo.risk_manager.peak_asset,
            "locked": ContextInfo.risk_manager.locked,
            "lock_reason": ContextInfo.risk_manager.lock_reason,
            "lock_date": ContextInfo.risk_manager.lock_date,
            "last_trade_date": getattr(ContextInfo, "last_trade_date", None),
            "rebalanced_today": list(getattr(ContextInfo, "rebalanced_today", set())),
        }
        _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))
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
    ContextInfo._bar_count = 0
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
    ContextInfo._bar_count += 1
    today = get_today(ContextInfo)
    is_new_day = ContextInfo.last_trade_date != today
    if is_new_day:
        ContextInfo.risk_manager.unlock_if_new_day(today)
        asset_info = get_account_info(ContextInfo)
        total_asset = asset_info.get("total_asset", 0)
        if total_asset > 0:
            ContextInfo.risk_manager.day_start_asset = total_asset
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
    is_safe, risk_reason = ContextInfo.risk_manager.check_portfolio_risk(total_asset, today)
    if not is_safe:
        _log(ContextInfo, "warning", "触发组合风险，原因=%s，执行清仓并锁定" % risk_reason)
        liquidate_all(ContextInfo, get_positions(ContextInfo), remark=risk_reason)
        _save_state(ContextInfo)
        return

    # 风控锁定中，不开新仓但仍要监控止损止盈
    if ContextInfo.risk_manager.is_locked():
        _log(ContextInfo, "info", "风控锁定中，原因=%s，仅监控止损止盈" % ContextInfo.risk_manager.lock_reason)

    # 同步实际持仓到本地风控器（按间隔降低频率）
    positions = get_positions(ContextInfo)
    if ContextInfo._bar_count % SYNC_INTERVAL_BARS == 0 or is_new_day:
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
    today = get_today(ContextInfo)
    _log(ContextInfo, "info", "===== 开始调仓 %s =====" % now_str)

    asset_info = get_account_info(ContextInfo)
    total_asset = asset_info.get("total_asset", 0)
    available_cash = asset_info.get("available_cash", 0)
    if total_asset <= 0:
        _log(ContextInfo, "warning", "未能获取账户资产，跳过调仓")
        return
    if ContextInfo.initial_asset == 0:
        ContextInfo.initial_asset = total_asset

    # 风控锁定检查
    if ContextInfo.risk_manager.is_locked():
        _log(ContextInfo, "warning", "风控锁定中，跳过调仓")
        return

    is_safe, risk_reason = ContextInfo.risk_manager.check_portfolio_risk(total_asset, today)
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

        # 先下单，成功后再加入风控器
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
