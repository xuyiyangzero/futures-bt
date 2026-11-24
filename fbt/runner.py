"""并行回测调度: 任务 = 一个行情文件; 进程池并行处理多个文件, 文件内按 (合约, 交易日) 切分, 每个切片独立回测。

说明: 各交易日独立回测, 日初仓位为 0、日末强平估值(做市策略通常日内平仓)。
若要两年连续带仓位, 需要改成按合约串行、按品种并行。
"""
from __future__ import annotations
import glob, importlib, os, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
import pandas as pd

from .core import SimConfig
from .contracts import get_spec, get_rule
from .data import read_file, normalize, build_ticks
from .engine import Engine


def _load_strategy_cls(ref):
    if isinstance(ref, str):                       # "examples.simple_mm:SimpleMM"
        mod, name = ref.split(":")
        return getattr(importlib.import_module(mod), name)
    return ref


def _run_file(job):
    (path, strat_ref, params, cfg_dict, colmap, instruments,
     spec_over, rule_over, out_dir) = job
    cfg = SimConfig(**cfg_dict)
    cls = _load_strategy_cls(strat_ref)
    df = normalize(read_file(path), colmap)
    dropped = df.attrs.get("n_dropped", 0)
    summaries = []
    for (inst, day), g in df.groupby(["instrument", "trading_day"], sort=True):   # 一个文件可含多合约/多交易日
        if instruments and inst not in instruments:
            continue
        day = str(day)
        ticks = build_ticks(g.reset_index(drop=True), inst)
        if len(ticks) < 10:
            continue
        eng = Engine(cls(dict(params or {})), ticks, get_spec(inst, spec_over), get_rule(inst, rule_over), cfg, day)
        s = eng.run()
        s["rows_dropped_in_file"] = dropped      # 该文件清洗时丢弃的无效/集合竞价行数
        summaries.append(s)
        if out_dir:
            os.makedirs(os.path.join(out_dir, "trades"), exist_ok=True)
            pd.DataFrame([asdict(t) for t in eng.ex.trades]).to_csv(
                os.path.join(out_dir, "trades", f"{day}_{inst}.csv"), index=False)
            pd.DataFrame(eng.equity_curve, columns=["ts", "equity"]).to_csv(
                os.path.join(out_dir, "trades", f"{day}_{inst}_equity.csv"), index=False)
    return summaries


def run_backtest(strategy, data, params=None, cfg: SimConfig | None = None, colmap=None,
                 instruments=None, n_workers: int | None = None, out_dir: str | None = None,
                 spec_overrides=None, rule_overrides=None) -> pd.DataFrame:
    """strategy: 策略类 或 "module:Class" 字符串; data: 文件列表 / glob 模式 / 目录。"""
    cfg = cfg or SimConfig()
    if isinstance(data, str):
        data = sorted(glob.glob(os.path.join(data, "*"))) if os.path.isdir(data) else sorted(glob.glob(data))
    files = [f for f in data if f.lower().endswith((".csv", ".parquet", ".pq", ".xlsx", ".xls")) and not os.path.basename(f).startswith("~$")]
    if not files:
        raise FileNotFoundError("没有找到行情文件")
    jobs = [(f, strategy, params, asdict(cfg), colmap, instruments, spec_overrides, rule_overrides, out_dir)
            for f in files]
    n_workers = n_workers or os.cpu_count() or 1
    t0, rows = time.time(), []
    if n_workers == 1:
        for j in jobs: rows += _run_file(j)
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futs = {pool.submit(_run_file, j): j[0] for j in jobs}
            for k, fu in enumerate(as_completed(futs), 1):
                rows += fu.result()
                print(f"[{k}/{len(jobs)}] {os.path.basename(futs[fu])} done, {time.time()-t0:.1f}s", flush=True)
    res = pd.DataFrame(rows).sort_values(["trading_day", "instrument"]).reset_index(drop=True)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        res.to_csv(os.path.join(out_dir, "summary.csv"), index=False)
    return res
