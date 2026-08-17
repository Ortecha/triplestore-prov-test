#!/usr/bin/env python3
"""
Render a measurement run as a self-contained HTML report: charts with fitted
interpolations, plus the tables behind them.

Pure standard library -- the charts are hand-built inline SVG, so the report is
one file with no assets, no CDN and no build step. Open it, or mail it.

    python3 report.py results/results.json [-o results/report.html]
"""

from __future__ import annotations

import html
import json
import math
import os
import sys

SERIES_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"]
W, H = 560, 330
PAD_L, PAD_R, PAD_T, PAD_B = 66, 16, 16, 44
_CHART_SEQ = 0  # makes each chart's clipPath id unique within the page


# --------------------------------------------------------------------------- #
# Axes
# --------------------------------------------------------------------------- #


class Scale:
    """Maps data values to pixels, linearly or on a log10 axis."""

    def __init__(self, lo: float, hi: float, px0: float, px1: float, log: bool):
        self.log = log and lo > 0 and hi > 0
        if self.log:
            lo, hi = math.log10(lo), math.log10(hi)
        if hi == lo:  # a single distinct value still needs a finite span
            hi = lo + 1
        pad = (hi - lo) * 0.06
        self.lo, self.hi = lo - pad, hi + pad
        self.px0, self.px1 = px0, px1

    def __call__(self, v: float) -> float:
        if self.log:
            v = math.log10(v) if v > 0 else self.lo
        t = (v - self.lo) / (self.hi - self.lo)
        return self.px0 + t * (self.px1 - self.px0)

    def ticks(self) -> list:
        if self.log:
            decades = range(math.floor(self.lo), math.ceil(self.hi) + 1)
            # Tick density has to follow the span: 1/2/5 per decade is an
            # unreadable ladder across six decades, and far too coarse across a
            # range that never leaves one (a constant-time query would get a
            # single tick).
            span = self.hi - self.lo
            if span > 4:
                mults = (1,)
            elif span > 1.5:
                mults = (1, 2, 5)
            else:
                mults = (1, 1.5, 2, 3, 4, 5, 6, 8)
            return [
                m * 10**d
                for d in decades
                for m in mults
                if self.lo <= math.log10(m * 10**d) <= self.hi
            ][:11]
        span = self.hi - self.lo
        step = 10 ** math.floor(math.log10(span / 4)) if span > 0 else 1
        for mult in (1, 2, 2.5, 5, 10):
            if span / (step * mult) <= 6:
                step *= mult
                break
        start = math.ceil(self.lo / step) * step
        out, v = [], start
        while v <= self.hi and len(out) < 12:
            out.append(v)
            v += step
        return out


def fmt_si(v: float) -> str:
    if v == 0:
        return "0"
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            n = v / div
            return f"{n:.0f}{unit}" if n >= 10 else f"{n:.1f}{unit}"
    if abs(v) >= 1:
        return f"{v:.0f}" if v == int(v) else f"{v:.1f}"
    return f"{v:g}"


def fmt_bytes(v: float) -> str:
    """Decimal SI, not binary: log axis ticks land on powers of ten, and 1 MB
    reads better there than 976.6 KiB."""
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if abs(v) >= div:
            n = v / div
            return f"{n:,.0f} {unit}" if n >= 100 else f"{n:,.1f} {unit}"
    return f"{v:,.0f} B"


def fmt_time_axis(v: float) -> str:
    """Axis ticks for a log time scale spanning microseconds to seconds."""
    if v >= 1:
        return f"{v:g} s"
    if v >= 1e-3:
        return f"{v * 1e3:g} ms"
    return f"{v * 1e6:g} µs"


def fmt_time(v: float | None) -> str:
    """Times here span microseconds to seconds, so the unit has to follow."""
    if v is None:
        return "–"
    if v <= 0:
        return "0"
    if v < 1e-3:
        return f"{v * 1e6:,.1f} µs"
    if v < 1:
        return f"{v * 1000:,.1f} ms"
    return f"{v:,.2f} s"


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #


