"""成交价位分配: 只有快照 tick 时, 把两个 tick 之间的成交量 dv 分配到具体价位上。

输入: 区间成交量 dv、成交均价 avg(= dTurnover / (dVolume*乘数))、区间开始的盘口(五档)、新 tick 的最新价 last。
输出: [(价格, 成交量), ...]  以及场景名(用于统计各场景出现的频率)。

场景 (均价 avg 相对盘口的位置, 以买盘为例, 卖盘对称):
  exact           avg 恰好落在某一档 -> 区间内全部成交都在该档
  inside_spread   avg 在 买一 与 卖一 之间 -> inside_spread_mode
  between_l1_l2   avg 在 一档 与 二档 之间 -> between_levels_mode      (两元一次方程: 线性分配到这两档)
  deep            avg 在 二档 以外的相邻两档之间 -> deep_levels_mode     (成交分布在 一档..外侧档 多个档位)
  beyond_depth    avg 比可见最深一档还差 -> beyond_depth_mode
  degraded        无法求解(越界且扩展后仍越界 / 方向矛盾) -> 降级为单一成交价 (degrade_mode), 并打标统计
  no_book         盘口缺失 -> 全部按 avg 成交
"""
from __future__ import annotations
import math


def trade_avg_price(prev, tick, dv, reset, spec, cfg):
    """区间成交均价。返回 (均价, 是否用了成交额)。成交额缺失/异常时退回最新价。"""
    last = tick.last
    if cfg.avg_source == "turnover" and dv > 0:
        tn, tp = tick.turnover, prev.turnover
        if tn == tn and (reset or tp == tp):
            dt = tn if reset else tn - tp
            if dt > 0:
                mult = spec.multiplier if cfg.turnover_includes_multiplier else 1.0
                avg = dt / (dv * mult * cfg.turnover_scale)
                if abs(avg - last) <= cfg.avg_outlier_ticks * spec.tick_size:
                    return avg, True
    return last, False


def _linear2(p_a, p_b, avg, dv):
    """二元一次方程: w*p_hi + (1-w)*p_lo = avg, 解出两个价位上各自的成交量。"""
    lo, hi = min(p_a, p_b), max(p_a, p_b)
    if hi - lo < 1e-12:
        return [(lo, dv)]
    w = min(1.0, max(0.0, (avg - lo) / (hi - lo)))
    return [(lo, dv * (1 - w)), (hi, dv * w)]


def _anchored(p_a, p_b, avg, dv, last, tol):
    """以 last 为中间锚点: avg 在 last 与哪一端之间, 就在 last 与那一端之间做线性分配。last 不在区间内则退化为两点分配。"""
    lo, hi = min(p_a, p_b), max(p_a, p_b)
    if lo + tol < last < hi - tol:
        return _linear2(lo, last, avg, dv) if avg <= last else _linear2(last, hi, avg, dv)
    return _linear2(lo, hi, avg, dv)


def _tilt(prices, weights, avg, dv, beta=1.0):
    """多档分配(指数倾斜): w_i ∝ u_i * exp(λ p_i), u_i = q_i^β 为先验权重, 解 λ 使 Σ w_i p_i = avg。
    mean(λ) 随 λ 严格单调, 取值覆盖 (min p, max p) 全区间, 所以只要 avg 在最低价与最高价之间就一定有唯一解。
    (β=0 先验均匀; β=1 按显示量; 等价于在满足均价的所有分布里, 取与先验 u 的 KL 距离最小的那个)"""
    x0 = prices[0]
    scale = max(abs(prices[0] - prices[-1]), 1e-9)
    xs = [(p - x0) / scale for p in prices]
    target = (avg - x0) / scale
    q = [max(w, 1e-9) ** beta for w in weights]

    def calc(lam):
        m = max(lam * x for x in xs)
        e = [qj * math.exp(lam * x - m) for qj, x in zip(q, xs)]
        s = sum(e)
        return sum(ej * x for ej, x in zip(e, xs)) / s, e, s

    lo, hi = -200.0, 200.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if calc(mid)[0] < target: lo = mid
        else: hi = mid
    _, e, s = calc((lo + hi) / 2)
    return [(p, dv * ej / s) for p, ej in zip(prices, e)]


def round_largest_remainder(alloc, total=None):
    """最大余数法取整: 各价位先取 floor, 剩余手数按小数部分从大到小各补 1 手, 总手数守恒。"""
    if not alloc:
        return alloc
    n = int(round(total if total is not None else sum(v for _, v in alloc)))
    fl = [int(math.floor(v + 1e-9)) for _, v in alloc]
    rem = [v - f for (_, v), f in zip(alloc, fl)]
    R = max(0, n - sum(fl))
    for i in sorted(range(len(alloc)), key=lambda i: -rem[i])[:R]:
        fl[i] += 1
    return [(p, float(f)) for (p, _), f in zip(alloc, fl) if f > 0]


def direction_contradiction(prev, tick, avg) -> bool:
    """方向矛盾: 成交均价偏向一侧(相对起点中价), 但终点中价朝相反方向走。中价没变(符号为0)不算矛盾。"""
    if not (prev.bid_p and prev.ask_p and tick.bid_p and tick.ask_p):
        return False
    sg = lambda x: (x > 1e-9) - (x < -1e-9)
    return sg(avg - prev.mid) * sg(tick.mid - prev.mid) < 0


