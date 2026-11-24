"""用逐笔成交核对快照分配。传入同一证券、同一交易日的数据。"""
from collections import Counter
from itertools import pairwise
import math

from .allocator import allocate, degrade_point, direction_contradiction, trade_avg_price


def compare_trade_allocations(ticks, trades, cfg, spec, *, amount_tolerance=0.01):
    """逐区间返回预测量、对账状态和逐价位误差。

    ticks 使用标准 Tick；trades 是 (交易所毫秒时间戳, 价格, 数量)。
    成交按 (起点, 终点] 归属。调用方须确认同毫秒事件的排序口径。
    数量或金额对不上时保留对账差额，跳过该区间的模型精度评价。
    """
    ticks = list(ticks)
    if any(prev.ts >= tick.ts or prev.instrument != tick.instrument for prev, tick in pairwise(ticks)):
        raise ValueError("Snapshots must have strictly increasing timestamps and one instrument")
    if not math.isfinite(amount_tolerance) or amount_tolerance < 0:
        raise ValueError("Invalid amount tolerance")
    trades = sorted(trades, key=lambda trade: trade[0])
    if any(not math.isfinite(p) or not math.isfinite(v) or p <= 0 or v <= 0 for _, p, v in trades):
        raise ValueError("Trades must have positive finite prices and quantities")
    trade_iter = iter(trades)
    current = next(trade_iter, None)
    multiplier = spec.multiplier if cfg.turnover_includes_multiplier else 1.0
    amount_scale = multiplier * cfg.turnover_scale
    if not math.isfinite(amount_scale) or amount_scale <= 0 or spec.tick_size <= 0:
        raise ValueError("Invalid price tick or turnover scale")
    for prev, tick in pairwise(ticks):
        actual = Counter()
        actual_amount = 0.0
        while current is not None and current[0] <= tick.ts:
            ts, price, volume = current
            if ts > prev.ts:
                actual[round(price / spec.tick_size)] += volume
                actual_amount += price * volume
            current = next(trade_iter, None)
        dv = tick.volume - prev.volume
        if dv == 0 and not actual and tick.turnover == prev.turnover:
            continue
        amount = (tick.turnover - prev.turnover) / amount_scale
        volume_diff = sum(actual.values()) - dv
        amount_diff = actual_amount - amount
        row = dict(start_ts=prev.ts, end_ts=tick.ts, volume_diff=volume_diff, amount_diff=amount_diff)
        if dv <= 0 or abs(volume_diff) > 1e-9 or not math.isfinite(amount_diff) or abs(amount_diff) > amount_tolerance:
            yield dict(row, status="unreconciled")
            continue
        avg, used_turnover = trade_avg_price(prev, tick, dv, False, spec, cfg)
        alloc, case = allocate(prev, tick, dv, avg, cfg, spec)
        if cfg.direction_check == "degrade" and direction_contradiction(prev, tick, avg):
            alloc, case = degrade_point(prev, tick, dv, cfg)
        predicted = Counter()
        off_grid_volume = 0.0
        for price, volume in alloc:
            key = round(price / spec.tick_size)
            if math.isclose(price, key * spec.tick_size, rel_tol=0, abs_tol=spec.tick_size * 1e-6):
                predicted[key] += volume
            else:
                off_grid_volume += volume
        error = sum(abs(predicted[k] - actual[k]) for k in predicted.keys() | actual.keys())
        yield dict(row, status="matched", case=case, used_turnover=used_turnover,
                   actual_by_tick=dict(actual), predicted_by_tick=dict(predicted),
                   actual_price_levels=len(actual), off_grid_volume=off_grid_volume,
                   price_volume_l1=error + off_grid_volume)
