#!/usr/bin/env python3
"""
Synthetic dataset generator for the RDFox named-graph provenance stress test.

Data pattern
------------
A corpus of documents is split into a section tree.  Every *leaf* subsection
becomes a **named graph** holding the triples "extracted" from that subsection.
The named graph URI is itself a subject in the **default graph**, where it is
linked to its original text, its section number and its parent section:

    # default graph
    <.../sec/12/3.2.7>  rdf:type          pv:Subsection ;
                        pv:text           "the full text of the subsection ..." ;
                        pv:sectionNumber  "3.2.7" ;
                        pv:parentSection  <.../sec/12/3.2> ;
                        pv:document       <.../doc/12> ;
                        pv:title          "..." ;
                        pv:ordinal        7 ;
                        pv:tripleCount    17 .

    # named graph
    GRAPH <.../sec/12/3.2.7> {
        <.../e/8123>  rdf:type      <.../c/4> ;
                      rdfs:label    "Vosen Ridal" ;
                      <.../p/3>     <.../e/91> ;
                      <.../p/19>    "1974-03-02"^^xsd:date .
    }

Design points that matter for a scaling study
---------------------------------------------
* **Prefix determinism.**  Graph *i* depends only on (seed, i) -- never on the
  total number of graphs.  So the 10k-graph dataset is a byte-exact prefix of
  the 1M-graph one.  You can therefore load shard after shard into a *single*
  RDFox process and record memory/query time at each checkpoint, instead of
  comparing separate processes (which mostly measures allocator noise).
* **Checkpoints.**  Shard boundaries are placed exactly on the requested
  checkpoints, so "import shards 0..k" lands on a round number of graphs.
* **No duplicate quads emitted.**  Triples are deduplicated within each graph,
  and every provenance subject is emitted exactly once, so the quad counts in
  `manifest.json` are what RDFox should report after loading.  Cross-graph
  repetition of the *same* triple is deliberate and is the point of the test:
  it is what a quad table has to store once per graph.
* **Entity reuse is tunable** in three tiers (global hubs / recently seen /
  brand new), which controls how much cross-graph joining the queries have to
  do and how fast the resource dictionary grows.
* **Text length is a first-class knob** (``--text-chars 0`` turns text off) so
  the memory cost of the long provenance literals can be isolated.

Output
------
    out/data/part-00000.nq.gz ...   gzipped N-Quads, one shard per range
    out/queries/q01_*.rq ...        SPARQL queries pre-bound to constants that
                                    really occur in the data
    out/manifest.json               parameters, per-shard and per-checkpoint
                                    cumulative counts, query constants

Example
-------
    python3 generate_dataset.py --graphs 100000 --workers 8
    python3 generate_dataset.py --graphs 5000000 --text-chars 0 --out out-notext
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace

# --------------------------------------------------------------------------- #
# Vocabulary URIs
# --------------------------------------------------------------------------- #

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
RDFS_LABEL = RDFS_NS + "label"
XSD = "http://www.w3.org/2001/XMLSchema#"

XSD_INT = XSD + "integer"
XSD_DEC = XSD + "decimal"
XSD_DATE = XSD + "date"
XSD_BOOL = XSD + "boolean"

ATTR_DATATYPES = (None, XSD_INT, XSD_DEC, XSD_DATE, XSD_BOOL)


@dataclass(frozen=True)
class Config:
    """Everything that influences the bytes on disk. Stored in the manifest."""

    graphs: int = 10_000
    seed: int = 42
    base: str = "https://rdfox-stress.example.org/"

    # ---- section tree -----------------------------------------------------
    # fanout[k] = number of children of a node at level k.
    # len(fanout) = number of section levels below the document.
    fanout: tuple = (8, 6)

    # ---- size of each named graph ----------------------------------------
    triples_per_graph: float = 20.0
    triples_sigma: float = 0.6  # 0 => every graph has exactly the mean
    entity_ratio: float = 0.35  # distinct entities per graph, as a fraction of triples

    # ---- entity reuse (drives cross-graph joins and dictionary growth) ----
    head_entities: int = 10_000  # size of the globally shared "hub" pool
    head_skew: float = 1.0  # Zipf exponent over the hub pool; 0 = uniform
    local_per_graph: int = 4  # id block reserved for each graph's own entities
    p_global: float = 0.25  # P(entity slot is drawn from the hub pool)
    p_recent: float = 0.45  # P(entity slot is drawn from a nearby graph's block)
    recent_window: int = 40  # how far back "nearby" reaches

    # ---- predicates / classes --------------------------------------------
    relation_predicates: int = 16
    attribute_predicates: int = 8
    classes: int = 12
    label_prob: float = 0.35  # P(an entity gets an rdfs:label in this graph)

    # ---- provenance text --------------------------------------------------
    text_chars: int = 600  # mean length of pv:text; 0 disables the property
    vocab_size: int = 20_000
    vocab_skew: float = 1.1

    # ---- output -----------------------------------------------------------
    # TriG is the default because RDFox's shell `import` picks its parser from
    # the file extension and only recognises .ttl/.trig/.dlog -- an .nq file is
    # parsed as Turtle and every quad fails. N-Quads remains available for the
    # REST endpoint (which does accept application/n-quads) and other tooling.
    fmt: str = "trig"
    gzip: bool = True
    compresslevel: int = 1
    max_shard_graphs: int = 250_000

    @property
    def predicates(self) -> int:
        return self.relation_predicates + self.attribute_predicates


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #

MASK64 = (1 << 64) - 1


def mix64(x: int) -> int:
    """splitmix64 finalizer -- a cheap, stable, well-mixed integer hash."""
    x = (x + 0x9E3779B97F4A7C15) & MASK64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & MASK64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & MASK64
    x ^= x >> 31
    return x


def zipf_cum_weights(n: int, s: float) -> list:
    """Cumulative weights for a Zipf(s) distribution over range(n)."""
    cum, total = [], 0.0
    for k in range(n):
        total += 1.0 / ((k + 1) ** s)
        cum.append(total)
    return cum


def build_vocab(seed: int, size: int) -> list:
    """Pronounceable ASCII-only pseudo-words. ASCII-only keeps the N-Quads
    writer free of any escaping in the hot path."""
    rng = random.Random(mix64(seed ^ 0xC0FFEE))
    cons = "bcdfghjklmnprstvwz"
    vows = "aeiou"
    words, seen = [], set()
    while len(words) < size:
        syllables = rng.randint(1, 4)
        w = "".join(rng.choice(cons) + rng.choice(vows) for _ in range(syllables))
        if rng.random() < 0.4:
            w += rng.choice(cons)
        if w not in seen:
            seen.add(w)
            words.append(w)
    return words


class State:
    """Derived, read-only data shared by every graph built in this process."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        b = cfg.base
        self.PV = b + "prov/"

        # Term rendering differs by format. TriG declares @base and @prefix once
        # per file and then writes short relative IRIs and prefixed names; that
        # is a large size win over N-Quads, which has neither and must spell
        # every IRI out in full. Everything below is built once and then
        # concatenated many millions of times.
        trig = cfg.fmt == "trig"
        u = "" if trig else b  # IRI stem: relative under @base, else absolute
        pv = "pv:" if trig else f"<{self.PV}"
        pvc = "" if trig else ">"  # closing bracket, absent for prefixed names
        self.dt_int = "^^xsd:integer" if trig else f"^^<{XSD_INT}>"
        self.dt_dec = "^^xsd:decimal" if trig else f"^^<{XSD_DEC}>"
        self.dt_date = "^^xsd:date" if trig else f"^^<{XSD_DATE}>"
        self.dt_bool = "^^xsd:boolean" if trig else f"^^<{XSD_BOOL}>"
        self.uri_stem = u

        self.vocab = build_vocab(cfg.seed, cfg.vocab_size)
        self.vocab_cw = zipf_cum_weights(cfg.vocab_size, cfg.vocab_skew)
        self.head_cw = zipf_cum_weights(cfg.head_entities, cfg.head_skew)
        self.head_pop = range(cfg.head_entities)

        self.rel_p = [f"<{u}p/{k}>" for k in range(cfg.relation_predicates)]
        self.attr_p = [
            f"<{u}p/{cfg.relation_predicates + k}>"
            for k in range(cfg.attribute_predicates)
        ]
        self.attr_dt = [
            ATTR_DATATYPES[k % len(ATTR_DATATYPES)]
            for k in range(cfg.attribute_predicates)
        ]
        self.cls = [f"<{u}c/{k}>" for k in range(cfg.classes)]

        # section tree geometry
        self.depth = len(cfg.fanout)
        self.weights = [
            math.prod(cfg.fanout[k + 1 :]) for k in range(self.depth)
        ]
        self.leaves_per_doc = math.prod(cfg.fanout)

        # provenance vocabulary
        self.P_TEXT = f"{pv}text{pvc}"
        self.P_SECNUM = f"{pv}sectionNumber{pvc}"
        self.P_PARENT = f"{pv}parentSection{pvc}"
        self.P_DOC = f"{pv}document{pvc}"
        self.P_TITLE = f"{pv}title{pvc}"
        self.P_ORDINAL = f"{pv}ordinal{pvc}"
        self.P_TRIPLES = f"{pv}tripleCount{pvc}"
        self.C_SUBSECTION = f"{pv}Subsection{pvc}"
        self.C_SECTION = f"{pv}Section{pvc}"
        self.C_DOCUMENT = f"{pv}Document{pvc}"
        self.A = "a" if trig else f"<{RDF_TYPE}>"
        self.LABEL = "rdfs:label" if trig else f"<{RDFS_LABEL}>"

        # Calibrate the word count against the *actual* Zipf-weighted mean word
        # length, so --text-chars lands on the requested size instead of an
        # assumed one. (+1 for the separator that follows each word.)
        prev, weighted, total = 0.0, 0.0, self.vocab_cw[-1]
        for w, c in zip(self.vocab, self.vocab_cw):
            weighted += (len(w) + 1) * (c - prev)
            prev = c
        self.mean_word_bytes = weighted / total
        self.words_per_text = max(0, round(cfg.text_chars / self.mean_word_bytes))

    # -- naming ------------------------------------------------------------
    # These return IRIs in the *current format's* notation: relative to @base
    # under TriG, absolute under N-Quads. pick_constants() deliberately uses an
    # N-Quads State so the queries always get absolute IRIs.
    def doc_uri(self, doc: int) -> str:
        return f"{self.uri_stem}doc/{doc}"

    def sec_uri(self, doc: int, digits) -> str:
        num = ".".join(str(d + 1) for d in digits)
        return f"{self.uri_stem}sec/{doc}/{num}"

    def entity_uri(self, eid: int) -> str:
        return f"{self.uri_stem}e/{eid}"

    def entity_label(self, eid: int) -> str:
        h = mix64(eid ^ 0xA5A5A5A5A5A5A5A5)
        v, n = self.vocab, self.cfg.vocab_size
        return f"{v[h % n].capitalize()} {v[(h // n) % n].capitalize()}"

    def entity_class(self, eid: int) -> str:
        return self.cls[mix64(eid ^ 0x5EED5EED) % self.cfg.classes]

    def title_for(self, key: int) -> str:
        h = mix64(key ^ 0x7171717171717171)
        v, n = self.vocab, self.cfg.vocab_size
        return f"{v[h % n].capitalize()} {v[(h // n) % n]} {v[(h // (n * n)) % n]}"

    def search_word(self, target: float = 0.25) -> str:
        """A word for q07 that matches roughly `target` of the texts.

        Picking by rank does not work: with ~90 words per text even a rank-4
        word appears in almost every one, so CONTAINS would match 100% and the
        query would only ever measure a full scan. Solve for the rate instead,
        from the Zipf weights that actually drive the sampler.
        """
        prev, total, best = 0.0, self.vocab_cw[-1], self.vocab[0]
        for k, (word, cum) in enumerate(zip(self.vocab, self.vocab_cw)):
            p = (cum - prev) / total
            prev = cum
            # Long enough not to hit as a substring of an unrelated word.
            if len(word) < 5:
                continue
            rate = 1.0 - (1.0 - p) ** max(1, self.words_per_text)
            best = word
            if rate <= target:
                break
        return best

    def position(self, idx: int):
        """Leaf index -> (document number, digit path down the section tree)."""
        doc, j = divmod(idx, self.leaves_per_doc)
        f, w = self.cfg.fanout, self.weights
        return doc, [(j // w[k]) % f[k] for k in range(self.depth)]


_STATE: State | None = None


def get_state(cfg: Config) -> State:
    """Lazily build State once per process (workers are spawned on macOS)."""
    global _STATE
    if _STATE is None or _STATE.cfg != cfg:
        _STATE = State(cfg)
    return _STATE


# --------------------------------------------------------------------------- #
# Graph construction -- the pure function everything else is built on
# --------------------------------------------------------------------------- #


def build_graph(idx: int, S: State) -> dict:
    """Build named graph `idx` and its provenance. Depends only on (cfg, idx)."""
    cfg = S.cfg
    rng = random.Random(mix64((cfg.seed << 24) ^ (idx + 1)))

    doc, digits = S.position(idx)
    g_uri = S.sec_uri(doc, digits)
    g = f"<{g_uri}>"
    parent = (
        f"<{S.sec_uri(doc, digits[:-1])}>"
        if S.depth > 1
        else f"<{S.doc_uri(doc)}>"
    )

    # ---- how many triples does this subsection yield? ---------------------
    mean = cfg.triples_per_graph
    if cfg.triples_sigma > 0:
        mu = math.log(mean) - cfg.triples_sigma**2 / 2
        n = round(rng.lognormvariate(mu, cfg.triples_sigma))
        n = max(1, min(n, int(mean * 10)))
    else:
        n = max(1, round(mean))

    # ---- which entities does it talk about? -------------------------------
    # At least 2 where possible so relations can exist, but never more than the
    # graph has room for -- otherwise a tiny graph would "mention" entities that
    # never make it into a triple.
    n_ent = min(n, max(2, round(n * cfg.entity_ratio)))
    H, L = cfg.head_entities, cfg.local_per_graph
    p_g, p_r = cfg.p_global, cfg.p_global + cfg.p_recent
    ents = []
    for _ in range(n_ent):
        r = rng.random()
        if r < p_g:
            eid = rng.choices(S.head_pop, cum_weights=S.head_cw, k=1)[0]
        elif r < p_r and idx > 0:
            back = rng.randrange(1, min(idx, cfg.recent_window) + 1)
            eid = H + (idx - back) * L + rng.randrange(L)
        else:
            eid = H + idx * L + rng.randrange(L)
        ents.append(eid)
    ents = list(dict.fromkeys(ents))  # de-duplicate, preserve order
    e_uri = [f"<{S.entity_uri(e)}>" for e in ents]
    n_ent = len(ents)

    # ---- the extracted triples -------------------------------------------
    triples, seen = [], set()

    def add(s: str, p: str, o: str) -> bool:
        t = (s, p, o)
        if t in seen:
            return False
        seen.add(t)
        triples.append(t)
        return True

    for k, e in enumerate(ents):
        add(e_uri[k], S.A, S.entity_class(e))
        if len(triples) >= n:
            break
    for k, e in enumerate(ents):
        if len(triples) >= n:
            break
        if rng.random() < cfg.label_prob:
            add(e_uri[k], S.LABEL, f'"{S.entity_label(e)}"')

    attempts = 0
    max_attempts = 12 * n + 40
    while len(triples) < n and attempts < max_attempts:
        attempts += 1
        if n_ent > 1 and rng.random() < 0.6:  # relation between two entities
            i1 = rng.randrange(n_ent)
            i2 = rng.randrange(n_ent)
            if i1 == i2:
                continue
            add(e_uri[i1], rng.choice(S.rel_p), e_uri[i2])
        else:  # attribute with a typed literal
            k = rng.randrange(cfg.attribute_predicates)
            add(e_uri[rng.randrange(n_ent)], S.attr_p[k], _attr_value(rng, k, S))
    n = len(triples)

    # ---- provenance for this subsection (default graph) -------------------
    # Returned as (s, p, o) triples, not finished lines: N-Quads writes one line
    # each, TriG groups them under a shared subject.
    ordinal = digits[-1] + 1
    secnum = ".".join(str(d + 1) for d in digits)
    doc_term = f"<{S.doc_uri(doc)}>"
    prov = [
        (g, S.A, S.C_SUBSECTION),
        (g, S.P_SECNUM, f'"{secnum}"'),
        (g, S.P_PARENT, parent),
        (g, S.P_DOC, doc_term),
        (g, S.P_TITLE, f'"{S.title_for(idx)}"'),
        (g, S.P_ORDINAL, f'"{ordinal}"{S.dt_int}'),
        (g, S.P_TRIPLES, f'"{n}"{S.dt_int}'),
    ]
    text = ""
    if S.words_per_text:
        text = _make_text(rng, S, ents)
        prov.append((g, S.P_TEXT, f'"{text}"'))

    # ---- ancestors: emitted by the graph that is their *first* leaf -------
    anc = []
    if all(d == 0 for d in digits):  # first leaf of the whole document
        anc += [
            (doc_term, S.A, S.C_DOCUMENT),
            (doc_term, S.P_TITLE, f'"{S.title_for(-1 - doc)}"'),
            (doc_term, S.P_ORDINAL, f'"{doc + 1}"{S.dt_int}'),
        ]
    for k in range(1, S.depth):  # internal section levels
        if any(d != 0 for d in digits[k:]):
            continue
        s_uri = f"<{S.sec_uri(doc, digits[:k])}>"
        s_parent = (
            f"<{S.sec_uri(doc, digits[: k - 1])}>" if k > 1 else doc_term
        )
        anc += [
            (s_uri, S.A, S.C_SECTION),
            (s_uri, S.P_SECNUM, '"' + ".".join(str(d + 1) for d in digits[:k]) + '"'),
            (s_uri, S.P_PARENT, s_parent),
            (s_uri, S.P_DOC, doc_term),
            (s_uri, S.P_TITLE, f'"{S.title_for(1 << 40 | idx << 4 | k)}"'),
            (s_uri, S.P_ORDINAL, f'"{digits[k - 1] + 1}"{S.dt_int}'),
        ]

    return {
        "g": g_uri,
        "g_term": g,
        "doc": doc,
        "parent": parent[1:-1],
        "prov": prov,
        "anc": anc,
        "triples": triples,
        "entities": ents,
        "text": text,
    }


def _attr_value(rng: random.Random, k: int, S: State) -> str:
    dt = S.attr_dt[k]
    if dt is None:
        return f'"{S.vocab[rng.randrange(S.cfg.vocab_size)]}"'
    if dt == XSD_INT:
        return f'"{rng.randrange(1, 100000)}"{S.dt_int}'
    if dt == XSD_DEC:
        return f'"{rng.randrange(0, 1000000) / 100:.2f}"{S.dt_dec}'
    if dt == XSD_DATE:
        return (
            f'"{rng.randrange(1950, 2026)}-{rng.randrange(1, 13):02d}-'
            f'{rng.randrange(1, 29):02d}"{S.dt_date}'
        )
    return f'"{"true" if rng.random() < 0.5 else "false"}"{S.dt_bool}'


def _make_text(rng: random.Random, S: State, ents: list) -> str:
    """Subsection text. Entity labels are woven in so that text search and the
    extracted triples actually correlate, as they would in a real pipeline."""
    words = rng.choices(S.vocab, cum_weights=S.vocab_cw, k=S.words_per_text)
    for e in ents[:3]:
        if len(words) > 4:
            words[rng.randrange(len(words))] = S.entity_label(e)
    out, i = [], 0
    while i < len(words):
        j = min(len(words), i + rng.randint(8, 20))
        chunk = words[i:j]
        chunk[0] = chunk[0].capitalize()
        out.append(" ".join(chunk) + ".")
        i = j
    return " ".join(out)


# --------------------------------------------------------------------------- #
# Sharded, parallel writing
# --------------------------------------------------------------------------- #

FLUSH_CHARS = 4 << 20

EXTENSION = {"trig": ".trig", "nquads": ".nq"}


def trig_header(S: State) -> str:
    return (
        f"@base <{S.cfg.base}> .\n"
        f"@prefix pv: <{S.PV}> .\n"
        f"@prefix rdfs: <{RDFS_NS}> .\n"
        f"@prefix xsd: <{XSD}> .\n\n"
    )


def _turtle_block(triples: list, indent: str = "") -> str:
    """Group triples by subject into `s p o ; p o .` form."""
    by_subject: dict = {}
    for s, p, o in triples:
        by_subject.setdefault(s, []).append(f"{p} {o}")
    sep = f" ;\n{indent}    "
    return "".join(
        f"{indent}{s} {sep.join(pos)} .\n" for s, pos in by_subject.items()
    )


def serialize_trig(gr: dict) -> str:
    out = _turtle_block(gr["anc"]) + _turtle_block(gr["prov"])
    if gr["triples"]:
        out += gr["g_term"] + " {\n" + _turtle_block(gr["triples"], "    ") + "}\n"
    return out


def serialize_nquads(gr: dict) -> str:
    g = gr["g_term"]
    return "".join(
        f"{s} {p} {o} .\n" for s, p, o in gr["anc"]
    ) + "".join(
        f"{s} {p} {o} .\n" for s, p, o in gr["prov"]
    ) + "".join(
        f"{s} {p} {o} {g} .\n" for s, p, o in gr["triples"]
    )


def run_shard(task: tuple) -> dict:
    """Generate graphs [start, end) into one file. Runs in a worker."""
    cfg, shard_idx, start, end, data_dir = task
    S = get_state(cfg)
    serialize = serialize_trig if cfg.fmt == "trig" else serialize_nquads

    name = f"part-{shard_idx:05d}{EXTENSION[cfg.fmt]}" + (".gz" if cfg.gzip else "")
    path = os.path.join(data_dir, name)
    fh = (
        gzip.open(path, "wt", compresslevel=cfg.compresslevel, encoding="ascii")
        if cfg.gzip
        else open(path, "w", encoding="ascii")
    )

    n_prov = n_content = 0
    head_counts: Counter = Counter()
    H = cfg.head_entities
    buf: list = []
    pending = 0
    t0 = time.time()
    try:
        if cfg.fmt == "trig":
            # Every shard is independently parseable: it repeats the header, so
            # shards can be imported in any order or on their own.
            fh.write(trig_header(S))
        for i in range(start, end):
            gr = build_graph(i, S)
            chunk = serialize(gr)
            buf.append(chunk)
            pending += len(chunk)
            n_prov += len(gr["anc"]) + len(gr["prov"])
            n_content += len(gr["triples"])
            for e in gr["entities"]:
                if e < H:
                    head_counts[e] += 1
            if pending >= FLUSH_CHARS:
                fh.write("".join(buf))
                buf.clear()
                pending = 0
        if buf:
            fh.write("".join(buf))
    finally:
        fh.close()

    return {
        "shard": shard_idx,
        "file": os.path.join("data", name),
        "graph_start": start,
        "graph_end": end,
        "graphs": end - start,
        "provenance_quads": n_prov,
        "content_quads": n_content,
        "quads": n_prov + n_content,
        "bytes": os.path.getsize(path),
        "seconds": round(time.time() - t0, 2),
        "head_counts": dict(head_counts.most_common(200)),
    }


def default_checkpoints(graphs: int) -> list:
    """Log-spaced 1/2/5 checkpoints, so the scaling curve gets even x-spacing."""
    out, mag = [], 10
    while mag <= graphs:
        for m in (1, 2, 5):
            v = mag * m
            if 0 < v < graphs:
                out.append(v)
        mag *= 10
    out.append(graphs)
    return sorted(set(out))


def plan_shards(graphs: int, checkpoints: list, max_shard: int) -> list:
    """Contiguous graph ranges whose boundaries include every checkpoint."""
    bounds = sorted({0, graphs} | {c for c in checkpoints if 0 < c < graphs})
    ranges = []
    for a, b in zip(bounds, bounds[1:]):
        n_parts = max(1, math.ceil((b - a) / max_shard))
        step = math.ceil((b - a) / n_parts)
        x = a
        while x < b:
            ranges.append((x, min(x + step, b)))
            x += step
    return ranges


# --------------------------------------------------------------------------- #
# Queries, bound to constants that really occur in the data
# --------------------------------------------------------------------------- #

# (name, description, body, needs_text). Queries flagged needs_text are not
# written when --text-chars 0 removes the property they read.
QUERIES = [
    (
        "q01_provenance_of_one_triple",
        "The core access path: given one extracted triple, recover the "
        "subsection it came from, its text, section number and parent.",
        """SELECT ?g ?sectionNumber ?parent{TEXT_SELECT}
WHERE {{
  GRAPH ?g {{ {S} {P} {O} }}
  ?g pv:sectionNumber ?sectionNumber ;
     pv:parentSection  ?parent{TEXT_PATTERN} .
}}""",
        False,
    ),
    (
        "q02_contents_of_one_graph",
        "Read back a single subsection's extraction. Bound graph, unbound "
        "triple pattern -- the cheapest possible graph-keyed access.",
        """SELECT ?s ?p ?o
WHERE {{
  GRAPH <{LEAF}> {{ ?s ?p ?o }}
}}""",
        False,
    ),
    (
        "q03_graphs_mentioning_hub_entity",
        "Fan out from a frequently mentioned entity to every subsection that "
        "mentions it, with provenance. Touches many named graphs.",
        """SELECT ?g ?sectionNumber ?parent ?p ?o
WHERE {{
  GRAPH ?g {{ <{HUB}> ?p ?o }}
  ?g pv:sectionNumber ?sectionNumber ;
     pv:parentSection  ?parent .
}}""",
        False,
    ),
    (
        "q04_quads_per_document",
        "Full scan plus aggregation: how many extracted triples each document "
        "contributed. Worst case for a quad table.",
        """SELECT ?doc (COUNT(*) AS ?n)
WHERE {{
  GRAPH ?g {{ ?s ?p ?o }}
  ?g pv:document ?doc .
}}
GROUP BY ?doc
ORDER BY DESC(?n)
LIMIT 20""",
        False,
    ),
    (
        "q05_ancestor_chain",
        "Walk the provenance hierarchy from one subsection up to its document "
        "with a property path.",
        """SELECT ?ancestor ?sectionNumber ?title
WHERE {{
  <{LEAF}> pv:parentSection+ ?ancestor .
  OPTIONAL {{ ?ancestor pv:sectionNumber ?sectionNumber }}
  ?ancestor pv:title ?title .
}}""",
        False,
    ),
    (
        "q06_cross_graph_two_hop",
        "Two hops that cross a graph boundary: a hub entity's neighbour in one "
        "subsection, then everything said about that neighbour elsewhere.",
        """SELECT ?g1 ?g2 ?x ?p ?y
WHERE {{
  GRAPH ?g1 {{ <{HUB}> ?r ?x }}
  GRAPH ?g2 {{ ?x ?p ?y }}
  FILTER(?g1 != ?g2)
}}
LIMIT 1000""",
        False,
    ),
    (
        "q07_text_search",
        "Scan every provenance literal. Isolates the cost of the long text "
        "values in the dictionary.",
        """SELECT (COUNT(*) AS ?matches)
WHERE {{
  ?g pv:text ?text .
  FILTER(CONTAINS(?text, "{WORD}"))
}}""",
        True,
    ),
    (
        "q08_class_with_provenance",
        "Every instance of one class, joined to its subsection's section "
        "number: a large join between named graphs and the default graph.",
        """SELECT ?s ?sectionNumber
WHERE {{
  GRAPH ?g {{ ?s a <{CLASS}> }}
  ?g pv:sectionNumber ?sectionNumber .
}}
LIMIT 10000""",
        False,
    ),
    (
        "q09_total_quads",
        "Baseline: count every quad in every named graph. Pure scan speed.",
        """SELECT (COUNT(*) AS ?quads)
WHERE {{
  GRAPH ?g {{ ?s ?p ?o }}
}}""",
        False,
    ),
    (
        "q10_subtree_extraction",
        "Provenance-driven retrieval: all triples extracted from the "
        "subsections directly under one section. The realistic read pattern.",
        """SELECT ?g ?s ?p ?o
WHERE {{
  ?g pv:parentSection <{SEC}> .
  GRAPH ?g {{ ?s ?p ?o }}
}}""",
        False,
    ),
]


def pick_constants(cfg: Config, S: State, head_counts: Counter) -> dict:
    """Sample real constants by regenerating a middle graph -- generation is a
    pure function of the index, so no need to read the output back.

    Deliberately rebuilt through an N-Quads State: the format only changes how
    terms are *rendered*, not which ones are drawn, so this yields the same
    triples with absolute IRIs. SPARQL has no @base to resolve against, so the
    relative IRIs a TriG State produces would be wrong in a query.
    """
    S = get_state(replace(cfg, fmt="nquads")) if cfg.fmt != "nquads" else S
    mid = build_graph(cfg.graphs // 2, S)
    rel_set = set(S.rel_p)

    # For the point-lookup query prefer a relation triple over a shared
    # rdf:type/label triple: those repeat across graphs and would turn the
    # "provenance of one triple" query into a fan-out. Small graphs may hold no
    # relation at all, so widen the search rather than fall back to a predicate
    # that might occur nowhere -- a query bound to an absent constant returns
    # instantly and measures nothing.
    rel_triple = next((t for t in mid["triples"] if t[1] in rel_set), None)
    probe = 0
    while rel_triple is None and probe < min(cfg.graphs, 500):
        other = build_graph((cfg.graphs // 2 + probe + 1) % cfg.graphs, S)
        rel_triple = next((t for t in other["triples"] if t[1] in rel_set), None)
        probe += 1

    s, p, o = rel_triple or mid["triples"][0]

    if head_counts:
        hub = S.entity_uri(head_counts.most_common(1)[0][0])
    else:
        hub = S.entity_uri(0)

    has_text = S.words_per_text > 0
    return {
        "S": s,
        "P": p,
        "O": o,
        "LEAF": mid["g"],
        "SEC": mid["parent"],
        "HUB": hub,
        "CLASS": S.entity_class(mid["entities"][0])[1:-1],
        "WORD": S.search_word(),
        # With --text-chars 0 the property is absent; keeping it in the query
        # would silently turn a point lookup into a guaranteed empty result.
        "TEXT_SELECT": " ?text" if has_text else "",
        "TEXT_PATTERN": " ;\n     pv:text           ?text" if has_text else "",
    }


def write_queries(qdir: str, S: State, const: dict) -> list:
    os.makedirs(qdir, exist_ok=True)
    written = []
    has_text = S.words_per_text > 0
    header = (
        f"PREFIX pv: <{S.PV}>\n"
        f"PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        f"PREFIX xsd: <{XSD}>\n"
    )
    for name, doc, body, needs_text in QUERIES:
        if needs_text and not has_text:
            continue  # nothing to read -- the dataset was built without text
        text = header + "\n# " + doc.replace(". ", ".\n# ") + "\n" + body.format(**const) + "\n"
        path = os.path.join(qdir, name + ".rq")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append({"name": name, "file": os.path.join("queries", name + ".rq"),
                        "description": doc})
    return written


# --------------------------------------------------------------------------- #
# Estimation
# --------------------------------------------------------------------------- #


def estimate(cfg: Config, S: State, sample: int = 200) -> dict:
    """Project the full run from a sample of real graphs, serialised for real."""
    serialize = serialize_trig if cfg.fmt == "trig" else serialize_nquads
    n = min(sample, cfg.graphs)
    quads = chars = 0
    for i in range(n):
        gr = build_graph(i, S)
        quads += len(gr["prov"]) + len(gr["anc"]) + len(gr["triples"])
        chars += len(serialize(gr))
    return {
        "graphs": cfg.graphs,
        "format": cfg.fmt,
        "estimated_quads": int(quads / n * cfg.graphs),
        "estimated_uncompressed_bytes": int(chars / n * cfg.graphs),
        "estimated_gzip_bytes": int(chars / n * cfg.graphs / 9),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n} B"


def parse_args(argv=None) -> argparse.Namespace:
    d = Config()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--graphs", type=int, default=d.graphs,
                    help="number of named graphs = leaf subsections")
    ap.add_argument("--seed", type=int, default=d.seed)
    ap.add_argument("--out", default="out", help="output directory")
    ap.add_argument("--base", default=d.base, help="base URI")

    g = ap.add_argument_group("section tree")
    g.add_argument("--fanout", default=",".join(map(str, d.fanout)),
                   help="children per level below the document, e.g. '8,6'")

    g = ap.add_argument_group("graph size")
    g.add_argument("--triples-per-graph", type=float, default=d.triples_per_graph)
    g.add_argument("--triples-sigma", type=float, default=d.triples_sigma,
                   help="lognormal sigma; 0 for a fixed size per graph")
    g.add_argument("--entity-ratio", type=float, default=d.entity_ratio)

    g = ap.add_argument_group("entity reuse")
    g.add_argument("--head-entities", type=int, default=d.head_entities)
    g.add_argument("--head-skew", type=float, default=d.head_skew)
    g.add_argument("--local-per-graph", type=int, default=d.local_per_graph)
    g.add_argument("--p-global", type=float, default=d.p_global)
    g.add_argument("--p-recent", type=float, default=d.p_recent)
    g.add_argument("--recent-window", type=int, default=d.recent_window)

    g = ap.add_argument_group("schema")
    g.add_argument("--relation-predicates", type=int, default=d.relation_predicates)
    g.add_argument("--attribute-predicates", type=int, default=d.attribute_predicates)
    g.add_argument("--classes", type=int, default=d.classes)
    g.add_argument("--label-prob", type=float, default=d.label_prob)

    g = ap.add_argument_group("provenance text")
    g.add_argument("--text-chars", type=int, default=d.text_chars,
                   help="mean characters of pv:text; 0 omits the property")
    g.add_argument("--vocab-size", type=int, default=d.vocab_size)
    g.add_argument("--vocab-skew", type=float, default=d.vocab_skew)

    g = ap.add_argument_group("output")
    g.add_argument("--format", choices=("trig", "nquads"), default=d.fmt,
                   help="trig (default) is what RDFox's shell `import` can "
                        "read; nquads is for the REST endpoint and other tools")
    g.add_argument("--no-gzip", action="store_true")
    g.add_argument("--compresslevel", type=int, default=d.compresslevel)
    g.add_argument("--max-shard-graphs", type=int, default=d.max_shard_graphs)
    g.add_argument("--checkpoints", default="",
                   help="comma-separated graph counts to align shards on; "
                        "default is log-spaced 1/2/5 per decade")
    g.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    g.add_argument("--estimate", action="store_true",
                   help="print projected size from a sample and exit")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = Config(
        graphs=args.graphs,
        seed=args.seed,
        base=args.base if args.base.endswith("/") else args.base + "/",
        fanout=tuple(int(x) for x in args.fanout.split(",") if x.strip()),
        triples_per_graph=args.triples_per_graph,
        triples_sigma=args.triples_sigma,
        entity_ratio=args.entity_ratio,
        head_entities=args.head_entities,
        head_skew=args.head_skew,
        local_per_graph=args.local_per_graph,
        p_global=args.p_global,
        p_recent=args.p_recent,
        recent_window=args.recent_window,
        relation_predicates=args.relation_predicates,
        attribute_predicates=args.attribute_predicates,
        classes=args.classes,
        label_prob=args.label_prob,
        text_chars=args.text_chars,
        vocab_size=args.vocab_size,
        vocab_skew=args.vocab_skew,
        fmt=args.format,
        gzip=not args.no_gzip,
        compresslevel=args.compresslevel,
        max_shard_graphs=args.max_shard_graphs,
    )
    if cfg.p_global + cfg.p_recent > 1.0:
        sys.exit("--p-global + --p-recent must not exceed 1.0")
    if cfg.graphs < 1:
        sys.exit("--graphs must be >= 1")

    S = get_state(cfg)

    if args.estimate:
        print(json.dumps(estimate(cfg, S), indent=2))
        return 0

    checkpoints = (
        [int(x) for x in args.checkpoints.split(",") if x.strip()]
        if args.checkpoints
        else default_checkpoints(cfg.graphs)
    )
    ranges = plan_shards(cfg.graphs, checkpoints, cfg.max_shard_graphs)

    out = os.path.abspath(args.out)
    data_dir = os.path.join(out, "data")
    os.makedirs(data_dir, exist_ok=True)

    tasks = [(cfg, k, a, b, data_dir) for k, (a, b) in enumerate(ranges)]
    print(
        f"{cfg.graphs:,} named graphs -> {len(ranges)} shard(s) in {out}",
        file=sys.stderr,
    )

    t0 = time.time()
    results = []
    if args.workers > 1 and len(tasks) > 1:
        with mp.Pool(min(args.workers, len(tasks))) as pool:
            for r in pool.imap_unordered(run_shard, tasks):
                results.append(r)
                print(
                    f"  shard {r['shard']:>4}  graphs {r['graph_start']:,}-"
                    f"{r['graph_end']:,}  {r['quads']:,} quads  "
                    f"{human(r['bytes'])}  {r['seconds']}s",
                    file=sys.stderr,
                )
    else:
        for t in tasks:
            r = run_shard(t)
            results.append(r)
            print(
                f"  shard {r['shard']:>4}  graphs {r['graph_start']:,}-"
                f"{r['graph_end']:,}  {r['quads']:,} quads  "
                f"{human(r['bytes'])}  {r['seconds']}s",
                file=sys.stderr,
            )
    results.sort(key=lambda r: r["shard"])

    # ---- cumulative view: this is what the scaling plot is built from -----
    head_counts: Counter = Counter()
    cum_g = cum_q = cum_p = cum_c = cum_b = 0
    for r in results:
        head_counts.update(r.pop("head_counts"))
        cum_g += r["graphs"]
        cum_q += r["quads"]
        cum_p += r["provenance_quads"]
        cum_c += r["content_quads"]
        cum_b += r["bytes"]
        r["cumulative"] = {
            "graphs": cum_g,
            "quads": cum_q,
            "provenance_quads": cum_p,
            "content_quads": cum_c,
            "bytes": cum_b,
        }

    by_graphs = {r["cumulative"]["graphs"]: r for r in results}
    checkpoint_rows = [
        {
            "graphs": c,
            "shards_to_load": by_graphs[c]["shard"] + 1,
            "quads": by_graphs[c]["cumulative"]["quads"],
            "provenance_quads": by_graphs[c]["cumulative"]["provenance_quads"],
            "content_quads": by_graphs[c]["cumulative"]["content_quads"],
            "gzip_bytes": by_graphs[c]["cumulative"]["bytes"],
        }
        for c in checkpoints
        if c in by_graphs
    ]

    const = pick_constants(cfg, S, head_counts)
    queries = write_queries(os.path.join(out, "queries"), S, const)

    manifest = {
        "generator": os.path.basename(__file__),
        "generated_unix_time": int(time.time()),
        "config": {**asdict(cfg), "fanout": list(cfg.fanout)},
        "totals": {
            "graphs": cum_g,
            "quads": cum_q,
            "provenance_quads": cum_p,
            "content_quads": cum_c,
            "documents": math.ceil(cfg.graphs / S.leaves_per_doc),
            "section_levels": S.depth,
            "leaves_per_document": S.leaves_per_doc,
            "gzip_bytes": cum_b,
            "quads_per_graph": round(cum_q / cum_g, 2),
            "generation_seconds": round(time.time() - t0, 1),
        },
        "checkpoints": checkpoint_rows,
        "shards": results,
        # TEXT_* are template mechanics, not data constants worth recording.
        "query_constants": {
            k: v for k, v in const.items() if not k.startswith("TEXT_")
        },
        "queries": queries,
        "notes": [
            "Shards are in graph-index order and each dataset is a prefix of "
            "any larger one generated with the same seed and parameters, so "
            "shards can be imported cumulatively into one RDFox process.",
            "No quad is emitted twice, so RDFox should report exactly the "
            "'quads' count above after loading.",
        ],
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(
        f"\n{cum_g:,} graphs  {cum_q:,} quads "
        f"({cum_c:,} content + {cum_p:,} provenance)  "
        f"{human(cum_b)} on disk  in {manifest['totals']['generation_seconds']}s",
        file=sys.stderr,
    )
    print(f"manifest: {os.path.join(out, 'manifest.json')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
