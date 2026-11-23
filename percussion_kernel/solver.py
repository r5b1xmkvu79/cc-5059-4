"""约束求解器：欧几里得种子 + 构造式修复 + 深度优先搜索 + 最小锁定集合。"""
from __future__ import annotations

import random
from functools import lru_cache

from .euclid import bjorklund, rotations, steps_of
from .model import ACCENT_LOCKED, ACCENT_SOFT, Config, VoiceSpec


class SolveFailure(Exception):
    """求解失败：携带最小锁定集合（unsat core），绝不静默降低要求。"""

    def __init__(self, core, message, kind="unsat"):
        super().__init__(message)
        self.core = core          # list[dict]: {id, text}
        self.kind = kind          # 'unsat' | 'budget'
        self.message = message


def cdist(a: int, b: int, n: int) -> int:
    """循环步距（乐句循环播放，首尾相邻）。"""
    d = abs(a - b) % n
    return min(d, n - d)


def locked_steps(v: VoiceSpec) -> frozenset:
    return frozenset(s for s, lv in v.accents.items() if lv == ACCENT_LOCKED)


def soft_steps(v: VoiceSpec) -> frozenset:
    return frozenset(s for s, lv in v.accents.items() if lv == ACCENT_SOFT)


# ---------------------------------------------------------------- 单声部精确可行性

def voice_feasible(n: int, k: int, g: int, required) -> bool:
    """在 n 步循环上恰好放 k 击、任意两击循环距离 >= g、且包含 required 全部位置。

    精确判定（动态规划），用于硬性失败检测与最小锁定集合求值。
    """
    required = set(required)
    if len(required) > k:
        return False
    if k > n // g:
        return False
    rs = sorted(required)
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            if cdist(rs[i], rs[j], n) < g:
                return False
    if k == 0:
        return not required
    if not required:
        return True  # 无必含点：k <= n//g 必可均布

    req_rel_all = []
    for anchor in rs:
        req_rel_all.append(sorted({(r - anchor) % n for r in required}))

    for req in req_rel_all:
        reqset = set(req)

        @lru_cache(maxsize=None)
        def f(i, placed, last):
            # 从位置 0（锚点，已选）出发线性 DP；环绕间隔 = n - last 必须 >= g
            if placed > k:
                return False
            if i == n:
                return placed == k and (n - last) >= g
            if i in reqset:
                return (i - last) >= g and f(i + 1, placed + 1, i)
            if f(i + 1, placed, last):
                return True
            return (i - last) >= g and f(i + 1, placed + 1, i)

        if f(1, 1, 0):
            return True
    return False


def _random_fill(n, k, g, required, rng, tries=60):
    """随机贪心填充（假定 voice_feasible 已判定可行）。"""
    required = set(required)
    for _ in range(tries):
        placed = set(required)
        ok = True
        while len(placed) < k:
            free = [s for s in range(n)
                    if s not in placed and all(cdist(s, t, n) >= g for t in placed)]
            if not free:
                ok = False
                break
            placed.add(rng.choice(free))
        if ok and len(placed) == k:
            return frozenset(placed)
    return None


def gen_patterns(v: VoiceSpec, n: int, rng, limit=240) -> list:
    """为单个声部生成可行步集合列表：欧几里得旋转 + 随机填充，去重截断。"""
    k, g = v.density, v.min_interval
    locked = locked_steps(v)
    pats = {}

    def consider(steps, origin):
        fs = frozenset(steps)
        if fs in pats:
            return
        if len(fs) != k or not locked <= fs:
            return
        ss = sorted(fs)
        if any(cdist(ss[i], ss[j], n) < g
               for i in range(len(ss)) for j in range(i + 1, len(ss))):
            return
        pats[fs] = origin

    # 1) 欧几里得种子：所有覆盖锁定点的合法旋转
    if k > 0:
        for rot in rotations(bjorklund(k, n)):
            consider(steps_of(rot), "euclid")
    # 2) 随机填充补足多样性
    attempts = 0
    while len(pats) < limit and attempts < limit * 30:
        attempts += 1
        fs = _random_fill(n, k, g, locked, rng)
        if fs is not None:
            consider(fs, "random")
    # 软重音命中数降序，利于搜索先遇到贴合偏好的模式
    soft = soft_steps(v)
    ordered = sorted(pats.items(), key=lambda kv: (-len(kv[0] & soft), sorted(kv[0])))
    return [fs for fs, _ in ordered[:limit]]


