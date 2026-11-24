"""生成合成 tick 数据, 只用来验证流程。真实数据请用 colmap 映射你的列名。"""
import sys, os
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fbt.contracts import get_spec

BASE = {"rb2601": 3300, "i2601": 800, "SR601": 5400}


def gen_day(date, insts, seed):
    rng = np.random.default_rng(seed)
    segs = [("09:00:00", "10:15:00"), ("10:30:00", "11:30:00"), ("13:30:00", "15:00:00")]
    ts = None
    for a, b in segs:
        r = pd.date_range(f"{date} {a}", f"{date} {b}", freq="500ms")
        ts = r if ts is None else ts.append(r)
    n, frames = len(ts), []
    for inst in insts:
        tk = get_spec(inst).tick_size
        walk = np.cumsum(rng.choice([-1, 0, 1], size=n, p=[.12, .76, .12]))
        bid1 = BASE[inst] + walk * tk
        spread = rng.choice([1, 2], size=n, p=[.85, .15]) * tk
        ask1 = bid1 + spread
        df = {"ts": ts.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3], "instrument": inst}
        has = rng.random(n) < 0.55
        dv = np.where(has, rng.integers(1, 25, n), 0)
        buy = rng.random(n) < 0.5
        last = np.where(has, np.where(buy, ask1, bid1), bid1)
        df["last_price"] = last
        df["volume"] = np.cumsum(dv)
        df["open_interest"] = 100000
        for k in range(1, 6):
            df[f"bid_price{k}"] = bid1 - (k - 1) * tk
            df[f"ask_price{k}"] = ask1 + (k - 1) * tk
            df[f"bid_volume{k}"] = rng.integers(5, 200, n)
            df[f"ask_volume{k}"] = rng.integers(5, 200, n)
        frames.append(pd.DataFrame(df))
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    os.makedirs(out, exist_ok=True)
    for d in range(days):
        date = (pd.Timestamp("2025-03-03") + pd.tseries.offsets.BDay(d)).strftime("%Y-%m-%d")
        gen_day(date, list(BASE), seed=d).to_csv(f"{out}/{date}.csv", index=False)
        print("wrote", date)
