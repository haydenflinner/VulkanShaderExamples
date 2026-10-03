#!/usr/bin/env python3
"""benchmark.py — run every rust-covered example in benchmark mode for a
fixed duration under each shader language and record fps. This is the
runtime benchmark matrix for rust-gpu (issue #315): same binary, same
scene, --shaders glsl vs --shaders rust → glslang-vs-rust-gpu codegen
perf per example.

Output is a JSON document with the git SHA, timestamp and per-example
fps so results can be trended (e.g. checked into a gh-pages dashboard
or diffed across commits CI-side).

Usage:
    tools/benchmark.py [--bin-dir build/bin] [--examples a b c]
                       [--duration-s 10] [--warmup-s 3] [--json out.json]

Requires a Vulkan driver (llvmpipe/lavapipe works; absolute fps values
on a software rasterizer are only meaningful relative to each other on
the same machine) and a display (X or Xvfb) for windowed examples.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIMEOUT_S = 120


def rust_covered_examples() -> list[str]:
    out = []
    for rust_dir in sorted((REPO / "shaders" / "rust").iterdir()):
        ex = rust_dir.name
        if (REPO / "examples" / ex).is_dir() and (REPO / "shaders" / "glsl" / ex).is_dir():
            out.append(ex)
    return out


def run_bench(binary: Path, shaders: str, warmup: float, duration: float) -> dict:
    cmd = [str(binary), "--shaders", shaders,
           "-b", "-bw", str(warmup), "-br", str(duration)]
    try:
        proc = subprocess.run(cmd, cwd=REPO, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              timeout=TIMEOUT_S + warmup + duration)
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True}
    m = re.search(r"fps\s*:\s*([0-9.]+)", proc.stdout)
    return {
        "ok": proc.returncode == 0,
        "timeout": False,
        "exit": proc.returncode,
        "fps": float(m.group(1)) if m else None,
        "stderr_tail": proc.stderr[-300:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin-dir", default=str(REPO / "build" / "bin"))
    ap.add_argument("--examples", nargs="*", default=None)
    ap.add_argument("--warmup-s", type=float, default=3.0)
    ap.add_argument("--duration-s", type=float, default=10.0)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    bin_dir = Path(args.bin_dir)
    examples = args.examples or rust_covered_examples()

    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=REPO, capture_output=True, text=True).stdout.strip()
    doc = {
        "git_sha": sha,
        "date": datetime.now(timezone.utc).isoformat(),
        "warmup_s": args.warmup_s,
        "duration_s": args.duration_s,
        "examples": {},
    }

    for ex in examples:
        binary = bin_dir / ex
        if not binary.exists():
            doc["examples"][ex] = {"status": "no-binary"}
            continue
        entry = {}
        for shaders in ("glsl", "rust"):
            entry[shaders] = run_bench(binary, shaders, args.warmup_s, args.duration_s)
        g, r = entry["glsl"], entry["rust"]
        if g["ok"] and r["ok"] and g.get("fps") and r.get("fps"):
            entry["status"] = "ok"
            entry["rust_vs_glsl"] = round(r["fps"] / g["fps"], 3)
        elif g["ok"] != r["ok"]:
            entry["status"] = "lang-mismatch"
        else:
            entry["status"] = "both-fail"
        doc["examples"][ex] = entry
        fps = {s: (entry[s].get("fps") or 0) for s in ("glsl", "rust")}
        print(f"{entry['status']:>13}  {ex}  glsl={fps['glsl']:.0f} rust={fps['rust']:.0f}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
