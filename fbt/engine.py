"""单合约单交易日的事件驱动回测引擎。

事件: 交易所tick(直接按序推进) + 堆里的 [订单/撤单到达交易所, 策略收到行情, 回报到达策略, 定时器]。
同一时刻的先后顺序: 交易所tick < 订单到达 < 策略收行情 < 回报 < 定时器。
"""
from __future__ import annotations
import heapq, math
from .core import *
from .matching import SimExchange
from .contracts import ContractSpec, ExchangeRule

K_ARRIVE, K_STRAT_TICK, K_REPORT, K_TIMER = 1, 2, 3, 4


class Engine:
    def __init__(self, strategy, ticks: list, spec: ContractSpec, rule: ExchangeRule,
                 cfg: SimConfig, trading_day: str = ""):
        self.strategy, self.ticks, self.spec, self.rule, self.cfg = strategy, ticks, spec, rule, cfg
        self.instrument = ticks[0].instrument
        self.trading_day = trading_day
        self.ex = SimExchange(spec, rule, cfg, self._ex_emit)
        self.heap, self._seq = [], 0
        self.now, self.pos = 0, 0
        self.orders: dict[int, Order] = {}     # 策略视角的存活订单
        self._oid = 1
        self._cancel_pending: set = set()
        self.last_tick: Tick | None = None
        self._i = 0
        self.equity_curve: list = []
        self.n_cancel_sent = 0

    # ---------------- 策略 -> 引擎 ----------------
    def send_order(self, side, price, qty, otype=LIMIT, tif=GFD) -> int:
        oid = self._oid; self._oid += 1
        args = (oid, self.instrument, side, float(price), int(qty), otype, tif)
        self.orders[oid] = Order(*args, send_ts=self.now)
        self._push(self.now + self.cfg.submit_latency_ms, K_ARRIVE, ("new", Order(*args, send_ts=self.now)))
        return oid

    def cancel(self, oid: int) -> bool:
        if oid not in self.orders or oid in self._cancel_pending:
            return False
        self._cancel_pending.add(oid)
        self.n_cancel_sent += 1
        self._push(self.now + self.cfg.cancel_latency_ms, K_ARRIVE, ("cancel", oid))
        return True

    # ---------------- 内部 ----------------
    def _push(self, t, kind, payload):
        self._seq += 1
        heapq.heappush(self.heap, (t, kind, self._seq, payload))

    def _ex_emit(self, ts, payload):
        self._push(ts + self.cfg.report_latency_ms, K_REPORT, payload)

    def _on_report(self, p):
        st = self.strategy
        if p[0] == "trade":
            t: Trade = p[1]
            self.pos += t.side * t.qty
            st.on_trade(t)
        elif p[0] == "order":
            _, oid, status, filled, avg, reason = p
            o = self.orders.get(oid)
            if o is None: return
            o.status, o.filled, o.avg_price, o.reason = status, filled, avg, reason
            if not o.active:
                self.orders.pop(oid, None); self._cancel_pending.discard(oid)
            st.on_order(o)
        else:  # cancel_reject
            _, oid, reason = p
            self._cancel_pending.discard(oid)
            o = self.orders.get(oid)
            if o is not None:
                o.reason = reason
                st.on_order(o)

    def _timer(self, t):
        cfg = self.cfg
        if self.last_tick is None: return
        if t - self.last_tick.ts > cfg.session_gap_ms:       # 休市: 跳到下一个tick再恢复
            if self._i < len(self.ticks):
                self._push(self.ticks[self._i].ts + cfg.md_latency_ms, K_TIMER, None)
            return
        self.strategy.on_timer(t)
        nxt = t + cfg.timer_ms
        if nxt <= self.ticks[-1].ts + cfg.md_latency_ms + cfg.timer_ms:
            self._push(nxt, K_TIMER, None)

    # ---------------- 主循环 ----------------
    def run(self) -> dict:
        ticks, n, heap, cfg = self.ticks, len(self.ticks), self.heap, self.cfg
        INF = float("inf")
        self.strategy._eng = self
        self.strategy.on_init()
        timer_started, last_sample = cfg.timer_ms <= 0, -INF
        while True:
            tt = ticks[self._i].ts if self._i < n else INF
            te = heap[0][0] if heap else INF
            if tt == INF and te == INF: break
            if tt <= te:
                tk = ticks[self._i]; self._i += 1
                self.now = tk.ts
                self.ex.on_tick(tk)
                if tk.ts - last_sample >= cfg.equity_sample_ms:
                    last_sample = tk.ts
                    self.equity_curve.append((tk.ts, self._equity(tk.mid)))
                self._push(tk.ts + cfg.md_latency_ms, K_STRAT_TICK, tk)
                if not timer_started:
                    timer_started = True
                    self._push(tk.ts + cfg.md_latency_ms, K_TIMER, None)
                continue
            t, kind, _, p = heapq.heappop(heap)
            self.now = t
            if kind == K_ARRIVE:
                if p[0] == "new": self.ex.on_new_order(p[1], t)
                else: self.ex.on_cancel(p[1], t)
            elif kind == K_STRAT_TICK:
                self.last_tick = p
                self.strategy.on_tick(p)
            elif kind == K_REPORT:
                self._on_report(p)
            else:
                self._timer(t)
        # 收盘: 撤掉所有挂单, 再把在途回报处理完
        self.ex.cancel_all(self.now)
        while heap:
            t, kind, _, p = heapq.heappop(heap)
            self.now = t
            if kind == K_REPORT: self._on_report(p)
        self.strategy.on_finish()
        return self._summary()

    def _equity(self, mark: float) -> float:
        return self.ex.cash + self.ex.pos * mark * self.spec.multiplier - self.ex.fees

    def _summary(self) -> dict:
        ex, mark = self.ex, self.ticks[-1].mid
        gross = ex.cash + ex.pos * mark * self.spec.multiplier
        return dict(
            trading_day=self.trading_day, instrument=self.instrument, n_ticks=len(self.ticks),
            n_orders=ex.n_orders, n_rejected=ex.n_rejected, n_cancels=self.n_cancel_sent,
            n_trades=len(ex.trades), volume=ex.volume,
            maker_ratio=(ex.maker_volume / ex.volume) if ex.volume else 0.0,
            final_pos=ex.pos, max_abs_pos=ex.max_abs_pos,
            pnl_gross=gross, fees=ex.fees, pnl_net=gross - ex.fees,
            avg_from_turnover=ex.n_avg_turnover, avg_fallback_last=ex.n_avg_fallback,
            **{f"case_{k}": ex.case_count.get(k, 0)
               for k in ("exact", "inside_spread", "between_l1_l2", "deep", "beyond_depth", "degraded", "no_book", "legacy")},
            dir_flags=ex.n_dir_flag, n_alloc=ex.n_alloc,
            alloc_err_mean_ticks=(ex.alloc_err_sum / ex.n_alloc) if ex.n_alloc else 0.0, alloc_err_max_ticks=ex.alloc_err_max,
            alloc_abs_amount_err=ex.alloc_amount_err,
            aggr_buy_vol=ex.aggr["buy"], aggr_sell_vol=ex.aggr["sell"], aggr_mid_vol=ex.aggr["mid"])
