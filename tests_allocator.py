"""成交价位分配 + 新排队参数 的单元测试: python tests_allocator.py"""
import math
from fbt import *
from fbt.allocator import allocate, trade_avg_price
from fbt.contracts import get_spec, get_rule

SPEC = get_spec("rb2601")                      # tick=1, 乘数=10


def T(ts, last, vol, tn=math.nan, bids=((100, 10), (99, 20), (98, 30), (97, 40)), asks=((101, 10), (102, 20), (103, 30), (104, 40))):
    return Tick(ts, "rb2601", last, vol, 0, [p for p, _ in bids], [v for _, v in bids],
                [p for p, _ in asks], [v for _, v in asks], turnover=tn)


def mean(al): return sum(p * v for p, v in al) / sum(v for _, v in al)
def tot(al): return sum(v for _, v in al)
def vol_at(al, p): return sum(v for q, v in al if abs(q - p) < 1e-9)


prev = T(0, 100, 0)
def alloc(avg, last=100, dv=100, **kw):
    return allocate(prev, T(500, last, dv), dv, avg, SimConfig(**kw), SPEC)

# 1) 恰好在某档
al, c = alloc(100.0); assert c == "exact" and al == [(100, 100)]
al, c = alloc(101.02); assert c == "exact" and al == [(101, 100)]      # 容差 0.05 tick 内

# 2) 买一卖一之间
al, c = alloc(100.25, inside_spread_mode="linear_bid_ask")
assert c == "inside_spread" and abs(vol_at(al, 100) - 75) < 1e-9 and abs(vol_at(al, 101) - 25) < 1e-9
al, c = alloc(100.5, inside_spread_mode="nearest"); assert al == [(100, 100)]
# last 锚点 (价差>1tick 的盘口)
wide = T(0, 100, 0, bids=((100, 10), (99, 20)), asks=((102, 10), (103, 20)))
for avg, last, exp in ((100.5, 101, {100: 50, 101: 50}), (101.5, 101, {101: 50, 102: 50}), (101.0, 101, {101: 100})):
    al, c = allocate(wide, T(500, last, 100), 100, avg, SimConfig(inside_spread_mode="last_anchored"), SPEC)
    for p, v in exp.items(): assert abs(vol_at(al, p) - v) < 1e-9, (avg, last, al)
al, _ = allocate(wide, T(500, 101, 100), 100, 100.5, SimConfig(inside_spread_mode="at_last"), SPEC); assert al == [(101, 100)]

# 3) 一档~二档之间: 线性分配 (二元一次方程)
al, c = alloc(99.6)
assert c == "between_l1_l2" and abs(vol_at(al, 100) - 60) < 1e-9 and abs(vol_at(al, 99) - 40) < 1e-9 and abs(mean(al) - 99.6) < 1e-9
assert alloc(99.6, between_levels_mode="at_inner")[0] == [(100, 100)]
assert alloc(99.6, between_levels_mode="at_outer")[0] == [(99, 100)]
al, _ = alloc(104 - 3 + 0.4 - 0.0)       # 卖盘 一~二档之间 (101.4): 对称
assert abs(vol_at(al, 101) - 60) < 1e-9 and abs(vol_at(al, 102) - 40) < 1e-9

# 4) 二档以外: 多档分配, 均价必须被还原
for mode in ("tilt_depth", "tilt_uniform"):
    al, c = alloc(97.5, deep_levels_mode=mode)
    assert c == "deep" and len(al) == 4 and all(v > 0 for _, v in al), al
    assert abs(tot(al) - 100) < 1e-6 and abs(mean(al) - 97.5) < 1e-6, (mode, al, mean(al))
al, _ = alloc(97.5, deep_levels_mode="linear_two"); assert len(al) == 2 and abs(mean(al) - 97.5) < 1e-9
al, _ = alloc(102.7)                      # 卖盘二~三档之间
assert abs(mean(al) - 102.7) < 1e-6 and len(al) == 3

# 5) 超出可见深度
assert alloc(90.0)[1] == "beyond_depth" and alloc(90.0)[0] == [(90.0, 100)]
assert alloc(90.0, beyond_depth_mode="deepest")[0] == [(97, 100)] and alloc(90.0, beyond_depth_mode="skip")[0] == []

# 6) 成交均价: dTurnover / (dVolume * 乘数)
cfg = SimConfig()
a, used = trade_avg_price(T(0, 100, 0, 0.0), T(500, 100, 10, 10 * 99.5 * 10), 10, False, SPEC, cfg); assert used and abs(a - 99.5) < 1e-9
a, used = trade_avg_price(T(0, 100, 0), T(500, 100, 10), 10, False, SPEC, cfg); assert (not used) and a == 100      # 无成交额 -> last
a, used = trade_avg_price(T(0, 100, 0, 0.0), T(500, 100, 10, 10 * 500 * 10), 10, False, SPEC, cfg); assert not used   # 异常 -> last
a, used = trade_avg_price(T(0, 100, 0, 5e9), T(500, 100, 10, 10 * 99 * 10), 10, True, SPEC, cfg); assert used and abs(a - 99) < 1e-9  # 日夜盘重置
a, used = trade_avg_price(T(0, 100, 0, 0.0), T(500, 100, 10, 10 * 99.5 * 10), 10, False, SPEC, SimConfig(avg_source="last")); assert not used