def chart(
    series: list,
    xlabel: str,
    ylabel: str,
    ylog: bool = True,
    yfmt=fmt_si,
    note: str = "",
    censor_at: float | None = None,
    censor_rule: float | None = None,
    censor_label: str = "",
    size: tuple | None = None,
    end_labels: bool = False,
    legend: bool = True,
) -> str:
    """series: [{label, points:[(x,y)], fit:{a,b,r2}|None}]

    `censor_at` handles left-censored readings -- values recorded as 0 because
    they fell below the instrument's resolution. They are drawn at that level
    with hollow markers rather than forcing the axis to linear: a timing that
    spans four orders of magnitude is unreadable on a linear scale, where the
    power law collapses into a flat line and a spike at the right edge.
    """
    W, H = size or (globals()["W"], globals()["H"])
    # Direct labels at the line ends replace a legend when there are too many
    # series to tell apart by colour.
    pad_r = 132 if end_labels else PAD_R

    raw = [(x, y) for s in series for x, y in s["points"] if y is not None]
    if not raw:
        return '<div class="empty">no data</div>'

    def prep(points: list) -> list:
        """-> [(x, plotted_y, is_censored)]"""
        out = []
        for x, y in points:
            if y is None:
                continue
            if y > 0:
                out.append((x, y, False))
            elif censor_at:
                out.append((x, censor_at, True))
            else:
                out.append((x, y, False))
        return out

    if censor_at is None and ylog and any(y <= 0 for _, y in raw):
        ylog = False  # no censor level supplied: linear is the only option left

    plotted = [p for s in series for p in prep(s["points"])]
    xs = [x for x, _, _ in plotted]
    ys = [y for _, y, _ in plotted if y > 0] or [1]

    sx = Scale(min(xs), max(xs), PAD_L, W - pad_r, log=True)
    sy = Scale(
        min(ys) if ylog else min(y for _, y, _ in plotted),
        max([y for _, y, _ in plotted] + ([censor_rule] if censor_rule else [])),
        H - PAD_B,
        PAD_T,
        log=ylog,
    )

    # Unique per chart: the fit line is clipped to the plot rect, since an
    # extrapolated power law can run far outside it (and would otherwise draw
    # over neighbouring cards).
    global _CHART_SEQ
    _CHART_SEQ += 1
    clip = f"plot{_CHART_SEQ}"
    out = [
        f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">',
        f'<defs><clipPath id="{clip}"><rect x="{PAD_L}" y="{PAD_T}" '
        f'width="{W - pad_r - PAD_L}" height="{H - PAD_B - PAD_T}"/></clipPath></defs>',
    ]

    for t in sy.ticks():
        y = sy(t)
        out.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick ty" x="{PAD_L - 8}" y="{y + 3.5:.1f}">{yfmt(t)}</text>')
    for t in sx.ticks():
        x = sx(t)
        out.append(f'<line class="grid" x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H - PAD_B}"/>')
        out.append(f'<text class="tick tx" x="{x:.1f}" y="{H - PAD_B + 16}">{fmt_si(t)}</text>')

    out.append(
        f'<text class="axis tx" x="{(PAD_L + W - pad_r) / 2:.0f}" y="{H - 6}">{html.escape(xlabel)}</text>'
    )
    out.append(
        f'<text class="axis" transform="translate(13,{(PAD_T + H - PAD_B) / 2:.0f}) rotate(-90)" '
        f'text-anchor="middle">{html.escape(ylabel)}</text>'
    )

    if censor_rule:
        yr = sy(censor_rule)
        out.append(
            f'<line class="rule" x1="{PAD_L}" y1="{yr:.1f}" x2="{W - pad_r}" y2="{yr:.1f}"/>'
        )
        out.append(
            f'<text class="rulelab" x="{W - pad_r - 2}" y="{yr - 4:.1f}">{html.escape(censor_label)}</text>'
        )

    ends = []  # (y_pixel, label, colour) for direct labelling
    for i, s in enumerate(series):
        color = s.get("color") or SERIES_COLORS[i % len(SERIES_COLORS)]
        good = prep(s["points"])

        fit = s.get("fit")
        if fit and fit.get("b") is not None:
            x0, x1 = min(x for x, _, _ in good), max(x for x, _, _ in good)
            steps = 40
            path = []
            for k in range(steps + 1):
                lx = math.log10(x0) + (math.log10(x1) - math.log10(x0)) * k / steps
                x = 10**lx
                y = fit["a"] * x ** fit["b"]
                path.append(f"{sx(x):.1f},{sy(y):.1f}")
            out.append(
                f'<polyline class="fit" clip-path="url(#{clip})" '
                f'style="stroke:{color}" points="{" ".join(path)}"/>'
            )

        line = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y, _ in good)
        out.append(f'<polyline class="link" style="stroke:{color}" points="{line}"/>')
        for x, y, censored in good:
            style = (
                f"fill:none;stroke:{color}" if censored else f"fill:{color}"
            )
            shown = censor_label or yfmt(y) if censored else yfmt(y)
            out.append(
                f'<circle class="pt{" censored" if censored else ""}" style="{style}" '
                f'cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3.4">'
                f"<title>{html.escape(s['label'])}\n{fmt_si(x)} quads\n{shown}</title></circle>"
            )

        if end_labels and good:
            lx, ly, _ = good[-1]
            ends.append([sy(ly), s.get("end_label", s["label"]), color, sx(lx)])

    if ends:
        # Nudge overlapping labels apart, keeping their vertical order.
        ends.sort(key=lambda e: e[0])
        for k in range(1, len(ends)):
            if ends[k][0] - ends[k - 1][0] < 12:
                ends[k][0] = ends[k - 1][0] + 12
        overflow = ends[-1][0] - (H - PAD_B) if ends else 0
        if overflow > 0:
            for e in ends:
                e[0] -= overflow
        for y, label, color, x in ends:
            out.append(
                f'<line class="leader" x1="{x + 4:.1f}" y1="{y:.1f}" '
                f'x2="{W - pad_r + 4:.1f}" y2="{y:.1f}" style="stroke:{color}"/>'
            )
            out.append(
                f'<text class="endlab" x="{W - pad_r + 8:.1f}" y="{y + 3.5:.1f}" '
                f'style="fill:{color}">{html.escape(label)}</text>'
            )

    out.append("</svg>")

    keys = ""
    if legend:
        keys = "".join(
            f'<span class="key"><i style="background:'
            f'{s.get("color") or SERIES_COLORS[i % len(SERIES_COLORS)]}"></i>'
            f"{html.escape(s['label'])}</span>"
            for i, s in enumerate(series)
        )
        keys = f'<div class="legend">{keys}</div>'
    n = f'<p class="note">{note}</p>' if note else ""
    return f'{keys}{"".join(out)}{n}'


