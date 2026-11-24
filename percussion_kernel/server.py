"""HTTP 服务：打击乐内核驱动控件，失败反馈回打击乐。

启动：python3 -m percussion_kernel.server [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .model import config_from_dict, config_to_dict, default_config, VOICE_CATALOG
from .solver import SolveFailure, generate
from .synth import render
from .timing import validate_actual, apply_timing
from .variation import VariationFailure, variate
from .model import Event

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUT = ROOT / "out"
SESS = ROOT / "sessions"

PRESETS = {
    "sparse": {
        "label": "可行稀疏节奏",
        "config": None,  # 由 default_config 提供
    },
    "overdense": {
        "label": "过高密度（不可解演示）",
        "config": {
            "voices": [
                {"name": "kick", "density": 4, "min_interval": 3,
                 "accents": {"0": 2, "16": 2}},
                {"name": "snare", "density": 4, "min_interval": 3, "hand": True,
                 "accents": {"4": 2, "12": 2, "20": 2, "28": 2}},
                {"name": "hat_closed", "density": 12, "min_interval": 3, "hand": True},
                {"name": "hat_open", "density": 0, "min_interval": 6, "enabled": False},
                {"name": "tom_hi", "density": 0, "enabled": False},
                {"name": "tom_lo", "density": 0, "enabled": False},
            ],
            "relations": [],
        },
    },
    "exclusive": {
        "label": "互斥重音（不可解演示）",
        "config": {
            "voices": [
                {"name": "kick", "density": 2, "min_interval": 2,
                 "accents": {"0": 2, "4": 2}},
                {"name": "snare", "density": 2, "min_interval": 2,
                 "accents": {"4": 2, "12": 2}},
                {"name": "hat_closed", "density": 8, "min_interval": 2, "hand": True},
                {"name": "hat_open", "density": 0, "enabled": False},
                {"name": "tom_hi", "density": 0, "enabled": False},
                {"name": "tom_lo", "density": 0, "enabled": False},
            ],
            "relations": [
                {"kind": "exclude", "a": "kick", "b": "snare", "window": 0},
            ],
        },
    },
}


def _preset_config(name: str) -> dict:
    base = config_to_dict(default_config())
    p = PRESETS[name]
    if p["config"] is None:
        return base
    merged = dict(base)
    merged.update(p["config"])
    return merged


def _json(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _file(handler, path: Path, ctype: str):
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _audition_payload(body):
    cfg = config_from_dict(body.get("config", {}))
    events = body.get("events", [])
    for e in events:
        if e.get("voice") not in VOICE_CATALOG:
            raise ValueError(f"未知声部: {e.get('voice')}")
        if not (0 <= int(e.get("step", -1)) < cfg.total_steps):
            raise ValueError(f"事件步号越界: {e}")
    voice_names = [v.name for v in cfg.voices if v.enabled]
    info = render(events, cfg.phrase_ms, OUT, voice_names)
    return cfg, {
        "mix_url": f"/audio/{info['mix']}",
        "stems": {v: f"/audio/{name}" for v, name in info["stems"].items()},
        "duration_ms": info["duration_ms"],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/":
                return _file(self, WEB / "index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return _file(self, WEB / "app.js", "application/javascript; charset=utf-8")
            if path == "/style.css":
                return _file(self, WEB / "style.css", "text/css; charset=utf-8")
            if path.startswith("/audio/"):
                name = Path(path).name
                f = OUT / name
                if f.exists() and f.suffix == ".wav":
                    return _file(self, f, "audio/wav")
                return _json(self, 404, {"error": "音频不存在"})
            if path == "/api/default":
                return _json(self, 200, {
                    "config": config_to_dict(default_config()),
                    "voices_catalog": VOICE_CATALOG,
                    "presets": {k: {"label": v["label"], "config": _preset_config(k)}
                                for k, v in PRESETS.items()},
                })
            if path == "/api/load":
                sid = parse_qs(url.query).get("id", [""])[0]
                f = SESS / f"{sid}.json"
                if sid and f.exists():
                    return _json(self, 200, json.loads(f.read_text("utf-8")))
                return _json(self, 404, {"error": "未找到该存档"})
            return _json(self, 404, {"error": "未知路径"})
        except Exception as e:  # noqa: BLE001
            return _json(self, 500, {"error": str(e)})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self._body()
            if url.path == "/api/generate":
                cfg = config_from_dict(body.get("config", {}))
                try:
                    result = generate(cfg)
                except SolveFailure as f:
                    return _json(self, 200, {
                        "status": f.kind, "message": f.message,
                        "core": [{"id": c["id"], "text": c["text"]} for c in f.core],
                        "config": config_to_dict(cfg),
                    })
                result.update({"status": "ok", "config": config_to_dict(cfg),
                               "seed": cfg.seed})
                return _json(self, 200, result)

            if url.path == "/api/variate":
                cfg = config_from_dict(body.get("config", {}))
                try:
                    result = variate(cfg, body.get("prototype", []),
                                     body.get("variation", {}))
                except VariationFailure as vf:
                    return _json(self, 200, {
                        "status": vf.kind, "message": vf.message,
                        "reasons": vf.reasons,
                        "config": config_to_dict(cfg),
                    })
                result.update({"config": config_to_dict(cfg), "seed": cfg.seed})
                return _json(self, 200, result)

            if url.path == "/api/audition":
                cfg, payload = _audition_payload(body)
                payload["actual_events"] = [
                    {"voice": e["voice"], "step": e["step"],
                     "time_ms": round(e["step"] * cfg.step_ms + e.get("shift_ms", 0.0), 2),
                     "velocity": e.get("velocity", 0.8)}
                    for e in sorted(body.get("events", []),
                                    key=lambda x: (x["step"], x["voice"]))]
                return _json(self, 200, payload)

            if url.path == "/api/validate":
                cfg = config_from_dict(body.get("config", {}))
                evs = [Event(voice=e["voice"], step=int(e["step"]),
                             velocity=float(e.get("velocity", 0.8)),
                             shift_ms=float(e.get("shift_ms", 0.0)))
                       for e in body.get("events", [])]
                violations = _validate_steps(evs, cfg)
                _, adjustments, time_violations = validate_actual(
                    [Event(**vars(e)) for e in evs], cfg)
                violations += time_violations
                return _json(self, 200, {"ok": not violations,
                                         "violations": violations,
                                         "adjustments": adjustments})

            if url.path == "/api/save":
                sid = f"{int(time.time()) & 0xffffff:x}"
                payload = {
                    "id": sid,
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "seed": body.get("seed"),
                    "config": body.get("config"),
                    "chosen": body.get("chosen"),
                    "edited_events": body.get("edited_events"),
                }
                SESS.mkdir(parents=True, exist_ok=True)
                (SESS / f"{sid}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
                return _json(self, 200, {"id": sid})

            return _json(self, 404, {"error": "未知路径"})
        except ValueError as e:
            return _json(self, 400, {"status": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            return _json(self, 500, {"status": "error", "message": str(e)})


def _validate_steps(events, cfg) -> list:
    """编辑结果的硬约束校验（步域）。"""
    from .solver import cdist, locked_steps, _relation_ok, _hand_ok
    n = cfg.total_steps
    by_name = {v.name: v for v in cfg.voices if v.enabled}
    assign = {}
    for e in events:
        assign.setdefault(e.voice, set()).add(e.step)
    out = []
    for name, steps in assign.items():
        v = by_name.get(name)
        if v is None:
            out.append(f"未知或未启用声部: {name}")
            continue
        missing = locked_steps(v) - steps
        if missing:
            out.append(f"{name} 缺少锁定重音: {sorted(missing)}")
        ss = sorted(steps)
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                if cdist(ss[i], ss[j], n) < v.min_interval:
                    out.append(f"{name}: {ss[i]} 与 {ss[j]} 间隔小于 {v.min_interval} 步")
    for rel in cfg.relations:
        a, b = assign.get(rel.a, set()), assign.get(rel.b, set())
        if not _relation_ok(rel, frozenset(a), frozenset(b), n):
            out.append(f"关系被破坏: {rel.kind} {rel.a}/{rel.b}")
    pairs = [(e.step, e.voice) for e in events if by_name.get(e.voice) and by_name[e.voice].hand]
    if not _hand_ok(pairs, n, cfg.hand_min_interval):
        out.append("双手组最短间隔被破坏")
    return out


def main():
    ap = argparse.ArgumentParser(description="打击乐节奏创作工具")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    SESS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"打击乐节奏创作工具已启动: http://localhost:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
