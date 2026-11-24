"""示例策略: 挂在买一/卖一排队吃价差, 价格没走远就不撤单(保住排队位置), 带库存限制。
交易员照这个模板写自己的策略: 只需要重写 on_timer / on_tick / on_order / on_trade。
参数: size 每次挂单手数; max_pos 最大持仓(手); requote_ticks 我方价格落后盘口几个tick才撤单重挂;
      min_spread_ticks 盘口价差小于该值时不报价; inv_skew_pos 持仓超过该值后, 只在盘口的反方向报价(降库存)
"""
from fbt import Strategy, BUY, SELL


class SimpleMM(Strategy):
    def on_init(self):
        p = self.params
        self.size = p.get("size", 1)
        self.max_pos = p.get("max_pos", 5)
        self.requote = p.get("requote_ticks", 1)
        self.min_spread = p.get("min_spread_ticks", 1)
        self.tk = self.spec.tick_size

    def on_timer(self, ts):
        t = self.last_tick
        if not t.bid_p or not t.ask_p:
            return
        bid, ask = t.bid_p[0], t.ask_p[0]
        if (ask - bid) / self.tk < self.min_spread - 1e-9:
            return
        for side, best in ((BUY, bid), (SELL, ask)):
            cur = self.orders_of(side)
            behind = [o for o in cur if (best - o.price if side == BUY else o.price - best) > self.requote * self.tk + 1e-9
                      or (o.price > ask - 1e-9 if side == BUY else o.price < bid + 1e-9)]
            for o in behind:                       # 价格已落后/越过盘口 -> 撤单, 下个定时器再重挂
                self.cancel_order(o.oid)
            if cur:
                continue
            if side == BUY and self.pos >= self.max_pos: continue
            if side == SELL and self.pos <= -self.max_pos: continue
            self.send_order(side, best, self.size)

    def on_trade(self, trade):
        pass