# ---------------------------------------------------------------- 跨声部约束检查

def _relation_ok(rel, steps_a: frozenset, steps_b: frozenset, n: int) -> bool:
    if rel.kind == "exclude":
        return all(cdist(sa, sb, n) > rel.window for sa in steps_a for sb in steps_b)
    if rel.kind == "unison":
        return all(sa in steps_b for sa in steps_a)
    if rel.kind == "answer":
        return all((sa + rel.offset) % n in steps_b for sa in steps_a)
    raise ValueError(f"未知关系: {rel.kind}")


def _hand_ok(pairs, n: int, min_gap: int) -> bool:
    """pairs: [(step, voice)]，同步允许（双手各一），其余循环距离必须 >= min_gap。"""
    ss = sorted({s for s, _ in pairs})
    for i in range(len(ss)):
        for j in range(i + 1, len(ss)):
            if cdist(ss[i], ss[j], n) < min_gap:
                return False
    return True


def full_check(assign: dict, config: Config) -> bool:
    """assign: voice -> frozenset(steps)。完整硬约束校验。"""
    n = config.total_steps
    by_name = {v.name: v for v in config.voices if v.enabled}
    for name, steps in assign.items():
        v = by_name[name]
        if len(steps) != v.density:
            return False
        if not locked_steps(v) <= steps:
            return False
        ss = sorted(steps)
        if any(cdist(ss[i], ss[j], n) < v.min_interval
               for i in range(len(ss)) for j in range(i + 1, len(ss))):
            return False
    for rel in config.relations:
        if rel.a in assign and rel.b in assign:
            if not _relation_ok(rel, assign[rel.a], assign[rel.b], n):
                return False
    hand_pairs = [(s, name) for name, steps in assign.items()
                  if by_name[name].hand for s in steps]
    return _hand_ok(hand_pairs, n, config.hand_min_interval)


# ---------------------------------------------------------------- 阶段 A：构造式修复

def constructive(config: Config, rng):
    """构造式播种：锁定重音先落位，自由击打按欧几里得模板引导填充。

    固定强拍（锁定）与双手最短间隔互斥导致无法落位时，
    回退最近一次自由击打（记入 tabu 防止循环），锁定重音绝不动；
    锁定与锁定直接冲突则记录，供最小锁定集合解释。
    返回 (assign|None, repairs, locked_conflicts)。
    """
    n = config.total_steps
    voices = sorted((v for v in config.voices if v.enabled),
                    key=lambda v: (-len(locked_steps(v)), -v.density))
    by_name = {v.name: v for v in voices}
    placed = {v.name: set(locked_steps(v)) for v in voices}
    history = []  # (voice, step, locked?) 落位顺序，双手组回退依据
    for v in voices:
        for s in sorted(locked_steps(v)):
            history.append((v.name, s, True))
    repairs, locked_conflicts = [], []
    tabu = {}  # (voice, step) -> 加入时的轮次，过期可重试

    def hand_conflicts(vname, s):
        if not by_name[vname].hand:
            return []
        return [(vn, st, lk) for (vn, st, lk) in history
                if by_name[vn].hand and not (vn == vname and st == s)
                and st != s and cdist(s, st, n) < config.hand_min_interval]

    def guided_slots(v, round_no):
        k = v.density
        cands = []
        if k > 0:
            best, best_cov = None, -1
            for rot in rotations(bjorklund(k, n)):
                st = steps_of(rot)
                cov = len(st & locked_steps(v))
                if cov > best_cov:
                    best, best_cov = st, cov
            if best is not None:
                cands.extend(sorted(best))
        rest = [s for s in range(n) if s not in cands]
        rng.shuffle(rest)
        cands.extend(rest)
        return [s for s in cands
                if tabu.get((v.name, s), -99) + 5 < round_no]

    def rollback(vname, due_to_locked):
        """回退最近一次自由击打（双手组内，优先其他声部）。"""
        free = [h for h in reversed(history)
                if not h[2] and by_name[h[0]].hand]
        if not free:
            return None
        victim = next((h for h in free if h[0] != vname), free[0])
        placed[victim[0]].discard(victim[1])
        history.remove(victim)
        return victim

    for round_no in range(40):
        for v in voices:
            need = v.density - len(placed[v.name])
            if need <= 0:
                continue
            blocked_by_locked = False
            blocked_by_free = False
            for s in guided_slots(v, round_no):
                if need == 0:
                    break
                if s in placed[v.name]:
                    continue
                if any(cdist(s, t, n) < v.min_interval for t in placed[v.name]):
                    continue
                conf = hand_conflicts(v.name, s)
                if conf:
                    if any(lk for _, _, lk in conf):
                        blocked_by_locked = True
                    else:
                        blocked_by_free = True
                    continue
                placed[v.name].add(s)
                history.append((v.name, s, False))
                need -= 1
            if need > 0 and v.hand and (blocked_by_locked or blocked_by_free):
                victim = rollback(v.name, blocked_by_locked)
                if victim is None:
                    if blocked_by_locked:
                        locked_conflicts.append(
                            f"{v.name} 在双手间隔约束下无法避开锁定强拍完成密度")
                else:
                    tabu[(victim[0], victim[1])] = round_no
                    if blocked_by_locked:
                        repairs.append(
                            f"固定强拍与双手最短间隔冲突：回退自由击打 "
                            f"{victim[0]}@{victim[1]}，保留全部锁定重音")
                    else:
                        repairs.append(
                            f"双手组击打过密：回退自由击打 {victim[0]}@{victim[1]} 后重排")
        if all(len(placed[v.name]) == v.density for v in voices):
            assign = {name: frozenset(st) for name, st in placed.items()}
            if full_check(assign, config):
                return assign, repairs, locked_conflicts
    return None, repairs, locked_conflicts


