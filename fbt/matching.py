"""仅用 Tick 的撮合模拟器 (单合约)。

核心假设 (全部可在 SimConfig 里调, 详见 说明文档):
 1. 两个 tick 之间盘口保持上一个 tick 的快照不变, 订单到达交易所时看到的是最近一个 tick 的盘口。
 2. 主动单(可成交价)按快照档位吃单, 同一快照内自己吃过的量会被扣除。
 3. 被动单挂入队列, 前方排队量 = queue_ratio * 显示量 * (1-implied_depth_ratio) (+ 自己更早的同价挂单)。
 4. 被动单的成交来自下一个 tick: 先用 成交额增量/成交量增量 算出区间成交均价, 再由 allocator 把区间成交量分配到各价位:
      - 我方价位 比 成交价更优(买单: 成交价 < 我方价)  -> 穿价, 按 through_fill_ratio 成交;
      - 我方价位 == 某个有成交量的价位           -> 该价位的成交量先消耗前方排队, 再轮到我方;
      - 盘口该价位量减少(撤单)按 cancel_model / cancel_ahead_ratio 推进队列。
"""
from __future__ import annotations
import math, random
from .core import *
from .contracts import ContractSpec, ExchangeRule
from .allocator import trade_avg_price, allocate, direction_contradiction, degrade_point
from .queue_flow import arrival_queue, cancel_ahead, insert_ahead


