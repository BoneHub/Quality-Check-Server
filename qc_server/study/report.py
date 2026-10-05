"""A study's results: the numbers, the figures, the report, and the CSV files.

* **Items** are the bones of the study's subjects, each judged accept or reject. A reading that
  rejects the subject as a whole rejects each of its bones.
* **Intra-rater**: each rater's readings of an item, compared with each other.
* **Inter-rater**: the raters' first readings of an item, for each pair of raters and for all
  of them together. Later readings are the rater's second look, so they are left out here.

Each comparison gets % agreement, Krippendorff's alpha and Gwet's AC1, with 95% intervals from
resampling the subjects (see :mod:`.reliability`), drawn with the study's seed, so the same
readings always give the same numbers.

Codes stand for the raters everywhere; no file made here names one. The figures are drawn
with matplotlib's object-oriented API, one figure at a time, since matplotlib's settings are
shared by the whole process.
"""

from __future__ import annotations

import base64
import csv
import html
import io
import threading
import zipfile
from dataclasses import dataclass, field

import numpy as np

from .. import __version__
from .models import VERDICTS, Reading, Study
from .reliability import Agreement, Estimate, agreement

# Chart colours: one categorical hue for the marks, one blue ramp for magnitude, and inks and
# hairlines for everything else -- the reference palette of the data-viz guidelines, light mode.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES = "#2a78d6"
RAMP = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
    "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]  # fmt: skip
WHITE = "#ffffff"

#: PNG resolution, for print.
PNG_DPI = 200

#: Past this many raters the pair grid leaves its numbers to the table.
MAX_LABELLED_RATERS = 10

FIGURE_NAMES = {
    "intra": "figure1_intra_rater",
    "pairs": "figure2_inter_rater_pairs",
    "group": "figure3_inter_rater_group",
}

_figure_lock = threading.Lock()


# ------------------------------------------------------------------- the numbers
@dataclass(frozen=True)
class Progress:
    code: str
    done: int
    total: int


@dataclass
class Results:
    """Every comparison the study's readings allow."""

    study: Study
    readings: list[Reading]
    progress: list[Progress]
    intra: list[tuple[str, Agreement]] = field(default_factory=list)
    pairs: list[tuple[str, str, Agreement]] = field(default_factory=list)
    group: Agreement | None = None

    @property
    def codes(self) -> list[str]:
        return [progress.code for progress in self.progress]


def _code_order(code: str) -> tuple[int, str]:
    return len(code), code


def compute_results(study: Study, readings: list[Reading]) -> Results:
    """% agreement, alpha and AC1 for each rater (intra), each pair and all raters (inter)."""
    raters = sorted(study.raters, key=lambda rater: _code_order(rater.code))
    codes = [rater.code for rater in raters]
    done: dict[str, int] = {}
    for reading in readings:
        done[reading.code] = done.get(reading.code, 0) + 1
    results = Results(
        study=study,
        readings=readings,
        progress=[Progress(rater.code, done.get(rater.code, 0), len(rater.order)) for rater in raters],
    )

    units = [(subject.subject_key, bone) for subject in study.subjects for bone in subject.bones]
    unit_index = {unit: index for index, unit in enumerate(units)}
    subject_index = {subject.subject_key: index for index, subject in enumerate(study.subjects)}
    subject_of_unit = np.array([subject_index[key] for key, _ in units], dtype=int)
    verdicts_of = {(reading.code, reading.subject_key, reading.reading): reading.verdicts for reading in readings}

    def counts(sources: list[tuple[str, int]]) -> np.ndarray:
        """Per item, how many of these readings (code, reading number) accept it and reject it."""
        table = np.zeros((len(units), len(VERDICTS)))
        for code, number in sources:
            for subject in study.subjects:
                verdicts = verdicts_of.get((code, subject.subject_key, number))
                for bone, verdict in (verdicts or {}).items():
                    if (subject.subject_key, bone) in unit_index:
                        table[unit_index[(subject.subject_key, bone)], VERDICTS.index(verdict)] += 1
        return table

    settings = study.settings
    analyses = 0

    def measure(sources: list[tuple[str, int]]) -> Agreement:
        nonlocal analyses
        analyses += 1
        rng = np.random.default_rng([settings.seed, analyses])
        return agreement(counts(sources), subject_of_unit, settings.bootstrap_samples, rng)

    if settings.readings_per_rater >= 2:
        for code in codes:
            results.intra.append((code, measure([(code, n) for n in range(1, settings.readings_per_rater + 1)])))
    for first, second in ((a, b) for i, a in enumerate(codes) for b in codes[i + 1 :]):
        results.pairs.append((first, second, measure([(first, 1), (second, 1)])))
    if len(codes) >= 2:
        results.group = measure([(code, 1) for code in codes])
    return results


