"""将深市 L2 快照与逐笔成交对账，然后比较期货回测所用的分配规则。"""
from argparse import ArgumentParser
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from itertools import pairwise
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fbt import SimConfig, Tick
from fbt.validation import compare_trade_allocations


def milliseconds(value):
    value = int(value)
    hours, minutes, seconds = value // 10_000_000, value // 100_000 % 100, value // 1000 % 100
    if not (0 <= hours < 24 and minutes < 60 and seconds < 60):
        raise ValueError(f"Invalid HHMMSSmmm time: {value}")
    return (hours * 3600 + minutes * 60 + seconds) * 1000 + value % 1000


def load_table(path):
    return pd.read_excel(path) if Path(path).suffix.lower() == ".xlsx" else pd.read_csv(path)


def cents(amount):
    scaled = Decimal(str(amount)) * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Expected an amount in whole cents: {amount}")
    return int(scaled)


def stock_tick(row, depth, price_scale):
    def side(prefix):
        prices = str(getattr(row, prefix + "_prices")).strip("[]").replace(",", " ").split()
        volumes = str(getattr(row, prefix + "_volumes")).strip("[]").replace(",", " ").split()
        levels = [(int(p) / price_scale, int(v)) for p, v in zip(prices, volumes, strict=True)
                  if int(p) > 0 and int(v) > 0][:depth]
        return [p for p, _ in levels], [v for _, v in levels]

    return Tick(milliseconds(row.time), str(row.securityid), row.last / price_scale,
                row.total_volume_trade, 0, *side("bid"), *side("offer"), turnover=row.total_value_trade)


def cumulative_anchors(snapshots, trades, window_ms):
    """按共同的累计笔数、量、额偏移识别边界。多个同样强的偏移留空。"""
    groups = defaultdict(dict)
    times = [row.ms for row in trades]
    prefix = [(0, 0)]
    for trade in trades:
        quantity, amount = prefix[-1]
        prefix.append((quantity + trade.trade_volume, amount + trade.amount_cents))
    for i, row in enumerate(snapshots):
        left = bisect_left(times, row.ts - window_ms)
        right = bisect_right(times, row.ts + window_ms)
        for end in range(left + 1, right + 1):
            quantity, amount = prefix[end]
            key = (end - row.count, quantity - row.tick.volume, amount - cents(row.tick.turnover))
            groups[key][i] = end
    ranked = sorted(groups.values(), key=len, reverse=True)
    if not ranked or len(ranked[0]) < 2 or (len(ranked) > 1 and len(ranked[0]) == len(ranked[1])):
        return {}
    return ranked[0]