class SimExchange:
    def __init__(self, spec: ContractSpec, rule: ExchangeRule, cfg: SimConfig, emit):
        if (cfg.queue_latency_ref_ms < 0 or not math.isfinite(cfg.queue_latency_insert_lots)
                or cfg.queue_latency_insert_lots < 0 or not 0 <= cfg.queue_latency_cancel_fraction <= 1):
            raise ValueError("Invalid queue latency parameters")
        self.spec, self.rule, self.cfg, self.emit = spec, rule, cfg, emit
        self.book: Tick | None = None
        self.rest = {BUY: {}, SELL: {}}      # side -> price -> [Order] (时间优先)
        self.orders: dict[int, Order] = {}   # 交易所侧存活订单
        self.consumed: dict = {}             # (side, price) -> 当前快照内被我方吃掉的量
        self.pos = 0
        self.cash = 0.0
        self.fees = 0.0
        self.volume = 0
        self.maker_volume = 0
        self.max_abs_pos = 0
        self.n_orders = 0
        self.n_rejected = 0
        self.trades: list[Trade] = []
        self._alloc = []
        self._legacy_budget = (0, math.nan, 0.0)
        self._mode_used = "alloc"
        rd = rule.implied_depth_ratio if rule.implied_mode != "none" else 0.0
        rt = rule.implied_trade_ratio if rule.implied_mode != "none" else 0.0
        self.imp_depth = 1 - (1 - rd) * (1 - cfg.implied_depth_ratio)     # 交易所级 与 全局 叠加
        self.imp_trade = 1 - (1 - rt) * (1 - cfg.implied_trade_ratio)
        self.rng = random.Random(cfg.queue_seed)
        self.case_count: dict = {}
        self.n_avg_turnover = 0
        self.n_avg_fallback = 0
        self.tick_idx = 0                    # 已处理的 Tick 序号(1开始), 用于 frame_delay
        self.n_dir_flag = 0                  # 方向矛盾帧数
        self.n_alloc = 0                     # 做过成交价位分配的区间数
        self.alloc_err_sum = 0.0             # 分配后加权均价与目标均价的偏差(tick)累计/最大
        self.alloc_err_max = 0.0
        self.alloc_amount_err = 0.0          # 分配金额与 均价*成交量 的绝对偏差累计(元)
        self.aggr = {"buy": 0.0, "sell": 0.0, "mid": 0.0}   # 主动买(打到卖盘)/主动卖(打到买盘)/买卖价差内 的成交量

    # ------------------------------------------------------------------ 行情
    def _elig(self, o: Order) -> bool:
        return o.eligible_idx <= self.tick_idx

    def on_tick(self, tick: Tick):
        prev, self.book = self.book, tick
        self.tick_idx += 1
        self.consumed.clear()
        self._alloc = []
        self._legacy_budget = (0, math.nan, 0.0)
        cfg = self.cfg
        if prev is None:
            return
        has_rest = bool(self.rest[BUY] or self.rest[SELL])
        if not has_rest and not cfg.diag_all_ticks:
            return
        if has_rest and cfg.queue_insert_lots > 0:                 # ξ: 每个区间固定有这么多手新单插到前面
            for side in (BUY, SELL):
                for lst in self.rest[side].values():
                    for o in lst:
                        if self._elig(o): o.queue_ahead += cfg.queue_insert_lots
        dv = tick.volume - prev.volume
        reset = dv < 0
        if reset:
            dv = tick.volume
        if dv > 0:
            dv_eff = dv * (1.0 - self.imp_trade)
            m = cfg.match_mode
            has_tn = tick.turnover == tick.turnover and (reset or prev.turnover == prev.turnover)
            self._mode_used = "legacy" if (m == "legacy" or (m == "auto" and not has_tn)) else "alloc"
            if self._mode_used == "legacy":
                self.case_count["legacy"] = self.case_count.get("legacy", 0) + 1
                if has_rest: self._on_trades_legacy(prev, tick, dv_eff)
            else:
                avg, used_turnover = trade_avg_price(prev, tick, dv, reset, self.spec, cfg)
                self.n_avg_turnover += int(used_turnover); self.n_avg_fallback += int(not used_turnover)
                alloc, case = allocate(prev, tick, dv_eff, avg, cfg, self.spec)
                if cfg.direction_check != "off" and direction_contradiction(prev, tick, avg):
                    self.n_dir_flag += 1
                    if cfg.direction_check == "degrade":
                        alloc, case = degrade_point(prev, tick, dv_eff, cfg)
                self._alloc = alloc
                self.case_count[case] = self.case_count.get(case, 0) + 1
                self._diagnostics(alloc, prev, avg, dv_eff)
                if has_rest: self._on_trades(prev, tick)
        if has_rest:
            self._update_queues(prev, tick)
            if cfg.queue_cancel_rate > 0:                          # δ: 每个区间前方排队量固定比例被撤
                for side in (BUY, SELL):
                    for lst in self.rest[side].values():
                        own = 0.0
                        for o in lst:
                            if self._elig(o):
                                ext = max(0.0, o.queue_ahead - own)
                                o.queue_ahead = ext * (1.0 - cfg.queue_cancel_rate) + own
                            own += o.remaining
            if cfg.frame_delay > 0:                                # 延后一帧的订单: 下个区间开始参与, 按当前帧盘口重新定位队列
                for side in (BUY, SELL):
                    for lst in self.rest[side].values():
                        own = 0.0
                        for o in lst:
                            if o.eligible_idx == self.tick_idx + 1:
                                o.queue_ahead = self._queue_for(o) + own
                            own += o.remaining

    def _diagnostics(self, alloc, prev: Tick, avg: float, dv_eff: float):
        tot = sum(v for _, v in alloc)
        if tot <= 1e-12:
            return
        amt = sum(p * v for p, v in alloc)
        err = abs(amt / tot - avg) / self.spec.tick_size
        self.n_alloc += 1
        self.alloc_err_sum += err
        self.alloc_err_max = max(self.alloc_err_max, err)
        self.alloc_amount_err += abs(amt - avg * dv_eff) * self.spec.multiplier
        b1 = prev.bid_p[0] if prev.bid_p else None
        a1 = prev.ask_p[0] if prev.ask_p else None
        eps = 1e-6 * self.spec.tick_size
        for p, v in alloc:
            if a1 is not None and p >= a1 - eps: self.aggr["buy"] += v
            elif b1 is not None and p <= b1 + eps: self.aggr["sell"] += v
            else: self.aggr["mid"] += v

    # ---- 旧路径(保留): 用 last 与盘口判断主动方向, 以 (成交量 - 更优价位被吃掉的量) 为预算 ----
    @staticmethod
    def _aggressor(prev: Tick, tick: Tick) -> int:
        lp = tick.last
        if prev.ask_p and lp >= prev.ask_p[0]: return BUY
        if prev.bid_p and lp <= prev.bid_p[0]: return SELL
        if lp > prev.last: return BUY
        if lp < prev.last: return SELL
        return BUY if lp >= tick.mid else SELL

    def _on_trades_legacy(self, prev: Tick, tick: Tick, dv: float):
        lp, keep = tick.last, 1.0 - self.imp_depth
        if self._aggressor(prev, tick) == BUY:
            side = SELL
            better = sum(v * keep for p, v in zip(prev.ask_p, prev.ask_v) if p < lp)
        else:
            side = BUY
            better = sum(v * keep for p, v in zip(prev.bid_p, prev.bid_v) if p > lp)
        budget = max(0.0, dv - better)
        self._legacy_budget = (side, lp, budget)
        book = self.rest[side]
        for px in list(book.keys()):
            lst = book[px]
            through = (px < lp) if side == SELL else (px > lp)
            if through:                      # 旧逻辑: 穿价一律全部成交
                for o in list(lst):
                    if self._elig(o): self._maker_fill(o, o.remaining, px, tick.ts)
            elif px == lp:
                for o in list(lst):
                    if not self._elig(o): continue
                    a = o.queue_ahead
                    f = int(min(o.remaining, max(0.0, budget - a)))
                    o.queue_ahead = max(0.0, a - budget)
                    if f > 0:
                        self._maker_fill(o, f, px, tick.ts)

    def _traded_at(self, side: int, px: float, old_vol: float) -> float:
        if self._mode_used == "legacy":
            bside, blp, budget = self._legacy_budget
            return min(budget, old_vol) if (bside == side and px == blp) else 0.0
        return min(self._vols(side, px)[1], old_vol)

    def _vols(self, side: int, px: float):
        """成交分配结果中: (比我方价位更优的成交量[穿价], 恰好在我方价位的成交量)。"""
        eps = 1e-6 * self.spec.tick_size
        thr = at = 0.0
        for p, v in self._alloc:
            if abs(p - px) <= eps: at += v
            elif (p < px) if side == BUY else (p > px): thr += v
        return thr, at

    def _on_trades(self, prev: Tick, tick: Tick):
        cfg = self.cfg
        span = max(1, tick.ts - prev.ts)
        for side in (BUY, SELL):
            book = self.rest[side]
            for px in list(book.keys()):
                lst = book[px]
                thr, at = self._vols(side, px)
                if thr > 0:                          # 成交价已穿过我方价位 -> 我方在队列最前, 按比例成交(按我方挂单价)
                    for o in list(lst):
                        if not self._elig(o): continue
                        f = int(o.remaining * cfg.through_fill_ratio + 1e-9)
                        o.queue_ahead = 0.0
                        if f > 0:
                            self._maker_fill(o, f, px, tick.ts)
                    continue                         # 该价位已按穿价处理, 不再重复计入"价位内成交量"
                if at > 0:
                    for o in list(lst):
                        if o.status == FILLED or not self._elig(o): continue
                        frac = 1.0
                        if cfg.partial_interval_mode == "pro_rata":     # 订单中途到达: 到达前的成交不算
                            frac = min(1.0, max(0.0, (tick.ts - max(o.arrive_ts, prev.ts)) / span))
                        budget = at * frac
                        a = o.queue_ahead
                        f = int(min(o.remaining, max(0.0, budget - a)))
                        o.queue_ahead = max(0.0, a - budget)
                        if f > 0:
                            self._maker_fill(o, f, px, tick.ts)

    def _update_queues(self, prev: Tick, tick: Tick):
        """盘口显示量减少 => 有人撤单 => 推进队列 (扣掉成交消耗后, 剩余减少部分视为撤单)。"""
        cfg = self.cfg
        if cfg.cancel_model == "none" and cfg.queue_insert_ratio <= 0:
            return
        for side in (BUY, SELL):
            if not self.rest[side]:
                continue
            if side == BUY:
                old = dict(zip(prev.bid_p, prev.bid_v)); new = dict(zip(tick.bid_p, tick.bid_v))
            else:
                old = dict(zip(prev.ask_p, prev.ask_v)); new = dict(zip(tick.ask_p, tick.ask_v))
            for px, lst in self.rest[side].items():
                if px not in old or px not in new:
                    continue
                traded = self._traded_at(side, px, old[px])
                rem_old = old[px] - traded
                if rem_old <= 0 and not cfg.queue_latency_ref_ms:
                    continue
                if new[px] > rem_old:                                   # 显示量净增加 => 有新单进来
                    if cfg.queue_insert_ratio > 0:
                        for o in lst:
                            if self._elig(o):
                                o.queue_ahead += insert_ahead(new[px] - rem_old, cfg.queue_insert_ratio,
                                                              self._queue_update_fraction(o, prev, tick))
                    continue
                if cfg.cancel_model == "none" or rem_old <= 0:
                    continue
                own = 0.0
                for o in lst:
                    if self._elig(o):
                        ext = max(0.0, o.queue_ahead - own)
                        ext = cancel_ahead(ext, rem_old, new[px], cfg.cancel_model, cfg.cancel_ahead_ratio,
                                           self._queue_update_fraction(o, prev, tick))
                        o.queue_ahead = ext + own
                    own += o.remaining

    def _queue_update_fraction(self, order, prev, tick):
        if not self.cfg.queue_latency_ref_ms or order.arrive_ts <= prev.ts:
            return 1.0
        return max(0.0, (tick.ts - order.arrive_ts) / max(1, tick.ts - prev.ts))

    # ------------------------------------------------------------------ 订单
    def _validate(self, o: Order) -> str:
        r, bk = self.rule, self.book
        if bk is None: return "NO_MARKET_DATA"
        if o.qty <= 0: return "BAD_QTY"
        if o.otype == LIMIT:
            t = self.spec.tick_size
            if abs(round(o.price / t) * t - o.price) > 1e-6 * t: return "PRICE_NOT_ON_TICK"
            if not math.isnan(bk.upper) and o.price > bk.upper + 1e-9: return "ABOVE_UPPER_LIMIT"
            if not math.isnan(bk.lower) and o.price < bk.lower - 1e-9: return "BELOW_LOWER_LIMIT"
            if r.max_limit_qty and o.qty > r.max_limit_qty: return "QTY_EXCEEDS_LIMIT_MAX"
        elif o.otype == MARKET:
            if not r.allow_market: return "MARKET_ORDER_NOT_SUPPORTED"
            if o.tif not in r.market_tifs: return "MARKET_TIF_NOT_SUPPORTED"
            if r.max_market_qty and o.qty > r.max_market_qty: return "QTY_EXCEEDS_MARKET_MAX"
        else:
            return "BAD_ORDER_TYPE"
        if o.tif == FAK and not r.allow_fak: return "FAK_NOT_SUPPORTED"
        if o.tif == FOK and not r.allow_fok: return "FOK_NOT_SUPPORTED"
        if o.tif not in (GFD, FAK, FOK): return "BAD_TIF"
        return ""

    def on_new_order(self, o: Order, ts: int):
        self.n_orders += 1
        reason = self._validate(o)
        if reason:
            o.status, o.reason = REJECTED, reason
            self.n_rejected += 1
            self._report_order(o, ts)
            return
        bk = self.book
        if o.side == BUY:
            prices, vols, opp = bk.ask_p, bk.ask_v, SELL
            ok = lambda p: o.otype == MARKET or p <= o.price
        else:
            prices, vols, opp = bk.bid_p, bk.bid_v, BUY
            ok = lambda p: o.otype == MARKET or p >= o.price
        levels = []
        for p, v in zip(prices, vols):
            if not ok(p): break
            a = int(v * (1.0 - self.imp_depth) + 1e-9) - self.consumed.get((opp, p), 0)   # 隐含单不可吃
            if a > 0: levels.append((p, a))
        if o.tif == FOK and sum(a for _, a in levels) < o.qty:
            o.status, o.reason = CANCELLED, "FOK_NOT_FILLED"
            self._report_order(o, ts)
            return
        for p, a in levels:
            if o.remaining <= 0: break
            q = int(min(a, o.remaining))
            self.consumed[(opp, p)] = self.consumed.get((opp, p), 0) + q
            self._fill(o, q, p, ts, maker=False)
        if o.remaining > 0:
            if o.otype == LIMIT and o.tif == GFD:
                if o.filled == 0: o.status = NEW
                o.arrive_ts = ts
                self._rest(o)
            else:
                o.status = CANCELLED
                o.reason = "NO_LIQUIDITY" if o.filled == 0 else "REMAINDER_CANCELLED"
        self._report_order(o, ts)

    def _queue_for(self, o: Order) -> float:
        """订单在当前盘口价位上的初始前方排队量(不含自己更早的挂单)。"""
        bk, px = self.book, o.price
        if o.side == BUY: prices, vols = bk.bid_p, bk.bid_v
        else: prices, vols = bk.ask_p, bk.ask_v
        vis = 0.0
        if prices:
            better = (px > prices[0]) if o.side == BUY else (px < prices[0])
            worse = (px < prices[-1]) if o.side == BUY else (px > prices[-1])
            if better: vis = 0.0
            elif worse: vis = vols[-1]
            else: vis = dict(zip(prices, vols)).get(px, 0.0)
        vis *= (1.0 - self.imp_depth)
        cfg, m = self.cfg, self.cfg.queue_pos_mode
        ratio = {"tail": 1.0, "head": 0.0, "mid": 0.5}.get(m)
        if ratio is None:
            ratio = self.rng.uniform(cfg.queue_random_low, cfg.queue_random_high) if m == "random" else cfg.queue_ratio
        return ratio * vis

    def _rest(self, o: Order):
        lst = self.rest[o.side].setdefault(o.price, [])
        ahead = self._queue_for(o)
        cfg = self.cfg
        if cfg.queue_latency_ref_ms:
            elapsed = o.arrive_ts - max(o.send_ts, self.book.ts)
            ahead = arrival_queue(ahead, elapsed, cfg.queue_latency_ref_ms,
                                  cfg.queue_latency_insert_lots, cfg.queue_latency_cancel_fraction)
        o.queue_ahead = ahead + sum(x.remaining for x in lst)
        o.eligible_idx = self.tick_idx + 1 + self.cfg.frame_delay
        lst.append(o)
        self.orders[o.oid] = o

    def on_cancel(self, oid: int, ts: int):
        o = self.orders.get(oid)
        if o is None:
            self.emit(ts, ("cancel_reject", oid, "ORDER_NOT_FOUND_OR_DONE"))
            return
        self._remove(o)
        o.status, o.reason = CANCELLED, "USER_CANCEL"
        self._report_order(o, ts)

    def _remove(self, o: Order):
        lst = self.rest[o.side].get(o.price, [])
        if o in lst:
            idx = lst.index(o)
            for x in lst[idx + 1:]:                      # 自己撤掉后, 后面自己的单排队前移
                x.queue_ahead = max(0.0, x.queue_ahead - o.remaining)
            lst.remove(o)
            if not lst: del self.rest[o.side][o.price]
        self.orders.pop(o.oid, None)

    def cancel_all(self, ts: int):
        for o in list(self.orders.values()):
            self._remove(o); o.status, o.reason = CANCELLED, "EOD"
            self._report_order(o, ts)

    # ------------------------------------------------------------------ 成交
    def _maker_fill(self, o: Order, qty: int, price: float, ts: int):
        self._fill(o, qty, price, ts, maker=True)
        if o.remaining <= 0:
            self._remove(o)
        self._report_order(o, ts)

    def _fill(self, o: Order, qty: int, price: float, ts: int, maker: bool):
        if qty <= 0: return
        fee = qty * self.spec.fee_per_lot + price * qty * self.spec.multiplier * self.spec.fee_rate
        fee += qty * self.cfg.fee_per_lot + price * qty * self.spec.multiplier * self.cfg.fee_rate
        o.avg_price = (o.avg_price * o.filled + price * qty) / (o.filled + qty)
        o.filled += qty
        o.status = FILLED if o.remaining <= 0 else PARTIAL
        self.pos += o.side * qty
        self.cash -= o.side * price * qty * self.spec.multiplier
        self.fees += fee
        self.volume += qty
        if maker: self.maker_volume += qty
        self.max_abs_pos = max(self.max_abs_pos, abs(self.pos))
        t = Trade(o.oid, o.instrument, ts, o.side, price, qty, maker, fee)
        self.trades.append(t)
        self.emit(ts, ("trade", t))

    def _report_order(self, o: Order, ts: int):
        self.emit(ts, ("order", o.oid, o.status, o.filled, o.avg_price, o.reason))
        if o.status in (FILLED, CANCELLED, REJECTED):
            self.orders.pop(o.oid, None)