# ------------------------------------------------------------------ the files
@dataclass
class ReportFiles:
    """Everything a study's results are made of, ready to save or zip."""

    name: str
    html: str
    readings_csv: str
    results_csv: str
    figures: dict[str, tuple[bytes, bytes]]  # file stem -> (svg, png)


def build_report(study: Study, readings: list[Reading], generated_at: str) -> ReportFiles:
    """The study's results, as a report with figures and as CSV files."""
    results = compute_results(study, readings)
    figures = draw_figures(results)
    return ReportFiles(
        name=study.settings.name,
        html=report_html(results, figures, generated_at),
        readings_csv=readings_csv(study, readings),
        results_csv=results_csv(results),
        figures=figures,
    )


def results_zip(files: ReportFiles) -> bytes:
    """The report, its figures as SVG and PNG, and the CSV files, in one zip."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.html", files.html)
        archive.writestr("readings.csv", files.readings_csv)
        archive.writestr("results.csv", files.results_csv)
        for stem, (svg, png) in sorted(files.figures.items()):
            archive.writestr(f"figures/{stem}.svg", svg)
            archive.writestr(f"figures/{stem}.png", png)
    return buffer.getvalue()


# ----------------------------------------------------------------------- CSV
def readings_csv(study: Study, readings: list[Reading]) -> str:
    """One row per bone per reading, with the rater's code in place of their name.
    ``subject_rejected`` marks a reading that rejected the subject as a whole, and so each of
    its bones."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [
            "rater", "subject", "reading", "position", "bone", "verdict", "subject_rejected",
            "handed_out_at", "submitted_at", "comment",
        ]
    )  # fmt: skip
    bones_of = {subject.subject_key: subject.bones for subject in study.subjects}
    for reading in sorted(readings, key=lambda r: (_code_order(r.code), r.position)):
        for bone in bones_of.get(reading.subject_key, sorted(reading.verdicts)):
            if bone not in reading.verdicts:
                continue
            writer.writerow(
                [
                    reading.code,
                    reading.subject_key,
                    reading.reading,
                    reading.position,
                    bone,
                    reading.verdicts[bone],
                    "true" if reading.subject_rejected else "false",
                    reading.handed_at,
                    reading.submitted_at,
                    reading.comment or "",
                ]
            )
    return out.getvalue()