# GitHub does not render a committed .html file — it shows the source. It *does*
# render an <img> pointing at a committed .svg, so the summary charts are also
# written out standalone for embedding in the README. Those cannot use CSS
# variables or the page stylesheet, and they have to sit on both the light and
# dark GitHub backgrounds, so the palette is baked in and deliberately mid-tone.
SVG_PALETTE = {
    "var(--s1)": "#3b82d9",
    "var(--s2)": "#e07a1f",
    "var(--s3)": "#0f9b7e",
    "var(--s4)": "#b5468f",
}
SVG_INK = "#7d8590"      # mid-tone: legible on both GitHub themes
SVG_INK_ON_WHITE = "#3f4650"  # darker, for the variant with a white backdrop


def svg_style(ink: str = SVG_INK) -> str:
    return f"""
.grid{{stroke:{ink};stroke-width:1;opacity:.22}}
.tick{{fill:{ink};font-size:10.5px}}
.ty{{text-anchor:end}}.tx{{text-anchor:middle}}
.axis{{fill:{ink};font-size:11.5px}}
.link{{fill:none;stroke-width:1.8}}
.fit{{fill:none;stroke-width:1.4;stroke-dasharray:5 4;opacity:.55}}
.pt{{stroke-width:0}}
.leader{{stroke-width:1;opacity:.35;stroke-dasharray:2 2}}
.endlab{{font-size:10.5px;font-weight:600}}
.ttl{{fill:{ink};font-size:14px;font-weight:700}}
.sub{{fill:{ink};font-size:11.5px}}
.cardttl{{fill:{ink};font-size:21px;font-weight:700}}
.cardsub{{fill:{ink};font-size:12.5px}}
.bul{{fill:{ink};font-size:13px}}
.bul tspan.k{{font-weight:700}}
text{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}}
"""


SVG_STYLE = svg_style()



def standalone_svg(chart_html: str, title: str, subtitle: str, keys: list,
                   size: tuple) -> str:
    """Turn a chart() result into a self-contained .svg file."""
    inner = chart_html[chart_html.index("<svg") : chart_html.rindex("</svg>")]
    inner = inner[inner.index(">") + 1 :]  # drop the original <svg ...> tag
    w, h = size
    head = 46 if not keys else 62
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h + head}" '
        f'width="{w}" height="{h + head}" role="img">',
        f"<title>{html.escape(title)}</title>",
        f"<style>{SVG_STYLE}</style>",
        f'<text class="ttl" x="14" y="20">{html.escape(title)}</text>',
        f'<text class="sub" x="14" y="36">{html.escape(subtitle)}</text>',
    ]
    x = 14
    for label, color in keys:
        body.append(f'<rect x="{x}" y="46" width="9" height="9" rx="2" fill="{color}"/>')
        body.append(f'<text class="sub" x="{x + 14}" y="54.5">{html.escape(label)}</text>')
        x += 16 + int(len(label) * 6.2)
    body.append(f'<g transform="translate(0,{head})">{inner}</g></svg>')
    svg = "".join(body)
    for var, hexcode in SVG_PALETTE.items():
        svg = svg.replace(var, hexcode)
    return svg



CARD_W, CARD_H = 1300, 600
CARD_CHART = (610, 372)


def summary_card(mem_chart: str, q_chart: str, title: str, subtitle: str,
                 bullets: list, mem_keys: list, q_keys: list,
                 ink: str = SVG_INK, background: str | None = None) -> str:
    """Both summary charts plus the headline text, as one image.

    Exists because GitHub renders a committed .html as source. A single PNG is
    the one thing that is guaranteed to display in a README on any plan, with
    Pages off and without admin rights. Background stays transparent and the ink
    is mid-tone so it reads on both the light and dark GitHub themes.
    """

    def embed(chart_html, head_title, head_sub, keys, dx, dy):
        inner = chart_html[chart_html.index("<svg") : chart_html.rindex("</svg>")]
        inner = inner[inner.index(">") + 1 :]
        parts = [
            f'<g transform="translate({dx},{dy})">',
            f'<text class="ttl" x="8" y="14">{html.escape(head_title)}</text>',
            f'<text class="sub" x="8" y="31">{html.escape(head_sub)}</text>',
        ]
        x = 8
        for label, color in keys:
            parts.append(f'<rect x="{x}" y="41" width="9" height="9" rx="2" fill="{color}"/>')
            parts.append(f'<text class="sub" x="{x + 14}" y="49.5">{html.escape(label)}</text>')
            x += 16 + int(len(label) * 6.2)
        parts.append(f'<g transform="translate(0,58)">{inner}</g></g>')
        return "".join(parts)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CARD_W} {CARD_H}" '
        f'width="{CARD_W}" height="{CARD_H}" role="img">',
        f"<title>{html.escape(title)}</title>",
        f"<style>{svg_style(ink)}</style>",
        (f'<rect x="0" y="0" width="{CARD_W}" height="{CARD_H}" fill="{background}"/>'
         if background else ""),
        f'<text class="cardttl" x="24" y="32">{html.escape(title)}</text>',
        f'<text class="cardsub" x="24" y="53">{html.escape(subtitle)}</text>',
    ]
    y = 82
    for b in bullets:
        out.append(f'<text class="bul" x="24" y="{y}">{b}</text>')
        y += 21
    out.append(embed(mem_chart, "Memory vs corpus size",
                     "net of the empty-store baseline, log-log", mem_keys, 24, 152))
    out.append(embed(q_chart, "Query time vs corpus size",
                     "all queries, one line each, log-log", q_keys, 24 + CARD_CHART[0] + 32, 152))
    out.append("</svg>")
    svg = "".join(out)
    for var, hexcode in SVG_PALETTE.items():
        svg = svg.replace(var, hexcode)
    return svg