# ---------------------------------------------------------------- 阶段 B：DFS 搜索

def search(config: Config, pattern_lists: dict, rng, max_solutions=160, node_budget=400000):
    voices = sorted((v for v in config.voices if v.enabled),
                    key=lambda v: len(pattern_lists[v.name]))
    by_name = {v.name: v for v in voices}
    n = config.total_steps
    rels = config.relations
    sols, nodes = [], [0]
    assign = {}

    def consistent(name, steps):
        for rel in rels:
            if rel.a == name and rel.b in assign:
                if not _relation_ok(rel, steps, assign[rel.b], n):
                    return False
            elif rel.b == name and rel.a in assign:
                if not _relation_ok(rel, assign[rel.a], steps, n):
                    return False
        if by_name[name].hand or any(by_name[x].hand for x in assign):
            pairs = [(s, name) for s in steps if by_name[name].hand]
            for x, st in assign.items():
                if by_name[x].hand:
                    pairs += [(s, x) for s in st]
            if not _hand_ok(pairs, n, config.hand_min_interval):
                return False
        return True

    order_lists = {}
    for v in voices:
        lst = list(pattern_lists[v.name])
        rng.shuffle(lst)
        order_lists[v.name] = lst

    def dfs(i):
        if len(sols) >= max_solutions or nodes[0] > node_budget:
            return
        if i == len(voices):
            sols.append(dict(assign))
            return
        v = voices[i]
        for pat in order_lists[v.name]:
            nodes[0] += 1
            if nodes[0] > node_budget or len(sols) >= max_solutions:
                return
            if consistent(v.name, pat):
                assign[v.name] = pat
                dfs(i + 1)
                del assign[v.name]

    dfs(0)
    return sols, nodes[0]


# ---------------------------------------------------------------- 最小锁定集合

