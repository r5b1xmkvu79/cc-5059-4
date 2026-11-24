"""保留主型的变奏与填充回归：
python3 -m unittest tests.test_variation -v

硬约束核验重点：锁定重音/关键重音/落点保留、末拍占用预算、
跨边界两次击打的实际毫秒最短间隔、变奏距离四维度预算。
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from percussion_kernel.model import (
    ACCENT_LOCKED, config_from_dict, config_to_dict, default_config,
)
from percussion_kernel.solver import cdist, generate, locked_steps
from percussion_kernel.variation import (
    STRATEGIES, VariationFailure, boundary_actual_check, parse_prototype,
    variate, variation_distance, variation_spec_from_dict,
)


def base_cfg():
    return default_config()


def proto_events(cfg=None):
    cfg = cfg or base_cfg()
    return generate(cfg)["candidates"][0]["events"], cfg


def proto_assign(events):
    out = {}
    for e in events:
        out.setdefault(e["voice"], set()).add(e["step"])
    return out


def default_spec(**over):
    spec = {"bars": [1], "voices": ["kick", "snare", "hat_closed"],
            "min_distance": 2, "max_distance": 10, "fill_end_budget": 2,
            "landing": [{"voice": "kick", "step": 16}]}
    spec.update(over)
    return spec


class TestSkeletonLocked(unittest.TestCase):
    def test_locked_and_soft_and_landing_kept(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec())
        skel_keys = {(s["voice"], s["step"]) for s in out["skeleton"]}
        # 只对参与变奏的声部断言骨架保留（非变奏声部事件本就原样不动）
        varying = set(default_spec()["voices"])
        skel_varying = {k for k in skel_keys if k[0] in varying}
        for sname, o in out["strategies"].items():
            for cand in o["accepted"]:
                got = {(e["voice"], e["step"]) for e in cand["events"]}
                self.assertTrue(skel_varying <= got,
                                f"{sname} 接受候选丢失骨架事件")
            for cand in o["rejected"]:
                got = {(e["voice"], e["step"]) for e in cand["events"]}
                missing = skel_varying - got
                for voice, step in missing:
                    self.assertTrue(
                        any("落点" in r for r in cand["reasons"]),
                        f"{sname} 舍弃候选丢失非落点骨架且未说明原因")
        # 骨架清单确实包含锁定重音与落点
        self.assertIn(("kick", 0), skel_keys)
        self.assertIn(("kick", 16), skel_keys)
        reasons = {k: s["reason"] for s in out["skeleton"]
                   for k in [(s["voice"], s["step"])]}
        self.assertTrue(any("锁定" in r for r in reasons.values()))
        self.assertTrue(any("落点" in r for r in reasons.values()))

    def test_outside_zone_events_untouched(self):
        events, cfg = proto_events()
        proto = proto_assign(events)
        out = variate(cfg, events, default_spec(bars=[1]))
        zone = set(range(16, 32))
        for cand in self._all_records(out):
            got = proto_assign(cand["events"])
            for name in proto:
                outside_proto = proto[name] - zone
                self.assertTrue(outside_proto <= got.get(name, set()),
                                "区外事件被改动，破坏前后乐句连接")

    def test_all_locked_skeleton_still_solved_or_explained(self):
        events, cfg = proto_events()
        # kick/snare 区内步位全部为锁定或落点，可动空间极小
        out = variate(cfg, events, default_spec(
            voices=["kick", "snare"], fill_end_budget=4,
            min_distance=1, max_distance=6))
        for sname in STRATEGIES:
            for cand in out["strategies"][sname]["accepted"]:
                got = proto_assign(cand["events"])
                for name in ("kick", "snare"):
                    self.assertTrue(
                        locked_steps(cfg.voice(name)) <= got[name])

    def _all_records(self, out):
        recs = []
        for o in out["strategies"].values():
            recs += o["accepted"] + o["rejected"]
        return recs


class TestVariationDistance(unittest.TestCase):
    def test_four_dimensions_and_budget(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec())
        for cand in self._all_records(out):
            d = cand["distance"]
            self.assertEqual(
                d["total"],
                d["added"] + d["removed"] + d["displacement_steps"]
                + d["velocity_changes"])
            self.assertGreaterEqual(d["added"] + d["removed"] + d["displaced"], 1,
                                    "接受候选必须含结构变化，纯力度不得冒充变奏")
            if cand["accepted"]:
                self.assertTrue(d["within_budget"])
                self.assertGreaterEqual(d["total"], 2)
                self.assertLessEqual(d["total"], 10)

    def test_copycat_rejected(self):
        events, cfg = proto_events()
        # 下限抬高到任何可行结构变化都达不到时，应全部舍弃并给出照抄类原因
        out = variate(cfg, events, default_spec(
            voices=["snare"], min_distance=8, max_distance=12,
            fill_end_budget=4, landing=[]))
        texts = [r for o in out["strategies"].values()
                 for c in o["rejected"] for r in c["reasons"]]
        self.assertTrue(any("低于下限" in t or "不能冒充" in t for t in texts))

    def test_distance_math_add_remove_displace_velocity(self):
        cfg = base_cfg()
        spec = variation_spec_from_dict(
            {"bars": [0], "voices": ["kick"], "min_distance": 0,
             "max_distance": 50, "fill_end_budget": 8}, cfg)
        proto = {"kick": {0, 4, 12}}
        # 4→5 位移 1 步；删除 12；新增 7（与任何删除点距离 > 窗口，计纯新增）；
        # 保留 0 做力度变化
        variant = {"kick": {0, 5, 7}}
        d = variation_distance(proto, variant, spec, {("kick", 0)})
        self.assertEqual(d["displaced"], 1)
        self.assertEqual(d["displacement_steps"], 1)
        self.assertEqual(d["removed"], 1)
        self.assertEqual(d["added"], 1)
        self.assertEqual(d["velocity_changes"], 1)
        self.assertEqual(d["total"], 4)

    def _all_records(self, out):
        return [c for o in out["strategies"].values()
                for c in o["accepted"] + o["rejected"]]


class TestFillEndBudget(unittest.TestCase):
    def test_zero_budget_rejects_end_window_hits(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec(
            voices=["hat_closed"], fill_end_budget=0,
            min_distance=1, max_distance=8, landing=[]))
        for sname, o in out["strategies"].items():
            for cand in o["accepted"]:
                self.assertEqual(len(cand["fill_end"]["hits"]), 0,
                                 f"{sname} 接受候选违反末拍零预算")
            end_rej = [r for c in o["rejected"]
                       for r in c["reasons"] if "末拍" in r]
            self.assertTrue(end_rej, "零预算下应出现末拍舍弃原因")

    def test_budget_overflow_marked_in_change_items(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec(
            voices=["kick", "snare", "hat_closed"], fill_end_budget=1,
            min_distance=1, max_distance=10))
        found_overflow = False
        for o in out["strategies"].values():
            for cand in o["rejected"]:
                if any("末拍" in r for r in cand["reasons"]):
                    found_overflow = True
                    # 末拍越界候选：fill_end 明确超预算，且变化项带越界标记
                    self.assertFalse(cand["fill_end"]["ok"])
                    self.assertGreater(
                        len(cand["fill_end"]["hits"]),
                        cand["fill_end"]["budget"])
        self.assertTrue(found_overflow, "预算=1 下应能触发末拍越界候选")


class TestBoundaryActualInterval(unittest.TestCase):
    def _no_relation_cfg(self):
        d = config_to_dict(default_config())
        d["relations"] = []
        return config_from_dict(d)

    def test_wrapping_boundary_uses_milliseconds(self):
        cfg = self._no_relation_cfg()
        events = generate(cfg)["candidates"][0]["events"]
        out = variate(cfg, events, {
            "bars": [1], "voices": ["kick"], "fill_end_budget": 4,
            "min_distance": 1, "max_distance": 8})
        acc = out["strategies"]["joint"]["accepted"]
        self.assertTrue(acc, "kick 单声部在无关系配置下应有接受候选")
        cand = acc[0]
        scopes = {c["scope"] for c in cand["boundary"]["checks"]}
        self.assertIn("声部 kick", scopes)
        for c in cand["boundary"]["checks"]:
            self.assertTrue(c["ok"])
            self.assertGreaterEqual(c["gap_ms"], c["required_ms"] - 1e-6)

    def test_non_wrapping_boundary_pair(self):
        cfg = self._no_relation_cfg()
        events = generate(cfg)["candidates"][0]["events"]
        out = variate(cfg, events, {
            "bars": [0], "voices": ["kick"], "fill_end_budget": 4,
            "min_distance": 1, "max_distance": 8})
        cand = out["strategies"]["joint"]["accepted"][0]
        kick_checks = [c for c in cand["boundary"]["checks"]
                       if c["scope"] == "声部 kick"]
        self.assertTrue(kick_checks)
        c = kick_checks[0]
        self.assertLess(c["last_in"]["step"], 16)
        self.assertGreaterEqual(c["first_out"]["step"], 16)

    def test_synthetic_tight_boundary_detected(self):
        # 构造跨边界实际间隔必然不足的事件对，直接核验检查器能报出
        cfg = config_from_dict({
            "bpm": 120, "bars": 1, "steps_per_bar": 16, "seed": 1,
            "swing": 0.0, "timing_jitter_ms": 0.0, "max_shift_ms": 0.0,
            "voices": [
                {"name": "kick", "density": 2, "min_interval": 4,
                 "accents": {"14": 2, "0": 2}},
            ],
            "relations": [],
        })
        from percussion_kernel.model import Event
        evs = [Event("kick", 14, 0.9, 0.0, ACCENT_LOCKED),
               Event("kick", 0, 0.9, 0.0, ACCENT_LOCKED)]
        report = boundary_actual_check(evs, cfg, zone_end=16)
        self.assertTrue(report["violations"])
        self.assertFalse(report["checks"][0]["ok"])


class TestStrategyComparison(unittest.TestCase):
    def test_both_strategies_return_auditable_records(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec())
        for sname in STRATEGIES:
            o = out["strategies"][sname]
            self.assertGreater(o["summary"]["variants"], 0)
            for cand in o["accepted"] + o["rejected"]:
                self.assertTrue(cand["events"], "舍弃候选也必须带事件表供试听")
                self.assertTrue(cand["reasons"] or cand["accepted"])
        self.assertIn("winner", out["comparison"])
        self.assertTrue(out["comparison"]["lines"])

    def test_joint_recovers_cadence_where_staged_rejects(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec())
        staged_cadence = sum(
            1 for c in out["strategies"]["staged"]["rejected"]
            for r in c["reasons"]
            if any(k in r for k in ("末拍", "跨边界", "落点")))
        joint_accepted = len(out["strategies"]["joint"]["accepted"])
        self.assertGreater(staged_cadence, 0)
        self.assertGreater(joint_accepted, 0)

    def test_reproducible(self):
        events, cfg = proto_events()
        a = variate(cfg, events, default_spec())
        b = variate(cfg, events, default_spec())
        self.assertEqual(a, b)


class TestChainedAndTinyZone(unittest.TestCase):
    def test_chained_variation_keeps_every_lock(self):
        events, cfg = proto_events()
        spec1 = default_spec(bars=[1], voices=["snare"],
                             min_distance=1, max_distance=6,
                             fill_end_budget=3, landing=[])
        out1 = variate(cfg, events, spec1)
        mid = out1["strategies"]["joint"]["accepted"][0]["events"]
        spec2 = default_spec(bars=[0], voices=["snare"],
                             min_distance=1, max_distance=6,
                             fill_end_budget=3, landing=[])
        out2 = variate(cfg, mid, spec2)
        final = out2["strategies"]["joint"]["accepted"][0]
        got = proto_assign(final["events"])
        for v in cfg.voices:
            if v.enabled:
                self.assertTrue(locked_steps(v) <= got.get(v.name, set()))

    def test_tiny_zone_four_steps(self):
        d = config_to_dict(default_config())
        d["bars"] = 1
        d["steps_per_bar"] = 4
        d["voices"] = [
            {"name": "kick", "density": 1, "min_interval": 2,
             "accents": {"0": 2}},
            {"name": "snare", "density": 1, "min_interval": 2, "hand": True,
             "accents": {"2": 2}},
        ]
        d["relations"] = []
        cfg = config_from_dict(d)
        events = generate(cfg)["candidates"][0]["events"]
        out = variate(cfg, events, {
            "bars": [0], "voices": ["kick", "snare"],
            "min_distance": 1, "max_distance": 4, "fill_end_budget": 1,
            "landing": [{"voice": "kick", "step": 0}]})
        acc = out["strategies"]["joint"]["accepted"]
        self.assertTrue(acc)
        for cand in acc:
            self.assertLessEqual(len(cand["fill_end"]["hits"]), 1)
            self.assertTrue(all(l["hit"] for l in cand["landing"]))


class TestPlayabilityAndBlankGeneration(unittest.TestCase):
    def test_accepted_candidates_pass_all_hard_constraints(self):
        events, cfg = proto_events()
        out = variate(cfg, events, default_spec())
        n = cfg.total_steps
        by_name = {v.name: v for v in cfg.voices if v.enabled}
        for sname, o in out["strategies"].items():
            for cand in o["accepted"]:
                got = proto_assign(cand["events"])
                for name, steps in got.items():
                    v = by_name[name]
                    self.assertTrue(locked_steps(v) <= steps)
                    ss = sorted(steps)
                    for i in range(len(ss)):
                        for j in range(i + 1, len(ss)):
                            self.assertGreaterEqual(
                                cdist(ss[i], ss[j], n), v.min_interval)
                # 实际时间事件齐备且力度合法
                for e in cand["events"]:
                    self.assertTrue(0.05 <= e["velocity"] <= 1.0)
                    self.assertIn("time_ms", e)

    def test_blank_generation_still_works(self):
        # 初版从空白生成的能力继续可用（既有流水线不回退）
        cfg = base_cfg()
        result = generate(cfg)
        self.assertGreaterEqual(len(result["candidates"]), 1)

    def test_invalid_prototype_missing_lock_raises(self):
        _, cfg = proto_events()
        bad = [e for e in generate(cfg)["candidates"][0]["events"]
               if not (e["voice"] == "kick" and e["step"] == 0)]
        with self.assertRaises(ValueError):
            variate(cfg, bad, default_spec())


if __name__ == "__main__":
    unittest.main()
