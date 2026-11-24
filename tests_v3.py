"""v3 新增功能测试: python tests_v3.py"""
import math
from fbt import *
from fbt.allocator import allocate, _tilt, round_largest_remainder, direction_contradiction
from fbt.contracts import get_spec, get_rule

SPEC, RULE = get_spec("rb2601"), get_rule("rb2601")


def T(ts, last, vol, tn=math.nan, bids=((100, 10), (99, 20), (98, 30), (97, 40)), asks=((101, 10), (102, 20), (103, 30), (104, 40))):
    return Tick(ts, "rb2601", last, vol, 0, [p for p, _ in bids], [v for _, v in bids],
                [p for p, _ in asks], [v for _, v in asks], turnover=tn)

mean = lambda al: sum(p * v for p, v in al) / sum(v for _, v in al)
tot = lambda al: sum(v for _, v in al)
prev = T(0, 100, 0)
def alloc(avg, last=100, dv=100, **kw): return allocate(prev, T(500, last, dv), dv, avg, SimConfig(**kw), SPEC)

# ---- 1) 指数倾斜 + β 先验: 在"挂单量不单调"的盘口上也必有解 (幂均值方程族会无解)
prices, q = [100, 99, 98], [10, 100, 20]
for beta in (0.0, 0.5, 1.0, 2.0):
    al = _tilt(prices, q, 98.5, 100, beta)
    assert abs(mean(al) - 98.5) < 1e-6 and abs(tot(al) - 100) < 1e-6, (beta, al)
def power_mean(prices, q, beta, a):
    u = [x ** beta for x in q]; la = [a * math.log(x) for x in u]; m = max(la); e = [math.exp(l - m) for l in la]
    return sum(p * x for p, x in zip(prices, e)) / sum(e)
assert all(power_mean(prices, q, 1, a / 10) > 98.9 for a in range(-2000, 2001))      # 反例: 幂均值方程族够不到 98.5
assert max(power_mean(prices, q, 0, a / 10) for a in range(-50, 50)) - min(power_mean(prices, q, 0, a / 10) for a in range(-50, 50)) < 1e-9  # β=0 时 α 失效
# β 越大越偏向厚档
w_depth = dict(_tilt([100, 99, 98], [10, 100, 20], 99.0, 100, 1.0))[99]
w_unif = dict(_tilt([100, 99, 98], [10, 100, 20], 99.0, 100, 0.0))[99]
assert w_depth > w_unif
# 配置入口: deep_levels_mode="tilt" 使用 alloc_beta
a0, _ = alloc(97.5, deep_levels_mode="tilt", alloc_beta=0.0); a1, _ = alloc(97.5, deep_levels_mode="tilt_uniform")
assert all(abs(x[1] - y[1]) < 1e-9 for x, y in zip(a0, a1))

# ---- 2) 最大余数法取整: 总手数守恒, 每档与浮点值相差 < 1
r = round_largest_remainder([(100, 30.6), (99, 10.7), (98, 9.7)], 51)
assert tot(r) == 51 and all(float(v).is_integer() for _, v in r)
al, _ = alloc(99.6, dv=31, alloc_round="largest_remainder")           # 60%/40% -> 18.6/12.4 -> 19/12
assert tot(al) == 31 and dict(al) == {100: 19.0, 99: 12.0}, al
al, _ = alloc(97.5, dv=37, alloc_round="largest_remainder"); assert tot(al) == 37 and all(float(v).is_integer() for _, v in al)

# ---- 3) 方向校验: 中价不变不算矛盾; 严格相反才算
bid_ask = lambda b, a: dict(bids=((b, 10), (b - 1, 10)), asks=((a, 10), (a + 1, 10)))   # 起点: 买100卖101, 中价100.5
p0 = T(0, 100, 0, **bid_ask(100, 101))
assert not direction_contradiction(p0, T(1, 101, 5, **bid_ask(100, 101)), 101.0)      # 均价偏卖, 中价没变
assert not direction_contradiction(p0, T(1, 101, 5, **bid_ask(101, 102)), 101.0)      # 均价偏卖, 中价上涨
assert direction_contradiction(p0, T(1, 101, 5, **bid_ask(99, 100)), 101.0)           # 均价偏卖, 中价下跌 -> 矛盾
assert direction_contradiction(p0, T(1, 100, 5, **bid_ask(101, 102)), 100.0)          # 均价偏买, 中价上涨 -> 矛盾

