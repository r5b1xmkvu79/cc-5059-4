"""打击乐内核测试：python3 -m unittest discover -s tests -v"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from percussion_kernel.model import (
    ACCENT_LOCKED, config_from_dict, config_to_dict, default_config,
)
from percussion_kernel.solver import (
    SolveFailure, cdist, constructive, full_check, generate, minimal_core,
)
from percussion_kernel.timing import validate_actual


def sparse_cfg():
    return default_config()


def overdense_cfg():
    d = config_to_dict(default_config())
    for v in d["voices"]:
        if v["name"] == "hat_closed":
            v["density"] = 12
            v["min_interval"] = 3
    d["relations"] = []
    return config_from_dict(d)


def exclusive_cfg():
    d = config_to_dict(default_config())
    for v in d["voices"]:
        if v["name"] == "kick":
            v["density"] = 2
            v["accents"] = {"0": 2, "4": 2}
        if v["name"] == "snare":
            v["density"] = 2
            v["accents"] = {"4": 2, "12": 2}
        if v["name"] in ("hat_closed", "hat_open"):
            v["enabled"] = False
            v["density"] = 0
    d["relations"] = [{"kind": "exclude", "a": "kick", "b": "snare", "window": 0}]
    return config_from_dict(d)


def hand_repair_cfg():
    return config_from_dict({
        "bpm": 100, "bars": 1, "steps_per_bar": 16, "seed": 5,
        "hand_min_interval": 3, "swing": 0.0,
        "voices": [
            {"name": "snare", "density": 3, "min_interval": 2, "hand": True,
             "accents": {"0": 2, "10": 2}},
            {"name": "hat_closed", "density": 5, "min_interval": 2, "hand": True},
        ],
        "relations": [],
    })


def events_of(candidate):
    out = {}
    for e in candidate["events"]:
        out.setdefault(e["voice"], set()).add(e["step"])
    return out


class TestFeasibleSparse(unittest.TestCase):
    def test_candidates_satisfy_all_hard_constraints(self):
        cfg = sparse_cfg()
        result = generate(cfg)
        self.assertGreaterEqual(len(result["candidates"]), 1)
        n = cfg.total_steps
        by_name = {v.name: v for v in cfg.voices if v.enabled}
        for cand in result["candidates"]:
            ev = events_of(cand)
            for name, v in by_name.items():
                steps = ev.get(name, set())
                self.assertEqual(len(steps), v.density, f"{name} 密度被改动")
                self.assertTrue({s for s, l in v.accents.items()
                                 if l == ACCENT_LOCKED} <= steps)
                ss = sorted(steps)
                for i in range(len(ss)):
                    for j in range(i + 1, len(ss)):
                        self.assertGreaterEqual(cdist(ss[i], ss[j], n), v.min_interval)
            # answer(kick -> snare, +4)
            for s in ev["kick"]:
                self.assertIn((s + 4) % n, ev["snare"])
            # exclude(hat_open, hat_closed)
            for a in ev.get("hat_open", set()):
                for b in ev.get("hat_closed", set()):
                    self.assertGreater(cdist(a, b, n), 0)
            # 双手组步域
            hand = sorted({s for name, st in ev.items()
                           if by_name[name].hand for s in st})
            for i in range(len(hand)):
                for j in range(i + 1, len(hand)):
                    if hand[i] != hand[j]:
                        self.assertGreaterEqual(cdist(hand[i], hand[j], n),
                                                cfg.hand_min_interval)
            # 力度/时移上限
            for e in cand["events"]:
                self.assertLessEqual(abs(e["shift_ms"]), cfg.max_shift_ms + 1e-6)
                self.assertTrue(0.05 <= e["velocity"] <= 1.0)
            # 解释与分数存在
            self.assertTrue(cand["explanations"])
            self.assertIn("syncopation", cand["scores"])
            self.assertIn("repetition", cand["scores"])

    def test_reproducible_same_seed(self):
        cfg = sparse_cfg()
        r1 = generate(cfg)
        r2 = generate(cfg)
        self.assertEqual(r1["candidates"], r2["candidates"])

    def test_different_seed_differs(self):
        cfg = sparse_cfg()
        r1 = generate(cfg)
        d = config_to_dict(cfg)
        d["seed"] = 99
        r2 = generate(config_from_dict(d))
        self.assertNotEqual([c["events"] for c in r1["candidates"]],
                            [c["events"] for c in r2["candidates"]])


class TestOverdensity(unittest.TestCase):
    def test_unsat_core_and_no_silent_relaxation(self):
        cfg = overdense_cfg()
        with self.assertRaises(SolveFailure) as ctx:
            generate(cfg)
        f = ctx.exception
        self.assertEqual(f.kind, "unsat")
        ids = {c["id"] for c in f.core}
        self.assertIn("density:hat_closed", ids)
        self.assertIn("minint:hat_closed", ids)
        # 不静默降要求：配置保持原样
        self.assertEqual(cfg.voice("hat_closed").density, 12)
        self.assertEqual(cfg.voice("hat_closed").min_interval, 3)


class TestExclusiveAccents(unittest.TestCase):
    def test_core_contains_both_locks_and_relation(self):
        cfg = exclusive_cfg()
        with self.assertRaises(SolveFailure) as ctx:
            generate(cfg)
        ids = {c["id"] for c in ctx.exception.core}
        self.assertIn("lock:kick@4", ids)
        self.assertIn("lock:snare@4", ids)
        self.assertIn("rel:0", ids)

    def test_minimality(self):
        cfg = exclusive_cfg()
        core = minimal_core(cfg)
        self.assertIsNotNone(core)
        # 核中任意一条移除后应恢复可满足（极小性抽查）
        from percussion_kernel.solver import _constraints, _oracle
        self.assertFalse(_oracle(cfg, core))
        for c in core:
            rest = [x for x in core if x["id"] != c["id"]]
            self.assertTrue(_oracle(cfg, rest), f"移除 {c['id']} 后仍不可满足")


class TestHandRepair(unittest.TestCase):
    def test_constructive_rolls_back_free_hit_keeps_locked(self):
        cfg = hand_repair_cfg()
        assign, repairs, locked_conflicts = constructive(cfg, random.Random(cfg.seed))
        self.assertIsNotNone(assign, "构造式求解应成功")
        self.assertTrue(any("回退自由击打" in r for r in repairs),
                        "应发生自由击打回退")
        self.assertFalse(locked_conflicts)
        # 锁定强拍全部保留
        self.assertTrue({0, 10} <= set(assign["snare"]))
        self.assertTrue(full_check(assign, cfg))

    def test_generate_end_to_end(self):
        cfg = hand_repair_cfg()
        result = generate(cfg)
        self.assertGreaterEqual(len(result["candidates"]), 1)
        for cand in result["candidates"]:
            ev = events_of(cand)
            self.assertTrue({0, 10} <= ev["snare"])


class TestActualTimeFinalCheck(unittest.TestCase):
    def test_swing_does_not_reintroduce_conflict(self):
        d = config_to_dict(sparse_cfg())
        d["bpm"] = 150.0  # 步长 100ms：0.45*步长 = 45ms < 60ms 上限，触发收紧
        d["swing"] = 0.6
        d["max_shift_ms"] = 60.0  # 故意放宽：内核应收紧并告警
        cfg = config_from_dict(d)
        result = generate(cfg)
        self.assertTrue(any("收紧" in w for w in result["warnings"]))
        by_name = {v.name: v for v in cfg.voices if v.enabled}
        for cand in result["candidates"]:
            evs = cand["events"]
            hand_evs = sorted((e for e in evs if by_name[e["voice"]].hand),
                              key=lambda e: e["step"])
            for i in range(len(hand_evs)):
                for j in range(i + 1, len(hand_evs)):
                    a, b = hand_evs[i], hand_evs[j]
                    if a["step"] == b["step"]:
                        continue
                    # 加入摇摆后：不得塌缩到同一瞬间，次序不得颠倒
                    self.assertGreater(b["time_ms"] - a["time_ms"], 1.0)
            # 互斥关系以实际演奏时间复检
            for e1 in evs:
                for e2 in evs:
                    if {e1["voice"], e2["voice"]} == {"hat_open", "hat_closed"}:
                        self.assertGreater(
                            abs(e1["time_ms"] - e2["time_ms"]), 0.0)


if __name__ == "__main__":
    unittest.main()