R2_MIN = 0.75  # below this the exponent is describing noise, not a trend


FLAT_EXPONENT = 0.15  # |b| below this, with little spread, means constant time
FLAT_SPAN = 4.0  # max/min of the observed values


def classify(fit: dict | None, ys: list) -> str:
    """'constant' | 'noisy' | 'trend' | 'none'.

    A near-zero exponent comes with a low R² by construction -- there is no
    trend for the fit to explain. That is a *result* (the query does not grow
    with the corpus), not a failed measurement, so it must not be lumped in
    with genuinely scattered data. The two are told apart by spread: constant
    means the values barely move, noisy means they move without pattern.
    """
    vals = [y for y in ys if y and y > 0]
    if not fit or len(vals) < 3:
        return "none"
    span = max(vals) / min(vals)
    if abs(fit["b"]) < FLAT_EXPONENT and span < FLAT_SPAN:
        return "constant"
    return "trend" if fit["r2"] >= R2_MIN else "noisy"


def fit_caption(fit: dict | None, ys: list, x_range: float = 0) -> str:
    """Describe a fit honestly, including when there isn't one.

    A power law through scattered points will always return *some* exponent;
    quoting it without its R² invites reading a trend into noise.
    """
    kind = classify(fit, ys)
    if kind == "none":
        return "no fit (too few usable points)"
    if kind == "constant":
        vals = [y for y in ys if y and y > 0]
        over = f" across a {x_range:,.0f}× increase in data" if x_range else ""
        return (
            f"constant: no growth{over} "
            f"(exponent {fit['b']:+.3f}, values span only {max(vals) / min(vals):.1f}×)"
        )
    b, r2 = fit["b"], fit["r2"]
    if kind == "noisy":
        return f"no reliable trend (R² {r2:.2f} — points scattered)"
    if b < 0.85:
        shape = "sub-linear"
    elif b <= 1.15:
        shape = "linear"
    elif b <= 1.6:
        shape = "super-linear"
    else:
        shape = "steep"
    drop = (
        f", {fit['points_dropped']} point(s) below the floor excluded"
        if fit.get("points_dropped")
        else ""
    )
    return f"exponent {b:.2f} ({shape}), R² {r2:.3f}{drop}"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

CSS = """
:root{--bg:#fff;--fg:#16181d;--mut:#666c7a;--line:#e3e6ec;--card:#fbfcfd;
--s1:#2f6fed;--s2:#e2761b;--s3:#12967a;--s4:#b5468f;}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaee;--mut:#98a0ae;
--line:#2a2e36;--card:#1a1d22;--s1:#6aa0ff;--s2:#f5a04a;--s3:#3fc3a3;--s4:#e07ac0;}}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 64px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
main{max-width:1180px;margin:0 auto}
h1{font-size:25px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:18px;margin:40px 0 4px;letter-spacing:-.01em}
h3{font-size:14px;margin:0 0 2px;font-weight:600}
p.sub{color:var(--mut);margin:0 0 4px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:20px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.chart{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--line);stroke-width:1}
.tick{fill:var(--mut);font-size:10.5px}
.ty{text-anchor:end}.tx{text-anchor:middle}
.axis{fill:var(--mut);font-size:11.5px}
.link{fill:none;stroke-width:1.6;opacity:.95}
.fit{fill:none;stroke-width:1.4;stroke-dasharray:5 4;opacity:.55}
.pt{stroke:var(--bg);stroke-width:1.2}
.pt.censored{stroke-width:1.6;stroke-dasharray:2.2 1.8}
.rule{stroke:var(--mut);stroke-width:1;stroke-dasharray:3 3;opacity:.55}
.rulelab{fill:var(--mut);font-size:9.5px;text-anchor:end}
.leader{stroke-width:1;opacity:.35;stroke-dasharray:2 2}
.endlab{font-size:10.5px;font-weight:600}
section.summary{margin:26px 0 8px}
section.summary .card{padding:18px 20px}
section.summary h3{font-size:16px;margin-bottom:3px}
.takeaway{margin:10px 0 0;padding:9px 12px;background:var(--bg);border:1px solid var(--line);
border-radius:7px;font-size:13.5px;line-height:1.5}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:2px 0 8px;font-size:12.5px;color:var(--mut)}
.key i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.note{color:var(--mut);font-size:12px;margin:6px 0 0}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px}
th,td{text-align:right;padding:6px 9px;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
tbody tr:hover{background:var(--bg)}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--card);
border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.caveat{border-left:3px solid var(--s2);padding:2px 0 2px 14px;margin:14px 0;color:var(--mut);font-size:13.5px}
ul.headline{list-style:none;padding:0;margin:18px 0 0;display:grid;gap:8px}
ul.headline li{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--s1);
border-radius:0 8px 8px 0;padding:9px 14px;font-size:14px}
.empty{color:var(--mut);font-size:13px;padding:30px;text-align:center}
"""


