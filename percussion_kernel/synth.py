"""真实音频渲染：纯 Python 合成打击乐，输出 WAV（混音 + 各声部分轨）。"""
from __future__ import annotations

import hashlib
import json
import math
import random
import wave
from array import array
from pathlib import Path

SR = 44100
_rng = random.Random(20240501)
NOISE = [_rng.uniform(-1.0, 1.0) for _ in range(SR)]


def _add_kick(buf, i0, vel):
    phase = 0.0
    length = min(len(buf) - i0, int(0.24 * SR))
    for i in range(length):
        t = i / SR
        f = 46.0 + 115.0 * math.exp(-t * 30.0)
        phase += 2.0 * math.pi * f / SR
        buf[i0 + i] += vel * 1.25 * math.exp(-t * 20.0) * math.sin(phase)


def _add_snare(buf, i0, vel):
    phase = 0.0
    length = min(len(buf) - i0, int(0.18 * SR))
    for i in range(length):
        t = i / SR
        phase += 2.0 * math.pi * 185.0 / SR
        tone = 0.5 * math.sin(phase) * math.exp(-t * 45.0)
        noise = 0.8 * NOISE[i % SR] * math.exp(-t * 28.0)
        buf[i0 + i] += vel * (tone + noise)


def _add_hat(buf, i0, vel, decay):
    length = min(len(buf) - i0, int(decay * 6 * SR))
    prev = 0.0
    for i in range(length):
        t = i / SR
        x = NOISE[(i * 7) % SR]
        hp = x - prev  # 一阶差分近似高通
        prev = x
        buf[i0 + i] += vel * 0.55 * hp * math.exp(-t / decay)


def _add_tom(buf, i0, vel, f0):
    phase = 0.0
    length = min(len(buf) - i0, int(0.3 * SR))
    for i in range(length):
        t = i / SR
        f = f0 * (1.0 - 0.35 * t)
        phase += 2.0 * math.pi * f / SR
        buf[i0 + i] += vel * math.exp(-t * 11.0) * math.sin(phase)


def _add_rim(buf, i0, vel):
    length = min(len(buf) - i0, int(0.03 * SR))
    for i in range(length):
        t = i / SR
        buf[i0 + i] += vel * (0.6 * NOISE[i % SR] + 0.4 * math.sin(2 * math.pi * 880 * t)) \
            * math.exp(-t * 260.0)


def _add_clap(buf, i0, vel):
    length = min(len(buf) - i0, int(0.2 * SR))
    for i in range(length):
        t = i / SR
        burst = sum(math.exp(-max(0.0, t - d) * 90.0) for d in (0.0, 0.02, 0.045))
        buf[i0 + i] += vel * 0.7 * NOISE[(i * 3) % SR] * burst * math.exp(-t * 12.0)


DRUMS = {
    "kick": _add_kick,
    "snare": _add_snare,
    "hat_closed": lambda b, i, v: _add_hat(b, i, v, 0.008),
    "hat_open": lambda b, i, v: _add_hat(b, i, v, 0.05),
    "tom_hi": lambda b, i, v: _add_tom(b, i, v, 175.0),
    "tom_lo": lambda b, i, v: _add_tom(b, i, v, 125.0),
    "rim": _add_rim,
    "clap": _add_clap,
}


def _write_wav(path: Path, buf):
    out = array("h", bytes(2 * len(buf)))
    for i, x in enumerate(buf):
        y = math.tanh(x * 0.9) * 0.92
        out[i] = int(max(-1.0, min(1.0, y)) * 32767)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(out.tobytes())


def render(events: list, phrase_ms: float, out_dir: Path, voices: list):
    """events: [{voice, time_ms, velocity}]。返回 {tag, mix, stems, duration_ms}。"""
    tag = hashlib.sha1(json.dumps(
        {"e": [(e["voice"], round(e["time_ms"], 2), round(e["velocity"], 3)) for e in events],
         "p": round(phrase_ms, 1), "v": sorted(voices)},
        sort_keys=True).encode()).hexdigest()[:12]
    mix_path = out_dir / f"{tag}_mix.wav"
    stem_paths = {v: out_dir / f"{tag}_{v}.wav" for v in voices}
    duration_ms = phrase_ms + 450.0

    if mix_path.exists() and all(p.exists() for p in stem_paths.values()):
        return {"tag": tag, "mix": mix_path.name,
                "stems": {v: p.name for v, p in stem_paths.items()},
                "duration_ms": round(duration_ms, 1)}

    n = int(duration_ms / 1000.0 * SR) + 1
    stems = {v: array("f", bytes(4 * n)) for v in voices}
    for e in events:
        voice = e["voice"]
        if voice not in stems:
            continue
        fn = DRUMS.get(voice, _add_rim)
        i0 = max(0, min(n - 1, int(e["time_ms"] / 1000.0 * SR)))
        fn(stems[voice], i0, e["velocity"])

    mix = array("f", bytes(4 * n))
    for v, buf in stems.items():
        for i in range(n):
            mix[i] += buf[i]
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_wav(mix_path, mix)
    for v, buf in stems.items():
        _write_wav(stem_paths[v], buf)
    return {"tag": tag, "mix": mix_path.name,
            "stems": {v: p.name for v, p in stem_paths.items()},
            "duration_ms": round(duration_ms, 1)}