def degrade_point(prev, tick, dv, cfg):
    """降级: 全部成交量落在单一价位 (最新价 或 区间中价)。"""
    if cfg.degrade_mode == "mid" and prev.bid_p and prev.ask_p and tick.bid_p and tick.ask_p:
        return [((prev.mid + tick.mid) / 2, dv)], "degraded"
    return [(tick.last, dv)], "degraded"


def _bracket(levels, xs, target, avg, dv, last, cfg, tol):
    """均价落在 levels[k] 与 levels[k+1] 之间 (xs 为从内向外递增的坐标)。"""
    k = max(i for i in range(len(xs) - 1) if xs[i] < target)
    p_in, p_out = levels[k][0], levels[k + 1][0]
    if k == 0:
        m = cfg.between_levels_mode
        if m == "at_inner": return [(p_in, dv)], "between_l1_l2"
        if m == "at_outer": return [(p_out, dv)], "between_l1_l2"
        if m == "at_last": return [(min(max(last, min(p_in, p_out)), max(p_in, p_out)), dv)], "between_l1_l2"
        if m == "with_last": return _anchored(p_in, p_out, avg, dv, last, tol), "between_l1_l2"
        return _linear2(p_in, p_out, avg, dv), "between_l1_l2"
    m = cfg.deep_levels_mode
    if m == "linear_two": return _linear2(p_in, p_out, avg, dv), "deep"
    if m == "with_last": return _anchored(p_in, p_out, avg, dv, last, tol), "deep"
    ps = [p for p, _ in levels[:k + 2]]
    beta = {"tilt_uniform": 0.0, "tilt_depth": 1.0}.get(m, cfg.alloc_beta)
    return _tilt(ps, [v for _, v in levels[:k + 2]], avg, dv, beta), "deep"


def _beyond(levels, s, avg, dv, prev, tick, cfg, tol, last):
    """均价比可见最深一档还差(越界)。"""
    m = cfg.beyond_depth_mode
    if m == "skip": return [], "beyond_depth"
    if m == "deepest": return [(levels[-1][0], dv)], "beyond_depth"
    if m in ("last", "mid"): return degrade_point(prev, tick, dv, replace_mode(cfg, m))
    if m == "depth_weighted":
        tot = sum(v for _, v in levels)
        return [(p, dv * v / tot) for p, v in levels], "beyond_depth"
    if m == "sweep":                                         # 按档位顺序吞噬: 从最优档向外, 每档最多吃掉其显示量
        out, left = [], dv
        for p, v in levels:
            take = min(v, left)
            if take > 0: out.append((p, take)); left -= take
            if left <= 0: break
        if left > 1e-9: out.append((levels[-1][0], left))   # 五档吃完仍有剩余: 放在最深一档
        return out, "beyond_depth"
    if m == "extend":                                        # 用终点帧同侧档位扩展候选集, 重解; 仍超出则降级
        end = list(zip(tick.bid_p, tick.bid_v)) if s == -1 else list(zip(tick.ask_p, tick.ask_v))
        seen = {p for p, _ in levels}
        ext = sorted(levels + [(p, v) for p, v in end if p not in seen], key=lambda t: s * t[0])
        xs = [s * p for p, _ in ext]
        if s * avg <= xs[-1] and s * avg > xs[0]:
            return _bracket(ext, xs, s * avg, avg, dv, last, cfg, tol)
        return degrade_point(prev, tick, dv, cfg)
    return [(avg, dv)], "beyond_depth"                       # avg: 按均价本身成交


def replace_mode(cfg, m):
    from dataclasses import replace
    return replace(cfg, degrade_mode=m)


def _allocate_raw(prev, tick, dv, avg, cfg, spec):
    book = prev if cfg.book_ref == "prev" else tick
    tol = cfg.exact_level_tol_ticks * spec.tick_size
    bids = list(zip(book.bid_p, book.bid_v))
    asks = list(zip(book.ask_p, book.ask_v))
    last = tick.last

    for p, _ in bids + asks:                                   # 1) 恰好落在某一档
        if abs(avg - p) <= tol:
            return [(p, dv)], "exact"

    if bids and asks and bids[0][0] < avg < asks[0][0]:          # 2) 买一与卖一之间
        b1, a1 = bids[0][0], asks[0][0]
        m = cfg.inside_spread_mode
        if m == "linear_bid_ask": return _linear2(b1, a1, avg, dv), "inside_spread"
        if m == "at_last": return [(min(max(last, b1), a1), dv)], "inside_spread"
        if m == "nearest": return [(b1 if avg - b1 <= a1 - avg else a1, dv)], "inside_spread"
        return _anchored(b1, a1, avg, dv, last, tol), "inside_spread"   # last_anchored

    for levels, s in ((bids, -1), (asks, 1)):                    # 3) 买盘/卖盘 档位之间
        if not levels:
            continue
        xs = [s * p for p, _ in levels]                         # 越往外越大
        target = s * avg
        if target <= xs[0]:
            continue
        if target > xs[-1]:                                     # 4) 超出可见深度
            return _beyond(levels, s, avg, dv, prev, tick, cfg, tol, last)
        return _bracket(levels, xs, target, avg, dv, last, cfg, tol)
    return [(avg, dv)], "no_book"


def allocate(prev, tick, dv, avg, cfg, spec):
    """返回 ([(价格, 成交量)], 场景名)。场景名: exact / inside_spread / between_l1_l2 / deep / beyond_depth / degraded / no_book。"""
    alloc, case = _allocate_raw(prev, tick, dv, avg, cfg, spec)
    if cfg.alloc_round == "largest_remainder" and alloc:
        alloc = round_largest_remainder(alloc, dv)
    return alloc, case
