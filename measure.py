#!/usr/bin/env python3
"""
Measurement harness: load a generated dataset into RDFox at increasing sizes and
record memory and query time at each point.

Method
------
For each storage type under test, ONE RDFox process is started and the dataset's
shards are imported cumulatively, stopping at every checkpoint in the manifest to
record `info` (memory, fact count) and to time every query. Measuring inside a
single process is deliberate: separate processes per size mostly measure
allocator state and page cache, which would swamp the effect being studied.

`sandbox` mode is used, so nothing is persisted and every run starts cold. That
also fixes the baseline at "freshly imported" -- a store restored from disk or
from a binary measures about 1% smaller, so the two must never be mixed.

Query timing does NOT use RDFox's own `Total statement evaluation time`, which
is printed to the millisecond and reads 0.000 s for the point-lookup queries at
every corpus size. Instead each query is repeated with `exec N` and the block is
timed from outside the process, so resolution comes from the host clock. N is
chosen per query per size to target a fixed amount of work, and the shell's own
per-statement cost is measured with a trivial control query and subtracted.

Usage
-----
    python3 measure.py --data out/ --rdfox /path/to/RDFox-macOS-arm64-7.6
    python3 report.py results/results.json        # re-render without re-running
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_TYPES = ("quad-table-sg-fi", "quad-table-sg-pi")

RE_MEMORY = re.compile(r"Aggregate memory consumed \(bytes\)\s*:\s*([\d,]+)")
RE_FACTS = re.compile(r"Aggregate number of explicit facts\s*:\s*([\d,]+)")
RE_IMPORT = re.compile(r"Import operation took ([\d.]+) s")
RE_QTIME = re.compile(r"Total statement evaluation time: ([\d.]+) s")
RE_QANSWERS = re.compile(r"Total number of query answers:\s*(\d+)")
RE_ERROR = re.compile(r"An error occurred|^Error:", re.MULTILINE)

MARK_CP = "@@CP"
MARK_Q = "@@Q"
MARK_BASE = "@@BASE"

# Timing a block of repeats from outside the process. RDFox's own
# `Total statement evaluation time` is printed to the millisecond, which is too
# coarse for the point-lookup queries -- they report 0.000 s at every size, so
# the most interesting result (that provenance lookup does not grow with corpus
# size) cannot be quantified. RDFox flushes stdout per command, so bracketing
# `exec N <script>` with echo markers and timestamping their arrival measures
# the block with the host clock's resolution instead.
TARGET_BLOCK_SECONDS = 0.15  # aim each measurement at roughly this much work
MIN_REPEATS, MAX_REPEATS = 3, 3000
CONTROL_REPEATS = 200
CONTROL_QUERY = "SELECT (1 AS ?one) WHERE { }"


class RDFoxShell:
    """Drives one long-lived RDFox shell over stdin, timing command blocks.

    Commands are written with echo markers around them; the wall time between
    the markers arriving on stdout is the block's duration.
    """

    def __init__(self, rdfox_bin: str, cwd: str):
        self.proc = subprocess.Popen(
            [rdfox_bin, "sandbox", "."],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
        self.log: list = []
        self._seq = 0

    def run(self, *commands: str) -> tuple:
        """Execute commands; return (output_lines, elapsed_seconds)."""
        self._seq += 1
        start, end = f"@@S{self._seq}", f"@@E{self._seq}"
        script = (
            f"echo {start}\n" + "".join(c + "\n" for c in commands) + f"echo {end}\n"
        )
        self.proc.stdin.write(script)
        self.proc.stdin.flush()

        lines, t_start, t_end = [], None, None
        for line in self.proc.stdout:
            self.log.append(line)
            stripped = line.strip()
            if stripped == start:
                t_start = time.perf_counter()
                continue
            if stripped == end:
                t_end = time.perf_counter()
                break
            if t_start is not None:
                lines.append(line)
        if t_end is None:
            raise RuntimeError(
                "RDFox shell closed unexpectedly:\n" + "".join(self.log[-40:])
            )
        return lines, t_end - t_start

    def close(self) -> None:
        try:
            self.proc.stdin.write("quit\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _info(lines: list) -> dict:
    out = {}
    for line in lines:
        m = RE_MEMORY.search(line)
        if m:
            out["memory_bytes"] = int(m.group(1).replace(",", ""))
        m = RE_FACTS.search(line)
        if m:
            out["facts"] = int(m.group(1).replace(",", ""))
        m = RE_QANSWERS.search(line)
        if m:
            out["answers"] = int(m.group(1))
        m = RE_QTIME.search(line)
        if m:
            out.setdefault("rdfox_reported", []).append(float(m.group(1)))
    return out


def _check(lines: list, what: str) -> None:
    errs = [l.rstrip() for l in lines if RE_ERROR.search(l)]
    if errs:
        sys.exit(f"RDFox error during {what}:\n" + "\n".join(errs[:8]))


def run_storage_type(storage_type: str, cfg: dict, verbose: bool) -> dict:
    sh = RDFoxShell(cfg["rdfox_bin"], cfg["rdfox_dir"])
    t0 = time.time()
    try:
        lines, _ = sh.run(
            f"dstore create bench quad-table-type {storage_type}",
            "active bench",
            "set output null",
        )
        _check(lines, "store creation")
        # Empty-store baseline: RDFox holds several MB before a single quad is
        # loaded, and that fixed cost would otherwise flatten the scaling fit.
        lines, _ = sh.run("info")
        baseline = _info(lines).get("memory_bytes", 0)

        points = []
        prev_shard = 0
        for cp in cfg["checkpoints"]:
            shards = cfg["shard_files"][prev_shard : cp["shards_to_load"]]
            prev_shard = cp["shards_to_load"]
            if not shards:
                continue

            lines, import_seconds = sh.run("import " + " ".join(shards))
            _check(lines, f"import at {cp['graphs']} graphs")
            lines, _ = sh.run("info")
            info = _info(lines)

            # Fixed per-statement cost of the shell (parse, dispatch, printing
            # the answer count), measured at this size and subtracted below so
            # the reported figure is the query's own marginal cost.
            _, control_elapsed = sh.run(
                f"exec {CONTROL_REPEATS} {cfg['control_script']}"
            )
            overhead = control_elapsed / CONTROL_REPEATS

            queries = {}
            for name, path in cfg["queries"]:
                probe_lines, probe = sh.run(f"answer {path}")
                _check(probe_lines, f"query {name}")
                per = max(probe - overhead, 1e-7)
                n = int(min(MAX_REPEATS, max(MIN_REPEATS, cfg["target_block"] / per)))
                _, block = sh.run(f"exec {n} {cfg['scripts'][name]}")
                seconds = max((block / n) - overhead, 0.0)
                queries[name] = {
                    "seconds": seconds,
                    "repeats": n,
                    "answers": _info(probe_lines).get("answers"),
                    "overhead_seconds": overhead,
                    "rdfox_reported_seconds": min(
                        _info(probe_lines).get("rdfox_reported", [0.0])
                    ),
                }

            points.append(
                {
                    "graphs": cp["graphs"],
                    "facts": info.get("facts"),
                    "memory_bytes": info.get("memory_bytes"),
                    "memory_net_bytes": (info.get("memory_bytes") or 0) - baseline,
                    "import_seconds": import_seconds,
                    "queries": queries,
                }
            )
            if verbose:
                print(
                    f"    {cp['graphs']:>9,} graphs  {info.get('facts', 0):>11,} quads  "
                    f"{(info.get('memory_bytes') or 0) / 2**20:>7,.0f} MiB  "
                    f"overhead {overhead * 1e6:.0f}us",
                    file=sys.stderr,
                )
    finally:
        sh.close()

    if not points:
        sys.exit(f"No datapoints for {storage_type}")
    return {
        "storage_type": storage_type,
        "wall_seconds": round(time.time() - t0, 1),
        "baseline_bytes": baseline,
        "points": points,
    }


# --------------------------------------------------------------------------- #
# Curve fitting -- pure Python, no numpy
# --------------------------------------------------------------------------- #


def fit_power_law(xs: list, ys: list) -> dict | None:
    """Least squares on (log x, log y): y = a * x^b.

    b is the headline number for a scaling study -- b close to 1 means memory
    (or time) grows in proportion to the data, b > 1 means it grows faster.
    """
    pts = [(x, y) for x, y in zip(xs, ys) if x > 0 and y and y > 0]
    if len(pts) < 3:
        return None
    import math

    lx = [math.log(x) for x, _ in pts]
    ly = [math.log(y) for _, y in pts]
    n = len(pts)
    mx, my = sum(lx) / n, sum(ly) / n
    sxx = sum((v - mx) ** 2 for v in lx)
    if sxx == 0:
        return None
    b = sum((lx[i] - mx) * (ly[i] - my) for i in range(n)) / sxx
    a = math.exp(my - b * mx)

    ss_res = sum((ly[i] - (math.log(a) + b * lx[i])) ** 2 for i in range(n))
    ss_tot = sum((v - my) ** 2 for v in ly)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {
        "a": a,
        "b": b,
        "r2": r2,
        "points_used": n,
        "points_dropped": len(xs) - n,
    }


def analyse(runs: list) -> dict:
    """Attach fits to every series the report will draw."""
    fits: dict = {"memory": {}, "memory_net": {}, "import": {}, "queries": {}}
    for run in runs:
        st = run["storage_type"]
        pts = run["points"]
        quads = [p.get("facts") or 0 for p in pts]

        fits["memory"][st] = fit_power_law(quads, [p.get("memory_bytes") for p in pts])
        # Net of the empty-store baseline: this is the number that actually
        # describes how the data itself scales.
        fits["memory_net"][st] = fit_power_law(
            quads, [p.get("memory_net_bytes") for p in pts]
        )
        fits["import"][st] = fit_power_law(
            quads, [p.get("import_seconds") for p in pts]
        )
        for name in sorted({q for p in pts for q in p["queries"]}):
            series = [p["queries"].get(name, {}).get("seconds") for p in pts]
            fits["queries"].setdefault(name, {})[st] = fit_power_law(quads, series)
    return fits


# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="dataset directory (with manifest.json)")
    ap.add_argument("--rdfox", required=True,
                    help="RDFox install directory, or the RDFox binary itself")
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES),
                    help="comma-separated quad-table-type values to compare")
    ap.add_argument("--target-block", type=float, default=TARGET_BLOCK_SECONDS,
                    help="seconds of repeated work to aim for per measurement; "
                         "higher is more precise and slower")
    ap.add_argument("--out", default="results", help="output directory")
    ap.add_argument("--min-graphs", type=int, default=0,
                    help="skip checkpoints below this size (they sit at the "
                         "timing resolution floor and add little)")
    ap.add_argument("--verbose", action="store_true", help="stream RDFox output")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)

    rdfox = os.path.abspath(args.rdfox)
    rdfox_dir, rdfox_bin = (
        (rdfox, os.path.join(rdfox, "RDFox"))
        if os.path.isdir(rdfox)
        else (os.path.dirname(rdfox), rdfox)
    )
    if not os.access(rdfox_bin, os.X_OK):
        sys.exit(f"No executable RDFox at {rdfox_bin}")

    data = os.path.abspath(args.data)
    manifest = json.load(open(os.path.join(data, "manifest.json")))
    if manifest["config"].get("fmt") != "trig":
        sys.exit(
            "This dataset is not TriG. RDFox's shell `import` picks its parser "
            "from the file extension and would parse it as Turtle. Regenerate "
            "with the default --format trig."
        )

    shard_files = [os.path.join(data, s["file"]) for s in manifest["shards"]]
    checkpoints = [c for c in manifest["checkpoints"] if c["graphs"] >= args.min_graphs]
    if not checkpoints:
        sys.exit("No checkpoints left after --min-graphs")
    queries = sorted(
        (q["name"], os.path.join(data, q["file"])) for q in manifest["queries"]
    )

    # `exec N <script>` repeats a shell script N times, which is how a block of
    # identical queries gets timed as one unit. One tiny script per query, plus
    # a control that measures the shell's own per-statement cost.
    script_dir = os.path.join(os.path.abspath(args.out), "scripts")
    os.makedirs(script_dir, exist_ok=True)
    control_rq = os.path.join(script_dir, "_control.rq")
    with open(control_rq, "w") as fh:
        fh.write(CONTROL_QUERY + "\n")
    control_script = os.path.join(script_dir, "_control.rdfox")
    with open(control_script, "w") as fh:
        fh.write(f"answer {control_rq}\n")
    scripts = {}
    for name, path in queries:
        s = os.path.join(script_dir, name + ".rdfox")
        with open(s, "w") as fh:
            fh.write(f"answer {path}\n")
        scripts[name] = s

    cfg = {
        "rdfox_dir": rdfox_dir,
        "rdfox_bin": rdfox_bin,
        "shard_files": shard_files,
        "checkpoints": checkpoints,
        "queries": queries,
        "scripts": scripts,
        "control_script": control_script,
        "target_block": args.target_block,
    }

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    print(
        f"{len(checkpoints)} checkpoints x {len(queries)} queries x "
        f"{args.target_block * 1000:.0f}ms per measurement x {len(types)} storage types",
        file=sys.stderr,
    )

    runs = []
    for st in types:
        print(f"  running {st} ...", file=sys.stderr, end="", flush=True)
        run = run_storage_type(st, cfg, args.verbose)
        top = run["points"][-1]
        print(
            f" {len(run['points'])} points, "
            f"{top.get('facts', 0):,} quads, "
            f"{top.get('memory_bytes', 0) / 2**20:,.0f} MiB "
            f"[{run['wall_seconds']}s]",
            file=sys.stderr,
        )
        runs.append(run)

    results = {
        "generated_unix_time": int(time.time()),
        "dataset": data,
        "dataset_config": manifest["config"],
        "dataset_totals": manifest["totals"],
        "target_block_seconds": args.target_block,
        "rdfox": rdfox_dir,
        "runs": runs,
        "fits": analyse(runs),
    }

    os.makedirs(args.out, exist_ok=True)
    results_path = os.path.join(args.out, "results.json")
    with open(results_path, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nresults: {results_path}", file=sys.stderr)

    if not args.no_report:
        import report

        html = report.render(results)
        report_path = os.path.join(args.out, "report.html")
        with open(report_path, "w") as fh:
            fh.write(html)
        print(f"report:  {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
