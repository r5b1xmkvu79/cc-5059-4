"""数据模型与配置校验。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

ACCENT_SOFT = 1    # 软重音：偏好，排序时加权
ACCENT_LOCKED = 2  # 锁定重音：硬约束，必须命中

VOICE_CATALOG = ["kick", "snare", "hat_closed", "hat_open", "tom_hi", "tom_lo", "rim", "clap"]

REL_KINDS = ("exclude", "unison", "answer")


@dataclass
class VoiceSpec:
    name: str
    density: int = 4            # 每段落的击打次数（硬约束，不会被动降低）
    min_interval: int = 2       # 声部内相邻击打最短间隔（步）
    accents: dict = field(default_factory=dict)   # step(绝对步号) -> 1 软 / 2 锁定
    hand: bool = False          # 是否属于双手组（受双手最短间隔约束）
    base_velocity: float = 0.8
    enabled: bool = True


@dataclass
class Relation:
    kind: str                   # exclude | unison | answer
    a: str
    b: str
    offset: int = 0             # answer：b 在 a 之后 offset 步呼应
    window: int = 0             # exclude：禁止距离 <= window 步（0 = 仅同步）


@dataclass
class Config:
    bpm: float = 96.0
    bars: int = 2
    steps_per_bar: int = 16
    seed: int = 1
    swing: float = 0.0              # 0..1，八分反拍延迟比例
    max_shift_ms: float = 12.0      # 微小时移上限（含摇摆+抖动）
    timing_jitter_ms: float = 3.0
    velocity_jitter: float = 0.08   # 力度抖动上限（比例）
    hand_min_interval: int = 2      # 双手组最短间隔（步）
    n_candidates: int = 3
    sync_weight: float = 0.6        # 排序：切分权重（重复结构权重 = 1 - 它）
    voices: list = field(default_factory=list)
    relations: list = field(default_factory=list)

    @property
    def total_steps(self) -> int:
        return self.bars * self.steps_per_bar

    @property
    def step_ms(self) -> float:
        return 60000.0 / self.bpm / (self.steps_per_bar / 4.0)

    @property
    def phrase_ms(self) -> float:
        return self.total_steps * self.step_ms

    def voice(self, name: str) -> VoiceSpec:
        for v in self.voices:
            if v.name == name:
                return v
        raise KeyError(f"未知声部: {name}")

    def strong_steps(self) -> list:
        """每小节四分音符强拍位置（绝对步号）。"""
        q = max(1, self.steps_per_bar // 4)
        return [b * self.steps_per_bar + i * q
                for b in range(self.bars) for i in range(4)]


@dataclass
class Event:
    voice: str
    step: int
    velocity: float = 0.8
    shift_ms: float = 0.0
    accent: int = 0

    def to_dict(self, cfg: Config) -> dict:
        return {
            "voice": self.voice,
            "step": self.step,
            "velocity": round(self.velocity, 4),
            "shift_ms": round(self.shift_ms, 3),
            "time_ms": round(self.step * cfg.step_ms + self.shift_ms, 3),
            "accent": self.accent,
        }


def _check_int(name, v, lo, hi):
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{name} 必须是整数")
    if not (lo <= v <= hi):
        raise ValueError(f"{name} 超出范围 [{lo}, {hi}]: {v}")
    return v


def _check_num(name, v, lo, hi):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{name} 必须是数值")
    if not (lo <= float(v) <= hi):
        raise ValueError(f"{name} 超出范围 [{lo}, {hi}]: {v}")
    return float(v)


def config_from_dict(d: dict) -> Config:
    if not isinstance(d, dict):
        raise ValueError("配置必须是 JSON 对象")
    cfg = Config()
    cfg.bpm = _check_num("bpm", d.get("bpm", cfg.bpm), 30, 300)
    cfg.bars = _check_int("bars", d.get("bars", cfg.bars), 1, 4)
    cfg.steps_per_bar = _check_int("steps_per_bar", d.get("steps_per_bar", cfg.steps_per_bar), 4, 32)
    cfg.seed = _check_int("seed", d.get("seed", cfg.seed), 0, 2**31 - 1)
    cfg.swing = _check_num("swing", d.get("swing", cfg.swing), 0.0, 1.0)
    cfg.max_shift_ms = _check_num("max_shift_ms", d.get("max_shift_ms", cfg.max_shift_ms), 0.0, 60.0)
    cfg.timing_jitter_ms = _check_num("timing_jitter_ms", d.get("timing_jitter_ms", cfg.timing_jitter_ms), 0.0, 30.0)
    cfg.velocity_jitter = _check_num("velocity_jitter", d.get("velocity_jitter", cfg.velocity_jitter), 0.0, 0.5)
    cfg.hand_min_interval = _check_int("hand_min_interval", d.get("hand_min_interval", cfg.hand_min_interval), 0, 8)
    cfg.n_candidates = _check_int("n_candidates", d.get("n_candidates", cfg.n_candidates), 1, 5)
    cfg.sync_weight = _check_num("sync_weight", d.get("sync_weight", cfg.sync_weight), 0.0, 1.0)

    n = cfg.total_steps
    voices = []
    for vd in d.get("voices", []):
        name = vd.get("name")
        if name not in VOICE_CATALOG:
            raise ValueError(f"未知声部: {name}")
        v = VoiceSpec(name=name)
        v.enabled = bool(vd.get("enabled", True))
        v.density = _check_int(f"{name}.density", vd.get("density", 4), 0, n)
        v.min_interval = _check_int(f"{name}.min_interval", vd.get("min_interval", 2), 1, n)
        v.hand = bool(vd.get("hand", False))
        v.base_velocity = _check_num(f"{name}.base_velocity", vd.get("base_velocity", 0.8), 0.05, 1.0)
        accents = {}
        for k, val in (vd.get("accents") or {}).items():
            step = int(k)
            if not (0 <= step < n):
                raise ValueError(f"{name} 重音步号越界: {step}")
            if val in (ACCENT_SOFT, ACCENT_LOCKED):
                accents[step] = val
        v.accents = accents
        voices.append(v)
    if not voices:
        raise ValueError("缺少声部配置")
    cfg.voices = voices

    names = {v.name for v in voices}
    relations = []
    for rd in d.get("relations", []):
        kind = rd.get("kind")
        if kind not in REL_KINDS:
            raise ValueError(f"未知关系类型: {kind}")
        a, b = rd.get("a"), rd.get("b")
        if a not in names or b not in names:
            raise ValueError(f"关系引用了未知声部: {a}, {b}")
        if a == b:
            raise ValueError("关系的两个声部不能相同")
        relations.append(Relation(
            kind=kind, a=a, b=b,
            offset=_check_int("relation.offset", rd.get("offset", 0), 0, n - 1),
            window=_check_int("relation.window", rd.get("window", 0), 0, 4),
        ))
    cfg.relations = relations
    return cfg


def config_to_dict(cfg: Config) -> dict:
    out = asdict(cfg)
    for vd in out["voices"]:
        vd["accents"] = {str(k): val for k, val in vd["accents"].items()}
    return out


def default_config() -> Config:
    """可行稀疏节奏的出厂配置（同时也是 UI 的初始预设之一）。"""
    d = {
        "bpm": 96, "bars": 2, "steps_per_bar": 16, "seed": 7,
        "swing": 0.18, "max_shift_ms": 12, "timing_jitter_ms": 3,
        "velocity_jitter": 0.08, "hand_min_interval": 2, "n_candidates": 3,
        "voices": [
            {"name": "kick", "density": 4, "min_interval": 3,
             "accents": {"0": 2, "8": 2, "16": 2, "24": 2}},
            {"name": "snare", "density": 4, "min_interval": 3, "hand": True,
             "accents": {"4": 2, "12": 2, "20": 2, "28": 2}},
            {"name": "hat_closed", "density": 12, "min_interval": 2, "hand": True,
             "accents": {"2": 1, "10": 1, "18": 1, "26": 1}},
            {"name": "hat_open", "density": 2, "min_interval": 6, "hand": True,
             "accents": {"14": 1, "30": 1}},
            {"name": "tom_hi", "density": 0, "min_interval": 2, "enabled": False},
            {"name": "tom_lo", "density": 0, "min_interval": 2, "enabled": False},
        ],
        "relations": [
            {"kind": "exclude", "a": "hat_open", "b": "hat_closed", "window": 0},
            {"kind": "answer", "a": "kick", "b": "snare", "offset": 4},
        ],
    }
    return config_from_dict(d)
