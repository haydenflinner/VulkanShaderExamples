#!/usr/bin/env python3
"""difftest.py — run the same example with --shaders glsl and --shaders rust,
then compare observable output. This is the difftest corpus harness for
rust-gpu: every example with a Rust shader port is run twice and checked
for parity.

Three comparison modes, picked automatically per example:
  * file     — example writes a framebuffer/output file (e.g. renderheadless
               writes headless.ppm); files are byte-compared.
  * stdout   — example prints computed results (e.g. computeheadless prints
               the buffer contents); the printed payload is compared.
  * screenshot — windowed example run with -b -ss (benchmark mode, then
               dump the last frame to .ppm). The rust frame is diffed
               against the glsl frame; a second glsl run provides the
               run-to-run noise baseline (animated scenes can't be
               byte-exact across runs). MATCH = rust-vs-glsl diff within
               the glsl-vs-glsl baseline; fps is recorded for triage.

Usage:
    tools/difftest.py [--bin-dir build/bin] [--examples a b c] [--json out.json]

Requires a Vulkan driver (llvmpipe/lavapipe works) and, for windowed
examples, a display (X or Xvfb).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Examples that produce a comparable file on disk.
FILE_OUTPUT = {
    "renderheadless": ["headless.ppm"],
}
# Examples whose stdout contains a deterministic computed payload we can diff.
STDOUT_PAYLOAD = {
    "computeheadless": r"Compute output:\n(.*?)\nFinished",
}
# Examples that are expected to need a window; run them in benchmark mode so
# they self-terminate after warmup+runtime seconds.
BENCH_WARMUP_S = 1
BENCH_RUNTIME_S = 2
TIMEOUT_S = 90


def rust_covered_examples() -> list[str]:
    """Examples with both a glsl/ and rust/ shader dir."""
    out = []
    for rust_dir in sorted((REPO / "shaders" / "rust").iterdir()):
        ex = rust_dir.name
        if (REPO / "examples" / ex).is_dir() and (REPO / "shaders" / "glsl" / ex).is_dir():
            out.append(ex)
    return out


def run_example(binary: Path, shaders: str, cwd: Path, windowed: bool,
                screenshot: Path | None = None) -> dict:
    cmd = [str(binary), "--shaders", shaders]
    if windowed:
        cmd += ["-b", "-bw", str(BENCH_WARMUP_S), "-br", str(BENCH_RUNTIME_S)]
    if screenshot is not None:
        cmd += ["-ss", str(screenshot)]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
        return {
            "exit": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit": None,
            "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            "stderr": (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
            "timeout": True,
        }


def extract_payload(stdout: str, pattern: str) -> str:
    m = re.search(pattern, stdout, re.DOTALL)
    return m.group(1).strip() if m else ""


def fps_from(stdout: str) -> float | None:
    m = re.search(r"fps\s*:\s*([0-9.]+)", stdout)
    return float(m.group(1)) if m else None


def ppm_pixels(data: bytes) -> bytes | None:
    """Return the raw RGB payload of a binary P6 .ppm, or None if unparseable."""
    if not data.startswith(b"P6"):
        return None
    # header is "P6\n<w>\n<h>\n<max>\n"; max ends with one whitespace byte
    parts = data.split(None, 4)
    if len(parts) < 5 or parts[3] != b"255":
        return None
    return parts[4]


def ppm_diff(a: bytes, b: bytes, stride: int = 16) -> float | None:
    """Fraction of sampled bytes that differ between two .ppm payloads."""
    pa, pb = ppm_pixels(a), ppm_pixels(b)
    if pa is None or pb is None or len(pa) != len(pb) or not pa:
        return None
    diffs = sum(1 for i in range(0, len(pa), stride) if pa[i] != pb[i])
    return diffs / ((len(pa) + stride - 1) // stride)


# rust-vs-glsl diff is a match when it stays under the glsl-vs-glsl
# baseline scaled by NOISE_FACTOR plus a small absolute slack
NOISE_FACTOR = 2.0
NOISE_SLACK = 0.005


def compare_one(example: str, bin_dir: Path) -> dict:
    binary = bin_dir / example
    if not binary.exists():
        return {"example": example, "status": "no-binary"}

    result = {"example": example, "status": "unknown", "runs": {}}

    if example in FILE_OUTPUT:
        files_glsl, files_rust = [], []
        for shaders, sink in (("glsl", files_glsl), ("rust", files_rust)):
            with tempfile.TemporaryDirectory() as td:
                r = run_example(binary, shaders, Path(td), windowed=False)
                result["runs"][shaders] = {"exit": r["exit"], "timeout": r["timeout"],
                                           "stderr_tail": r["stderr"][-500:]}
                for f in FILE_OUTPUT[example]:
                    p = Path(td) / f
                    sink.append(p.read_bytes() if p.exists() else None)
        if files_glsl[0] is None or files_rust[0] is None:
            result["status"] = "missing-output"
        else:
            same = files_glsl[0] == files_rust[0]
            result["status"] = "match" if same else "MISMATCH"
            result["file"] = FILE_OUTPUT[example][0]
            result["bytes"] = len(files_glsl[0])
        return result

    if example in STDOUT_PAYLOAD:
        pat = STDOUT_PAYLOAD[example]
        payloads = {}
        for shaders in ("glsl", "rust"):
            r = run_example(binary, shaders, Path(td if False else REPO), windowed=False)
            result["runs"][shaders] = {"exit": r["exit"], "timeout": r["timeout"],
                                       "stderr_tail": r["stderr"][-500:]}
            payloads[shaders] = extract_payload(r["stdout"], pat)
        result["status"] = "match" if payloads["glsl"] == payloads["rust"] and payloads["glsl"] else "MISMATCH"
        return result

    # Windowed: benchmark mode + screenshot capture.
    # glsl runs twice (baseline noise) and rust once; screenshots diffed.
    with tempfile.TemporaryDirectory() as td:
        shots = {}
        for tag, shaders in (("glsl_a", "glsl"), ("glsl_b", "glsl"), ("rust", "rust")):
            shot = Path(td) / f"{tag}.ppm"
            r = run_example(binary, shaders, REPO, windowed=True, screenshot=shot)
            result["runs"][tag] = {
                "shaders": shaders, "exit": r["exit"], "timeout": r["timeout"],
                "fps": fps_from(r["stdout"]), "stderr_tail": r["stderr"][-500:],
            }
            shots[tag] = shot.read_bytes() if shot.exists() else None
    a, b, ru = result["runs"]["glsl_a"], result["runs"]["glsl_b"], result["runs"]["rust"]
    ok_g = a["exit"] == 0 and not a["timeout"]
    ok_r = ru["exit"] == 0 and not ru["timeout"]
    # also record under "glsl"/"rust" keys so the summary printer works
    result["runs"]["glsl"] = a
    if not ok_g and not ok_r:
        result["status"] = "both-fail"
    elif ok_g != ok_r:
        result["status"] = "MISMATCH"
    elif shots["glsl_a"] is None or shots["rust"] is None:
        result["status"] = "missing-output"
    else:
        d_rg = ppm_diff(shots["rust"], shots["glsl_a"])
        d_gg = ppm_diff(shots["glsl_b"], shots["glsl_a"]) if shots["glsl_b"] else None
        result["diff_rust_vs_glsl"] = d_rg
        result["diff_glsl_baseline"] = d_gg
        if d_rg is None:
            result["status"] = "uncomparable"
        elif d_rg == 0:
            result["status"] = "match"
        else:
            baseline = d_gg if d_gg is not None else 0.0
            limit = max(baseline * NOISE_FACTOR, baseline + NOISE_SLACK)
            result["status"] = "match" if d_rg <= limit else "MISMATCH"
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin-dir", default=str(REPO / "build" / "bin"))
    ap.add_argument("--examples", nargs="*", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    bin_dir = Path(args.bin_dir)
    examples = args.examples or rust_covered_examples()
    results = [compare_one(ex, bin_dir) for ex in examples]

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        line = f"{r['status']:>13}  {r['example']}"
        if "runs" in r and "rust" in r["runs"]:
            fps = r["runs"]["rust"].get("fps")
            g_fps = r["runs"]["glsl"].get("fps")
            if fps is not None and g_fps:
                line += f"  (fps glsl={g_fps:.0f} rust={fps:.0f})"
        print(line)
    print("\nsummary:", json.dumps(counts))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))
    return 0 if counts.get("MISMATCH", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