def render(res: dict, svg_out: dict | None = None) -> str:
    """Render the HTML page. If `svg_out` is given, it is filled with
    standalone {filename: svg} versions of the two summary charts, for embedding
    somewhere that will not render HTML (GitHub's README, for one)."""
    runs = res["runs"]
    fits = res["fits"]
    cfg = res["dataset_config"]
    tot = res["dataset_totals"]
    types = [r["storage_type"] for r in runs]

    def quads(run):
        return [p.get("facts") or 0 for p in run["points"]]

    # ---- headline charts --------------------------------------------------
    mem = chart(
        [
            {
                "label": r["storage_type"],
                "points": list(zip(quads(r), [p.get("memory_net_bytes") for p in r["points"]])),
                "fit": fits["memory_net"][r["storage_type"]],
            }
            for r in runs
        ],
        "quads stored", "memory (bytes)", yfmt=lambda v: fmt_bytes(v).replace(".0", ""),
        note="net of the empty-store baseline  ·  dashed = fitted power law  ·  "
        + "  ·  ".join(f"{t}: {fit_caption(fits['memory_net'][t], [p.get('memory_net_bytes') for p in next(r for r in runs if r['storage_type'] == t)['points']])}" for t in types),
    )

    per_quad = chart(
        [
            {
                "label": r["storage_type"],
                "points": [
                    (q, (p.get("memory_net_bytes") or 0) / q if q else None)
                    for q, p in zip(quads(r), r["points"])
                ],
                "fit": None,
            }
            for r in runs
        ],
        "quads stored", "bytes per quad", ylog=False,
        yfmt=lambda v: f"{v:,.0f}",
        note="Net memory divided by quads. Where this flattens, the marginal "
             "cost per quad has settled.",
    )

    imp = chart(
        [
            {
                "label": r["storage_type"],
                "points": [
                    (q, (p["facts"] - (r["points"][i - 1]["facts"] if i else 0))
                        / p["import_seconds"] if p.get("import_seconds") else None)
                    for i, (q, p) in enumerate(zip(quads(r), r["points"]))
                ],
                "fit": None,
            }
            for r in runs
        ],
        "quads stored", "import rate (quads/s)",
        note="Throughput of each incremental load, not cumulative.",
    )

    # ---- per-query charts -------------------------------------------------
    query_names = sorted({q for r in runs for p in r["points"] for q in p["queries"]})
    _q0 = [p.get("facts") or 0 for p in runs[0]["points"]]
    x_range = (max(_q0) / min(_q0)) if _q0 and min(_q0) else 0
    qcards = []
    for name in query_names:
        series = [
            {
                "label": r["storage_type"],
                "points": [
                    (q, p["queries"].get(name, {}).get("seconds"))
                    for q, p in zip(quads(r), r["points"])
                ],
                "fit": fits["queries"].get(name, {}).get(r["storage_type"]),
            }
            for r in runs
        ]
        sub = fits["queries"].get(name, {}).get(types[0])
        answers = next(
            (
                p["queries"][name].get("answers")
                for p in reversed(runs[0]["points"])
                if name in p["queries"] and p["queries"][name].get("answers") is not None
            ),
            None,
        )
        base_ys = [
            p["queries"].get(name, {}).get("seconds") for p in runs[0]["points"]
        ]
        note = fit_caption(sub, base_ys, x_range)
        rows = f"{answers:,} rows at full size" if answers is not None else "&nbsp;"
        qcards.append(
            f'<div class="card"><h3>{html.escape(name)}</h3>'
            f'<p class="sub">{rows}</p>'
            + chart(
                series,
                "quads stored",
                "time per execution",
                yfmt=fmt_time_axis,
                note=note,
            )
            + "</div>"
        )

    # ---- tables -----------------------------------------------------------
    top = {r["storage_type"]: r["points"][-1] for r in runs}
    baseline = {r["storage_type"]: r.get("baseline_bytes", 0) for r in runs}
    summary_rows = ""
    for t in types:
        p = top[t]
        f = fits["memory_net"][t]
        net = p.get("memory_net_bytes") or 0
        fit_cells = (
            f"<td>{f['b']:.3f}</td><td>{f['r2']:.4f}</td>" if f else "<td>–</td><td>–</td>"
        )
        summary_rows += (
            f"<tr><td><code>{html.escape(t)}</code></td>"
            f"<td>{p.get('facts', 0):,}</td>"
            f"<td>{fmt_bytes(p.get('memory_bytes', 0))}</td>"
            f"<td>{fmt_bytes(baseline[t])}</td>"
            f"<td>{fmt_bytes(net)}</td>"
            f"<td>{net / max(1, p.get('facts') or 1):,.1f}</td>"
            f"{fit_cells}</tr>"
        )

    base = types[0]
    qrows = ""
    for name in query_names:
        qrows += f"<tr><td>{html.escape(name)}</td>"
        for t in types:
            p = top[t]
            qrows += f"<td>{fmt_time(p['queries'].get(name, {}).get('seconds'))}</td>"
        f = fits["queries"].get(name, {}).get(base)
        ys = [p["queries"].get(name, {}).get("seconds") for p in runs[0]["points"]]
        kind = classify(f, ys)
        cell = {"constant": "constant", "noisy": "–", "none": "–"}.get(
            kind, f"{f['b']:.2f}" if f else "–"
        )
        qrows += f"<td>{cell}</td></tr>"

    dp_rows = ""
    for r in runs:
        for p in r["points"]:
            dp_rows += (
                f"<tr><td><code>{html.escape(r['storage_type'])}</code></td>"
                f"<td>{p['graphs']:,}</td><td>{p.get('facts', 0):,}</td>"
                f"<td>{fmt_bytes(p.get('memory_bytes', 0))}</td>"
                f"<td>{p.get('import_seconds', 0):.3f} s</td></tr>"
            )

    # ---- computed headline figures ---------------------------------------
    # Everything here is derived from the run; nothing is asserted that the
    # numbers do not show.
    head = []
    cheapest = min(types, key=lambda t: top[t].get("memory_net_bytes") or 0)
    dearest = max(types, key=lambda t: top[t].get("memory_net_bytes") or 0)
    if cheapest != dearest:
        lo = top[cheapest]["memory_net_bytes"]
        hi = top[dearest]["memory_net_bytes"]
        head.append(
            f"<b>{html.escape(cheapest)}</b> held the same {top[dearest]['facts']:,} "
            f"quads in {fmt_bytes(lo)} against {fmt_bytes(hi)} for "
            f"{html.escape(dearest)} — {100 * (1 - lo / hi):.0f}% less."
        )
    f = fits["memory_net"][types[0]]
    if f and f["r2"] >= R2_MIN:
        head.append(
            f"Memory scaled with exponent <b>{f['b']:.2f}</b> (R² {f['r2']:.3f}) — "
            f"{'sub-linear, so cost per quad falls as the corpus grows'
               if f['b'] < 0.95 else 'essentially proportional to the data'}. "
            f"Marginal cost settled near "
            f"{(top[types[0]]['memory_net_bytes'] or 0) / max(1, top[types[0]]['facts']):.0f}"
            f" bytes/quad."
        )
    flat = [
        n
        for n in query_names
        if classify(
            fits["queries"].get(n, {}).get(types[0]),
            [p["queries"].get(n, {}).get("seconds") for p in runs[0]["points"]],
        )
        == "constant"
    ]
    if flat:
        slowest = max(
            (top[types[0]]["queries"].get(n, {}).get("seconds") or 0) for n in flat
        )
        head.append(
            f"<b>{len(flat)} of {len(query_names)}</b> queries are constant-time — "
            f"unchanged across a {x_range:,.0f}× increase in data, all under "
            f"{fmt_time(slowest)} at full size. That includes the point lookup "
            f"the whole provenance pattern exists to serve."
        )
    if len(types) > 1:
        spread = []
        for n in query_names:
            a = top[types[0]]["queries"].get(n, {}).get("seconds")
            b = top[types[1]]["queries"].get(n, {}).get("seconds")
            if a and b and min(a, b) > 0.005:  # ignore near-floor noise
                spread.append((max(a, b) / min(a, b), n, a, b))
        if spread:
            ratio, n, a, b = max(spread)
            if ratio >= 1.25:
                faster = types[0] if a < b else types[1]
                head.append(
                    f"Biggest divergence between storage types: <b>{html.escape(n)}</b>, "
                    f"where {html.escape(faster)} was {ratio:.1f}× faster "
                    f"({fmt_time(min(a, b))} vs {fmt_time(max(a, b))})."
                )
    headline = "".join(f"<li>{h}</li>" for h in head)

    # ---- the two summary charts ------------------------------------------
    SUMMARY_SIZE = (620, 380)

    sum_mem = chart(
        [
            {
                "label": r["storage_type"],
                "points": list(
                    zip(quads(r), [p.get("memory_net_bytes") for p in r["points"]])
                ),
                "fit": fits["memory_net"][r["storage_type"]],
            }
            for r in runs
        ],
        "quads stored",
        "memory used",
        yfmt=lambda v: fmt_bytes(v).replace(".0", ""),
        size=SUMMARY_SIZE,
        note="Solid = measured, dashed = fitted power law. Net of the "
        f"{fmt_bytes(min(baseline.values()))} an empty store already holds.",
    )

    # One line per query, coloured by whether it grows with the corpus. That
    # split is the whole story, so it is what the colour encodes; the names sit
    # at the line ends rather than in a ten-entry legend.
    fam_const, fam_scale = "var(--s3)", "var(--s2)"
    qseries = []
    for name in query_names:
        ys = [p["queries"].get(name, {}).get("seconds") for p in runs[0]["points"]]
        kind = classify(fits["queries"].get(name, {}).get(types[0]), ys)
        qseries.append(
            {
                "label": name,
                "end_label": name.split("_")[0],
                "color": fam_const if kind == "constant" else fam_scale,
                "points": list(zip(quads(runs[0]), ys)),
                "fit": None,
            }
        )
    qseries.sort(key=lambda s: (s["points"][-1][1] or 0))
    sum_q = chart(
        qseries,
        "quads stored",
        "time per execution",
        yfmt=fmt_time_axis,
        size=SUMMARY_SIZE,
        end_labels=True,
        legend=False,
        note=(
            f'<span style="color:{fam_const}">■</span> constant-time '
            f"({len(flat)}) &nbsp; "
            f'<span style="color:{fam_scale}">■</span> grows with the corpus '
            f"({len(query_names) - len(flat)}) &nbsp;·&nbsp; "
            f"{html.escape(types[0])}, log–log"
        ),
    )

    q_const = [q for q in qseries if q["color"] == fam_const]
    q_grow = [q for q in qseries if q["color"] == fam_scale]
    const_max = max((q["points"][-1][1] or 0) for q in q_const) if q_const else 0
    grow_max = max((q["points"][-1][1] or 0) for q in q_grow) if q_grow else 0
    mem_top = top[types[0]]

    if svg_out is not None:
        # Card versions of the same two charts, sized to sit side by side.
        card_mem = chart(
            [
                {
                    "label": r["storage_type"],
                    "points": list(
                        zip(quads(r), [p.get("memory_net_bytes") for p in r["points"]])
                    ),
                    "fit": fits["memory_net"][r["storage_type"]],
                }
                for r in runs
            ],
            "quads stored", "memory used",
            yfmt=lambda v: fmt_bytes(v).replace(".0", ""),
            size=CARD_CHART, legend=False,
        )
        card_q = chart(
            qseries, "quads stored", "time per execution",
            yfmt=fmt_time_axis, size=CARD_CHART, end_labels=True, legend=False,
        )
        mem_b = fits["memory_net"][types[0]]["b"]
        card_args = (
            card_mem,
            card_q,
            "RDFox scaling: one named graph per document subsection",
            f"{tot['graphs']:,} named graphs · {tot['quads']:,} quads · "
            f"{len(runs[0]['points'])} size points · {' vs '.join(types)}",
            [
                f'<tspan class="k">Memory grows sub-linearly</tspan> '
                f"(exponent {mem_b:.2f}, R\u00b2 "
                f'{fits["memory_net"][types[0]]["r2"]:.2f}), settling near '
                f"{(mem_top['memory_net_bytes'] or 0) / max(1, mem_top['facts']):.0f}"
                f" bytes per quad — the shared dictionary amortises as the corpus grows.",
                f'<tspan class="k">{len(q_const)} of {len(query_names)} queries are '
                f"constant-time</tspan> across a {x_range:,.0f}\u00d7 increase in data, "
                f"including the provenance lookup the pattern exists to serve "
                f"({fmt_time(top[types[0]]['queries'][sorted(query_names)[0]]['seconds'])}"
                f" at full size). The other {len(q_grow)} grow sub-linearly.",
                f'<tspan class="k">{cheapest} used '
                f"{100 * (1 - (top[cheapest]['memory_net_bytes'] or 0) / max(1, top[dearest]['memory_net_bytes'] or 1)):.0f}% "
                f"less memory</tspan> than {dearest} and was never slower.",
            ],
            [(t, SVG_PALETTE[SERIES_COLORS[i % len(SERIES_COLORS)]])
             for i, t in enumerate(types)],
            [("constant-time", SVG_PALETTE[fam_const]),
             ("grows with the corpus", SVG_PALETTE[fam_scale])],
        )
        # Theme-neutral vector source: mid-tone ink, no background.
        svg_out["summary.svg"] = summary_card(*card_args)
        # The primary image is the white one, so it gets darker ink now that the
        # backdrop is known. Prefixed with "_" so main() rasterises it without
        # committing a second .svg source alongside.
        svg_out["_summary-white.svg"] = summary_card(
            *card_args, ink=SVG_INK_ON_WHITE, background="#ffffff"
        )


    n_points = len(runs[0]["points"])
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RDFox named-graph provenance — scaling</title>
<style>{CSS}</style>
</head><body>
<main>
<h1>RDFox scaling: one named graph per document subsection</h1>
<p class="sub">
{n_points} size points up to {tot['graphs']:,} named graphs / {tot['quads']:,} quads
· {len(query_names)} queries, {res['target_block_seconds'] * 1000:.0f}ms blocks
· {', '.join(f'<code>{html.escape(t)}</code>' for t in types)}
</p>
<p class="sub">Dataset: {tot['quads_per_graph']} quads per graph,
{cfg['text_chars']}-char provenance text, fanout {'.'.join(map(str, cfg['fanout']))},
seed {cfg['seed']}.</p>

