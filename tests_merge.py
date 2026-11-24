"""合并功能测试: 旧路径 / auto 模式 / random 排队 / 交易所级隐含单。 python tests_merge.py"""
import math
from dataclasses import replace
from fbt import *
from fbt.contracts import get_spec, get_rule
from fbt.matching import SimExchange

SPEC, RULE = get_spec("rb2601"), get_rule("rb2601")


def T(ts, last, vol, tn=math.nan, bids=((100, 10), (99, 20)), asks=((101, 10), (102, 20))):
    return Tick(ts, "rb2601", last, vol, 0, [p for p, _ in bids], [v for _, v in bids],
                [p for p, _ in asks], [v for _, v in asks], turnover=tn)


class S(Strategy):
    def on_timer(self, ts):
        if ts == 0: self.buy(100, 5)


def run(ticks, rule=RULE, **kw):
    base = dict(timer_ms=1, submit_latency_ms=0, cancel_latency_ms=0, cancel_model="none"); base.update(kw)
    return Engine(S(), ticks, SPEC, rule, SimConfig(**base)).run()


# ---- 1) 旧路径: 与第一版行为一致 (无成交额数据也能跑); 买一队尾排队 10, 成交 12 手 -> 成交 2 手
t = [T(0, 100, 0), T(500, 100, 12), T(1000, 100, 12)]
r = run(t, match_mode="legacy"); assert r["final_pos"] == 2 and r["case_legacy"] == 1 and r["avg_fallback_last"] == 0, r
# 穿价: 旧路径一律全成交 (through_fill_ratio 不生效)
t = [T(0, 100, 0), T(500, 99, 3, bids=((98, 10), (97, 20)), asks=((99, 10), (100, 20))), T(1000, 99, 3)]
assert run(t, match_mode="legacy", through_fill_ratio=0.0)["final_pos"] == 5
# ---- 2) auto: 有成交额走分配路径, 没有走旧路径
with_tn = [T(0, 100, 0, 0.0), T(500, 100, 12, 12 * 100 * 10), T(1000, 100, 12, 12 * 100 * 10)]
no_tn = [T(0, 100, 0), T(500, 100, 12), T(1000, 100, 12)]
r = run(with_tn, match_mode="auto"); assert r["case_exact"] == 1 and r["case_legacy"] == 0 and r["final_pos"] == 2
r = run(no_tn, match_mode="auto"); assert r["case_legacy"] == 1 and r["case_exact"] == 0 and r["final_pos"] == 2
r = run(with_tn, match_mode="alloc"); assert r["case_exact"] == 1
# 旧路径下隐含单也会折算"更优价位被吃掉的量"
# ---- 3) random 排队: 可复现、受范围限制、每笔订单不同
def queue_aheads(seed, lo=0.0, hi=1.0, n=40):
    ex = SimExchange(SPEC, RULE, SimConfig(queue_pos_mode="random", queue_seed=seed, queue_random_low=lo, queue_random_high=hi),
                     lambda ts, p: None)
    ex.on_tick(T(0, 100, 0, bids=((100, 100), (99, 20))))
    out = []
    for i in range(n):
        o = Order(i + 1, "rb2601", BUY, 99.0, 1)       # 买二价位(显示量 20) 避免与之前的单叠加: 每次单独看新增部分
        ex.on_new_order(o, 0); out.append(o.queue_ahead - (i * 1))   # 减去自己更早的同价挂单(每笔 1 手)
    return out
a1, a2, a3 = queue_aheads(1), queue_aheads(1), queue_aheads(2)
assert a1 == a2 and a1 != a3                                  # 同种子可复现, 不同种子不同
assert all(0 <= x <= 20 + 1e-9 for x in a1) and len(set(round(x, 6) for x in a1)) > 10
b = queue_aheads(3, 0.4, 0.6); assert all(8 - 1e-9 <= x <= 12 + 1e-9 for x in b)
for mode, exp in (("tail", 10), ("head", 0), ("mid", 5)):                  # 其余模式
    ex = SimExchange(SPEC, RULE, SimConfig(queue_pos_mode=mode), lambda ts, p: None)
    ex.on_tick(T(0, 100, 0)); o = Order(1, "rb2601", BUY, 100.0, 1); ex.on_new_order(o, 0); assert o.queue_ahead == exp, (mode, o.queue_ahead)
