#!/usr/bin/env python3
"""
Check a generated dataset before spending RDFox time on it.

Verifies the invariants the scaling study depends on:

  * every shard is syntactically valid N-Quads;
  * the manifest's quad counts match the bytes on disk, and no quad is emitted
    twice -- so RDFox's post-load count should equal the manifest's exactly,
    and any discrepancy is a real finding rather than a generator artefact;
  * every named graph URI carries its text / section number / parent section /
    document, and every parentSection resolves to a described resource;
  * pv:tripleCount agrees with the size of the graph it describes;
  * every generated query parses, executes, and returns a non-empty result
    (a query bound to a constant that does not occur measures nothing);
  * the prefix-determinism claim: a smaller dataset generated separately is
    byte-identical to the corresponding prefix of this one.

Needs rdflib (`pip install rdflib`) and is meant for small datasets -- it loads
everything into memory. A few thousand graphs exercises every code path.

    python3 validate_dataset.py out/
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import subprocess
import sys

try:
    from rdflib import Dataset, URIRef
    from rdflib.plugins.sparql import prepareQuery
except ImportError:
    sys.exit("validate_dataset.py needs rdflib:  pip install rdflib")

GENERATOR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "generate_dataset.py")
PROV_PROPS = ("text", "sectionNumber", "parentSection", "document", "tripleCount")

failures = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{extra}]" if extra else ""))


RDFLIB_FORMAT = {"trig": "trig", "nquads": "nquads"}


def load_lines(out: str, upto_graphs: int | None = None) -> list:
    """Shard content in load order, optionally truncated at a checkpoint."""
    man = json.load(open(os.path.join(out, "manifest.json")))
    shards = man["shards"]
    if upto_graphs is not None:
        n = next(c["shards_to_load"] for c in man["checkpoints"]
                 if c["graphs"] == upto_graphs)
        shards = shards[:n]
    lines = []
    for s in shards:
        path = os.path.join(out, s["file"])
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="ascii") as fh:
            lines += fh.read().splitlines()
    return lines


def find_data_files(out: str) -> list:
    return sorted(
        glob.glob(os.path.join(out, "data", "*.trig*"))
        + glob.glob(os.path.join(out, "data", "*.nq*"))
    )


def main(out: str) -> int:
    man = json.load(open(os.path.join(out, "manifest.json")))
    base = man["config"]["base"]
    pv = base + "prov/"
    text_chars = man["config"]["text_chars"]
    fmt = man["config"].get("fmt", "nquads")

    print(f"== {fmt} syntax and counts ==")
    lines = load_lines(out)
    ds = Dataset()
    # One parse call for the whole corpus: rdflib's Dataset resets its default
    # graph on every parse(), which would silently drop earlier shards.
    # Repeating @base/@prefix mid-stream is legal TriG, so concatenating works.
    ds.parse(data="\n".join(lines), format=RDFLIB_FORMAT[fmt])
    check(f"all shards parse as {fmt}", True)

    parsed = sum(1 for _ in ds.quads((None, None, None, None)))
    check("parsed quad count == manifest quads",
          parsed == man["totals"]["quads"],
          f"{parsed} vs {man['totals']['quads']}")
    if fmt == "nquads":
        # One line per quad, so a shortfall here means a duplicate was emitted.
        emitted = len([x for x in lines if x.strip()])
        check("no quad emitted twice (RDFox should report exactly this many)",
              parsed == emitted, f"parsed {parsed}, emitted {emitted}")

    dg = ds.default_graph
    named = [g for g in ds.graphs() if str(g.identifier).startswith(base)]
    check("named graph count == manifest graphs",
          len(named) == man["totals"]["graphs"],
          f"{len(named)} vs {man['totals']['graphs']}")

    print("\n== provenance completeness ==")
    missing = [(str(g.identifier), p) for g in named for p in PROV_PROPS
               if text_chars or p != "text"
               if (URIRef(str(g.identifier)), URIRef(pv + p), None) not in dg]
    check("every named graph URI is described in the default graph",
          not missing, str(missing[:3]))

    dangling = {str(o) for _, _, o in dg.triples((None, URIRef(pv + "parentSection"), None))
                if (o, None, None) not in dg}
    check("every parentSection resolves to a described resource",
          not dangling, str(sorted(dangling)[:3]))

    bad = []
    for g in named:
        declared = dg.value(URIRef(str(g.identifier)), URIRef(pv + "tripleCount"))
        actual = len(list(ds.graph(g.identifier)))
        if declared is None or int(declared) != actual:
            bad.append((str(g.identifier), declared, actual))
    check("pv:tripleCount matches each graph's real size", not bad, str(bad[:3]))

    print("\n== queries ==")
    for qf in sorted(glob.glob(os.path.join(out, "queries", "*.rq"))):
        name = os.path.basename(qf)
        query = open(qf).read()
        try:
            prepareQuery(query)
            rows = list(ds.query(query))
        except Exception as exc:  # noqa: BLE001 - report, don't crash the run
            check(f"{name}", False, repr(exc)[:100])
            continue
        # An aggregate always yields one row; make sure it is not a zero count.
        values = [v for v in rows[0]] if rows else []
        nonempty = bool(rows) and (
            len(rows) > 1
            or not values
            or values[0] is None
            or not str(values[0]).isdigit()
            or int(str(values[0])) > 0
        )
        check(f"{name} parses, executes, returns results", nonempty, f"{len(rows)} rows")

    print("\n== prefix determinism ==")
    cp = [c["graphs"] for c in man["checkpoints"] if c["graphs"] < man["totals"]["graphs"]]
    if not cp:
        check("smaller dataset is a prefix of this one", True, "no smaller checkpoint")
    else:
        small_n = cp[len(cp) // 2]
        tmp = os.path.join(out.rstrip("/") + f".prefixcheck-{small_n}")
        # Rebuild the command line from the *whole* recorded config -- forwarding
        # a hand-picked subset would silently compare against a differently
        # parameterised dataset and blame the generator for it.
        cmd = [sys.executable, GENERATOR, "--out", tmp, "--graphs", str(small_n),
               "--checkpoints", str(small_n), "--workers", "1"]
        # Config field -> CLI flag, where the two names differ.
        flag_for = {"fmt": "--format"}
        for key, value in man["config"].items():
            if key in ("graphs", "gzip"):
                continue
            if key == "fanout":
                value = ",".join(map(str, value))
            cmd += [flag_for.get(key, "--" + key.replace("_", "-")), str(value)]
        if not man["config"]["gzip"]:
            cmd.append("--no-gzip")
        subprocess.run(cmd, check=True, capture_output=True)
        # Compare content lines only. TriG repeats its @base/@prefix header in
        # every shard, and the same graphs are split across a different number
        # of shards in the two runs, so the directive lines legitimately differ
        # in count while the data does not.
        def content(ls):
            return [x for x in ls if x.strip() and not x.startswith("@")]

        same = content(load_lines(tmp)) == content(load_lines(out, small_n))
        check(f"a separately generated {small_n:,}-graph dataset is byte-identical "
              f"to the first {small_n:,} graphs here", same)
        print(f"         (left {tmp} in place for inspection)")

    if text_chars:
        print("\n== text calibration ==")
        lens = [len(str(o)) for _, _, o in dg.triples((None, URIRef(pv + "text"), None))]
        mean = sum(lens) / len(lens)
        check(f"mean pv:text length is within 10% of --text-chars ({text_chars})",
              abs(mean - text_chars) / text_chars < 0.1, f"actual {mean:.0f}")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: validate_dataset.py <output-directory>")
    sys.exit(main(sys.argv[1]))