def results_csv(results: Results) -> str:
    """Every number of the report, one comparison a row."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [
            "comparison", "raters", "subjects", "items", "verdicts", "rejected_percent",
            "agreement_percent", "agreement_low", "agreement_high",
            "alpha", "alpha_low", "alpha_high", "ac1", "ac1_low", "ac1_high",
        ]
    )  # fmt: skip
    rows = [("intra-rater", code, found) for code, found in results.intra]
    rows += [("inter-rater pair", f"{a}-{b}", found) for a, b, found in results.pairs]
    if results.group is not None:
        rows.append(("inter-rater group", ",".join(results.codes), results.group))

    def number(value: float | None, scale: float = 1.0) -> str:
        return "" if value is None else f"{value * scale:.4f}"

    for comparison, raters, found in rows:
        writer.writerow(
            [
                comparison,
                raters,
                found.subjects,
                found.items,
                found.values,
                number(_rejected(found), 100),
                number(found.agreement.value, 100),
                number(found.agreement.low, 100),
                number(found.agreement.high, 100),
                number(found.alpha.value),
                number(found.alpha.low),
                number(found.alpha.high),
                number(found.ac1.value),
                number(found.ac1.low),
                number(found.ac1.high),
            ]
        )
    return out.getvalue()


def _rejected(found: Agreement) -> float | None:
    """The share of reject verdicts among those compared."""
    return found.shares[VERDICTS.index("reject")] if found.shares else None


# ------------------------------------------------------------------- figures
def _intra_measured(results: Results) -> bool:
    """Some rater has read a subject twice."""
    return any(found.items for _, found in results.intra)


def _inter_measured(results: Results) -> bool:
    """Some subject has been read by two raters."""
    return results.group is not None and results.group.items > 0


def draw_figures(results: Results) -> dict[str, tuple[bytes, bytes]]:
    """The figures the readings allow: intra-rater (two readings or more), the pair grid (three
    raters or more), and inter-rater by pair and for the group (two raters or more) -- each
    once there are readings to compare."""
    import matplotlib

    figures: dict[str, tuple[bytes, bytes]] = {}
    with _figure_lock, matplotlib.rc_context(_STYLE):
        if _intra_measured(results):
            rows = [(f"Rater {code}", found) for code, found in results.intra]
            figures[FIGURE_NAMES["intra"]] = _render(_dot_panels(rows))
        if len(results.codes) >= 3 and _inter_measured(results):
            figures[FIGURE_NAMES["pairs"]] = _render(_pair_grid(results))
        if _inter_measured(results):
            rows = [(f"{a}–{b}", found) for a, b, found in results.pairs] if len(results.codes) >= 3 else []
            rows.append(("All raters", results.group))
            figures[FIGURE_NAMES["group"]] = _render(_dot_panels(rows, summary_last=True))
    return figures


_STYLE = {
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "font.size": 9,
    "text.color": INK,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_SECONDARY,
    "xtick.color": INK_SECONDARY,
    "ytick.color": INK_SECONDARY,
    "svg.hashsalt": "bonehub-qc-study",  # the same SVG for the same results
    "svg.fonttype": "path",
}

_PANELS = (
    ("agreement", "% agreement", True),
    ("alpha", "Krippendorff's α", False),
    ("ac1", "Gwet's AC1", False),
)


def _render(figure) -> tuple[bytes, bytes]:
    svg, png = io.BytesIO(), io.BytesIO()
    figure.savefig(svg, format="svg", facecolor=SURFACE, metadata={"Date": None})
    figure.savefig(png, format="png", dpi=PNG_DPI, facecolor=SURFACE)
    return svg.getvalue(), png.getvalue()


def _dot_panels(rows: list[tuple[str, Agreement]], summary_last: bool = False):
    """One row per comparison, one panel per measure: the estimate as a dot, its 95% interval
    as a line. A summary row comes last, as a diamond below a hairline."""
    from matplotlib.figure import Figure

    n = len(rows)
    figure = Figure(figsize=(9.0, 0.95 + 0.34 * n), facecolor=SURFACE, layout="constrained")
    axes = figure.subplots(1, len(_PANELS), sharey=True)
    positions = np.arange(n)
    # Alpha and AC1 share a scale, so that the two coefficients can be compared at a glance.
    coefficient_range = _axis_range([getattr(found, name) for _, found in rows for name in ("alpha", "ac1")], 1.0, False)
    for column, (ax, (name, title, percent)) in enumerate(zip(axes, _PANELS)):
        scale = 100.0 if percent else 1.0
        estimates = [getattr(found, name) for _, found in rows]
        low, high = _axis_range(estimates, scale, percent) if percent else coefficient_range
        _style_axes(ax, low, high, percent)
        ax.set_title(title, loc="left", fontsize=10, color=INK, pad=6)
        for y, estimate in zip(positions, estimates):
            summary = summary_last and y == n - 1
            if estimate.value is None:
                ax.text(low, y, "  undefined", va="center", ha="left", fontsize=8, color=INK_MUTED)
                continue
            if estimate.low is not None and estimate.high is not None:
                ax.hlines(y, estimate.low * scale, estimate.high * scale, color=SERIES, linewidth=1.5, capstyle="round")
            ax.plot(
                [estimate.value * scale],
                [y],
                marker="D" if summary else "o",
                markersize=8 if summary else 6.5,
                color=SERIES,
                markeredgecolor=SURFACE,
                markeredgewidth=1.5,
                zorder=3,
            )
        if summary_last and n > 1:
            ax.axhline(n - 1.5, color=GRID, linewidth=0.8)
        if column == 0:
            ax.set_yticks(positions, [label for label, _ in rows])
            for tick, (label, _) in zip(ax.get_yticklabels(), rows):
                if summary_last and label == rows[-1][0]:
                    tick.set_fontweight("bold")
                    tick.set_color(INK)
    axes[0].set_ylim(n - 0.5, -0.5)
    return figure


def _axis_range(estimates: list[Estimate], scale: float, percent: bool) -> tuple[float, float]:
    """From a round number below the lowest interval -- 0 at most for a coefficient, so that
    chance agreement is on the axis -- to the maximum, 1 or 100%."""
    values = [v * scale for e in estimates for v in (e.value, e.low, e.high) if v is not None]
    top = 100.0 if percent else 1.0
    if not values:
        return (0.0, top)
    lowest = min(values)
    if percent:
        return (max(0.0, np.floor((lowest - 2) / 10) * 10), top)
    return (min(0.0, np.floor((lowest - 0.02) * 10) / 10), top)


def _style_axes(ax, low: float, high: float, percent: bool) -> None:
    from matplotlib.ticker import MultipleLocator

    span = high - low
    ax.set_xlim(low - 0.02 * span, high + 0.02 * span)
    if percent:
        ax.xaxis.set_major_locator(MultipleLocator(20 if span > 50 else 10))
    else:
        ax.xaxis.set_major_locator(MultipleLocator(0.25 if span > 1.25 else 0.2))
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(axis="both", length=0, labelsize=8.5, pad=4)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    if percent:
        ax.xaxis.set_major_formatter(lambda value, _: f"{value:.0f}%")


def _pair_grid(results: Results):
    """Krippendorff's alpha of each pair of raters, darker for higher, with % agreement below it:
    a triangle, each pair once."""
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    codes = results.codes
    n = len(codes)
    found = {(a, b): agreement_ for a, b, agreement_ in results.pairs}
    cmap = LinearSegmentedColormap.from_list("ramp", RAMP)
    size = 1.6 + 0.62 * (n - 1)
    figure = Figure(figsize=(size + 1.3, size), facecolor=SURFACE, layout="constrained")
    ax = figure.subplots()
    ax.set_facecolor(SURFACE)
    labelled = n <= MAX_LABELLED_RATERS
    for row in range(1, n):
        for column in range(row):
            pair = found[(codes[column], codes[row])]
            alpha = pair.alpha.value
            fill = SURFACE if alpha is None else cmap(min(max(alpha, 0.0), 1.0))
            # The surface-coloured edge is the gap between neighbouring cells.
            ax.add_patch(Rectangle((column, row - 1), 1, 1, facecolor=fill, edgecolor=SURFACE, linewidth=2.0))
            if alpha is None:
                ax.add_patch(Rectangle((column + 0.04, row - 1 + 0.04), 0.92, 0.92, fill=False, edgecolor=GRID))
            if labelled:
                ink = INK if alpha is None else _text_on(fill)
                ax.text(
                    column + 0.5,
                    row - 1 + 0.42,
                    "n/a" if alpha is None else f"{alpha:.2f}",
                    ha="center",
                    va="center",
                    fontsize=9.5,
                    color=ink,
                    fontweight="bold",
                )
                if pair.agreement.value is not None:
                    ax.text(
                        column + 0.5,
                        row - 1 + 0.72,
                        f"{pair.agreement.value * 100:.0f}%",
                        ha="center",
                        va="center",
                        fontsize=7.5,
                        color=ink,
                    )
    ax.set_xlim(0, n - 1)
    ax.set_ylim(n - 1, 0)
    ax.set_aspect("equal")
    ax.set_xticks(np.arange(n - 1) + 0.5, [f"Rater {code}" for code in codes[:-1]])
    ax.set_yticks(np.arange(n - 1) + 0.5, [f"Rater {code}" for code in codes[1:]])
    ax.tick_params(axis="both", length=0, labelsize=8.5, colors=INK_SECONDARY)
    ax.xaxis.tick_top()
    for spine in ax.spines.values():
        spine.set_visible(False)
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize

    scale = figure.colorbar(
        ScalarMappable(norm=Normalize(0.0, 1.0), cmap=cmap),
        ax=ax,
        fraction=0.05,
        pad=0.04,
        shrink=0.8,
        ticks=[0, 0.2, 0.4, 0.6, 0.8, 1.0],
    )
    scale.set_label("Krippendorff's α  (% agreement below it)" if labelled else "Krippendorff's α", color=INK_SECONDARY)
    scale.outline.set_visible(False)
    scale.ax.tick_params(length=0, labelsize=8, colors=INK_SECONDARY)
    return figure


def _text_on(fill) -> str:
    """Ink or white, whichever stands out more on the fill."""
    red, green, blue = fill[:3] if not isinstance(fill, str) else _rgb(fill)
    background = _luminance(red, green, blue)
    on_white = (1.05) / (background + 0.05)
    on_ink = (background + 0.05) / (_luminance(*_rgb(INK)) + 0.05)
    return WHITE if on_white > on_ink else INK


def _rgb(hex_color: str) -> tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(red: float, green: float, blue: float) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


# ----------------------------------------------------------------------- HTML
def report_html(results: Results, figures: dict[str, tuple[bytes, bytes]], generated_at: str) -> str:
    """The report: one self-contained page, its figures inline, with a table of the numbers
    beside each, and codes in place of the raters' names."""
    study = results.study
    settings = study.settings
    e = html.escape
    final = study.state == "ended"
    done = sum(progress.done for progress in results.progress)
    planned = sum(progress.total for progress in results.progress)
    status = (
        f"Final: the study ended at {e(_time(study.ended_at))}."
        if final
        else f"Provisional: the study is still running, with {done} of {planned} readings done."
    )
    bones = [len(subject.bones) for subject in study.subjects]
    subjects = ", ".join(e(subject.subject_key) for subject in study.subjects)

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{e(settings.name)} · reliability study</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f"<h1>{e(settings.name)}</h1>",
        '<p class="sub">Reliability study of the reviewers: intra-rater and inter-rater agreement</p>',
        f'<p class="status{"" if final else " provisional"}">{status}</p>',
        "<h2>Study</h2>",
        "<table class=\"facts\"><tbody>",
        _fact("Subjects", f"{len(study.subjects)} <details><summary>list</summary>{subjects}</details>"),
        _fact(
            "Items (bones)",
            (
                f"{sum(bones)}, {min(bones)}{'' if min(bones) == max(bones) else f'–{max(bones)}'} per subject"
                if bones
                else "0"
            ),
        ),
        _fact("Raters", f"{len(results.codes)}: {', '.join(results.codes)}"),
        _fact("Readings per rater", f"{settings.readings_per_rater} of each subject"),
        _fact(
            "Minimum gap",
            (
                f"{study.min_gap} other readings between two readings of one subject"
                if settings.readings_per_rater >= 2
                else "not applicable: one reading of each subject"
            ),
        ),
        _fact("Subject ids shown to raters", "yes" if settings.show_subject_id else "no"),
        _fact("Seed", str(settings.seed)),
        _fact("Bootstrap samples", f"{settings.bootstrap_samples} per interval"),
        _fact("Started", e(_time(study.started_at))),
        _fact("Ended", e(_time(study.ended_at)) if study.ended_at else "not yet"),
        _fact("Report made", e(_time(generated_at))),
        "</tbody></table>",
        "<h2>Completeness</h2>",
        _progress_table(results),
        "<h2>How to read this report</h2>",
        _METHOD,
        "<h2>Intra-rater reliability</h2>",
    ]
    if results.intra and _intra_measured(results):
        parts += [
            "<p>Does each rater agree with themselves? Each rater's readings of the same bone, compared.</p>",
            _figure(figures, "intra", 1, "Intra-rater reliability per rater: estimate (dot) and 95% interval (line)."),
            _agreement_table([(f"Rater {code}", found) for code, found in results.intra], "Rater"),
        ]
    elif results.intra:
        parts.append("<p>Nothing to compare yet: no rater has read a subject twice.</p>")
    else:
        parts.append("<p>Not measured: each rater read each subject once.</p>")

    parts.append("<h2>Inter-rater reliability</h2>")
    if results.group is not None and not _inter_measured(results):
        parts.append("<p>Nothing to compare yet: no subject has been read by two raters.</p>")
    elif results.group is not None:
        parts += [
            "<p>Do the raters agree with each other? Their first readings of each bone, compared. "
            "All raters together:</p>",
            _tiles(results.group),
        ]
        if len(results.codes) >= 3:
            parts.append(
                _figure(
                    figures,
                    "pairs",
                    2,
                    "Krippendorff's α of each pair of raters, darker for higher, with their % agreement below it.",
                )
            )
        parts += [
            _figure(
                figures,
                "group",
                3,
                (
                    "Inter-rater reliability for each pair of raters and for all raters together: estimate and 95% "
                    "interval."
                    if len(results.codes) >= 3
                    else "Inter-rater reliability of the two raters: estimate and 95% interval."
                ),
            ),
            _agreement_table(
                [(f"{a}–{b}", found) for a, b, found in results.pairs] + [("All raters", results.group)], "Raters"
            ),
        ]
    else:
        parts.append("<p>Not measured: the study has one rater.</p>")

    parts += [
        '<p class="foot">Codes stand for the raters throughout; only the administrator knows which code is whom. '
        f"BoneHub Quality Check server {e(__version__)}.</p>",
        "</main></body></html>",
    ]
    return "\n".join(parts)


