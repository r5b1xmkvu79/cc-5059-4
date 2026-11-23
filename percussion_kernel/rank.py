"""候选排序：切分程度 + 重复结构，附多样性筛选。"""
from __future__ import annotations

from .model import Config


def metrical_weight(step: int, spb: int) -> int:
    """节拍层级权重：位置越强的拍点权重越高。"""
    pos = step % spb
    q = max(1, spb // 4)
    if pos == 0:
        return 4
    if pos % (spb // 2) == 0:
        return 3
    if pos % q == 0:
        return 2
    if pos % max(1, q // 2) == 0:
        return 1
    return 0


def syncopation_score(config: Config, assign: dict) -> float:
    """切分 = 弱位击打 + 强位空置。0..1，越高越切分。"""
    n = config.total_steps
    spb = config.steps_per_bar
    events = [(name, s) for name, steps in assign.items() for s in steps]
    if not events:
        return 0.0
    weak = sum(4 - metrical_weight(s, spb) for _, s in events) / (4.0 * len(events))
    strong_positions = [s for s in range(n) if metrical_weight(s, spb) >= 3]
    occupied = {s for _, s in events}
    empty_strong = (sum(1 for s in strong_positions if s not in occupied)
                    / max(1, len(strong_positions)))
    return round(0.7 * weak + 0.3 * empty_strong, 4)


def repetition_score(config: Config, assign: dict) -> float:
    """重复结构：跨小节同一（声部, 小节内位置）击打的复现比例。0..1。"""
    bars, spb = config.bars, config.steps_per_bar
    if bars <= 1:
        return 0.5
    total, repeated = 0, 0
    for steps in assign.values():
        by_bar = {}
        for s in steps:
            by_bar.setdefault(s // spb, set()).add(s % spb)
        for b in range(1, bars):
            cur, prev = by_bar.get(b, set()), by_bar.get(b - 1, set())
            total += len(cur)
            repeated += len(cur & prev)
    if total == 0:
        return 0.0
    return round(repeated / total, 4)


def _hamming(a: dict, b: dict) -> int:
    sa = {(k, s) for k, steps in a.items() for s in steps}
    sb = {(k, s) for k, steps in b.items() for s in steps}
    return len(sa ^ sb)


def rank_and_select(config: Config, pool: list) -> list:
    """pool: [(assign, repairs)] → [(assign, repairs, scores)]，按综合分降序并保持多样性。"""
    sw = config.sync_weight
    scored = []
    for assign, repairs in pool:
        sync = syncopation_score(config, assign)
        rep = repetition_score(config, assign)
        total = round(sw * sync + (1.0 - sw) * rep, 4)
        scored.append((assign, repairs,
                       {"syncopation": sync, "repetition": rep, "total": total}))
    scored.sort(key=lambda x: -x[2]["total"])

    picked = []
    for cand in scored:
        total_events = sum(len(s) for s in cand[0].values())
        min_dist = max(2, int(0.12 * total_events))
        if all(_hamming(cand[0], p[0]) >= min_dist for p in picked):
            picked.append(cand)
        if len(picked) >= config.n_candidates * 2:  # 留余量给时移终检淘汰
            break
    return picked