def _constraints(config: Config) -> list:
    """可解释的约束清单：锁定重音 / 密度 / 最短间隔 / 关系 / 双手规则。"""
    cons = []
    n = config.total_steps
    for v in config.voices:
        if not v.enabled:
            continue
        cons.append({"id": f"density:{v.name}", "kind": "density", "voice": v.name,
                     "text": f"{v.name} 密度={v.density}（每 {n} 步）"})
        cons.append({"id": f"minint:{v.name}", "kind": "minint", "voice": v.name,
                     "text": f"{v.name} 最短间隔={v.min_interval} 步"})
        for s in sorted(locked_steps(v)):
            cons.append({"id": f"lock:{v.name}@{s}", "kind": "lock", "voice": v.name,
                         "step": s, "text": f"{v.name} 锁定重音 @ 第{s}步"})
    for i, rel in enumerate(config.relations):
        if rel.kind == "answer":
            t = f"呼应 {rel.a} → {rel.b}（+{rel.offset}步）"
        elif rel.kind == "unison":
            t = f"齐奏 {rel.a} ⇒ {rel.b}"
        else:
            t = f"互斥 {rel.a} ⊥ {rel.b}（窗口 {rel.window} 步）"
        cons.append({"id": f"rel:{i}", "kind": "relation", "rel": rel, "text": t})
    if any(v.hand for v in config.voices if v.enabled):
        cons.append({"id": "handrule", "kind": "handrule",
                     "text": f"双手组最短间隔={config.hand_min_interval} 步"})
    return cons


def _oracle(config: Config, subset) -> bool:
    """锁定片段精确可行性：密度/间隔 DP + 锁定传播 + 关系与双手规则。"""
    n = config.total_steps
    by_name = {v.name: v for v in config.voices if v.enabled}
    ids = {c["id"] for c in subset}
    required = {name: set() for name in by_name}
    for c in subset:
        if c["kind"] == "lock":
            required[c["voice"]].add(c["step"])
    # 关系传播到不动点
    changed = True
    while changed:
        changed = False
        for c in subset:
            if c["kind"] != "relation":
                continue
            rel = c["rel"]
            if rel.kind == "answer":
                for s in list(required[rel.a]):
                    t = (s + rel.offset) % n
                    if t not in required[rel.b]:
                        required[rel.b].add(t)
                        changed = True
            elif rel.kind == "unison":
                for s in list(required[rel.a]):
                    if s not in required[rel.b]:
                        required[rel.b].add(s)
                        changed = True
    # 声部内：密度 + 间隔 + 必含点
    for name, req in required.items():
        v = by_name[name]
        k = v.density if f"density:{name}" in ids else None
        g = v.min_interval if f"minint:{name}" in ids else 1
        if k is None:
            rs = sorted(req)
            if any(cdist(rs[i], rs[j], n) < g
                   for i in range(len(rs)) for j in range(i + 1, len(rs))):
                return False
        elif not voice_feasible(n, k, g, req):
            return False
    # 互斥关系
    for c in subset:
        if c["kind"] == "relation" and c["rel"].kind == "exclude":
            rel = c["rel"]
            if any(cdist(sa, sb, n) <= rel.window
                   for sa in required[rel.a] for sb in required[rel.b]):
                return False
    # 双手规则
    if "handrule" in ids:
        pairs = [(s, name) for name, req in required.items()
                 if by_name[name].hand for s in req]
        if not _hand_ok(pairs, n, config.hand_min_interval):
            return False
    return True


def minimal_core(config: Config):
    """删除式极小化：保留求解失败的最小锁定集合。可行则返回 None。"""
    cons = _constraints(config)
    if _oracle(config, cons):
        return None
    core = list(cons)
    for c in list(core):
        trial = [x for x in core if x["id"] != c["id"]]
        if not _oracle(config, trial):
            core = trial
    return core


# ---------------------------------------------------------------- 解释生成