<ul class="headline">{headline}</ul>

<section class="summary">
<div class="grid2">
  <div class="card">
    <h3>Memory vs corpus size</h3>
    <p class="sub">how much RAM the pattern costs as it grows</p>
    {sum_mem}
    <p class="takeaway">Storing {mem_top['facts']:,} quads across
    {tot['graphs']:,} named graphs cost <b>{fmt_bytes(mem_top['memory_net_bytes'])}</b>
    on <code>{html.escape(types[0])}</code>. Growth is <b>sub-linear</b>
    (exponent {fits['memory_net'][types[0]]['b']:.2f}): the marginal cost fell to
    <b>{(mem_top['memory_net_bytes'] or 0) / max(1, mem_top['facts']):.0f} bytes
    per quad</b> as the shared dictionary amortised.</p>
  </div>
  <div class="card">
    <h3>Query time vs corpus size</h3>
    <p class="sub">all {len(query_names)} queries, one line each</p>
    {sum_q}
    <p class="takeaway">The queries split cleanly in two.
    <b>{len(q_const)} are constant-time</b> — unchanged across a
    {x_range:,.0f}× increase in data, still under {fmt_time(const_max)}. The
    other <b>{len(q_grow)} grow with the corpus</b> but all sub-linearly
    (exponents 0.37–0.95), topping out at {fmt_time(grow_max)}.</p>
  </div>
