"""合约参数与各交易所下单规则。

!! 以下数值是按公开资料整理的“默认值”, 上线前请与交易员给的撮合规则/交易所最新业务规则逐项核对 !!
可通过 run_backtest(spec_overrides=..., rule_overrides=...) 覆盖。
"""
from __future__ import annotations
from dataclasses import dataclass
import re


@dataclass
class ContractSpec:
    tick_size: float
    multiplier: float
    fee_rate: float = 0.0
    fee_per_lot: float = 0.0


@dataclass
class ExchangeRule:
    name: str
    allow_market: bool                 # 是否支持市价单
    market_tifs: tuple = ("FAK", "FOK")  # 市价单允许的有效期类型(市价单不能挂单)
    allow_fak: bool = True
    allow_fok: bool = True
    max_limit_qty: int | None = None   # 限价单单笔上限, None 不限制 (请按规则表填写)
    max_market_qty: int | None = None  # 市价单单笔上限
    # 隐含单(套利/组合衍生的挂单): 默认全部关闭, 具体机制与比例必须向交易员确认后再填
    implied_mode: str = "none"         # none=不处理 / discount=按比例折算(显示量*(1-ratio))
    implied_depth_ratio: float = 0.0   # 显示深度中隐含单占比: 减少我方排队量, 也减少主动单可吃到的量
    implied_trade_ratio: float = 0.0   # 成交量中来自隐含单、不消耗单腿排队的占比


# 品种(小写) -> 交易所
_EX = {
    "SHFE": "rb hc cu al zn pb ni sn au ag ru bu fu sp ss br ao",
    "INE": "sc lu nr bc ec",
    "DCE": "a b bb c cs eb eg fb i j jd jm l lh m p pg pp rr v y",
    "CZCE": "ta ma sr cf fg rm oi sa ur ap cj pf pk sf sm zc rs wh pm ri lr jr",
    "GFEX": "si lc ps",
}
PRODUCT_EXCHANGE = {p: ex for ex, ps in _EX.items() for p in ps.split()}

# 品种 -> (最小变动价位, 合约乘数)   (请核对)
SPECS = {
    "rb": (1, 10), "hc": (1, 10), "i": (0.5, 100), "j": (0.5, 100), "jm": (0.5, 60),
    "m": (1, 10), "y": (2, 10), "p": (2, 10), "a": (1, 10), "c": (1, 10), "cs": (1, 10),
    "l": (1, 5), "v": (1, 5), "pp": (1, 5), "eg": (1, 10), "eb": (1, 5), "pg": (1, 20),
    "jd": (1, 10), "lh": (5, 16), "cu": (10, 5), "al": (5, 5), "zn": (5, 5), "pb": (5, 5),
    "ni": (10, 1), "sn": (10, 1), "au": (0.02, 1000), "ag": (1, 15), "ru": (5, 10),
    "bu": (2, 10), "fu": (1, 10), "sp": (2, 10), "ss": (5, 5), "sc": (0.1, 1000),
    "lu": (1, 10), "nr": (5, 10), "ta": (2, 5), "ma": (1, 10), "sr": (1, 10), "cf": (5, 5),
    "fg": (1, 20), "rm": (1, 10), "oi": (1, 10), "sa": (1, 20), "ur": (1, 20), "ap": (1, 10),
    "pf": (2, 5), "si": (5, 5), "lc": (50, 1),
}

# 交易所规则 (请核对): 上期所/能源中心不支持市价单; 大商所/郑商所/广期所支持市价单(只能 FAK/FOK)
RULES = {
    "SHFE": ExchangeRule("SHFE", allow_market=False),
    "INE": ExchangeRule("INE", allow_market=False),
    "DCE": ExchangeRule("DCE", allow_market=True),
    "CZCE": ExchangeRule("CZCE", allow_market=True),
    "GFEX": ExchangeRule("GFEX", allow_market=True),
}


def product_of(inst: str) -> str:
    m = re.match(r"[A-Za-z]+", inst)
    return m.group(0).lower() if m else inst.lower()


def exchange_of(inst: str) -> str:
    return PRODUCT_EXCHANGE.get(product_of(inst), "DCE")


def get_spec(inst: str, overrides: dict | None = None) -> ContractSpec:
    p = product_of(inst)
    if overrides and inst in overrides: return overrides[inst]
    if overrides and p in overrides: return overrides[p]
    if p not in SPECS:
        raise KeyError(f"未知品种 {p}, 请通过 spec_overrides 提供 ContractSpec")
    t, m = SPECS[p]
    return ContractSpec(t, m)


def get_rule(inst: str, overrides: dict | None = None) -> ExchangeRule:
    ex = exchange_of(inst)
    if overrides and ex in overrides: return overrides[ex]
    return RULES[ex]
