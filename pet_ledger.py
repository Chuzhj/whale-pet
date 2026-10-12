# -*- coding: utf-8 -*-
"""小鲸鱼记账：**靠余额观测推算"今日已用"**，不需要任何交易/用量接口。

思路借自 dsh-whale-widget（思路，不是代码），它的三条判据都很硬，值得照搬：

1. **下降 = 消费，上升 = 充值/赠金**。余额上升**不冲掉**已经记下的消费
   （否则充值当天"今日已用"会被抹成 0），而是单独累计成 credit。
2. **乱序 / 重复的观测直接丢掉**（`at <= last_at`），否则一次网络重试能让数字倒跳。
3. **部分数据要如实标记**：记账只发生在"程序开着 + 余额取得回来"的时候，
   当天第一个观测如果离 00:00 超过 10 分钟，这段就是没观测到的 ——
   界面上必须说"这是部分数据"，而不是让人以为今天只花了这么点。

钱一律按 **定点整数**（1e-8 元为单位）存和算：浮点连加会累积误差，
而这个数字是要显示成"今天花了多少钱"的。

模块定位：**纯函数 + 一个 JSON 文件**，不碰 Qt、不联网、不读时钟以外的东西，
所以记账逻辑可以在自检里直接喂样本序列验证。
"""
import json
from datetime import datetime, timedelta, timezone

from pet_constants import (
    BALANCE_LEDGER_VERSION, BALANCE_PARTIAL_GAP_SEC, BALANCE_SCALE,
    HOLIDAY_VALLEY, HOLIDAY_VALLEY_FROM, PEAK_HOURS, WEEKEND_VALLEY_FROM,
)
from pet_store import _atomic_write_text, _read_text, data_dir

BEIJING = timezone(timedelta(hours=8))
DAY_FMT = "%Y-%m-%d"


# ---------------------------------------------------------------- 金额与时间
def to_units(value):
    """元 → 定点整数（1e-8 元）。转不了就抛，让调用方决定怎么兜。"""
    return int(round(float(value) * BALANCE_SCALE))


def money(units):
    """定点整数 → 元（给界面用，两位小数由界面自己格式化）。"""
    return int(units) / BALANCE_SCALE


def beijing_day(at=None):
    """某个时间戳落在北京时间的哪一天。"""
    at = datetime.now().timestamp() if at is None else float(at)
    return datetime.fromtimestamp(at, BEIJING).strftime(DAY_FMT)


def _day_bounds(day):
    """北京时间某一天的 [起, 止) 时间戳。北京 00:00 = UTC 前一天 16:00。"""
    start = datetime.strptime(day, DAY_FMT).replace(tzinfo=BEIJING)
    return start.timestamp(), start.timestamp() + 86400.0


def ledger_path(config_file=None):
    from pet_constants import BALANCE_LEDGER_NAME
    return data_dir(config_file) / BALANCE_LEDGER_NAME


# ---------------------------------------------------------------- 读写
def empty_ledger():
    return {"version": BALANCE_LEDGER_VERSION, "active": "", "books": {},
            "last_at": 0}


def load(config_file=None):
    """读记账本；文件不在/坏了就当空的（绝不能因为账本坏了起不来）。"""
    path = ledger_path(config_file)
    try:
        raw = json.loads(_read_text(path))
    except Exception:
        return empty_ledger()
    if not isinstance(raw, dict) or raw.get("version") != BALANCE_LEDGER_VERSION:
        return empty_ledger()
    if not isinstance(raw.get("books"), dict):
        return empty_ledger()
    return raw


