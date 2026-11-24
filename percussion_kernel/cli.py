"""命令行辅助工具：生成 / 变奏并渲染 WAV。

图形界面（python3 -m percussion_kernel.server）是主入口；本 CLI 仅用于
无界面环境下的快速核验与回归，输出音频到 out/cli/ 并打印约束摘要。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .model import config_from_dict, config_to_dict, default_config
from .server import OUT
from .solver import SolveFailure, generate
from .synth import render
from .variation import VariationFailure, variate

CLI_OUT = OUT / "cli"


def _load_config(path: str | None):
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return config_from_dict(json.load(f))
    return default_config()


def _write_wav(tag: str, events: list, cfg) -> dict:
    voices = [v.name for v in cfg.voices if v.enabled]
    info = render(events, cfg.phrase_ms, CLI_OUT, voices)
    return {"mix": str(CLI_OUT / info["mix"]),
            "stems": {v: str(CLI_OUT / name) for v, name in info["stems"].items()}}


def cmd_generate(args) -> int:
    cfg = _load_config(args.config)
    if args.seed is not None:
        cfg.seed = args.seed
    try:
        result = generate(cfg)
    except SolveFailure as f:
        print(f"[失败/{f.kind}] {f.message}")
        for c in f.core:
            print(f"  - {c['id']}: {c['text']}")
        return 2
    cand = result["candidates"][args.candidate]
    paths = _write_wav("gen", cand["events"], cfg)
    print(f"[生成] 候选 {args.candidate + 1}/{len(result['candidates'])}，"
          f"{len(cand['events'])} 击，综合分 {cand['scores']['total']}")
    print(f"  混音: {paths['mix']}")
    for line in cand["explanations"]:
        print(f"  · {line}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"config": config_to_dict(cfg),
                        "events": cand["events"]}, ensure_ascii=False, indent=1),
            "utf-8")
        print(f"  事件 JSON: {args.json_out}")
    return 0


def cmd_variate(args) -> int:
    cfg = _load_config(args.config)
    with open(args.prototype, "r", encoding="utf-8") as f:
        payload = json.load(f)
    prototype = payload["events"] if "events" in payload else payload
    variation = json.loads(args.variation)
    try:
        result = variate(cfg, prototype, variation)
    except VariationFailure as vf:
        print(f"[变奏失败/{vf.kind}] {vf.message}")
        for why in vf.reasons:
            print(f"  - {why}")
        return 2
    for line in result["comparison"]["lines"]:
        print(f"  对照 · {line}")
    strat = result["strategies"].get(args.strategy)
    if not strat or not strat["accepted"]:
        print(f"[变奏失败] 策略 {args.strategy} 无接受候选")
        for sname, o in result["strategies"].items():
            for r in o["rejected"][:5]:
                print(f"  {sname} 舍弃: {'；'.join(r['reasons'])}")
        return 3
    cand = strat["accepted"][0]
    paths = _write_wav("var", cand["events"], cfg)
    print(f"[变奏/{args.strategy}] 距离 {cand['distance']}，"
          f"末拍预算 {'满足' if cand['fill_end']['ok'] else '超限'}")
    print(f"  混音: {paths['mix']}")
    for ch in cand["changes"]:
        flag = "允许" if ch["allowed"] else "越界"
        print(f"  · [{flag}] {ch['voice']}@{ch['step']} {ch['detail']}")
    for bc in cand["boundary"]["checks"]:
        print(f"  · 跨边界 {bc['scope']}: {bc['gap_ms']}ms / 要求 {bc['required_ms']}ms"
              f" {'通过' if bc['ok'] else '不足'}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"config": config_to_dict(cfg),
                        "events": cand["events"],
                        "variation_result": result}, ensure_ascii=False, indent=1),
            "utf-8")
        print(f"  事件 JSON: {args.json_out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="打击乐命令行辅助（主入口为图形界面：python3 -m percussion_kernel.server）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="从约束配置生成节奏并渲染 WAV")
    g.add_argument("--config", help="配置 JSON（缺省用出厂配置）")
    g.add_argument("--seed", type=int)
    g.add_argument("--candidate", type=int, default=0)
    g.add_argument("--json-out", help="把所选候选事件写成 JSON，可作为变奏原型")
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("variate", help="对原型做保留主型的变奏/填充")
    v.add_argument("--config", help="配置 JSON（缺省用出厂配置）")
    v.add_argument("--prototype", required=True,
                   help="原型事件 JSON（generate --json-out 的产物，或纯事件数组）")
    v.add_argument("--variation", required=True,
                   help='变奏参数 JSON，如 \'{"bars":[1],"fill_end_budget":2}\'')
    v.add_argument("--strategy", choices=("staged", "joint"), default="joint")
    v.add_argument("--json-out")
    v.set_defaults(func=cmd_variate)

    args = ap.parse_args(argv)
    CLI_OUT.mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
