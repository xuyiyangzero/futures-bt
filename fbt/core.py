"""基础类型: 方向/订单类型/订单/成交/Tick/仿真参数。时间统一为 int 毫秒时间戳。"""
from __future__ import annotations
from dataclasses import dataclass, field
import math

BUY, SELL = 1, -1
LIMIT, MARKET = "LIMIT", "MARKET"
GFD, FAK, FOK = "GFD", "FAK", "FOK"          # 当日有效 / 剩余撤销 / 全成或全撤
PENDING, NEW, PARTIAL, FILLED, CANCELLED, REJECTED = (
    "PENDING", "NEW", "PARTIAL", "FILLED", "CANCELLED", "REJECTED")
ACTIVE_STATUS = (PENDING, NEW, PARTIAL)


@dataclass(slots=True)
class Tick:
    ts: int
    instrument: str
    last: float
    volume: float            # 当日累计成交量
    oi: float
    bid_p: list
    bid_v: list
    ask_p: list
    ask_v: list
    upper: float = math.nan  # 涨停价
    lower: float = math.nan  # 跌停价
    turnover: float = math.nan  # 当日累计成交额(元), 用于计算两个tick之间的成交均价

    @property
    def bid1(self): return self.bid_p[0] if self.bid_p else math.nan
    @property
    def ask1(self): return self.ask_p[0] if self.ask_p else math.nan
    @property
    def mid(self):
        if self.bid_p and self.ask_p:
            return (self.bid_p[0] + self.ask_p[0]) / 2
        return self.last


@dataclass(slots=True)
class Order:
    oid: int
    instrument: str
    side: int
    price: float
    qty: int
    otype: str = LIMIT
    tif: str = GFD
    status: str = PENDING
    filled: int = 0
    avg_price: float = 0.0
    send_ts: int = 0
    reason: str = ""
    queue_ahead: float = 0.0     # 仅交易所侧使用: 排在我们前面的量(含自己更早的挂单)
    arrive_ts: int = 0           # 仅交易所侧使用: 订单到达交易所(开始排队)的时间
    eligible_idx: int = 0        # 仅交易所侧使用: 从第几个 Tick 的区间开始参与撮合

    @property
    def remaining(self): return self.qty - self.filled
    @property
    def active(self): return self.status in ACTIVE_STATUS


@dataclass(slots=True)
class Trade:
    oid: int
    instrument: str
    ts: int
    side: int
    price: float
    qty: int
    maker: bool
    fee: float