_METHOD = """
<ul class="method">
<li><b>What was judged.</b> Every bone of every study subject, <i>accept</i> or <i>reject</i>, at each
reading. Rejecting a whole subject counts as rejecting each of its bones. The <i>Items</i> in the tables are
these bones.</li>
<li><b>Intra-rater: does a rater agree with themselves?</b> Each rater read the same subjects more than once,
with other subjects in between. Their readings of each bone are compared. One result per rater.</li>
<li><b>Inter-rater: do the raters agree with each other?</b> Their first readings of each bone are compared,
for each pair of raters, which shows who disagrees with whom, and for all raters together, which gives one
result for the whole team. Later readings are left out here: they are the same rater looking again.</li>
<li><b>% agreement: how often the verdicts match.</b> For two raters, or one rater's two readings, the share
of bones given the same verdict. For all raters together: for each bone, the share of pairs of raters who gave
it the same verdict, averaged over the bones.</li>
<li><b>Gwet's AC1: agreement with the part due to luck taken out.</b> 1 is perfect agreement, 0 is no better
than luck; as a rough guide, above 0.8 is good. When most bones are accepted, raters agree on many of them
even without looking closely, so % agreement looks better than it really is. AC1 removes that luck and stays
fair when rejects are rare.</li>
<li><b>Krippendorff's α: whether the raters reject the same bones.</b> Also 1 for perfect and 0 for luck, but
stricter about luck: raters who did not look at all, accepting at random as often as these raters did, would
still agree on most bones. So agreeing on accepts counts for little, and a few disagreements on the rare
rejects pull α down a lot. For example, at 95% agreement with 5% of verdicts rejects, AC1 is 0.94 but α is
0.47. α is shown because it is the best-known measure. It is <i>n/a</i> when every verdict was the same, as
nothing then tells agreement from luck.</li>
<li><b>Rejected</b> (in the tables): the share of verdicts that were rejects. The rarer they are, the further
apart AC1 and α can be.</li>
<li><b>95% interval: how sure each number is.</b> With other subjects, the numbers would come out slightly
different. The interval is the range the true value very likely lies in: a narrow one can be trusted, a wide
one means more subjects are needed. It comes from reshuffling the study's own subjects into many pretend
studies (see <i>Bootstrap samples</i> above), some subjects drawn twice and some not at all, and keeping the
middle 95% of the results. Whole subjects are drawn, since the bones of one scan tend to be judged together.
The draws use the study's seed, so the same readings always give the same intervals.</li>
</ul>
<p class="foot">Technical details: % agreement is Gwet's p<sub>a</sub>; α is Krippendorff's alpha for nominal
data (Krippendorff 2011); AC1 is Gwet's AC1 (Gwet 2008); intervals are percentile bootstrap intervals,
resampling subjects with replacement.</p>
"""