# ---- 4) 越界处理的各种模式 (avg=90 远低于最深买档 97)
al, c = alloc(90.0, beyond_depth_mode="last", last=99.0); assert al == [(99.0, 100)] and c == "degraded"
al, c = alloc(90.0, beyond_depth_mode="mid", degrade_mode="mid"); assert c == "degraded" and abs(al[0][0] - 100.5) < 1e-9
al, c = alloc(90.0, beyond_depth_mode="sweep", dv=25); assert al == [(100, 10), (99, 15)] and c == "beyond_depth"   # 顺序吞噬
al, _ = alloc(90.0, beyond_depth_mode="sweep", dv=150); assert abs(tot(al) - 150) < 1e-9 and dict(al)[97] > 40         # 吃完仍有剩余放最深档
al, _ = alloc(90.0, beyond_depth_mode="depth_weighted"); assert abs(dict(al)[97] - 40) < 1e-9 and abs(tot(al) - 100) < 1e-9
deeper = T(500, 95, 100, bids=((97, 10), (96, 20), (95, 30), (94, 40)))                    # 终点帧价格下移, 有更深的买档
al, c = allocate(prev, deeper, 100, 95.5, SimConfig(beyond_depth_mode="extend"), SPEC)
assert c == "deep" and abs(mean(al) - 95.5) < 1e-6 and abs(tot(al) - 100) < 1e-6, (al, c)  # 扩展后可解, 均价被还原
al, c = allocate(prev, deeper, 100, 80.0, SimConfig(beyond_depth_mode="extend"), SPEC)
assert c == "degraded" and al == [(95, 100)]                                             # 扩展后仍越界 -> 降级为最新价

# ---- 5) 引擎层: frame_delay / 固定位移 δ ξ / 诊断统计
class S(Strategy):
    def on_timer(self, ts):
        if ts == 0: self.buy(100, 5)
def run(ticks, **kw):
    base = dict(timer_ms=1, submit_latency_ms=0, cancel_latency_ms=0, cancel_model="none", queue_ratio=0.0); base.update(kw)
    return Engine(S(), ticks, SPEC, RULE, SimConfig(**base)).run()

def TT(ts, vol, tn=math.nan): return T(ts, 100, vol, tn, bids=((100, 10), (99, 20)), asks=((101, 10), (102, 20)))
one = [TT(0, 0, 0.0), TT(500, 10, 10 * 100 * 10), TT(1000, 10, 10 * 100 * 10)]            # 只有第一个区间有成交
assert run(one)["final_pos"] == 5 and run(one, frame_delay=0)["final_pos"] == 5
assert run(one, frame_delay=1)["final_pos"] == 0                                         # 保守口径: 第一个区间不参与
two = one + [TT(1500, 20, 20 * 100 * 10), TT(2000, 20, 20 * 100 * 10)]                    # 第二个区间又有 10 手成交
assert run(two, frame_delay=1)["final_pos"] == 5                                         # 第二个区间才成交
# ξ: 前方排队 10, 每个区间固定新增 5 手插到前面; 成交 12 手: 无ξ 成交2手, ξ=5 -> 前方15 -> 不成交
ts12 = [TT(0, 0, 0.0), TT(500, 12, 12 * 100 * 10), TT(1000, 12, 12 * 100 * 10)]
assert run(ts12, queue_ratio=1.0)["final_pos"] == 2 and run(ts12, queue_ratio=1.0, queue_insert_lots=5)["final_pos"] == 0
# δ: 第一个区间无成交, 前方 10 被撤掉 50% -> 5; 第二个区间成交 7 手 -> 成交 2 手 (无δ则 0 手)
dl = [TT(0, 0, 0.0), TT(500, 0, 0.0), TT(1000, 7, 7 * 100 * 10), TT(1500, 7, 7 * 100 * 10)]
assert run(dl, queue_ratio=1.0)["final_pos"] == 0 and run(dl, queue_ratio=1.0, queue_cancel_rate=0.5)["final_pos"] == 2
# 诊断统计: 均价=卖一(101) 的成交算"主动买"; 均价=买一(100) 算"主动卖"
mk = lambda avg: [TT(0, 0, 0.0), TT(500, 10, 10 * avg * 10), TT(1000, 10, 10 * avg * 10)]
r = run(mk(101.0)); assert r["aggr_buy_vol"] == 10 and r["aggr_sell_vol"] == 0 and r["alloc_err_max_ticks"] < 1e-9
r = run(mk(100.0)); assert r["aggr_sell_vol"] == 10 and r["aggr_buy_vol"] == 0
# 方向校验计数 + degrade
rr = run([TT(0, 0, 0.0), T(500, 101, 10, 10 * 101 * 10, bids=((99, 10),), asks=((100, 10),)), T(1000, 101, 10, 10 * 101 * 10, bids=((99, 10),), asks=((100, 10),))], direction_check="flag")
assert rr["dir_flags"] == 1, rr
# diag_all_ticks: 没有挂单时也统计
class Idle(Strategy): pass
r = Engine(Idle(), mk(101.0), SPEC, RULE, SimConfig(timer_ms=0, diag_all_ticks=True)).run(); assert r["aggr_buy_vol"] == 10 and r["case_exact"] == 1
r = Engine(Idle(), mk(101.0), SPEC, RULE, SimConfig(timer_ms=0)).run(); assert r["aggr_buy_vol"] == 0
print("all v3 tests passed")