@dataclass
class SimConfig:
    # --- 延迟 (毫秒) ---
    submit_latency_ms: int = 5      # 策略发单 -> 到达交易所
    cancel_latency_ms: int = 5      # 策略撤单 -> 到达交易所
    md_latency_ms: int = 0          # 行情 交易所 -> 策略
    report_latency_ms: int = 0      # 回报 交易所 -> 策略
    # --- 策略驱动 ---
    timer_ms: int = 10              # on_timer 间隔, 0 关闭
    session_gap_ms: int = 60_000    # tick 间隔超过它视为休市, 休市期间不触发 on_timer
    # --- 排队假设 ---
    match_mode: str = "alloc"       # alloc=成交均价分配(新) / legacy=旧逻辑(主动方向+预算) / auto=该tick有成交额用alloc, 否则用legacy
    queue_pos_mode: str = "ratio"   # 初始排队位置: ratio(用 queue_ratio) / tail / head / mid / random(每笔订单在 [low,high] 内随机)
    queue_seed: int = 0             # random 模式的随机种子, 保证结果可复现
    queue_random_low: float = 0.0   # random 模式: 排队比例下限
    queue_random_high: float = 1.0  # random 模式: 排队比例上限
    queue_ratio: float = 1.0        # queue_pos_mode=ratio 时: 前面排队量 = ratio * 该价位显示量; 1=队尾, 0=队首, 0.5=中间
    cancel_model: str = "prob"      # 盘口量减少(撤单)对队列的影响: none / min / prob
    cancel_ahead_ratio: float = 1.0 # prob 模型下: 盘口净减少量中, 有多大比例是排在我们前面的人撤的 (0=都在我们后面, 1=按比例)
    frame_delay: int = 0            # 订单延后几帧才开始参与撮合: 0=到达该Tick后下个区间就参与(默认) / 1=保守口径, 多等一帧并按新帧重新定位队列
    queue_cancel_rate: float = 0.0  # δ: 每个区间前方排队量按该比例被撤掉 (固定位移, 与观测驱动的 cancel_model 可同时使用)
    queue_insert_lots: float = 0.0  # ξ: 每个区间有多少手新单插到我们前面 (固定位移)
    queue_insert_ratio: float = 0.0 # T1->T2 盘口某价位显示量净增加时, 假设其中多大比例的新单"插到我们前面"(0=严格时间优先, 新单都在后面)
    queue_latency_ref_ms: int = 0   # 0 关闭到达前队列估算；正值为参考快照间隔
    queue_latency_insert_lots: float = 0.0  # 每个参考间隔同价预计新增排在前方的量
    queue_latency_cancel_fraction: float = 0.0  # 每个参考间隔预计撤掉初始前方外部队列的比例
    partial_interval_mode: str = "full"  # 订单中途到达时, 到达前的成交是否算数: full=全算 / pro_rata=按剩余时间比例折算
    implied_depth_ratio: float = 0.0     # 全局额外叠加: 显示深度中假定为"隐含单"不可排队/不可吃的比例 (交易所级设置见 ExchangeRule)
    implied_trade_ratio: float = 0.0     # 全局额外叠加: 成交量中假定来自套利/隐含单、不消耗单腿排队的比例
    # --- 成交均价 -> 成交价位分配 (只有快照 tick 时, 用 成交额增量/成交量增量 反推区间成交价分布) ---
    avg_source: str = "turnover"    # turnover=用成交额算均价 / last=直接用最新价当均价(旧逻辑)
    turnover_includes_multiplier: bool = True  # 成交额是否已含合约乘数 (国内期货为 True: 均价=dTurnover/(dVolume*乘数))
    turnover_scale: float = 1.0     # 成交额单位换算 (如成交额以万元计则填 10000)
    avg_outlier_ticks: float = 50.0 # 算出的均价与最新价相差超过该 tick 数, 视为数据异常, 退回最新价
    book_ref: str = "prev"          # 用哪个盘口给成交分档: prev=区间开始的盘口(成交消耗的是它) / new=区间结束的盘口
    exact_level_tol_ticks: float = 0.05    # 均价与某档位价差小于该 tick 数 => 视为全部成交在该档
    inside_spread_mode: str = "last_anchored"  # 均价落在 买一~卖一 之间: linear_bid_ask / last_anchored / at_last / nearest
    between_levels_mode: str = "linear_two"    # 均价落在 一档~二档 之间: linear_two / with_last / at_inner / at_outer / at_last
    deep_levels_mode: str = "tilt_depth"       # 均价落在 二档以外的相邻档之间: linear_two / with_last / tilt_depth(β=1) / tilt_uniform(β=0) / tilt(用 alloc_beta)
    beyond_depth_mode: str = "avg"             # 均价超出可见最深一档: avg / deepest / skip / extend(用终点帧档位扩展后重解, 仍不行则降级) / last / mid / sweep(按档位顺序吞噬) / depth_weighted(按各档显示量摊)
    alloc_beta: float = 1.0         # 多档分配的先验权重 u_i = q_i^β: β=0 均匀 / β=1 按显示量 / β>1 更偏向厚档 (deep_levels_mode="tilt" 时生效)
    alloc_round: str = "none"       # none / largest_remainder: 把各价位成交量取整成整数手(最大余数法, 总量守恒)
    direction_check: str = "off"    # 方向一致性校验: off / flag(只统计) / degrade(矛盾帧降级为单点成交价)
    degrade_mode: str = "last"      # 降级(越界无法求解/方向矛盾)时的单点成交价: last=最新价 / mid=区间中价
    diag_all_ticks: bool = False    # True: 没有挂单时也计算分配并统计(场景/降级/主动买卖占比), 用于评估整个市场而非只看我方挂单期间
    through_fill_ratio: float = 1.0 # 成交价穿过我方价位时, 我方剩余量按该比例成交 (1=全成)
    # --- 费用 ---
    fee_rate: float = 0.0           # 按成交金额比例
    fee_per_lot: float = 0.0        # 每手固定费用
    # --- 其它 ---
    equity_sample_ms: int = 60_000