_CSS = """
:root { color-scheme: light; }
body { margin: 0; background: #f9f9f7; color: #0b0b0b;
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; background: #fcfcfb; }
h1 { font-size: 24px; margin: 0; }
h2 { font-size: 16px; margin: 32px 0 8px; padding-top: 12px; border-top: 1px solid #e1e0d9; }
.sub { color: #52514e; margin: 4px 0 12px; }
.status { display: inline-block; padding: 4px 10px; border-radius: 6px; border: 1px solid #e1e0d9; margin: 0; }
.status.provisional { border-color: #c98500; }
table { border-collapse: collapse; font-size: 13px; margin: 8px 0; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid #e1e0d9; vertical-align: top; }
th { color: #52514e; font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.facts th { width: 200px; }
details { display: inline; margin-left: 8px; color: #52514e; }
summary { cursor: pointer; display: inline; }
figure { margin: 16px 0; }
figure img { max-width: 100%; height: auto; display: block; }
figcaption { color: #52514e; font-size: 13px; margin-top: 4px; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; margin: 8px 0 16px; }
.tile { border: 1px solid #e1e0d9; border-radius: 8px; padding: 10px 14px; min-width: 180px; }
.tile .label { color: #52514e; font-size: 12px; }
.tile .value { font-size: 28px; font-weight: 600; }
.tile .ci { color: #52514e; font-size: 12px; font-variant-numeric: tabular-nums; }
.method li { margin: 4px 0; }
.foot { color: #898781; font-size: 12px; margin-top: 32px; }
@media print { body { background: #ffffff; } main { padding: 0; } h2 { break-after: avoid; } }
"""


