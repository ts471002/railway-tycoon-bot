"""旅程剩余站点求解器（与游戏内右侧信息面板对齐）。

游戏在每一站都会显示两块信息：

    未来3站将有食铺站数量最多
    旅程剩余站点：酒庄x5 食铺x5 商行x5

第二行是**硬约束**：它告诉你剩下的这 N 站里，三种店铺各还有几个。
有了"每种各几个"这个多重集，再加上"未来 3 站的相对多少"这条提示，
就能精确算出下一站三种类型的概率 —— 不是猜，是把所有符合约束的排列
按数量加权统计。

算法：
    剩余站数 L = 三种剩余数量之和。真正影响"下一站"的只有开头那几个位置，
    所以只需枚举前 k 个位置的所有 3^k 种填法（k = 提示窗口长度，通常 3），
    每种填法后面剩下的 L-k 个位置有多少种**合法**排法就作为它的权重，
    把权重加起来，就是每一站各类型的概率。k≤4 时最多 81 种填法，瞬时完成。

「合法」= 游戏规则：不会连续出现 3 个相同的站点。
所以尾部排法数不是简单的多重集排列 (L-k)!/(a!b!c!)，而是带着
"最后两站是什么"这个状态的动态规划计数（见 `_ways`）。这条规则会实打实
改变概率：比如剩 商行x2 且前面已经连着两个商行，那这两站之后不可能再是商行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from math import factorial
from typing import Dict, List, Optional, Sequence

WINERY = "酒庄"
FOOD = "食铺"
TRADE = "商行"
TYPES = (WINERY, FOOD, TRADE)

KIND_MOST = "most"
KIND_SAME = "same"
KIND_LEAST = "least"
KIND_NONE = "none"


@dataclass(frozen=True)
class Hint:
    """相对「下一站」的窗口提示。

    start: 窗口起点，0 = 从下一站开始算；**可以为负**
           负值表示这个窗口有一部分落在已经走过的站上。
           例：上一站给的「未来3站」，你走了一站之后它就变成 start=-1，
           覆盖「刚走过的站 + 接下来两站」。
    span:  窗口长度，一般 3
    kind:  most / same / least / none
    target: kind==most 或 least 时指向的类型
    """

    start: int
    span: int
    kind: str
    target: Optional[str] = None

    def check(self, window: Sequence[str]) -> bool:
        w = list(window)
        if len(w) < self.span:
            return True  # 剩余站数不足这个窗口，无法判定，不约束
        if self.kind == KIND_SAME:
            return len(set(w)) == len(w)
        if self.kind == KIND_MOST and self.target in TYPES:
            cnt = w.count(self.target)
            others = max(w.count(t) for t in TYPES if t != self.target)
            # 窗口只有 3 个位置：要做到"最多"且不和"各站相同"混淆，至少 2 个
            return cnt >= 2 and cnt >= others
        if self.kind == KIND_LEAST and self.target in TYPES:
            cnt = w.count(self.target)
            others = min(w.count(t) for t in TYPES if t != self.target)
            return cnt <= 1 and cnt <= others
        return True

    def describe(self) -> str:
        where = "未来" if self.start >= 0 else "从已走过的站算起"
        if self.kind == KIND_SAME:
            return f"{where}{self.span}站各类型各 1 个"
        if self.kind == KIND_MOST and self.target:
            return f"{where}{self.span}站中{self.target}最多"
        if self.kind == KIND_LEAST and self.target:
            return f"{where}{self.span}站中{self.target}最少"
        return "提示未能解析（不产生约束）"


def _windows_ok(past: Sequence[str], prefix: Sequence[str], hints: Sequence[Hint]) -> bool:
    """检查当前已填的前缀是否满足所有提示窗口。

    past 是「已经走过的站」的类型，按时间顺序（最老的在前、最近的在最后），
    用来支撑 start < 0 的窗口。调用方必须保证 past 长度够（不够的提示应提前剔除）。
    """
    if not hints:
        return True
    seq = list(past)
    seq.extend(prefix)
    n = len(past)
    for h in hints:
        s = n + h.start
        if s < 0:
            continue  # 历史不足：保守放行（调用方应已剔除这类提示）
        if not h.check(seq[s:s + h.span]):
            return False
    return True


def past_needed(hints: Sequence[Hint]) -> int:
    """这些提示一共需要回溯几站的历史。"""
    return max([0] + [-h.start for h in hints if h.start < 0])


# ------------------------------------------------------------------ 解析
def parse_counts(text: str) -> Dict[str, int]:
    """从「旅程剩余站点：酒庄x5 食铺x5 商行x5」里抠出三种各剩几个。"""
    if not text:
        return {}
    t = text.replace(" ", "").replace("\n", "")
    out: Dict[str, int] = {}
    for ty in TYPES:
        m = re.search(re.escape(ty) + r"[xX×*＊]?\s*(\d{1,2})", t)
        if m:
            out[ty] = int(m.group(1))
    return out if len(out) == len(TYPES) else {}


def parse_remaining_stations(text: str) -> Optional[int]:
    """从「剩余15站」或「旅程剩余站点：15站」里抠出剩余站数。"""
    if not text:
        return None
    t = text.replace(" ", "")
    m = re.search(r"剩余站点[：:]\s*(\d{1,2})", t)
    if m:
        return int(m.group(1))
    m = re.search(r"剩余\s*(\d{1,2})\s*站", t)
    return int(m.group(1)) if m else None


def parse_hint(text: str) -> Optional[Hint]:
    """把提示句解析成 Hint（窗口从下一站开始）。"""
    if not text:
        return None
    t = text.replace(" ", "").replace("\n", "")
    if "未来" not in t and "接下来" not in t and "之后" not in t and "站" not in t:
        return None

    span = 3
    m = re.search(r"(?:未来|接下来|之后)\s*(\d)\s*站", t)
    if m:
        span = int(m.group(1))
    if span <= 0:
        span = 3

    if "相同" in t:
        return Hint(0, span, KIND_SAME)
    if "最少" in t:
        for ty in TYPES:
            if ty in t:
                return Hint(0, span, KIND_LEAST, ty)
        return Hint(0, span, KIND_NONE)
    if "最多" in t:
        for ty in TYPES:
            if ty in t:
                return Hint(0, span, KIND_MOST, ty)
        return Hint(0, span, KIND_NONE)
    return None


# ------------------------------------------------------------------ 求解
def _multinom(n: int, counts: Dict[str, int]) -> int:
    """把 n 个位置按 counts 分配的多重集排列数（不考虑「不能三连」）。"""
    if n < 0:
        return 0
    r = factorial(n)
    for v in counts.values():
        if v < 0:
            return 0
        r //= factorial(v)
    return r


@lru_cache(maxsize=None)
def _ways(counts: tuple, p1: int, p2: int) -> int:
    """剩余 counts（各类型的个数）有多少种排法，且不出现连续 3 个相同站点。

    「不三连」是实际玩下来观察到的规律，不是游戏里明写的规则——如果哪天游戏改了，
    这里的前提就不成立，概率会偏，所以只把它当成一条经验约束。

    p1 / p2 是**已经排好的最后两站**的类型下标（-1 表示还没有）。
    例：前面已连着两个商行（p1=p2=2），那么下一站不能再是商行。
    """
    if counts[0] + counts[1] + counts[2] == 0:
        return 1
    total = 0
    for k in range(3):
        if counts[k] <= 0:
            continue
        if p1 == k and p2 == k:  # 再加一个就三连了
            continue
        rest = list(counts)
        rest[k] -= 1
        total += _ways(tuple(rest), p2, k)
    return total


def _last2(seq: Sequence[str]) -> tuple:
    """序列最后两站转成 _ways 需要的 (p1, p2) 下标。"""
    p1 = TYPES.index(seq[-2]) if len(seq) >= 2 else -1
    p2 = TYPES.index(seq[-1]) if len(seq) >= 1 else -1
    return p1, p2


def prefix_probs(
    counts: Dict[str, int],
    hints: Sequence[Hint] = (),
    depth: int = 3,
    past: Sequence[str] = (),
) -> Dict[int, Dict[str, float]]:
    """算出接下来第 1..depth 站各类型的概率（1-based 位置）。

    counts 是「剩余站点」三种各几个。返回 {1: {类型: 概率}, 2: ...}。
    past 是已经走过的站的类型（按时间顺序，最近的在最后），用来支撑 start<0 的提示。

    约束按可信度依次尝试，第一个能算出结果的为准：
        1) 提示 + 不能三连      ← 正常情况
        2) 只有「不能三连」      ← 提示互相矛盾（多半某帧 OCR 认错）
        3) 提示 + 允许三连      ← 剩余数量读错，导致「不能三连」根本排不出来
        4) 只按剩余数量         ← 只剩硬数量
        5) 均分                 ← 连数量都读错了
    """
    for hs, no_triple in ((tuple(hints), True), ((), True),
                          (tuple(hints), False), ((), False)):
        out = _prefix_probs(counts, hs, depth, past, no_triple)
        if out is not None:
            return out
    total_n = sum(counts.get(t, 0) for t in TYPES)
    return {i + 1: {t: 1 / 3 for t in TYPES} for i in range(min(depth, max(total_n, 1)))}


def _prefix_probs(
    counts: Dict[str, int],
    hints: Sequence[Hint],
    depth: int,
    past: Sequence[str] = (),
    no_triple: bool = True,
) -> Optional[Dict[int, Dict[str, float]]]:
    total_n = sum(counts.get(t, 0) for t in TYPES)
    if total_n <= 0:
        return None
    k = max([depth] + [h.start + h.span for h in hints])
    k = max(1, min(k, total_n))

    acc: List[Dict[str, int]] = [{t: 0 for t in TYPES} for _ in range(k)]
    grand = 0

    remaining = dict(counts)
    seq: List[str] = list(past)  # 历史 + 正在枚举的前缀

    def rec(pos: int) -> None:
        nonlocal grand
        if pos == k:
            if no_triple:
                w = _ways(tuple(remaining[t] for t in TYPES), *_last2(seq))
            else:
                w = _multinom(total_n - k, remaining)
            if w <= 0:
                return
            grand += w
            for i in range(k):
                acc[i][seq[-k + i]] += w
            return
        for t in TYPES:
            if remaining.get(t, 0) <= 0:
                continue
            if no_triple and len(seq) >= 2 and seq[-1] == t and seq[-2] == t:
                continue  # 不能连续三站同类型
            remaining[t] -= 1
            seq.append(t)
            if _windows_ok(past, seq[len(past):], hints):
                rec(pos + 1)
            seq.pop()
            remaining[t] += 1

    rec(0)

    if grand == 0:
        return None
    return {
        i + 1: {t: acc[i][t] / grand for t in TYPES}
        for i in range(min(depth, total_n))
    }


def next_station(
    counts: Dict[str, int],
    hints: Sequence[Hint] = (),
) -> Dict[str, float]:
    """便捷函数：下一站的三类概率。"""
    p = prefix_probs(counts, hints, depth=1)
    return p.get(1, {t: 1 / 3 for t in TYPES})


def consistent(
    counts: Dict[str, int],
    hints: Sequence[Hint],
    depth: int = 3,
    past: Sequence[str] = (),
) -> bool:
    """现有提示是否还能凑出至少一种合法排列（用于判断某帧读数是否自相矛盾）。"""
    for no_triple in (True, False):
        if _prefix_probs(counts, hints, depth, past, no_triple) is not None:
            return True
    return False


def describe_counts(counts: Dict[str, int]) -> str:
    return "  ".join(f"{t}x{counts.get(t, 0)}" for t in TYPES)