# ratio 模式仍按 queue_ratio
ex = SimExchange(SPEC, RULE, SimConfig(queue_ratio=0.3), lambda ts, p: None); ex.on_tick(T(0, 100, 0))
o = Order(1, "rb2601", BUY, 100.0, 1); ex.on_new_order(o, 0); assert abs(o.queue_ahead - 3.0) < 1e-9

# ---- 4) 交易所级隐含单: 排队量折算 + 主动单可吃量折算 + 成交量折算
imp = replace(RULE, implied_mode="discount", implied_depth_ratio=0.5, implied_trade_ratio=0.0)
ex = SimExchange(SPEC, imp, SimConfig(), lambda ts, p: None); ex.on_tick(T(0, 100, 0))
o = Order(1, "rb2601", BUY, 100.0, 1); ex.on_new_order(o, 0); assert abs(o.queue_ahead - 5.0) < 1e-9          # 前方 10 -> 5
reports = []
ex = SimExchange(SPEC, imp, SimConfig(), lambda ts, p: reports.append(p)); ex.on_tick(T(0, 100, 0))
o = Order(2, "rb2601", BUY, 101.0, 6, tif=FAK); ex.on_new_order(o, 0)                                          # 卖一显示 10, 可吃 5
assert o.filled == 5 and o.status == CANCELLED, (o.filled, o.status)
o = Order(3, "rb2601", BUY, 101.0, 6, tif=FOK); ex.on_new_order(o, 0); assert o.filled == 0 and o.reason == "FOK_NOT_FILLED"
# 关闭 (implied_mode=none) 即使比例非 0 也不生效
off = replace(RULE, implied_mode="none", implied_depth_ratio=0.5)
ex = SimExchange(SPEC, off, SimConfig(), lambda ts, p: None); ex.on_tick(T(0, 100, 0))
o = Order(1, "rb2601", BUY, 100.0, 1); ex.on_new_order(o, 0); assert abs(o.queue_ahead - 10.0) < 1e-9
# 交易所级与全局叠加: 1-(1-0.5)(1-0.5)=0.75 -> 排队 10 -> 2.5
ex = SimExchange(SPEC, imp, SimConfig(implied_depth_ratio=0.5), lambda ts, p: None); ex.on_tick(T(0, 100, 0))
o = Order(1, "rb2601", BUY, 100.0, 1); ex.on_new_order(o, 0); assert abs(o.queue_ahead - 2.5) < 1e-9
# 成交量折算: 12 手成交一半来自隐含单 -> 实际 6 手 < 前方 10 -> 不成交; 无折算则成交 2 手
imp_t = replace(RULE, implied_mode="discount", implied_trade_ratio=0.5)
assert run(with_tn, rule=imp_t)["final_pos"] == 0 and run(with_tn)["final_pos"] == 2
# ---- 5) T1->T2 新单插到前面: 一档显示量 10 -> 30 (无成交), 之后成交 15 手在买一
def tb(ts, last, vol, tn, bv=10): return T(ts, last, vol, tn, bids=((100, bv), (99, 20)))
ins = [tb(0, 100, 0, 0.0), tb(500, 100, 0, 0.0, bv=30), tb(1000, 100, 15, 15 * 100 * 10, bv=30), tb(1500, 100, 15, 15 * 100 * 10, bv=30)]
assert run(ins, queue_insert_ratio=0.0)["final_pos"] == 5      # 严格时间优先: 前方仍是 10, 15-10=5
assert run(ins, queue_insert_ratio=0.5)["final_pos"] == 0      # 前方 10+0.5*20=20 > 15, 不成交
assert run(ins, queue_insert_ratio=1.0, cancel_model="prob")["final_pos"] == 0
print("all merge tests passed")
