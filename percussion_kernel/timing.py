"""微小时移与力度：受上限控制；加入摇摆后禁止重叠的击打不得重新冲突。

最终检查以实际演奏时间（毫秒）为准。
"""
from __future__ import annotations

from .model import ACCENT_LOCKED, ACCENT_SOFT, Config, Event


def _off_eighth(step: int, spb: int) -> bool:
    """八分反拍位置（摇摆作用对象）。"""
    e = max(1, spb // 8)
    return step % e == 0 and (step // e) % 2 == 1


def apply_timing(assign: dict, config: Config, rng):
    """为每个事件计算力度与时移。返回 (events, warnings)。"""
    n = config.total_steps
    step_ms = config.step_ms
    by_name = {v.name: v for v in config.voices if v.enabled}
    warnings = []

    eff_max = min(config.max_shift_ms, 0.45 * step_ms)
    if eff_max < config.max_shift_ms - 1e-9:
        warnings.append(
            f"微小时移上限已收紧至 {eff_max:.1f}ms（< 步长一半），"
            f"以保证加入摇摆后击打次序与不重叠约束不被破坏")

    events = []
    for name in sorted(assign):
        v = by_name[name]
        for step in sorted(assign[name]):
            accent = v.accents.get(step, 0)
            boost = 1.25 if accent == ACCENT_LOCKED else (1.12 if accent == ACCENT_SOFT else 1.0)
            vel = v.base_velocity * boost
            vel *= 1.0 + rng.uniform(-config.velocity_jitter, config.velocity_jitter)
            vel = min(1.0, max(0.05, vel))

            shift = 0.0
            if config.swing > 0 and _off_eighth(step, config.steps_per_bar):
                e = max(1, config.steps_per_bar // 8)
                shift += config.swing * e * step_ms
            shift += rng.uniform(-config.timing_jitter_ms, config.timing_jitter_ms)
            shift = min(eff_max, max(-eff_max, shift))
            events.append(Event(voice=name, step=step, velocity=vel,
                                shift_ms=shift, accent=accent))
    return events, warnings


def _time_ms(ev: Event, config: Config) -> float:
    return ev.step * config.step_ms + ev.shift_ms


def validate_actual(events: list, config: Config):
    """以实际演奏时间终检：

    - 双手组：不同步号的击打不得塌缩到同一瞬间，且时间次序不得颠倒；
    - 互斥关系：实际时间距离必须大于窗口（毫秒）。
    发现冲突时先把时移向 0 回缩（确定性修复），仍失败则上报。
    返回 (events, adjustments, violations)。
    """
    by_name = {v.name: v for v in config.voices if v.enabled}
    adjustments, violations = [], []

    def pairs():
        out = []
        hand_evs = sorted((e for e in events if by_name[e.voice].hand),
                          key=lambda e: e.step)
        for i in range(len(hand_evs)):
            for j in range(i + 1, len(hand_evs)):
                a, b = hand_evs[i], hand_evs[j]
                if a.step == b.step:
                    continue  # 双手同步允许
                dt = _time_ms(b, config) - _time_ms(a, config)
                if dt <= 1.0:  # 塌缩或次序颠倒
                    out.append(("hand", a, b, 1.0 - dt + 0.5))
        for rel in config.relations:
            if rel.kind != "exclude":
                continue
            limit = rel.window * config.step_ms
            eas = [e for e in events if e.voice == rel.a]
            ebs = [e for e in events if e.voice == rel.b]
            for a in eas:
                for b in ebs:
                    dt = abs(_time_ms(a, config) - _time_ms(b, config))
                    if dt <= limit + 1e-6:
                        out.append(("exclude", a, b, limit - dt + 0.5))
        return out

    for _ in range(6):
        bad = pairs()
        if not bad:
            return events, adjustments, violations
        for kind, a, b, deficit in bad:
            # 时移绝对值较大者先回缩
            for ev in sorted((a, b), key=lambda e: -abs(e.shift_ms)):
                if deficit <= 0:
                    break
                take = min(abs(ev.shift_ms), deficit / 2.0 + 0.01)
                if take > 0:
                    ev.shift_ms -= take if ev.shift_ms > 0 else -take
                    deficit -= take
                    adjustments.append(
                        f"时移回缩 {ev.voice}@{ev.step}：{kind} 在实际演奏时间下冲突，"
                        f"已向 0 修正 {take:.2f}ms")
    violations = [f"{kind}: {a.voice}@{a.step} 与 {b.voice}@{b.step} 实际时间冲突"
                  for kind, a, b, _ in pairs()]
    return events, adjustments, violations
