"""快照队列变化。到达前的假设流量与到达后的观测净变化分别计量。"""


def arrival_queue(initial, elapsed_ms, reference_ms, insert_lots, cancel_fraction):
    """按尚未观测的时间折算到达前的队列变化。"""
    fraction = min(1.0, max(0.0, elapsed_ms / reference_ms))
    cancelled = initial * cancel_fraction * fraction
    inserted = insert_lots * fraction
    return initial - cancelled + inserted


def insert_ahead(net_added, share, fraction=1.0):
    return max(0.0, net_added) * share * fraction


def cancel_ahead(ahead, remaining, displayed, model, share, fraction=1.0):
    """按净撤单后的剩余显示量调整前方外部队列。"""
    if model == "prob":
        return ahead * (1.0 - share * (1.0 - displayed / remaining) * fraction)
    target = min(ahead, displayed)
    return ahead + (target - ahead) * fraction
