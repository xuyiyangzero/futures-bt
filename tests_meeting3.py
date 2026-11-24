"""第三次会议的队列与逐笔对账回归测试。"""
from dataclasses import replace
import math

from fbt import BUY, Order, SimConfig, Tick
from fbt.contracts import get_rule, get_spec
from fbt.matching import SimExchange
from fbt.validation import compare_trade_allocations


spec, rule = get_spec("rb2601"), get_rule("rb2601")


def snapshot(ts, depth, volume=0, turnover=0):
    return Tick(ts, "rb2601", 100, volume, 0, [100], [depth], [101], [100], turnover=turnover)


def resting_order(cfg, newer_snapshot=False):
    exchange = SimExchange(spec, rule, cfg, lambda *args: None)
    exchange.on_tick(snapshot(0, 100))
    if newer_snapshot:
        exchange.on_tick(snapshot(25, 100))
    order = Order(1, "rb2601", BUY, 100, 1, send_ts=0)
    exchange.on_new_order(order, 50)
    return exchange, order


cfg = SimConfig(queue_latency_ref_ms=250, queue_latency_insert_lots=1000, queue_insert_ratio=1)
ex, order = resting_order(cfg)
assert order.queue_ahead == 300
ex.on_tick(snapshot(250, 200))
assert order.queue_ahead == 380
_, order = resting_order(cfg, newer_snapshot=True)
assert order.queue_ahead == 200
ex, order = resting_order(replace(cfg, queue_latency_cancel_fraction=0.5))
assert order.queue_ahead == 290
ex.on_tick(snapshot(250, 50))
assert math.isclose(order.queue_ahead, 174)
_, order = resting_order(SimConfig())
assert order.queue_ahead == 100

ticks = [snapshot(0, 100), snapshot(250, 100, 2, 2010), snapshot(500, 100, 4, 4020)]
trades = [(0, 110, 100), (100, 100, 1), (250, 101, 1), (500, 100, 1)]
matched, unmatched = compare_trade_allocations(ticks, trades, SimConfig(), spec)
assert matched["status"] == "matched" and matched["price_volume_l1"] == 0
assert matched["actual_by_tick"] == {100: 1, 101: 1}
assert unmatched["status"] == "unreconciled" and unmatched["volume_diff"] == -1

# 同样的量额可以来自不同的逐价位分布。精确命中买一也需要逐笔验证。
ticks = [snapshot(0, 100), snapshot(250, 100, 2, 2000)]
trades = [(100, 99, 1), (250, 101, 1)]
row, = compare_trade_allocations(ticks, trades, SimConfig(), spec)
assert row["status"] == "matched" and row["case"] == "exact"
assert row["actual_price_levels"] == 2 and row["price_volume_l1"] == 4

row, = compare_trade_allocations(ticks, [(250, 101, 2)], SimConfig(), spec)
assert row["status"] == "unreconciled" and row["volume_diff"] == 0
assert row["amount_diff"] == 2
print("all meeting 3 tests passed")