def save(config_file, ledger):
    """原子写回。返回是否成功（写不进去只是这次白记，不抛）。"""
    return _atomic_write_text(
        ledger_path(config_file),
        json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")


def _book(ledger, currency):
    books = ledger.setdefault("books", {})
    book = books.get(currency)
    if not isinstance(book, dict) or not isinstance(book.get("days"), dict):
        book = {"currency": currency, "days": {}, "last_at": 0}
        books[currency] = book
    return book


def _new_row(at, units):
    return {"first_at": at, "last_at": at, "opening": units, "last": units,
            "debit": 0, "credit": 0, "correction": None}


# ---------------------------------------------------------------- 观测
def observe(ledger, balance, at=None, currency="CNY"):
    """记一次余额观测，返回该日汇总（dict）。

    - `balance` 是**总余额**（充值 + 赠金），单位元；
    - 同一时间或更早的样本直接忽略（返回当前汇总，不改账）。
    """
    at = datetime.now().timestamp() if at is None else float(at)
    units = to_units(balance)
    currency = str(currency or "CNY").upper()
    if not currency:
        currency = "CNY"
    book = _book(ledger, currency)
    if float(book.get("last_at") or 0) >= at:
        return day_summary(ledger, beijing_day(at), now=at)
    ledger["active"] = currency
    day = beijing_day(at)
    row = book["days"].get(day)
    if row is None:
        row = book["days"][day] = _new_row(at, units)
    else:
        delta = int(row["last"]) - units
        if delta > 0:
            row["debit"] = int(row["debit"]) + delta      # 余额降了 = 花了
        elif delta < 0:
            row["credit"] = int(row["credit"]) - delta    # 余额升了 = 充值/赠金
        row["last"] = units
        row["last_at"] = at
    book["last_at"] = at
    ledger["last_at"] = at
    return day_summary(ledger, day, now=at)


def _amount_units(row):
    """这一天的"消费"：有校正就按校正算，否则就是观测到的下降累计。"""
    corr = row.get("correction")
    if isinstance(corr, dict):
        return (int(row["opening"]) + int(corr.get("credits", 0))
                - int(corr.get("other_debits", 0)) - int(row["last"]))
    return int(row["debit"])


def day_summary(ledger, day, now=None):
    """某天的汇总。没有观测记录返回 None。"""
    currency = ledger.get("active") or ""
    book = (ledger.get("books") or {}).get(currency)
    row = (book or {}).get("days", {}).get(day) if book else None
    if not row:
        return None
    start, end = _day_bounds(day)
    first, last = float(row["first_at"]), float(row["last_at"])
    now = datetime.now().timestamp() if now is None else float(now)
    lead_gap = max(0.0, first - start)
    tail_gap = max(0.0, end - last) if now >= end else 0.0
    partial = (lead_gap > BALANCE_PARTIAL_GAP_SEC
               or tail_gap > BALANCE_PARTIAL_GAP_SEC)
    corr = row.get("correction")
    amount = _amount_units(row)
    return {
        "day": day,
        "currency": book.get("currency") or currency,
        "amount": money(amount),
        "amount_units": amount,
        "opening": money(row["opening"]),
        "current": money(row["last"]),
        "decrease": money(row["debit"]),
        "increase": money(row["credit"]),
        "first_at": first,
        "last_at": last,
        "partial": partial,
        "lead_gap_sec": lead_gap,
        "tail_gap_sec": tail_gap,
        "corrected": bool(isinstance(corr, dict)),
        "correction": corr,
        "source": ("balance-corrected" if isinstance(corr, dict)
                   else "balance-observed"),
    }


def recent_days(ledger, count=7, now=None):
    """最近 count 天（含今天）的 [(day, summary or None)]，从早到晚。"""
    now = datetime.now().timestamp() if now is None else float(now)
    today = beijing_day(now)
    base = datetime.strptime(today, DAY_FMT).replace(tzinfo=BEIJING)
    out = []
    for i in range(count - 1, -1, -1):
        day = (base - timedelta(days=i)).strftime(DAY_FMT)
        out.append((day, day_summary(ledger, day, now=now)))
    return out


def reconcile(ledger, day, credits=0, other_debits=0, now=None):
    """余额校正：按"这段区间里实际到账了多少、还扣过什么"重算该日消费。

    和挂件同一个公式：`消费 = 期初余额 + 累计到账 - 其它扣减 - 期末余额`。
    什么时候需要它：主人中途充值、或者余额被平台调整过 ——
    光看"下降累计"会把充值那一段的消费算丢。
    """
    now = datetime.now().timestamp() if now is None else float(now)
    currency = ledger.get("active") or ""
    book = (ledger.get("books") or {}).get(currency)
    row = (book or {}).get("days", {}).get(day) if book else None
    if not row:
        return None
    credits_units = to_units(credits or 0)
    other_units = to_units(other_debits or 0)
    amount = (int(row["opening"]) + credits_units - other_units
              - int(row["last"]))
    if amount < 0:
        # 校正后变成"负数消费" = 填错了，宁可拒绝也别记一笔假账
        return None
    row["correction"] = {"credits": credits_units, "other_debits": other_units,
                         "amount": amount, "at": now}
    return day_summary(ledger, day, now=now)


# ---------------------------------------------------------------- 峰谷
def _at_beijing_ts(day):
    """北京时间某天 00:00 的 epoch 秒。"""
    return datetime.strptime(day, DAY_FMT).replace(tzinfo=BEIJING).timestamp()


def peak_reason(at=None):
    """现在是不是高峰，以及**为什么**。返回 (是否高峰, 一句人话)。

    判据顺序和官方脚注一致：先看周末（含调休上班的周六日），再看法定节假日，
    最后才是时段表。任一条命中就是谷价。
    """
    at = datetime.now().timestamp() if at is None else float(at)
    local = datetime.fromtimestamp(at, BEIJING)
    if at >= _at_beijing_ts(WEEKEND_VALLEY_FROM) and local.weekday() >= 5:
        return False, "周末全天谷价"
    if (at >= _at_beijing_ts(HOLIDAY_VALLEY_FROM)
            and local.strftime(DAY_FMT) in HOLIDAY_VALLEY):
        return False, "法定节假日全天谷价"
    for start, end in PEAK_HOURS:
        if start <= local.hour < end:
            return True, "工作日高峰时段"
    return False, "非高峰时段"


def is_peak(at=None):
    return peak_reason(at)[0]


def next_peak_change(at=None):
    """下一个峰谷切换时刻；找不到（理论上不会）返回 None。

    只需扫**北京时间的 0/9/12/14/18 点**这些边界 —— 时段表的边界就这几个，
    再往后扫 12 天（足够跨过最长的春节连休）。判据与 peak_reason 完全同源，
    否则会出现"面板说 3 分钟后切换、3 分钟后却还是原样"。
    """
    at = datetime.now().timestamp() if at is None else float(at)
    current = is_peak(at)
    base = datetime.fromtimestamp(at, BEIJING).replace(
        hour=0, minute=0, second=0, microsecond=0)
    for day in range(0, 13):
        for edge in (0, 9, 12, 14, 18):
            cand = (base + timedelta(days=day, hours=edge)).timestamp()
            if cand <= at + 1:
                continue
            if is_peak(cand) != current:
                return cand
    return None


def peak_state(now=None):
    """现在是峰价还是谷价 + 为什么 + 下一次切换还有多久。"""
    now = datetime.now().timestamp() if now is None else float(now)
    peak, why = peak_reason(now)
    nxt = next_peak_change(now)
    remain = max(0, int(nxt - now)) if nxt else 0
    return {
        "peak": peak,
        "offpeak": not peak,
        "label": "峰价时段" if peak else "谷价时段",
        "why": why,
        # 当前是谷价 → 下一次切换是"回到峰价"，反之是"进入谷价"
        "next_label": "进入谷价" if peak else "回到峰价",
        "next_at": nxt,
        "remain_sec": remain,
        "remain_text": ("还有 " + _human_remain(remain)) if nxt else "暂无切换",
    }


def _human_remain(sec):
    hours, rest = divmod(int(sec), 3600)
    minutes = rest // 60
    if hours and minutes:
        return "%d 小时 %d 分" % (hours, minutes)
    if hours:
        return "%d 小时" % hours
    return "%d 分" % max(1, minutes)