def explain(assign: dict, config: Config) -> list:
    n = config.total_steps
    by_name = {v.name: v for v in config.voices if v.enabled}
    out = []
    for name in sorted(assign):
        v = by_name[name]
        steps = sorted(assign[name])
        if steps:
            gaps = [cdist(steps[i], steps[j], n)
                    for i in range(len(steps)) for j in range(i + 1, len(steps))]
            min_gap = min(gaps) if gaps else n
        else:
            min_gap = 0
        lk = sorted(locked_steps(v))
        soft = soft_steps(v)
        soft_hit = len(soft & set(steps))
        out.append(
            f"{name}：{len(steps)}/{n} 击，实际最小间隔 {min_gap} 步"
            f"（要求 ≥{v.min_interval}），锁定重音 {lk or '无'} 全部命中，"
            f"软重音命中 {soft_hit}/{len(soft)}")
    for rel in config.relations:
        a, b = assign.get(rel.a, frozenset()), assign.get(rel.b, frozenset())
        if rel.kind == "answer":
            hits = sum(1 for s in a if (s + rel.offset) % n in b)
            out.append(f"呼应 {rel.a}→{rel.b}（+{rel.offset}步）：{hits}/{len(a)} 次呼叫均有应答")
        elif rel.kind == "unison":
            out.append(f"齐奏 {rel.a}⇒{rel.b}：{len(a)} 击全部同步")
        else:
            out.append(f"互斥 {rel.a}⊥{rel.b}：无冲突（窗口 {rel.window} 步）")
    hand_pairs = sorted({s for name, st in assign.items() if by_name[name].hand for s in st})
    if hand_pairs:
        gaps = [cdist(hand_pairs[i], hand_pairs[j], n)
                for i in range(len(hand_pairs)) for j in range(i + 1, len(hand_pairs))
                if hand_pairs[i] != hand_pairs[j]]
        if gaps:
            out.append(f"双手组：实际最小间隔 {min(gaps)} 步（要求 ≥{config.hand_min_interval}）")
    return out


# ---------------------------------------------------------------- 主流程

def generate(config: Config) -> dict:
    """求解 → 排序 → 时移/力度 → 实际时间终检。失败抛 SolveFailure。"""
    from .rank import rank_and_select
    from .timing import apply_timing, validate_actual

    n = config.total_steps
    rng = random.Random(config.seed)
    enabled = [v for v in config.voices if v.enabled]
    if not enabled:
        raise SolveFailure([], "没有启用的声部")

    # 1) 单声部精确可行性（密度/间隔/锁定）
    for v in enabled:
        if not voice_feasible(n, v.density, v.min_interval, locked_steps(v)):
            core = minimal_core(config) or []
            raise SolveFailure(core, f"声部 {v.name} 的密度/间隔/锁定组合不可满足")

    # 2) 锁定片段全局可行性（关系传播 + 双手规则）
    core = minimal_core(config)
    if core is not None:
        raise SolveFailure(core, "锁定约束集合不可满足")

    # 3) 阶段 A：构造式修复播种
    a_assign, repairs, _ = constructive(config, rng)

    # 4) 阶段 B：DFS 枚举候选
    pattern_lists = {v.name: gen_patterns(v, n, rng) for v in enabled}
    for v in enabled:
        if not pattern_lists[v.name]:
            raise SolveFailure([], f"声部 {v.name} 未能枚举出可行模式", kind="budget")
    sols, nodes = search(config, pattern_lists, rng)

    pool = []
    seen = set()
    if a_assign is not None:
        pool.append((a_assign, repairs))
        seen.add(frozenset((k, tuple(sorted(v))) for k, v in a_assign.items()))
    for s in sols:
        key = frozenset((k, tuple(sorted(v))) for k, v in s.items())
        if key in seen:
            continue
        seen.add(key)
        pool.append((s, []))
    if not pool:
        core = minimal_core(config)
        if core:
            raise SolveFailure(core, "约束不可满足")
        raise SolveFailure([], f"搜索预算内未找到解（已探索 {nodes} 节点，未确认不可解）",
                           kind="budget")

    # 5) 排序 + 多样性筛选
    ranked = rank_and_select(config, pool)

    # 6) 微小时移/力度 + 实际时间终检
    warnings = []
    candidates = []
    for idx, (assign, rep, scores) in enumerate(ranked):
        crng = random.Random(config.seed * 7919 + idx)
        events, w = apply_timing(assign, config, crng)
        warnings.extend(w)
        events, adjustments, violations = validate_actual(events, config)
        if violations:
            continue  # 终检失败：丢弃该候选（实际演奏时间为准）
        candidates.append({
            "id": len(candidates),
            "events": [e.to_dict(config) for e in
                       sorted(events, key=lambda e: (e.step, e.voice))],
            "scores": scores,
            "repairs": rep,
            "adjustments": adjustments,
            "explanations": explain(assign, config),
        })
        if len(candidates) >= config.n_candidates:
            break
    if not candidates:
        raise SolveFailure([], "所有候选在实际时间终检中失败", kind="budget")
    return {"candidates": candidates, "warnings": sorted(set(warnings))}
