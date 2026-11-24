"""行情读取与标准化。

标准列名 (用 colmap={标准名: 你的列名} 做映射):
  ts            时间(字符串/datetime/毫秒时间戳均可)
  instrument    合约代码, 如 rb2601
  last_price, volume(当日累计成交量), open_interest(可选)
  bid_price1..5, bid_volume1..5, ask_price1..5, ask_volume1..5   (有几档给几档)
  turnover      当日累计成交额(元, 可选但强烈建议: 用来反推两个tick之间的成交均价)
  upper_limit, lower_limit (可选, 用于涨跌停价格检查)
  trading_day (可选)
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from .core import Tick


def read_file(path: str, columns=None) -> pd.DataFrame:
    p = str(path).lower()
    if p.endswith((".parquet", ".pq")):
        return pd.read_parquet(path, columns=columns)
    if p.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(path)       # 大文件很慢, 建议先用 tools/convert_to_parquet.py 转换
    if columns is None:
        head = pd.read_csv(path, nrows=0).columns
        if {"exchange_t", "a_p1", "b_p1", "last_p", "v"}.issubset(head):     # snap 格式: 只读需要的列
            need = {"time", "exchange_t"} | set(_SNAP_MAP)
            columns = [c for c in head if c in need]
    return pd.read_csv(path, usecols=columns)


# ---------------------------------------------------------------------------
# 适配 snap_*.xlsx 这种格式: time(UTC) / exchange_t(HHMMSSmmm) / a_p1,a_v1,b_p1,b_v1 / last_p / v ...
# ---------------------------------------------------------------------------
_SNAP_MAP = {"insid_md": "instrument", "turnover": "turnover", "last_p": "last_price", "v": "volume",
             "total_position": "open_interest", "upper_limit_p": "upper_limit", "lower_limit_p": "lower_limit"}
for _i in range(1, 6):
    _SNAP_MAP.update({f"a_p{_i}": f"ask_price{_i}", f"a_v{_i}": f"ask_volume{_i}",
                      f"b_p{_i}": f"bid_price{_i}", f"b_v{_i}": f"bid_volume{_i}"})


def is_snap_format(df: pd.DataFrame) -> bool:
    return {"exchange_t", "a_p1", "b_p1", "last_p", "v"}.issubset(df.columns)


def adapt_snap(df: pd.DataFrame) -> pd.DataFrame:
    """把 snap 格式转成标准列。ts = 北京时间(无时区)的毫秒时间戳, 以交易所时间 exchange_t 为准。"""
    out = df.rename(columns=_SNAP_MAP)
    t = df["exchange_t"].astype("int64")
    t_ms = (t // 10_000_000) * 3_600_000 + ((t // 100_000) % 100) * 60_000 + ((t // 1000) % 100) * 1000 + t % 1000
    # 日期取自接收时间 time(UTC)+8h; 若与交易所时间相差超过12小时(跨零点边界)则校正一天
    bj = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None) + pd.Timedelta(hours=8)
    day_ms = bj.dt.normalize().astype("datetime64[ms]").astype("int64")
    recv_ms = bj.astype("datetime64[ms]").astype("int64") - day_ms
    diff = recv_ms - t_ms
    day_ms = day_ms + np.where(diff < -43_200_000, -86_400_000, 0) + np.where(diff > 43_200_000, 86_400_000, 0)
    out["ts"] = (day_ms + t_ms).astype("int64")
    return out


def infer_trading_day(ts_ms: pd.Series) -> pd.Series:
    """夜盘(>=18点)归属下一个工作日, 周六凌晨(周五夜盘跨零点)归属周一。未处理法定节假日, 有 trading_day 列时以该列为准。"""
    dt = pd.to_datetime(ts_ms, unit="ms")
    day, wd, hour = dt.dt.normalize(), dt.dt.weekday, dt.dt.hour
    nxt = day + pd.to_timedelta(np.where(wd == 4, 3, 1), unit="D")
    td = day.where(hour < 18, nxt)
    td = td.where(~((wd == 5) & (hour < 18)), day + pd.Timedelta(days=2))
    return td.dt.strftime("%Y-%m-%d")


def clean_rows(df: pd.DataFrame, drop_auction: bool = True) -> pd.DataFrame:
    """清洗无效行:
      - 买卖盘全空(如夜盘开盘前的 last=0、价格为空的垃圾行)
      - 买一>=卖一 (锁盘/交叉, 通常是集合竞价)
      - 最新价<=0 时用中间价代替
      - 集合竞价时段快照 (08:55-09:00, 20:55-21:00): 此时不能连续撮合, 默认丢弃
    """
    n0 = len(df)

    def lv(col):
        x = pd.to_numeric(df[col], errors="coerce")
        return x.where((x > 0) & (x < 1e300))

    bid1, ask1 = lv("bid_price1"), lv("ask_price1")
    ok = bid1.notna() | ask1.notna()
    ok &= ~(bid1.notna() & ask1.notna() & (bid1 >= ask1))
    if drop_auction:
        tod = (df["ts"] % 86_400_000) // 60_000            # 当天第几分钟(北京时间)
        ok &= ~(((tod >= 8 * 60 + 55) & (tod < 9 * 60)) | ((tod >= 20 * 60 + 55) & (tod < 21 * 60)))
    last = pd.to_numeric(df["last_price"], errors="coerce")
    mid = ((bid1 + ask1) / 2).fillna(bid1).fillna(ask1)
    df["last_price"] = last.where(last > 0, mid)
    df = df[ok & df["last_price"].notna()].reset_index(drop=True)
    df.attrs["n_dropped"] = n0 - len(df)
    return df


def normalize(df: pd.DataFrame, colmap: dict | None = None, drop_auction: bool = True) -> pd.DataFrame:
    if colmap:
        df = df.rename(columns={v: k for k, v in colmap.items()})
    elif is_snap_format(df):
        df = adapt_snap(df)
    if pd.api.types.is_numeric_dtype(df["ts"]):
        ts = df["ts"].astype("int64")
        if ts.iloc[0] < 10**11:      # 秒 -> 毫秒
            ts = ts * 1000
        df["ts"] = ts
    else:
        df["ts"] = pd.to_datetime(df["ts"]).astype("datetime64[ms]").astype("int64")
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    df = clean_rows(df, drop_auction)
    if "trading_day" not in df.columns:
        df["trading_day"] = infer_trading_day(df["ts"])
    return df


def build_ticks(df: pd.DataFrame, instrument: str | None = None, levels: int = 5) -> list:
    n_lv = 0
    for i in range(1, levels + 1):
        if all(c in df.columns for c in (f"bid_price{i}", f"bid_volume{i}", f"ask_price{i}", f"ask_volume{i}")):
            n_lv = i
    if n_lv == 0:
        raise ValueError("至少需要 bid_price1/bid_volume1/ask_price1/ask_volume1")
    rng = range(1, n_lv + 1)

    def mat(prefix):
        a = df[[f"{prefix}{i}" for i in rng]].to_numpy(dtype=float)
        a[~np.isfinite(a) | (np.abs(a) > 1e300)] = 0.0   # CTP 用 DBL_MAX 表示空档
        return a.tolist()

    bp, bv, ap, av = mat("bid_price"), mat("bid_volume"), mat("ask_price"), mat("ask_volume")
    ts = df["ts"].tolist()
    last = df["last_price"].astype(float).tolist()
    vol = df["volume"].astype(float).tolist()
    oi = df["open_interest"].astype(float).tolist() if "open_interest" in df else [0.0] * len(df)
    up = df["upper_limit"].astype(float).tolist() if "upper_limit" in df else [math.nan] * len(df)
    lo = df["lower_limit"].astype(float).tolist() if "lower_limit" in df else [math.nan] * len(df)
    tn = df["turnover"].astype(float).tolist() if "turnover" in df else [math.nan] * len(df)
    inst = instrument or str(df["instrument"].iloc[0])

    def clean(ps, vs):
        out = [(p, v) for p, v in zip(ps, vs) if p > 0 and v > 0]
        return [p for p, _ in out], [v for _, v in out]

    ticks = []
    for i in range(len(df)):
        b_p, b_v = clean(bp[i], bv[i]); a_p, a_v = clean(ap[i], av[i])
        ticks.append(Tick(ts[i], inst, last[i], vol[i], oi[i], b_p, b_v, a_p, a_v, up[i], lo[i], tn[i]))
    return ticks