def _fact(label: str, value: str) -> str:
    return f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>"


def _time(iso: str | None) -> str:
    return iso.replace("T", " ").replace("Z", " UTC") if iso else "—"


def _progress_table(results: Results) -> str:
    rows = "".join(
        f"<tr><td>Rater {html.escape(p.code)}</td><td class=\"num\">{p.done} of {p.total}</td>"
        f"<td class=\"num\">{(100 * p.done / p.total if p.total else 0):.0f}%</td></tr>"
        for p in results.progress
    )
    return (
        '<table><thead><tr><th>Rater</th><th class="num">Readings done</th><th class="num">Complete</th></tr>'
        f"</thead><tbody>{rows}</tbody></table>"
    )


def _figure(figures: dict[str, tuple[bytes, bytes]], key: str, number: int, caption: str) -> str:
    """A figure, its SVG inside the page as a data URI -- each its own document, so the ids of
    one figure's glyphs and clip paths cannot clash with another's."""
    svg = figures.get(FIGURE_NAMES[key], (b"", b""))[0]
    image = (
        f'<img alt="Figure {number}" src="data:image/svg+xml;base64,{base64.b64encode(svg).decode("ascii")}">'
        if svg
        else ""
    )
    return (
        f"<figure>{image}<figcaption><b>Figure {number}.</b> {html.escape(caption)} "
        f"<span class=\"file\">({FIGURE_NAMES[key]}.svg / .png)</span></figcaption></figure>"
    )


