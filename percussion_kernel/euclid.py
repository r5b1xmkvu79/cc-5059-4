"""欧几里得（均匀）节奏：Bjorklund 算法，作为候选分布的起点。"""
from __future__ import annotations


def bjorklund(k: int, n: int) -> list:
    """返回长度 n、含 k 个脉冲的 0/1 列表，脉冲尽量均匀分布。"""
    if n <= 0:
        return []
    k = max(0, min(k, n))
    if k == 0:
        return [0] * n
    if k == n:
        return [1] * n
    pattern = [[1] if i < k else [0] for i in range(n)]
    return _bjorklund(pattern)


def _bjorklund(groups: list) -> list:
    # 标准 Bjorklund：反复把尾部余数并入前面各组
    counts = {}
    for g in groups:
        counts[len(g)] = counts.get(len(g), 0) + 1
    if len(counts) <= 1:
        return [x for g in groups for x in g]
    # 找到最短组长度
    min_len = min(counts)
    if counts[min_len] <= 1:
        return [x for g in groups for x in g]
    front = [g for g in groups if len(g) != min_len]
    back = [g for g in groups if len(g) == min_len]
    merged = []
    for i, g in enumerate(front):
        if i < len(back):
            merged.append(g + back[i])
        else:
            merged.append(g)
    merged.extend(back[len(front):])
    return _bjorklund(merged)


def rotations(pattern: list) -> list:
    """所有去重旋转。"""
    n = len(pattern)
    seen = set()
    out = []
    for r in range(n):
        rot = tuple(pattern[r:] + pattern[:r])
        if rot not in seen:
            seen.add(rot)
            out.append(list(rot))
    return out


def steps_of(pattern: list) -> frozenset:
    return frozenset(i for i, v in enumerate(pattern) if v)