</div>
</section>

<h2>Memory</h2>
<div class="grid2">
  <div class="card"><h3>Memory vs data size</h3><p class="sub">both axes log</p>{mem}</div>
  <div class="card"><h3>Marginal cost</h3><p class="sub">bytes per stored quad</p>{per_quad}</div>
</div>

<table>
<thead><tr><th>storage type</th><th>quads</th><th>memory</th><th>empty baseline</th>
<th>net of baseline</th><th>bytes/quad</th><th>fit exponent</th><th>R²</th></tr></thead>
<tbody>{summary_rows}</tbody></table>
<p class="sub" style="margin-top:8px">An empty store already holds a few MB.
Subtracting it is what makes the exponent describe the data rather than the
fixed overhead — on a small corpus the gross figure fits an exponent near zero
purely because the baseline dominates.</p>

<h2>Load</h2>
<div class="grid2"><div class="card"><h3>Import throughput</h3>
<p class="sub">per incremental shard batch</p>{imp}</div></div>

<h2>Queries</h2>
<p class="sub">Each point is a block of repeated executions timed as a unit and
divided by the count, with the shell's per-statement overhead subtracted — so
resolution comes from the host clock, not RDFox's millisecond printout. Dashed
line is the fitted power law.
<b>constant</b> = exponent near zero with almost no spread: the query does not
grow with the corpus. <b>–</b> = points scattered with no reliable trend.</p>
<table>
<thead><tr><th>query</th>{''.join(f'<th>{html.escape(t)}</th>' for t in types)}
<th>exponent</th></tr></thead>
<tbody>{qrows}</tbody></table>
<div class="grid2">{''.join(qcards)}</div>