def _tiles(found: Agreement) -> str:
    tiles = []
    for name, label, percent in _PANELS:
        estimate = getattr(found, name)
        tiles.append(
            f'<div class="tile"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{_value(estimate, percent)}</div>'
            f'<div class="ci">95% interval {_interval(estimate, percent)}</div></div>'
        )
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _agreement_table(rows: list[tuple[str, Agreement]], first: str) -> str:
    head = (
        f"<tr><th>{html.escape(first)}</th><th class=\"num\">Subjects</th><th class=\"num\">Items</th>"
        '<th class="num">Rejected</th><th class="num">% agreement</th><th class="num">95% interval</th>'
        '<th class="num">α</th><th class="num">95% interval</th><th class="num">AC1</th>'
        '<th class="num">95% interval</th></tr>'
    )
    body = "".join(
        f"<tr><td>{html.escape(label)}</td><td class=\"num\">{found.subjects}</td><td class=\"num\">{found.items}</td>"
        f"<td class=\"num\">{_percent(_rejected(found))}</td>"
        f"<td class=\"num\">{_value(found.agreement, True)}</td><td class=\"num\">{_interval(found.agreement, True)}</td>"
        f"<td class=\"num\">{_value(found.alpha, False)}</td><td class=\"num\">{_interval(found.alpha, False)}</td>"
        f"<td class=\"num\">{_value(found.ac1, False)}</td><td class=\"num\">{_interval(found.ac1, False)}</td></tr>"
        for label, found in rows
    )
    note = ""
    if any(found.alpha.value is None and found.items for _, found in rows):
        note = '<p class="foot">n/a: undefined, as every verdict compared was the same.</p>'
    return f'<div style="overflow-x:auto"><table><thead>{head}</thead><tbody>{body}</tbody></table></div>{note}'


def _value(estimate: Estimate, percent: bool) -> str:
    if estimate.value is None:
        return "n/a"
    return f"{estimate.value * 100:.1f}%" if percent else f"{estimate.value:.2f}"


def _interval(estimate: Estimate, percent: bool) -> str:
    if estimate.low is None or estimate.high is None:
        return "—"
    if percent:
        return f"{estimate.low * 100:.1f}–{estimate.high * 100:.1f}%"
    return f"{estimate.low:.2f} to {estimate.high:.2f}"


def _percent(share: float | None) -> str:
    return "—" if share is None else f"{share * 100:.1f}%"
