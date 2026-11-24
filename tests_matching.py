"""撮合规则单元测试: python tests_matching.py"""
import math
from fbt import *
from fbt.contracts import get_spec, get_rule


def tk(ts, last, vol, bid, bv, ask, av, inst="rb2601"):
    return Tick(ts, inst, last, vol, 0, [bid, bid - 1], [bv, 50], [ask, ask + 1], [av, 50])


class S(Strategy):
    script = []
    def on_init(self): self.log = []
    def on_timer(self, ts):
        for when, fn in self.script:
            if when == ts: fn(self)
    def on_order(self, o): self.log.append((o.status, o.filled, o.reason))


def run(ticks, script, inst="rb2601", **cfgkw):
    kw = dict(timer_ms=1, submit_latency_ms=0, cancel_latency_ms=0); kw.update(cfgkw)
    cfg = SimConfig(**kw)
    S.script = script
    st = S()
    eng = Engine(st, ticks, get_spec(inst), get_rule(inst), cfg)
    return eng, st, eng.run()


# 1) 上期所不支持市价单 -> 拒单; 大商所(i)可以市价 FAK 成交
base = [tk(0, 100, 0, 100, 10, 101, 10), tk(500, 100, 0, 100, 10, 101, 10)]
_, st, _ = run(base, [(0, lambda s: s.buy(0, 1, MARKET, FAK))])
assert st.log[-1][0] == REJECTED and "MARKET" in st.log[-1][2], st.log
ib = [tk(0, 800, 0, 800, 10, 800.5, 10, "i2601"), tk(500, 800, 0, 800, 10, 800.5, 10, "i2601")]
_, st, r = run(ib, [(0, lambda s: s.buy(0, 3, MARKET, FAK))], inst="i2601")
assert st.log[-1][:2] == (FILLED, 3) and r["final_pos"] == 3

# 2) FOK: 对手盘不足 -> 全撤; FAK: 部分成交后剩余撤销
_, st, _ = run(base, [(0, lambda s: s.buy(101, 15, LIMIT, FOK))])
assert st.log[-1][0] == CANCELLED and st.log[-1][2] == "FOK_NOT_FILLED"
_, st, r = run(base, [(0, lambda s: s.buy(101, 15, LIMIT, FAK))])
assert st.log[-1][:2] == (CANCELLED, 10) and r["final_pos"] == 10

# 3) 被动单队尾: 前方 10 手, 之后一笔在 100 的卖方主动成交 12 手 -> 我方 (12-10)=2 手
t = [tk(0, 100, 0, 100, 10, 101, 10), tk(500, 100, 12, 100, 10, 101, 10), tk(1000, 100, 12, 100, 10, 101, 10)]
_, st, r = run(t, [(0, lambda s: s.buy(100, 5))], cancel_model="none")
assert r["final_pos"] == 2 and (PARTIAL, 2, "") in st.log, (st.log, r)
# 队首: 排队量 0 -> 全部 5 手
_, st, r = run(t, [(0, lambda s: s.buy(100, 5))], queue_ratio=0.0, cancel_model="none")
assert r["final_pos"] == 5

# 4) 穿价: 成交价 99 < 我方买价 100 -> 全成(按100)
t = [tk(0, 100, 0, 100, 10, 101, 10), tk(500, 99, 3, 98, 10, 99, 10), tk(1000, 99, 3, 98, 10, 99, 10)]
_, st, r = run(t, [(0, lambda s: s.buy(100, 5))])
assert r["final_pos"] == 5 and abs(r["pnl_gross"] - (5 * (98.5 - 100) * 10)) < 1e-6, r

# 5) 延迟: 发单延迟 600ms, 此时盘口已变 (ask 变成 102), 限价 101 买单不能吃到, 挂单
t = [tk(0, 100, 0, 100, 10, 101, 10), tk(500, 100, 0, 101, 10, 102, 10), tk(1000, 100, 0, 101, 10, 102, 10)]
_, st, r = run(t, [(0, lambda s: s.buy(101, 1))], submit_latency_ms=600)
assert r["final_pos"] == 0 and st.log[0][0] == NEW, (st.log, r)   # 延迟600ms后ask已是102, 只能挂单
_, st, r = run(t, [(0, lambda s: s.buy(101, 1))], submit_latency_ms=0)
assert r["final_pos"] == 1, r                                      # 无延迟则按旧盘口 101 成交
print("all matching tests passed")