# 7) 接入撮合: 同样的成交量, 均价不同 => 成交结果不同
class S(Strategy):
    def on_init(self): self.log = []
    def on_timer(self, ts):
        if ts == 0: self.buy(100, 5)
    def on_order(self, o): self.log.append((o.status, o.filled))


def run(ticks, **kw):
    base = dict(timer_ms=1, submit_latency_ms=0, cancel_latency_ms=0, cancel_model="none"); base.update(kw)
    st = S(); eng = Engine(st, ticks, SPEC, get_rule("rb2601"), SimConfig(**base)); r = eng.run(); return st, r

def two_ticks(avg, dv=10):
    return [T(0, 100, 0, 0.0), T(500, 100, dv, dv * avg * 10), T(1000, 100, dv, dv * avg * 10)]

# 均价 = 买一: 全部 10 手成交在买一, 队尾排队 10 手 -> 我方 0 手
_, r = run(two_ticks(100.0)); assert r["final_pos"] == 0 and r["case_exact"] == 1
# 均价 = 99.5 (一二档之间): 一半在二档(穿过我方买一价) -> 我方全成
_, r = run(two_ticks(99.5)); assert r["final_pos"] == 5 and r["case_between_l1_l2"] == 1, r
# 均价 100.5 (买卖之间, 线性): 买一分到 5 手 < 前方排队 10 -> 不成交; 队首(queue_ratio=0)则成交 5 手
_, r = run(two_ticks(100.5)); assert r["final_pos"] == 0 and r["case_inside_spread"] == 1
_, r = run(two_ticks(100.5), queue_ratio=0.0); assert r["final_pos"] == 5
# through_fill_ratio=0.5: 穿价只成交一半 (5 手 * 0.5 = 2 手(四舍五入))
_, r = run(two_ticks(99.5), through_fill_ratio=0.5); assert r["final_pos"] == 2, r
# 没有成交额数据: 退回最新价(=买一) -> 与"均价=买一"相同
tk_no = [T(0, 100, 0), T(500, 100, 10), T(1000, 100, 10)]
_, r = run(tk_no); assert r["final_pos"] == 0 and r["avg_fallback_last"] == 1

# 8) implied_depth_ratio: 排队量中 50% 为隐含单 -> 前方排队 10 -> 5; 均价=买一, 成交 10 手 -> 我方成交 5 手
_, r = run(two_ticks(100.0), implied_depth_ratio=0.5); assert r["final_pos"] == 5, r
# implied_trade_ratio: 成交量中 50% 来自隐含单 -> 实际只有 5 手消耗队列 -> 前方 10 手 -> 不成交
_, r = run(two_ticks(100.0), queue_ratio=0.5, implied_trade_ratio=0.5); assert r["final_pos"] == 0, r

# 9) partial_interval_mode: 订单在 tick 间隔中途(t=250)到达, 前一半的成交不算
class S2(S):
    def on_timer(self, ts):
        if ts == 250: self.buy(100, 20)
tk3 = [T(0, 100, 0, 0.0), T(500, 100, 10, 10 * 100 * 10), T(1000, 100, 10, 10 * 100 * 10)]
def run2(**kw):
    base = dict(timer_ms=1, submit_latency_ms=0, cancel_latency_ms=0, cancel_model="none", queue_ratio=0.0); base.update(kw)
    st = S2(); eng = Engine(st, tk3, SPEC, get_rule("rb2601"), SimConfig(**base)); return eng.run()
assert run2(partial_interval_mode="full")["final_pos"] == 10
assert run2(partial_interval_mode="pro_rata")["final_pos"] == 5     # 到达时已过 50% 区间 -> 只算后一半

# 10) cancel_ahead_ratio: 盘口一档量从 10 降到 5 且无成交 => prob(1.0) 前方排队 10->5, ratio=0 则不变
def qtest(car):
    ticks = [T(0, 100, 0, 0.0), T(500, 100, 0, 0.0, bids=((100, 5), (99, 20), (98, 30), (97, 40))),
             T(1000, 100, 10, 10 * 100 * 10, bids=((100, 5), (99, 20), (98, 30), (97, 40)))]
    st = S(); eng = Engine(st, ticks, SPEC, get_rule("rb2601"),
                           SimConfig(timer_ms=1, submit_latency_ms=0, cancel_latency_ms=0, cancel_model="prob", cancel_ahead_ratio=car))
    return eng.run()["final_pos"]
assert qtest(1.0) == 5 and qtest(0.0) == 0
print("all allocator/queue tests passed")
