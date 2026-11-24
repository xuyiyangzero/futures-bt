"""把大的 xlsx/csv 行情转成 parquet (读取快几十倍)。用法:
    python tools/convert_to_parquet.py 输入目录或文件 输出目录
需要: pip install pyarrow
"""
import sys, os, glob
import pandas as pd

src, dst = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.join(src, "*"))) if os.path.isdir(src) else [src]
os.makedirs(dst, exist_ok=True)
for f in files:
    if f.lower().endswith((".xlsx", ".xls")): df = pd.read_excel(f)
    elif f.lower().endswith(".csv"): df = pd.read_csv(f)
    else: continue
    out = os.path.join(dst, os.path.splitext(os.path.basename(f))[0] + ".parquet")
    df.to_parquet(out, index=False)
    print(f, "->", out, len(df), "rows")