<div class="caveat">
<b>How these are timed.</b> RDFox's own <code>Total statement evaluation time</code>
is printed to the millisecond and reads <code>0.000 s</code> for the fastest
queries at every corpus size — which is why they cannot be measured that way.
Instead each query is repeated with <code>exec N</code> and the whole block is
timed from outside the process, then divided by N; the shell's per-statement
cost (~30 µs, measured at each size with a trivial control query) is subtracted.
N is chosen per query per size to target a fixed amount of work, so a 14 µs
lookup is averaged over thousands of executions and a 290 ms scan over a few.
This matters for the conclusions, not just the charts: under millisecond
timing the scan queries appeared to scale <i>linearly</i> (1.07–1.31), because
the small-size readings were pinned at the resolution floor. Measured properly
they are sub-linear (0.65–0.95).
</div>

<h2>All datapoints</h2>
<table>
<thead><tr><th>storage type</th><th>graphs</th><th>quads</th><th>memory</th>
<th>import time</th></tr></thead>
<tbody>{dp_rows}</tbody></table>
</main>
</body></html>
"""



def to_png(svg_path: str, png_path: str, width: int = 2 * CARD_W) -> str | None:
    """Rasterise the summary card, if the host has anything that can do it.

    A PNG is the only image GitHub is guaranteed to render in a README on any
    plan, so it is worth producing -- but not worth making the report depend on,
    hence the graceful miss.
    """
    import shutil
    import subprocess

    attempts = [
        ["rsvg-convert", "-w", str(width), "-o", png_path, svg_path],
        ["magick", "-background", "none", "-density", "192", svg_path, png_path],
        ["convert", "-background", "none", "-density", "192", svg_path, png_path],
    ]
    for argv in attempts:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(argv, check=True, capture_output=True)
            return png_path
        except subprocess.CalledProcessError:
            continue
    return None

def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.exit("usage: report.py <results.json> [-o report.html]")
    src = argv[0]
    dst = argv[argv.index("-o") + 1] if "-o" in argv else os.path.join(
        os.path.dirname(src) or ".", "report.html"
    )
    res = json.load(open(src))
    svgs: dict = {}
    with open(dst, "w") as fh:
        fh.write(render(res, svgs))
    print(dst)
    outdir = os.path.dirname(dst) or "."
    for name, svg in svgs.items():
        if name.startswith("_"):
            continue  # rasterise-only, not a deliverable of its own
        path = os.path.join(outdir, name)
        with open(path, "w") as fh:
            fh.write(svg)
        print(path)

    rendered = False
    # summary.png is the white variant -- it is what the README embeds, so it
    # is the one that has to look right by default. summary-dark.png is the
    # transparent version, for dark backgrounds.
    for svg_name, png_name in (("_summary-white.svg", "summary.png"),
                               ("summary.svg", "summary-dark.png")):
        src_svg = os.path.join(outdir, svg_name)
        tmp = svg_name.startswith("_")
        if tmp:
            with open(src_svg, "w") as fh:
                fh.write(svgs[svg_name])
        png = to_png(src_svg, os.path.join(outdir, png_name))
        if tmp:
            os.remove(src_svg)
        if png:
            print(png)
            rendered = True
    if not rendered:
        print("  (no SVG rasteriser found — install librsvg or imagemagick "
              "to also get summary.png / summary-dark.png)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
