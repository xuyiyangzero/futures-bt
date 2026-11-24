"""策略模板: 交易员只需继承 Strategy, 重写回调, 通过 buy/sell/cancel_order 交互。

接口刻意保持很窄(只有 tick / 订单回报 / 成交回报 / 定时器 + 下单撤单), 方便后续用 pybind11 映射到 C++ 引擎。
"""
from __future__ import annotations
from .core import *


class Strategy:
    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self._eng = None

    # ------------------------- 回调 (按需重写) -------------------------
    def on_init(self): """回测开始(每个合约每个交易日一次)"""
    def on_tick(self, tick: Tick): """行情回调 (已叠加行情延迟)"""
    def on_timer(self, ts: int): """定时器回调, 间隔 SimConfig.timer_ms; 用于每 10ms 报撤单"""
    def on_order(self, order: Order): """订单状态回报: NEW/PARTIAL/FILLED/CANCELLED/REJECTED"""
    def on_trade(self, trade: Trade): """成交回报"""
    def on_finish(self): """回测结束"""

    # ------------------------- 交易接口 -------------------------
    def send_order(self, side: int, price: float, qty: int, otype: str = LIMIT, tif: str = GFD) -> int:
        return self._eng.send_order(side, price, qty, otype, tif)

    def buy(self, price, qty, otype=LIMIT, tif=GFD): return self.send_order(BUY, price, qty, otype, tif)
    def sell(self, price, qty, otype=LIMIT, tif=GFD): return self.send_order(SELL, price, qty, otype, tif)

    def cancel_order(self, oid: int) -> bool:
        return self._eng.cancel(oid)

    def cancel_all(self):
        for oid in list(self._eng.orders):
            self._eng.cancel(oid)

    # ------------------------- 状态查询 -------------------------
    @property
    def now(self) -> int: return self._eng.now
    @property
    def pos(self) -> int: return self._eng.pos
    @property
    def last_tick(self) -> Tick | None: return self._eng.last_tick
    @property
    def spec(self): return self._eng.spec
    @property
    def instrument(self) -> str: return self._eng.instrument
    @property
    def active_orders(self) -> dict: return self._eng.orders   # oid -> Order (策略视角的状态)

    def orders_of(self, side: int) -> list:
        return [o for o in self._eng.orders.values() if o.side == side]