def evaluate(quotes, events, *, depth, price_scale, tick_size, alignment, window_ms):
    rows = []
    if events.empty:
        raise ValueError("No positive-price B/S trades in input")
    modes = ("linear_two", "tilt_depth")
    spec = SimpleNamespace(tick_size=tick_size, multiplier=1)
    base = SimConfig(turnover_includes_multiplier=False, beyond_depth_mode="extend",
                     inside_spread_mode="linear_bid_ask")
    for (symbol, market, date), group in events.groupby(["securityid", "market", "date"]):
        supplied = quotes[(quotes.securityid == symbol) & (quotes.market == market) & (quotes.date == date)]
        continuous = supplied[supplied.trading_phase_code.eq("T0")].sort_values("ms", kind="stable")
        if continuous.ms.duplicated().any():
            raise ValueError(f"Duplicate snapshot time for {symbol} on {date}")
        for session in (continuous[continuous.ms < 43_200_000], continuous[continuous.ms >= 43_200_000]):
            if session.empty:
                continue
            morning = session.ms.iloc[0] < 43_200_000
            subset = group[group.ms.lt(43_200_000).eq(morning)]
            trades = list(subset.sort_values(["ms", "trade_index"], kind="stable").itertuples(index=False))
            if not trades:
                continue
            times = [row.ms for row in trades]
            to_check = session[session.ms.between(times[0], times[-1])]
            snapshots = [SimpleNamespace(ts=row.ms, count=int(row.num_trades), tick=stock_tick(row, depth, price_scale))
                         for row in to_check.itertuples(index=False)]
            bounds = ({i: bisect_right(times, row.ts) for i, row in enumerate(snapshots)} if alignment == "clock"
                      else cumulative_anchors(snapshots, trades, window_ms))
            for i, (prev, curr) in enumerate(pairwise(snapshots)):
                row = dict(securityid=symbol, market=market, date=date, start_ms=prev.ts, end_ms=curr.ts,
                           depth=depth, alignment=alignment, snapshot_count=curr.count-prev.count,
                           snapshot_volume=curr.tick.volume-prev.tick.volume,
                           snapshot_amount=curr.tick.turnover-prev.tick.turnover,
                           start_boundary_ms=times[bounds[i]-1] if bounds.get(i, 0) else None,
                           end_boundary_ms=times[bounds[i+1]-1] if bounds.get(i+1, 0) else None)
                if i not in bounds or i + 1 not in bounds:
                    rows.append(dict(row, status="missing_boundary"))
                    continue
                first, last = bounds[i], bounds[i+1]
                actual = trades[first:last]
                amount = sum(trade.amount_cents for trade in actual) / 100
                row.update(trade_count=len(actual), trade_volume=sum(trade.trade_volume for trade in actual),
                           trade_amount=amount, amount_diff=amount-row["snapshot_amount"],
                           first_trade_index=actual[0].trade_index if actual else None,
                           last_trade_index=actual[-1].trade_index if actual else None)
                if (len(actual) != row["snapshot_count"] or row["trade_volume"] != row["snapshot_volume"]
                        or abs(row["amount_diff"]) > .010001):
                    rows.append(dict(row, status="unreconciled"))
                    continue
                if not actual:
                    rows.append(dict(row, status="no_trade"))
                    continue
                avg = (curr.tick.turnover - prev.tick.turnover) / row["snapshot_volume"]
                row.update(avg_price=avg, avg_hit=None, all_at_hit=None)
                for side, levels in (("bid1", prev.tick.bid_p), ("ask1", prev.tick.ask_p)):
                    if levels and abs(avg - levels[0]) < tick_size * 1e-6:
                        row["avg_hit"] = side
                        row["all_at_hit"] = all(abs(t.trade_price / price_scale - levels[0]) < tick_size * 1e-6
                                                for t in actual)
                        break
                # 已确认的逐笔边界映射到相应快照区间，分配器仍使用原始起止快照。
                samples = [(curr.ts, t.trade_price / price_scale, t.trade_volume) for t in actual]
                for mode in modes:
                    cfg = replace(base, deep_levels_mode=mode)
                    result, = compare_trade_allocations([prev.tick, curr.tick], samples, cfg, spec,
                                                        amount_tolerance=.010001)
                    rows.append(dict(row, mode=mode, **result))
    return pd.DataFrame(rows)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", required=True)
    parser.add_argument("--trades", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--alignment", choices=("clock", "cumulative"), default="cumulative")
    parser.add_argument("--window-ms", type=int, default=3000)
    parser.add_argument("--depth", type=int, choices=(5, 10), default=5)
    parser.add_argument("--price-scale", type=int, default=10000)
    parser.add_argument("--tick-size", type=float, default=.01)
    args = parser.parse_args()
    if args.window_ms < 0 or args.price_scale <= 0 or not 0 < args.tick_size < float("inf"):
        parser.error("Invalid time window, price scale or tick size")
    quotes, events = load_table(args.snapshots), load_table(args.trades)
    for frame in (quotes, events):
        frame["securityid"] = frame.securityid.astype(str).str.zfill(6)
        frame["ms"] = frame.time.map(milliseconds)
    eligible = events.quote_type.eq("trade") & events.trade_bs_flag.isin(["B", "S"])
    ignored = len(events) - int(eligible.sum())
    events = events[eligible].copy()
    if (events.trade_volume.le(0) | events.trade_price.le(0)).any():
        parser.error("B/S trades must have positive price and quantity")
    if events.duplicated(["market", "date", "trade_order_channel", "trade_index"]).any():
        parser.error("Duplicate trade sequence within channel")
    events["amount_cents"] = [cents(Decimal(str(p)) * Decimal(str(v)) / args.price_scale)
                              for p, v in zip(events.trade_price, events.trade_volume)]
    results = evaluate(quotes, events, depth=args.depth, price_scale=args.price_scale,
                       tick_size=args.tick_size, alignment=args.alignment, window_ms=args.window_ms)
    if results.empty:
        parser.error("No overlapping complete snapshot intervals")
    states = results.drop_duplicates(["securityid", "market", "date", "start_ms", "end_ms"])
    print(f"Trades: {len(events)}, excluded events: {ignored}")
    print(states.status.value_counts().to_string())
    matched = results[results.status.eq("matched")]
    if not matched.empty:
        for mode, group in matched.groupby("mode"):
            error = group.price_volume_l1.sum()
            print(f"{mode}: per-price L1={error:.2f}, L1 / actual volume={error/group.trade_volume.sum():.2%}")
        first = matched.drop_duplicates(["securityid", "market", "date", "start_ms", "end_ms"])
        if "avg_hit" in first:
            hits = first[first.avg_hit.notna()]
            print(f"Exact L1 average hits: {len(hits)}, all trades at that price: {hits.all_at_hit.sum()}")
    for column in ("actual_by_tick", "predicted_by_tick"):
        if column in results:
            results[column] = results[column].map(lambda value: json.dumps(value) if isinstance(value, dict) else "")
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(output, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
