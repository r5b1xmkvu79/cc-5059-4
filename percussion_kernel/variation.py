"""保留主型的变奏与填充。

在指定小节（变奏/填充区）内调整击打，同时硬保留：
- 用户锁定的节奏骨架（锁定重音）与关键重音（软重音）；
- 进入下一乐句的落点（边界步上的指定事件）；
- 区外事件原样不动，保证前后乐句的音乐连接。

两种策略在同一候选空间上对照：
- staged（先锁强拍再配填充）：骨架先落位，填充构造完成后才在叶节点检查
  末拍占用预算、落点与跨边界间隔，顺序相关且会留下被舍弃候选；
- joint（约束搜索中同时决定变奏与收束）：DFS 逐层剪枝，收束条件（落点、
  末拍预算、跨边界间隔）与变奏动作一起决定，能在 staged 失败处找到可衔接解。

变奏距离按 事件新增 / 删除 / 位移（配对，按步量计）/ 力度变化 四个维度定义，
总距离必须落在 [min_distance, max_distance]：既不得照抄原型，也不得改成另一套节奏。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .model import ACCENT_LOCKED, ACCENT_SOFT, Config, Event
from .rank import repetition_score, syncopation_score
from .solver import (
    _hand_ok,
    _relation_ok,
    cdist,
    locked_steps,
    soft_steps,
)
from .timing import apply_timing, validate_actual

STRATEGIES = ("staged", "joint")


class VariationFailure(Exception):
    """变奏不可行：携带结构化原因，绝不静默放宽预算或挪动骨架。"""

    def __init__(self, message, reasons=None, kind="budget"):
        super().__init__(message)
        self.message = message
        self.reasons = reasons or []
        self.kind = kind


@dataclass
class VariationSpec:
    bars: list                      # 变奏/填充区小节（0 基）
    voices: list                    # 参与变奏的启用声部
    min_distance: int = 2
    max_distance: int = 12
    fill_end_budget: int = 2        # 末拍窗口内全部声部的击打上限
    velocity_delta: float = 0.2     # 保留事件允许的力度调整幅度
    disp_window: int = 4            # 位移配对的最大步距
    keep_soft: bool = True          # 软重音视为关键重音一并保留
    landing: list = field(default_factory=list)  # [{"voice","step"}]
    strategies: list = field(default_factory=lambda: list(STRATEGIES))


def variation_spec_from_dict(d: dict, cfg: Config) -> VariationSpec:
    if not isinstance(d, dict):
        raise ValueError("变奏参数必须是 JSON 对象")
    enabled = {v.name for v in cfg.voices if v.enabled}
    bars = d.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("变奏区至少指定一个小节")
    bars = sorted({int(b) for b in bars})
    if any(not (0 <= b < cfg.bars) for b in bars):
        raise ValueError(f"变奏小节越界，允许 0..{cfg.bars - 1}")
    voices = d.get("voices") or sorted(enabled)
    if any(v not in enabled for v in voices):
        raise ValueError("变奏声部引用了未启用声部")
    min_d = int(d.get("min_distance", 2))
    max_d = int(d.get("max_distance", 12))
    if not (0 <= min_d <= max_d <= 128):
        raise ValueError("变奏距离要求 0 <= min <= max <= 128")
    budget = int(d.get("fill_end_budget", 2))
    if not (0 <= budget <= cfg.total_steps):
        raise ValueError("末拍占用预算越界")
    vdelta = float(d.get("velocity_delta", 0.2))
    if not (0.0 <= vdelta <= 0.5):
        raise ValueError("力度调整幅度须在 0..0.5")
    disp_w = int(d.get("disp_window", max(1, cfg.steps_per_bar // 4)))
    if not (1 <= disp_w <= cfg.steps_per_bar):
        raise ValueError("位移窗口越界")
    landing = []
    for item in d.get("landing", []):
        voice, step = item.get("voice"), int(item.get("step", -1))
        if voice not in enabled or not (0 <= step < cfg.total_steps):
            raise ValueError(f"落点无效: {item}")
        if voice not in voices:
            raise ValueError(f"落点声部 {voice} 不在参与变奏的声部级内")
        landing.append({"voice": voice, "step": step})
    strategies = d.get("strategies") or list(STRATEGIES)
    if any(s not in STRATEGIES for s in strategies):
        raise ValueError(f"未知策略，可选 {STRATEGIES}")
    return VariationSpec(
        bars=bars, voices=sorted(voices), min_distance=min_d,
        max_distance=max_d, fill_end_budget=budget, velocity_delta=vdelta,
        disp_window=disp_w, keep_soft=bool(d.get("keep_soft", True)),
        landing=landing, strategies=strategies,
    )


def spec_to_dict(spec: VariationSpec) -> dict:
    return {
        "bars": list(spec.bars), "voices": list(spec.voices),
        "min_distance": spec.min_distance, "max_distance": spec.max_distance,
        "fill_end_budget": spec.fill_end_budget,
        "velocity_delta": spec.velocity_delta, "disp_window": spec.disp_window,
        "keep_soft": spec.keep_soft, "landing": [dict(x) for x in spec.landing],
        "strategies": list(spec.strategies),
    }


# ---------------------------------------------------------------- 原型与骨架

def parse_prototype(events: list, cfg: Config) -> dict:
    """事件表 → {voice: set(step)}，并校验原型本身满足锁定与步域硬约束。"""
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    assign = {name: set() for name in by_name}
    for e in events:
        name = e.get("voice")
        if name not in by_name:
            continue
        step = int(e["step"])
        if not (0 <= step < cfg.total_steps):
            raise ValueError(f"原型事件步号越界: {e}")
        assign[name].add(step)
    for name, v in by_name.items():
        missing = locked_steps(v) - assign[name]
        if missing:
            raise ValueError(f"原型缺少锁定重音 {sorted(missing)}（{name}），不能作为主型")
    return assign


def build_skeleton(cfg: Config, spec: VariationSpec) -> dict:
    """骨架 = 锁定重音 + 关键（软）重音 + 显式落点。返回 voice -> {step: reason}。"""
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    skel = {name: {} for name in by_name}
    in_zone = _zone_steps(cfg, spec)
    for name, v in by_name.items():
        for s in sorted(locked_steps(v)):
            skel[name][s] = "锁定重音"
        if spec.keep_soft:
            for s in sorted(soft_steps(v)):
                if s in in_zone:
                    skel[name].setdefault(s, "关键重音")
    for item in spec.landing:
        skel[item["voice"]][item["step"]] = "乐句落点"
    return skel


def _zone_steps(cfg: Config, spec: VariationSpec) -> set:
    spb = cfg.steps_per_bar
    out = set()
    for b in spec.bars:
        out.update(range(b * spb, (b + 1) * spb))
    return out


def _zone_span(cfg: Config, spec: VariationSpec):
    """返回 (zone_start, zone_end, end_window_steps, boundary_step)。"""
    spb = cfg.steps_per_bar
    lo = min(spec.bars) * spb
    end_bar = max(spec.bars)
    zone_end = (end_bar + 1) * spb
    q = max(1, spb // 4)
    window = set(range(max(0, zone_end - q), zone_end))
    boundary = zone_end % cfg.total_steps
    return lo, zone_end, window, boundary


# ---------------------------------------------------------------- 变奏距离

def _match_displacements(removed: list, added: list, window: int):
    """删除事件与新增事件按最近步距贪心配对；配对成功记为位移，其余维持增/删。"""
    pairs = sorted((abs(a - b), a, b)
                   for a in removed for b in added
                   if 0 < abs(a - b) <= window)
    used_r, used_a, disp = set(), set(), []
    for d, a, b in pairs:
        if a in used_r or b in used_a:
            continue
        used_r.add(a)
        used_a.add(b)
        disp.append((a, b, d))
    final_removed = sorted(s for s in removed if s not in used_r)
    final_added = sorted(s for s in added if s not in used_a)
    return disp, final_removed, final_added


def variation_distance(proto: dict, variant: dict, spec: VariationSpec,
                       vel_changes: set) -> dict:
    """四维度距离：新增 / 删除 / 位移（按步量）/ 力度变化。"""
    added, removed, displaced, disp_steps = 0, 0, 0, 0
    for name in spec.voices:
        p, v = set(proto.get(name, ())), set(variant.get(name, ()))
        rem = sorted(p - v)
        add = sorted(v - p)
        disp, left_r, left_a = _match_displacements(rem, add, spec.disp_window)
        displaced += len(disp)
        disp_steps += sum(d for _, _, d in disp)
        removed += len(left_r)
        added += len(left_a)
    vel_n = len(vel_changes)
    total = added + removed + disp_steps + vel_n
    return {
        "added": added, "removed": removed, "displaced": displaced,
        "displacement_steps": disp_steps, "velocity_changes": vel_n,
        "total": total,
        "within_budget": spec.min_distance <= total <= spec.max_distance,
    }


# ---------------------------------------------------------------- 候选空间枚举

def _enumerate_zone_patterns(required: set, proto: set, positions: list,
                             min_gap: int, max_hamming: int, rng, cap=90) -> list:
    """枚举区内步集合：必含 required，线性间隔 >= min_gap，与原型的汉明距 <= max_hamming。"""
    required = set(required)
    rs = sorted(required)
    if any(rs[i + 1] - rs[i] < min_gap for i in range(len(rs) - 1)):
        return []
    optional = [p for p in positions if p not in required]
    rng.shuffle(optional)
    out, seen = [], set()

    def dfs(i, chosen, hamming):
        if len(out) >= cap or hamming > max_hamming:
            return
        if i == len(optional):
            fs = frozenset(chosen | required)
            if fs not in seen:
                seen.add(fs)
                out.append(fs)
            return
        s = optional[i]
        prev = max((x for x in (chosen | required) if x < s), default=None)
        # 排除该位（原型上有事件则计一次删除距离）
        dfs(i + 1, chosen, hamming + (1 if s in proto else 0))
        # 纳入该位（原型上没有则计一次新增距离）
        if prev is None or s - prev >= min_gap:
            chosen.add(s)
            dfs(i + 1, chosen, hamming + (0 if s in proto else 1))
            chosen.discard(s)

    dfs(0, set(), 0)
    return out


def _staged_trials(cfg: Config, spec: VariationSpec, skeleton: dict,
                   proto: dict, zone_pos: list, rng, trials=24) -> list:
    """策略 staged：骨架先落位，再随机增删自由击打，收束条件留到叶节点检查。"""
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    combos, seen = [], set()
    for _ in range(trials):
        local = {}
        for name in spec.voices:
            v = by_name[name]
            sk = {s for s in skeleton[name] if s in zone_pos}
            chosen = set(sk)
            p_local = {s for s in proto[name] if s in zone_pos}
            target = max(len(sk), len(p_local) + rng.choice((-1, 0, 1)))
            free_proto = sorted(p_local - sk)
            rng.shuffle(free_proto)
            while len(chosen) < target and free_proto:
                chosen.add(free_proto.pop())
            pool = [s for s in zone_pos if s not in chosen]
            rng.shuffle(pool)
            for s in pool:
                if len(chosen) >= target:
                    break
                if all(cdist(s, t, cfg.total_steps) >= v.min_interval
                       for t in chosen):
                    chosen.add(s)
            while len(chosen) > target:
                victims = sorted(chosen - sk)
                if not victims:
                    break
                chosen.discard(victims[-1])
            local[name] = frozenset(chosen)
        key = frozenset((k, tuple(sorted(v))) for k, v in local.items())
        if key not in seen:
            seen.add(key)
            combos.append(local)
    return combos


def _joint_search(cfg: Config, spec: VariationSpec, skeleton: dict,
                  proto: dict, zone_pos: list, rng, cap=120) -> list:
    """策略 joint：逐声部 DFS，关系/双手/末拍预算/距离逐层剪枝，收束与变奏同决。"""
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    n = cfg.total_steps
    _, _, end_window, _ = _zone_span(cfg, spec)
    landing_names = {item["voice"] for item in spec.landing}
    # 最约束声部优先：落点声部 → 骨架/密度高的声部
    order = sorted(spec.voices, key=lambda nm: (
        nm not in landing_names,
        -len({s for s in skeleton[nm] if s in zone_pos}),
        -by_name[nm].density))
    pattern_lists = {}
    for name in order:
        v = by_name[name]
        req = {s for s in skeleton[name] if s in zone_pos}
        p_local = {s for s in proto[name] if s in zone_pos}
        patterns = _enumerate_zone_patterns(
            req, p_local, list(zone_pos), v.min_interval,
            spec.max_distance, rng, cap=120)
        if not patterns:
            return []
        # 边界优先：把末拍窗口占用更少、与原型距离更近的模式排前面（收束先行）
        patterns.sort(key=lambda fs: (len(fs & end_window),
                                      len(fs ^ p_local), sorted(fs)))
        pattern_lists[name] = patterns

    outside = {name: frozenset(s for s in proto[name] if s not in zone_pos)
               for name in order}
    # DFS 只携带已决策声部；区外事件在每个 full_steps 里即时并入
    base_assign = {}
    combos, nodes = [], [0]

    def consistent(name, full_steps, assign):
        for rel in cfg.relations:
            if rel.a == name and rel.b in assign:
                if not _relation_ok(rel, full_steps, assign[rel.b], n):
                    return False
            elif rel.b == name and rel.a in assign:
                if not _relation_ok(rel, assign[rel.a], full_steps, n):
                    return False
        if by_name[name].hand or any(by_name[x].hand for x in assign):
            pairs = [(s, name) for s in full_steps if by_name[name].hand]
            for x, st in assign.items():
                if by_name[x].hand:
                    pairs += [(s, x) for s in st]
            if not _hand_ok(pairs, n, cfg.hand_min_interval):
                return False
        return True

    def dfs(i, assign, hamming, end_hits):
        if len(combos) >= cap or nodes[0] > 60000:
            return
        # 末拍预算单调累加可安全剪枝；结构汉明距对远距离位移是低估，不做距离剪枝
        if end_hits > spec.fill_end_budget:
            return
        if i == len(order):
            combos.append({k: frozenset(v) for k, v in assign.items()})
            return
        name = order[i]
        p_local = {s for s in proto[name] if s in zone_pos}
        for local in pattern_lists[name]:
            nodes[0] += 1
            full_steps = outside[name] | local
            if not consistent(name, full_steps, assign):
                continue
            new_end = end_hits + len(full_steps & end_window)
            if new_end > spec.fill_end_budget:
                continue
            dfs(i + 1, {**assign, name: full_steps},
                hamming + len(local ^ p_local), new_end)

    dfs(0, base_assign, 0, 0)
    return combos


# ---------------------------------------------------------------- 落点补全与硬约束

def _complete_landing(cfg: Config, spec: VariationSpec, assign: dict,
                      skeleton: dict, allow_add: bool) -> list:
    """确保落点全部命中。allow_add 时 joint 策略可在边界步补入收束事件。"""
    n = cfg.total_steps
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    reasons = []
    for item in spec.landing:
        name, step = item["voice"], item["step"]
        if step in assign[name]:
            continue
        if not allow_add:
            reasons.append(f"落点缺失：{name}@{step}（staged 不在收束处新增事件）")
            continue
        v = by_name[name]
        if any(cdist(step, t, n) < v.min_interval for t in assign[name]):
            reasons.append(f"落点无法补入：{name}@{step} 与本声部既有击打间隔不足")
            continue
        if v.hand:
            pairs = [(s, x) for x, st in assign.items()
                     for s in st if by_name[x].hand] + [(step, name)]
            if not _hand_ok(pairs, n, cfg.hand_min_interval):
                reasons.append(f"落点无法补入：{name}@{step} 破坏双手组最短间隔")
                continue
        assign[name] = frozenset(set(assign[name]) | {step})
        skeleton[name][step] = "乐句落点（搜索补入）"
    return reasons


def _step_domain_violations(cfg: Config, assign: dict) -> list:
    """声部间隔 / 关系 / 双手规则（步域，循环距离）。"""
    n = cfg.total_steps
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    out = []
    for name, steps in assign.items():
        v = by_name[name]
        ss = sorted(steps)
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                if cdist(ss[i], ss[j], n) < v.min_interval:
                    out.append(f"{name}: {ss[i]} 与 {ss[j]} 间隔小于 {v.min_interval} 步")
    for rel in cfg.relations:
        if rel.a in assign and rel.b in assign:
            if not _relation_ok(rel, assign[rel.a], assign[rel.b], n):
                out.append(f"关系被破坏: {rel.kind} {rel.a}/{rel.b}")
    pairs = [(s, name) for name, steps in assign.items()
             if by_name[name].hand for s in steps]
    if not _hand_ok(pairs, n, cfg.hand_min_interval):
        out.append(f"双手组最短间隔被破坏（要求 ≥{cfg.hand_min_interval} 步）")
    return out


def _end_budget_violations(cfg: Config, spec: VariationSpec, assign: dict):
    _, _, end_window, _ = _zone_span(cfg, spec)
    hits = sorted((name, s) for name, steps in assign.items() for s in steps
                  if s in end_window)
    reasons, overflow = [], []
    if len(hits) > spec.fill_end_budget:
        reasons.append(
            f"末拍占用 {len(hits)} 击超过预算 {spec.fill_end_budget} "
            f"（末拍窗口 {sorted(end_window)[0]}..{sorted(end_window)[-1]} 步）")
        overflow = [{"voice": name, "step": s} for name, s in
                    hits[spec.fill_end_budget:]]
    return hits, overflow, reasons


# ---------------------------------------------------------------- 实际时间跨边界核验

def _actual_time(ev: Event, cfg: Config) -> float:
    return ev.step * cfg.step_ms + ev.shift_ms


def boundary_actual_check(events: list, cfg: Config, zone_end: int) -> dict:
    """填充区末端衔接核验：跨边界的两次击打也参与最短间隔限制（以毫秒计）。

    zone_end == total_steps 时乐句循环播放，边界两侧分别是乐句末尾与开头。
    """
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    n = cfg.total_steps
    wrap = zone_end >= n
    boundary = zone_end % n
    step_ms = cfg.step_ms

    def gap_ms(a: Event, b: Event) -> float:
        ta, tb = _actual_time(a, cfg), _actual_time(b, cfg)
        return (tb + cfg.phrase_ms - ta) if wrap else tb - ta

    def straddling(pool):
        # 取区内最后一击与区外（下一乐句）第一击；循环包裹时两侧是同一批事件的
        # 末尾与开头，必须分别选取而非互斥分区。
        if wrap:
            inside_pool = [e for e in pool if e.step < zone_end]
            outside_pool = list(pool)  # 乐句开头即下一乐句进入点
        else:
            inside_pool = [e for e in pool if e.step < zone_end]
            outside_pool = [e for e in pool if e.step >= boundary]
        if not inside_pool or not outside_pool:
            return None
        a = max(inside_pool, key=lambda e: _actual_time(e, cfg))
        b = min(outside_pool, key=lambda e: _actual_time(e, cfg))
        if not wrap and a is b:
            return None
        return a, b, gap_ms(a, b)

    checks, violations = [], []

    def record(scope, a, b, gap, grid_gap):
        required_ms = (grid_gap - 1) * step_ms + 1.0
        ok = gap >= required_ms
        checks.append({"scope": scope,
                       "last_in": {"voice": a.voice, "step": a.step,
                                   "time_ms": round(_actual_time(a, cfg), 2)},
                       "first_out": {"voice": b.voice, "step": b.step,
                                     "time_ms": round(_actual_time(b, cfg), 2)},
                       "gap_ms": round(gap, 2),
                       "required_ms": round(required_ms, 2), "ok": ok})
        if not ok:
            violations.append(
                f"跨边界实际间隔不足：{a.voice}@{a.step} → {b.voice}@{b.step} "
                f"实际 {gap:.1f}ms < 要求 {required_ms:.1f}ms（{scope}）")

    for name, v in by_name.items():
        pair = straddling([e for e in events if e.voice == name])
        if pair:
            record(f"声部 {name}", *pair, grid_gap=v.min_interval)
    hand_events = [e for e in events if by_name[e.voice].hand]
    pair = straddling(hand_events)
    if pair:
        record("双手组", *pair, grid_gap=cfg.hand_min_interval)
    return {"checks": checks, "violations": violations}


# ---------------------------------------------------------------- 候选分类

def _velocity_plans(assign: dict, proto: dict, spec: VariationSpec,
                    structural_total: int) -> list:
    """返回 [(vel_changes_set, label)]；结构距离不足时用力度变化补足下限。

    结构零变化时不产生力度计划——纯力度调整不得冒充节奏变奏。
    """
    retained = sorted((name, s) for name in spec.voices
                      for s in assign[name] if s in proto.get(name, ()))
    if structural_total <= 0 or spec.velocity_delta <= 0 or not retained:
        return [set()]
    plans = [set()]
    if structural_total < spec.min_distance:
        need = spec.min_distance - structural_total
        picked = set()
        for idx, key in enumerate(retained):
            if len(picked) >= min(need, len(retained)):
                break
            picked.add(key)
        if picked:
            plans.append(picked)
    return plans


def _change_items(cfg: Config, spec: VariationSpec, skeleton: dict,
                  proto: dict, assign: dict, vel_changes: set,
                  overflow: list) -> list:
    """逐项变化清单，allowed 标明是否落在允许范围（界面逐项显示）。"""
    items = []
    over_keys = {(o["voice"], o["step"]) for o in overflow}
    for name in spec.voices:
        p, v = set(proto.get(name, ())), set(assign[name])
        rem, add = sorted(p - v), sorted(v - p)
        disp, left_r, left_a = _match_displacements(rem, add, spec.disp_window)
        for a, b, d in disp:
            items.append({"voice": name, "step": b, "from_step": a,
                          "kind": "displaced", "detail": f"位移 {a}→{b}（{d} 步）",
                          "allowed": (name, b) not in over_keys})
        for s in left_a:
            items.append({"voice": name, "step": s, "kind": "added",
                          "detail": "新增击打",
                          "allowed": (name, s) not in over_keys})
        for s in left_r:
            items.append({"voice": name, "step": s, "kind": "removed",
                          "detail": "删除原型击打", "allowed": True})
        for s in sorted(p & v):
            if (name, s) in vel_changes:
                items.append({"voice": name, "step": s, "kind": "velocity",
                              "detail": f"力度调整 ±{spec.velocity_delta:.2f}",
                              "allowed": True})
            elif s in skeleton[name]:
                items.append({"voice": name, "step": s, "kind": "skeleton",
                              "detail": f"骨架保留（{skeleton[name][s]}）",
                              "allowed": True})
            else:
                items.append({"voice": name, "step": s, "kind": "retained",
                              "detail": "原型事件保留", "allowed": True})
    return items


def _classify(strategy: str, cfg: Config, spec: VariationSpec, skeleton: dict,
              proto: dict, assign: dict, rng, idx: int,
              landing_reasons: list) -> dict:
    """对一个完整候选执行全部硬约束与预算检查，给出接受/舍弃结论与原因。"""
    _, zone_end, end_window, boundary = _zone_span(cfg, spec)
    reasons = list(landing_reasons)
    reasons += _step_domain_violations(cfg, assign)
    end_hits, overflow, end_reasons = _end_budget_violations(cfg, spec, assign)
    reasons += end_reasons

    # 落点命中情况（供界面展示）
    landing_status = [
        {"voice": item["voice"], "step": item["step"],
         "hit": item["step"] in assign[item["voice"]]}
        for item in spec.landing
    ]

    structural_total = _structural_total(proto, assign, spec)
    vel_plans = _velocity_plans(assign, proto, spec, structural_total)
    best = None
    for plan in vel_plans:
        dist = variation_distance(proto, assign, spec, plan)
        if dist["within_budget"]:
            best = (plan, dist)
            break
    if best is None:
        plan = vel_plans[-1]
        dist = variation_distance(proto, assign, spec, plan)
        if dist["total"] < spec.min_distance:
            reasons.append(f"变奏距离 {dist['total']} 低于下限 {spec.min_distance}"
                           "（不得照抄原型冒充变化）")
        else:
            reasons.append(f"变奏距离 {dist['total']} 超过上限 {spec.max_distance}"
                           "（不得改成另一套节奏）")
    # 结构零变化（仅力度）不算变奏：必须有增/删/位移中的至少一项
    structural_dims = (dist["added"] + dist["removed"] + dist["displaced"])
    if structural_dims == 0:
        reasons.append("结构与原型完全相同，仅力度变化不能冒充节奏变奏"
                       "（新增/删除/位移至少各需一项）")
    vel_changes, dist = best if best else (plan, dist)

    # 真实音符事件：时移/力度 + 刻意力度调整 + 实际时间终检
    events, _ = apply_timing(assign, cfg, rng)
    sign = 1
    for ev in events:
        if (ev.voice, ev.step) in vel_changes:
            ev.velocity = min(1.0, max(0.05,
                              ev.velocity * (1.0 + sign * spec.velocity_delta)))
            sign *= -1
    events, adjustments, time_violations = validate_actual(events, cfg)
    boundary_report = boundary_actual_check(events, cfg, zone_end)
    reasons += time_violations
    reasons += boundary_report["violations"]

    changes = _change_items(cfg, spec, skeleton, proto, assign,
                            vel_changes, overflow)
    accepted = not reasons
    return {
        "strategy": strategy,
        "id": idx,
        "accepted": accepted,
        "reasons": reasons,
        "events": [e.to_dict(cfg) for e in
                   sorted(events, key=lambda e: (e.step, e.voice))],
        "distance": dist,
        "changes": changes,
        "fill_end": {"window_steps": sorted(end_window),
                     "hits": [{"voice": n, "step": s} for n, s in end_hits],
                     "budget": spec.fill_end_budget,
                     "ok": len(end_hits) <= spec.fill_end_budget},
        "boundary": boundary_report,
        "landing": landing_status,
        "adjustments": adjustments,
        "scores": {"syncopation": syncopation_score(cfg, assign),
                   "repetition": repetition_score(cfg, assign)},
    }


def _structural_total(proto: dict, assign: dict, spec: VariationSpec) -> int:
    total = 0
    for name in spec.voices:
        total += len(set(proto.get(name, ())) ^ set(assign[name]))
    return total


# ---------------------------------------------------------------- 主入口

def variate(cfg: Config, prototype_events: list, spec_dict: dict) -> dict:
    """保留主型的变奏与填充主流程。失败抛 VariationFailure。"""
    spec = variation_spec_from_dict(spec_dict, cfg)
    proto = parse_prototype(prototype_events, cfg)
    skeleton = build_skeleton(cfg, spec)
    zone_pos = sorted(_zone_steps(cfg, spec))
    rng = random.Random(cfg.seed * 104729 + 7)
    _, zone_end, _, _ = _zone_span(cfg, spec)

    # 区外事件固定；区内每个声部的完整候选 = 区外 ∪ 区内局部
    outside = {name: frozenset(s for s in proto[name] if s not in zone_pos)
               for name in spec.voices}

    strategy_outputs = {}
    for strategy in spec.strategies:
        if strategy == "staged":
            local_combos = _staged_trials(cfg, spec, skeleton, proto,
                                          zone_pos, rng)
            allow_add = False
        else:
            local_combos = _joint_search(cfg, spec, skeleton, proto,
                                         zone_pos, rng)
            allow_add = True
        if not local_combos:
            strategy_outputs[strategy] = {
                "accepted": [], "rejected": [],
                "summary": {"variants": 0,
                            "fatal": "约束下枚举不到任何区内模式（骨架/间隔/预算互相冲突）"},
            }
            continue

        records, idx = [], 0
        for local in local_combos:
            if strategy == "staged":
                assign = {name: frozenset(outside[name] | local.get(name, frozenset()))
                          for name in spec.voices}
            else:
                assign = {name: frozenset(local[name]) for name in spec.voices}
            # 非变奏声部原样保留，供全局关系/双手/末拍核验
            for name, steps in proto.items():
                assign.setdefault(name, frozenset(steps))
            work_skel = {k: dict(v) for k, v in skeleton.items()}
            landing_reasons = _complete_landing(cfg, spec, assign, work_skel,
                                                allow_add)
            crng = random.Random(cfg.seed * 7919 + idx * 131 +
                                 (1 if strategy == "joint" else 0))
            rec = _classify(strategy, cfg, spec, work_skel, proto, assign,
                            crng, idx, landing_reasons)
            records.append(rec)
            idx += 1

        accepted = [r for r in records if r["accepted"]][:6]
        rejected = [r for r in records if not r["accepted"]][:12]
        for i, rec in enumerate(accepted):
            rec["id"] = i
        reason_freq = {}
        for r in rejected:
            for why in r["reasons"]:
                key = why.split("：")[0]
                reason_freq[key] = reason_freq.get(key, 0) + 1
        strategy_outputs[strategy] = {
            "accepted": accepted,
            "rejected": rejected,
            "summary": {"variants": len(records),
                        "accepted": len(accepted),
                        "rejected": len(rejected),
                        "reason_freq": reason_freq},
        }

    if all(o["summary"]["variants"] == 0 for o in strategy_outputs.values()):
        raise VariationFailure(
            "变奏区在当前骨架与预算下没有任何可行模式",
            [o["summary"].get("fatal", "无候选") for o in strategy_outputs.values()])

    comparison = _compare(spec, strategy_outputs)
    total_accepted = sum(o["summary"].get("accepted", 0)
                         for o in strategy_outputs.values())
    return {
        "status": "ok" if total_accepted else "budget",
        "spec": spec_to_dict(spec),
        "zone": {"bars": list(spec.bars), "zone_end": zone_end,
                 "steps": [min(zone_pos), max(zone_pos)]},
        "skeleton": [{"voice": name, "step": s, "reason": why}
                     for name in sorted(skeleton)
                     for s, why in sorted(skeleton[name].items())],
        "strategies": strategy_outputs,
        "comparison": comparison,
    }


def _compare(spec: VariationSpec, outputs: dict) -> dict:
    """两种策略在该处的表现对照，含等距候选的衔接差异。"""
    lines, winner, best_n = [], None, -1
    cadence_keys = ("末拍占用", "跨边界实际间隔不足", "落点缺失", "落点无法补入")
    for name, label in (("staged", "先锁强拍再配填充"),
                        ("joint", "约束搜索同时决定变奏与收束")):
        if name not in outputs:
            continue
        s = outputs[name]["summary"]
        if s["variants"] == 0:
            lines.append(f"{label}：无可行模式（{s.get('fatal', '')}）")
            continue
        cadence_rejects = sum(c for k, c in s["reason_freq"].items()
                              if any(k.startswith(ck) for ck in cadence_keys))
        lines.append(
            f"{label}：枚举 {s['variants']} 个候选，接受 {s['accepted']}、"
            f"舍弃 {s['rejected']}，其中收束类舍弃 {cadence_rejects} 个")
        if s["accepted"] > best_n:
            best_n, winner = s["accepted"], name

    # 等距候选衔接对照：相同总距离下仅一个接得上下句
    if all(name in outputs and outputs[name]["summary"]["variants"]
           for name in ("staged", "joint")):
        j_acc = {r["distance"]["total"] for r in outputs["joint"]["accepted"]}
        s_rej = [r for r in outputs["staged"]["rejected"]
                 if r["distance"]["total"] in j_acc]
        if s_rej:
            r = s_rej[0]
            lines.append(
                f"等距对照：总距离 {r['distance']['total']} 的候选在 staged 被舍弃"
                f"（{'；'.join(r['reasons'])}），joint 同距离候选通过末拍预算与"
                "跨边界实际间隔核验，可接入下句")
    if winner is None:
        winner = "tie"
    return {"winner": winner, "lines": lines}
