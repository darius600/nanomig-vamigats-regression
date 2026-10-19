#!/usr/bin/env python3
"""Publish a vAmigaTS regression run into the results repository."""

import argparse
from collections import Counter
import concurrent.futures
from datetime import datetime
from functools import lru_cache
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.report import fail_size_markdown, fail_size_rows, get_vamigats_group
from loader.retrosh import parse_retrosh
from loader.run_test import get_git_revision, this_machine
from loader.side_by_side import render

VAMIGATS = REPO_ROOT / "external" / "vAmigaTS"


@lru_cache(maxsize=None)
def calls_synccpu(test_dir: str) -> bool:
    """Whether a test's own sources call vAmigaTS's synccpu routine."""
    return any("synccpu" in s.read_text(encoding="utf-8", errors="ignore").lower()
               for s in (VAMIGATS / test_dir).glob("*.s"))


def parse_manual_verdicts(md_content: str) -> Dict[str, Tuple[str, str]]:
    """Parse hand verdicts from a group markdown table in manual-2024-08.

    Columns: Test Case | Description / Source | Last run | Result | Comment
    Returns a dict mapping test_case -> (result_raw, comment_raw).
    """
    verdicts = {}
    for line in md_content.splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")]
        # Table row: cols[0] is empty, cols[1] is Test Case, cols[4] is Result, cols[5] is Comment
        if len(cols) >= 6:
            test_case = cols[1]
            if test_case in ("Test Case", "Test", "---") or test_case.startswith("---"):
                continue
            res = cols[4]
            comment = " | ".join(cols[5:-1]).strip() if len(cols) > 7 else (cols[5] if len(cols) > 5 else "")
            verdicts[test_case] = (res, comment)
    return verdicts


def normalize_verdict(raw_verdict: str) -> str:
    """Normalize hand verdict text into 'identical', 'different', or 'n/a'."""
    if "Identical" in raw_verdict:
        return "identical"
    if "Different" in raw_verdict:
        return "different"
    return "n/a"


def compute_agreement(
    manual_verdicts: Dict[str, Tuple[str, str]],
    variants_by_test_dir: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, int]:
    """Compute agreement between hand verdicts and automatic results for a group.

    Agreement categories per test directory:
    - both identical: hand == 'identical' and auto == 'identical'
    - both different: hand == 'different' and auto == 'different'
    - hand ident / auto diff: hand == 'identical' and auto == 'different'
    - hand diff / auto ident: hand == 'different' and auto == 'identical'
    - n/a: either hand verdict or automatic verdict is 'n/a'
    """
    tally = {
        "both_identical": 0,
        "both_different": 0,
        "hand_ident_auto_diff": 0,
        "hand_diff_auto_ident": 0,
        "na": 0,
    }

    all_test_dirs = set(manual_verdicts) | set(variants_by_test_dir)
    for td in all_test_dirs:
        variants = variants_by_test_dir.get(td, [])
        classes = {v.get("class") for v in variants}
        if not variants:
            auto = "n/a"
        elif classes == {"PASS"}:
            auto = "identical"
        elif "FAIL" in classes:
            auto = "different"
        else:
            auto = "n/a"

        raw_hand = manual_verdicts.get(td, ("", ""))[0]
        hand = normalize_verdict(raw_hand)

        if hand == "identical" and auto == "identical":
            tally["both_identical"] += 1
        elif hand == "different" and auto == "different":
            tally["both_different"] += 1
        elif hand == "identical" and auto == "different":
            tally["hand_ident_auto_diff"] += 1
        elif hand == "different" and auto == "identical":
            tally["hand_diff_auto_ident"] += 1
        else:
            tally["na"] += 1

    return tally


CLASSES = ["PASS", "FAIL", "DEVIATING_CONFIG", "NOT_CALIBRATED", "NO_REFERENCE", "ERROR"]
# Classes that ask nothing of the reader: listed after the compared variants
NOT_COMPARED = ("DEVIATING_CONFIG", "NOT_CALIBRATED", "NO_REFERENCE")
MACHINES = {"A500_OCS_1MB": "A500 OCS", "A500_ECS_1MB": "A500 ECS", "A500_PLUS_1MB": "A500+"}
NEAR_MISS_PX = 20
REGRESSION_URL = "https://github.com/darius600/nanomig-vamigats-regression"


def class_counts(results: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {c: 0 for c in CLASSES}
    for r in results:
        c = r.get("class", "ERROR")
        counts[c if c in counts else "ERROR"] += 1
    return counts


def test_dir_of(r: Dict[str, Any]) -> str:
    return str(Path(r.get("retrosh_path", "")).parent)


def machine_of(r: Dict[str, Any]) -> str:
    m = r.get("machine") or ""
    return MACHINES.get(m, m)


def image_link(r: Dict[str, Any]) -> str:
    """Relative link from the results repo root to the variant's image."""
    variant = r.get("variant", "")
    return f"[{variant}.png]({test_dir_of(r)}/{variant}.png)"


def not_compared_reason(r: Dict[str, Any]) -> str:
    c = r.get("class")
    if c == "NOT_CALIBRATED":
        geom = r.get("geometry") or r.get("detail", "").split("uncalibrated geometry:")[-1].strip()
        return f"raster {geom} not calibrated" if geom else "raster not calibrated"
    if c == "NO_REFERENCE":
        return "no reference .raw in vAmigaTS"
    return r.get("detail", "")


def sorted_by_test(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(results, key=lambda r: (test_dir_of(r), r.get("variant", "")))


def generate_group_markdown(
    group: str,
    results: List[Dict[str, Any]],
    manual_verdicts: Dict[str, Tuple[str, str]],
    nanomig_sha: str,
    vamigats_sha: str,
) -> str:
    """Generate markdown page for a group (e.g. Agnus_Blitter.md).

    The compared variants (PASS, FAIL, ERROR) come first, in path order; the ones
    that were not compared follow in their own table at the end.
    """
    counts = class_counts(results)
    compared = [r for r in results if r.get("class", "ERROR") not in NOT_COMPARED]
    skipped = [r for r in results if r.get("class") in NOT_COMPARED]
    n_skipped = len(skipped)

    status = (f"- **Compared:** {counts['PASS'] + counts['FAIL']} "
              f"(PASS {counts['PASS']}, FAIL {counts['FAIL']}) | **ERROR:** {counts['ERROR']} | "
              f"**Not compared:** {n_skipped}")
    if n_skipped:
        status += (f" (DEVIATING_CONFIG {counts['DEVIATING_CONFIG']}, NOT_CALIBRATED "
                   f"{counts['NOT_CALIBRATED']}, NO_REFERENCE {counts['NO_REFERENCE']}; listed at the end)")
    lines = [
        f"# {group.replace('/', '_')}",
        "",
        f"- **NanoMig:** [{nanomig_sha}](https://github.com/harbaum/NanoMig/commit/{nanomig_sha}) | "
        f"**vAmigaTS:** [{vamigats_sha}](https://github.com/dirkwhoffmann/vAmigaTS/commit/{vamigats_sha})",
        status,
        "",
    ]

    if compared:
        lines += [
            "| Test | Variant | Machine | Result | Differing px | Labels | Hand 2024 | Hand comment | Image |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sorted_by_test(compared):
            res_class = r.get("class", "ERROR")
            diff_px = {"PASS": "0", "FAIL": str(r.get("differing_pixels", 0))}.get(res_class, "—")
            hand_res, hand_comment = manual_verdicts.get(test_dir_of(r), ("", ""))
            image_cell = image_link(r) if res_class in ("PASS", "FAIL") else "—"
            labels = ", ".join(f"`{label}`" for label in r.get("labels", []))
            lines.append(
                f"| {test_dir_of(r)} | {r.get('variant', '')} | {machine_of(r)} | {res_class} | {diff_px} "
                f"| {labels} | {hand_res} | {hand_comment} | {image_cell} |"
            )
    else:
        lines.append("No variant of this group was compared.")

    if skipped:
        lines += [
            "",
            "## Not compared",
            "",
            "| Test | Variant | Machine | Result | Reason | Labels |",
            "|---|---|---|---|---|---|",
        ]
        for r in sorted_by_test(skipped):
            labels = ", ".join(f"`{label}`" for label in r.get("labels", []))
            lines.append(
                f"| {test_dir_of(r)} | {r.get('variant', '')} | {machine_of(r)} | {r.get('class')} "
                f"| {not_compared_reason(r)} | {labels} |"
            )

    lines.append("")
    return "\n".join(lines)


def generate_test_report_markdown(
    results: List[Dict[str, Any]],
    group_agreements: Dict[str, Dict[str, int]],
    nanomig_sha: str,
    vamigats_sha: str,
    regression_sha: str,
    build_profile: str,
    date_str: str,
    synccpu_fails: Optional[int] = None,
) -> str:
    """Generate TestReport.md content.

    synccpu_fails: number of FAILs from tests whose source calls vAmigaTS's
    synccpu routine (None leaves the paragraph out).
    """
    summary = class_counts(results)
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_group.setdefault(get_vamigats_group(r.get("retrosh_path", "")), []).append(r)

    n_compared = summary["PASS"] + summary["FAIL"]
    n_skipped = sum(summary[c] for c in NOT_COMPARED)
    fails = [r for r in results if r.get("class") == "FAIL"]
    near = sorted((r for r in fails if (r.get("differing_pixels") or 0) <= NEAR_MISS_PX),
                  key=lambda r: (r.get("differing_pixels") or 0, test_dir_of(r), r.get("variant", "")))
    passes = [r for r in results if r.get("class") == "PASS"]
    raw_identical = sum(1 for r in passes if r.get("raw_status") == "byte-identical")

    simulated = [r for r in results if r.get("harness_exit_code") is not None]
    if simulated:
        first = min(r.get("start_time") or "" for r in simulated)[:10]
        last = max(r.get("end_time") or "" for r in simulated)[:10]
        simulated_line = f"- **Simulated:** {first} to {last} · **Published:** {date_str[:10]}"
    else:
        simulated_line = f"- **Published:** {date_str[:10]}"
    machines = Counter(r["simulated_on"] for r in results if r.get("simulated_on"))
    machine_line = ("- **Simulated on:** " + "; ".join(f"{m} ({n} variants)" for m, n in machines.most_common())
                    if machines else "")
    if regression_sha.endswith("-dirty") or regression_sha == "unknown":
        code_line = f"- **Published with:** nanomig-vamigats-regression `{regression_sha}`"
    else:
        code_line = (f"- **Published with:** [nanomig-vamigats-regression `{regression_sha[:7]}`]"
                     f"({REGRESSION_URL}/commit/{regression_sha})")

    def near_rows(rows: List[Dict[str, Any]]) -> List[str]:
        out = ["| Test | Variant | Machine | Differing px | Image |", "|---|---|---|---:|---|"]
        for r in rows:
            out.append(f"| {test_dir_of(r)} | {r.get('variant', '')} | {machine_of(r)} "
                       f"| {r.get('differing_pixels')} | {image_link(r)} |")
        return out

    lines = [
        "# NanoMig vAmigaTS Test Report",
        "",
        f"- **NanoMig:** [{nanomig_sha}](https://github.com/harbaum/NanoMig/commit/{nanomig_sha}) · "
        f"**vAmigaTS:** [{vamigats_sha}](https://github.com/dirkwhoffmann/vAmigaTS/commit/{vamigats_sha}) · "
        f"**Build profile:** {build_profile}",
        simulated_line,
        *([machine_line] if machine_line else []),
        code_line,
        "",
        "## Summary",
        "",
        f"**{n_compared} variants compared: {summary['PASS']} identical to the reference (PASS), "
        f"{summary['FAIL']} different (FAIL).** The other {n_skipped} were not compared "
        f"(DEVIATING_CONFIG {summary['DEVIATING_CONFIG']}, NOT_CALIBRATED {summary['NOT_CALIBRATED']}, "
        f"NO_REFERENCE {summary['NO_REFERENCE']}; see [How to read](#how-to-read)). ERROR: {summary['ERROR']}.",
        "",
        "| Variants | PASS | FAIL | DEVIATING_CONFIG | NOT_CALIBRATED | NO_REFERENCE | ERROR |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {len(results)} | " + " | ".join(str(summary[c]) for c in CLASSES) + " |",
        "",
        "## Results per group",
        "",
        "| Group | Variants | PASS | FAIL | DEVIATING_CONFIG | NOT_CALIBRATED | NO_REFERENCE | ERROR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for grp in sorted(by_group):
        c = class_counts(by_group[grp])
        lines.append(f"| [{grp}]({grp.replace('/', '_')}.md) | {len(by_group[grp])} | "
                     + " | ".join(str(c[k]) for k in CLASSES) + " |")
    lines.append(f"| **Total** | **{len(results)}** | " + " | ".join(f"**{summary[c]}**" for c in CLASSES) + " |")
    lines.append("")

    if near or synccpu_fails:
        lines += ["## Where to start", ""]
    if near:
        lines += [
            f"The {min(10, len(near))} FAILs closest to the reference. Each is one test that reruns in "
            "about 15 minutes:",
            "",
            *near_rows(near[:10]),
            "",
            f"All {len(near)} FAILs with {NEAR_MISS_PX} differing pixels or fewer: [Near misses](#near-misses).",
            "",
        ]
    if synccpu_fails:
        lines += [
            f"**One routine behind many FAILs:** {synccpu_fails} of the {summary['FAIL']} FAILs come from tests "
            "whose source calls vAmigaTS's `synccpu` routine (`include/util.i`). It synchronises the CPU with the "
            "beam and draws the magenta bar at the top of the image. Where that bar differs, everything the CPU "
            "draws after it is shifted as well, so these FAILs may share one cause. That has not been established yet.",
            "",
        ]

    lines += [
        "## How far off the FAILs are",
        "",
        "Differing pixels per FAIL, of the 716 x 285 = 204060 compared pixels. Most tests draw their result as "
        "Copper or DMA stripes, so a stripe one colour clock off already costs thousands of pixels: the size says "
        "more about the kind of difference than about how close NanoMig is.",
        "",
        *fail_size_markdown(fail_size_rows(results)),
        "",
        "## Agreement with the 2024 hand verdicts",
        "",
        "Counted per **test directory**, not per variant: one directory holds the variants of one test "
        "(`_ocs`, `_ecs`, ...). A directory counts as identical when all its compared variants PASS, and as "
        "different when one of them FAILs. Groups without any hand verdict are left out. Where the two "
        "disagree, the class counts: it comes from the exact comparison, while the hand verdict was made by "
        "eye on older screenshots.",
        "",
        "| Group | Both identical | Both different | Hand identical, now different | Hand different, now identical |",
        "|---|---:|---:|---:|---:|",
    ]
    keys = ("both_identical", "both_different", "hand_ident_auto_diff", "hand_diff_auto_ident")
    totals = {k: 0 for k in keys}
    for grp in sorted(by_group):
        agr = group_agreements.get(grp, {})
        if not any(agr.get(k, 0) for k in keys):
            continue
        for k in keys:
            totals[k] += agr.get(k, 0)
        lines.append(f"| [{grp}]({grp.replace('/', '_')}.md) | " + " | ".join(str(agr.get(k, 0)) for k in keys) + " |")
    lines.append("| **Total** | " + " | ".join(f"**{totals[k]}**" for k in keys) + " |")

    if passes:
        raw_sentence = (f"In all {len(passes)} PASS" if raw_identical == len(passes)
                        else f"In {raw_identical} of the {len(passes)} PASS")
        raw_sentence += " the `.raw` rebuilt from the capture is also byte-identical to the reference (`raw_status`)."
    else:
        raw_sentence = ""
    lines += [
        "",
        "## How to read",
        "",
        "- **PASS:** The frame is identical to the vAmigaTS reference at the Amiga's 4 bits per channel inside "
        f"the cutout. {raw_sentence}".rstrip(),
        "- **FAIL:** Differences were found inside the compared area (cutout). The group pages give the number "
        "of differing pixels and link the image.",
        "- **DEVIATING_CONFIG:** The script asks for something this run could not set NanoMig on a Tang Nano 20k "
        "up for, e.g. a 68010. Not simulated; the reason is on the group page.",
        "- **NOT_CALIBRATED:** Simulated, but the raster has no verified crop yet, so it is not compared.",
        "- **NTSC** frames are compared on the 237 rows an NTSC beam reaches; the `_ocs` NTSC references are "
        "black there, so their PASS says little.",
        "- **NO_REFERENCE:** vAmigaTS ships no reference `.raw` for this variant; not simulated.",
        "- **ERROR:** The simulation itself failed or exceeded its time limit. Rerun it.",
        "- **`raw_status` \"missing colours\"** (in `.result.json`): NanoMig produced a colour that no reference "
        "uses, so no `.raw` can be rebuilt. The frame is still compared at 4 bits per channel.",
        "- **The references are vAmiga screenshots**, not photos of real hardware: `PASS` means identical to "
        "vAmiga. See [REFERENCES.md](REFERENCES.md).",
        "- **Labels** never change a result. `denise-ecs-sensitive`: an `_ecs` test that uses an ECS-only Denise "
        "feature; vAmiga's A500 ECS has an OCS Denise, NanoMig's ECS mode an ECS Denise. "
        "`reference-contradicts-photo`: the vAmigaTS hardware photo of this test shows something else than the "
        "reference.",
        "- **Image titles** give positions in vAmiga's coordinates (x 196–911, y 26–310); in the 716 × 285 "
        "panels, subtract 196 from x and 26 from y.",
        *(["- **Several machines** simulated this run (see *Simulated on*, and `simulated_on` in each "
           "`.result.json`). All ran the same harness binary, and a machine took part only after its captures "
           "of a control set were byte-identical with those of the publishing machine."] if len(machines) > 1 else []),
        "- **Hand verdicts (2024):** The hand verdicts of August 2024 are kept in tag `manual-2024-08`, whose "
        "`_ecs` screenshots came from a simulation without an ECS mode.",
        "",
    ]
    if near:
        lines += [
            "## Near misses",
            "",
            f"All FAILs with {NEAR_MISS_PX} differing pixels or fewer, fewest first.",
            "",
            *near_rows(near),
            "",
        ]

    return "\n".join(lines)


def generate_readme_markdown() -> str:
    """Generate updated README.md for the results repository."""
    lines = [
        "# nanomig-vamigats-results",
        "",
        "Results of running the [vAmiga Test Suite](https://github.com/dirkwhoffmann/vAmigaTS) (vAmigaTS) "
        "against the [NanoMig](https://github.com/harbaum/NanoMig) core in a Verilator simulation.",
        "",
        "NanoMig is based on the [MiSTer Minimig-AGA](https://github.com/MiSTer-devel/Minimig-AGA_MiSTer) code "
        "(NanoMig's README says so), so a FAIL here may concern other Minimig-derived cores as well. Whether it "
        "does has to be checked on that core.",
        "",
        "Every frame is compared pixel by pixel with the test's `.raw` reference. **These references are "
        "screenshots made by the vAmiga emulator**, the baselines vAmigaTS uses for vAmiga's own regression "
        "tests. A `PASS` therefore means *identical to vAmiga*. The vAmigaTS test folders also hold photos "
        "of real Amiga screens (`*_A500_*.JPG`); only those show real hardware. "
        "[REFERENCES.md](./REFERENCES.md) explains this, which machines are compared, and the known cases "
        "where a reference contradicts the photo.",
        "",
        "👉 **Start at [TestReport.md](./TestReport.md)**: summary, results per group, then where to start "
        "(the FAILs closest to the reference) and how far off the FAILs are.",
        "",
        "## Contents",
        "",
        "- **Group pages (`<Chip>_<Group>.md`):** one row per compared variant with machine, result, differing "
        "pixels, labels, the 2024 hand verdict where one exists, and a link to the image; the variants that were "
        "not compared follow at the end, with the reason.",
        "- **Images (`.png`)**, per compared variant: NanoMig | vAmigaTS reference | differences "
        "(differing pixels red, area outside the compared cutout shaded blue). Positions in the titles are "
        "vAmiga's coordinates (x 196–911, y 26–310).",
        "- **Captures (`.frame.vga.gz`):** NanoMig's raw VGA frame, so the comparison can be repeated without simulating.",
        "- **`.result.json`:** per variant: class, differing pixels, machine, labels, configuration, versions and timing.",
        "",
        "## Running it yourself",
        "",
        f"The harness, the comparison and the commands are in [nanomig-vamigats-regression]({REGRESSION_URL}). "
        "Its README shows how to run one test or a group, also against your own NanoMig checkout, and how to "
        "compare that run with these results (`loader.vs_published`).",
        "",
        "## Historical Hand Comparison (2024-08)",
        "",
        "The previous manual comparison from August 2024 is preserved in git tag `manual-2024-08`. Note that `_ecs` screenshots in that tag originated from a simulation run without an ECS mode.",
        "",
    ]
    return "\n".join(lines)


def check_publishable(results: List[Tuple[Path, Dict[str, Any]]]) -> None:
    """Only normal runs of a committed NanoMig checkout are published. Results of
    experimental modes carry a marker (zygote_fork_at_s from the experimental/zygote
    branch, test_wait_s from a shortened test run) and are refused."""
    marked = [str(rf) for rf, r in results if "zygote_fork_at_s" in r or "test_wait_s" in r]
    dirty = [str(rf) for rf, r in results if str(r.get("nanomig_sha", "")).endswith("-dirty")]
    problems = []
    if marked:
        problems.append(f"{len(marked)} results come from an experimental mode (rerun them normally), "
                        f"e.g. {marked[0]}")
    if dirty:
        problems.append(f"{len(dirty)} results come from an uncommitted NanoMig checkout, e.g. {dirty[0]}")
    if problems:
        raise RuntimeError("refusing to publish: " + "; ".join(problems))


def _render_worker(task: Tuple[Path, Path, Path]) -> str:
    """Worker function for parallel rendering."""
    vga_path, dest_dir, old_dir = task
    return render(vga_path, dest_dir, old_dir=old_dir)


def publish(
    run_dir: Path | str,
    results_repo: Path | str,
    tag: str = "manual-2024-08",
    jobs: int = 8,
    with_old: bool = False,
) -> None:
    """Publish a run directory into the results repository.

    The hand verdicts always come from the tag; its old screenshots are added
    to the images only with with_old (images then grow from 3 to 6 panels).
    """
    run_path = Path(run_dir).resolve()
    results_path = Path(results_repo).resolve()

    if not run_path.exists():
        raise FileNotFoundError(f"Run directory not found: {run_path}")
    if not results_path.exists():
        raise FileNotFoundError(f"Results repo directory not found: {results_path}")

    # 1. Collect all results from run_dir
    result_files = sorted(run_path.rglob("result.json"))
    if not result_files:
        raise RuntimeError(f"No result.json files found in {run_path}")

    results_data: List[Tuple[Path, Dict[str, Any]]] = []
    local_machine = this_machine()
    for rf in result_files:
        data = json.loads(rf.read_text(encoding="utf-8"))
        # Labels and machine come from the test sources, not from the run: a
        # corrected label applies without simulating again
        retrosh_file = VAMIGATS / data.get("retrosh_path", "")
        if retrosh_file.is_file():
            cfg = parse_retrosh(retrosh_file)
            data["labels"] = cfg.labels
            data["machine"] = cfg.machine
        # the run's diff image is not published; the side-by-side image replaces it
        data.pop("diff_image", None)
        # which CPU simulated it, not which machine: results from before simulated_on
        # existed were all simulated on the publishing machine
        data.pop("simulated_by", None)
        if data.get("harness_exit_code") is not None and not data.get("simulated_on"):
            data["simulated_on"] = local_machine
        results_data.append((rf, data))
    check_publishable(results_data)

    # Read run metadata from report.json if available
    report_json_path = run_path / "report.json"
    if report_json_path.exists():
        rep_info = json.loads(report_json_path.read_text(encoding="utf-8"))
        nanomig_sha = rep_info.get("nanomig_sha", results_data[0][1].get("nanomig_sha", "unknown"))
        vamigats_sha = rep_info.get("vamigats_sha", results_data[0][1].get("vamigats_sha", "unknown"))
    else:
        nanomig_sha = results_data[0][1].get("nanomig_sha", "unknown")
        vamigats_sha = results_data[0][1].get("vamigats_sha", "unknown")

    build_profile = "Tang Nano 20k, fx68k"
    regression_sha = get_git_revision(REPO_ROOT)
    date_str = datetime.now().astimezone().isoformat()

    # 2. Extract old tag tree into a temp dir
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_dir = Path(tmp_dir)
        p1 = subprocess.Popen(
            ["git", "-C", str(results_path), "archive", tag],
            stdout=subprocess.PIPE,
        )
        p2 = subprocess.Popen(["tar", "-x", "-C", str(old_dir)], stdin=p1.stdout)
        p1.stdout.close()
        p2.communicate()
        if p2.returncode != 0:
            raise RuntimeError(f"Failed to extract tag '{tag}' via git archive")

        # 3. Read hand verdicts from tag
        # Group results by 2-level group
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for _, r in results_data:
            grp = get_vamigats_group(r.get("retrosh_path", ""))
            by_group.setdefault(grp, []).append(r)

        manual_verdicts_by_group: Dict[str, Dict[str, Tuple[str, str]]] = {}
        for grp in by_group:
            md_name = f"{grp.replace('/', '_')}.md"
            try:
                out = subprocess.check_output(
                    ["git", "-C", str(results_path), "show", f"{tag}:{md_name}"],
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8")
                manual_verdicts_by_group[grp] = parse_manual_verdicts(out)
            except subprocess.CalledProcessError:
                manual_verdicts_by_group[grp] = {}

        # 4. Clean published content in working tree (filesystem only)
        # Identify top-level directories present in run (e.g. Agnus)
        top_level_dirs: Set[str] = set()
        for _, r in results_data:
            parts = Path(r.get("retrosh_path", "")).parts
            if parts:
                top_level_dirs.add(parts[0])

        for top_dir in top_level_dirs:
            target_top = results_path / top_dir
            # Only plain vAmigaTS top-level names inside the results repo may be deleted
            if (not top_dir or top_dir in (".", "..") or "/" in top_dir or "\\" in top_dir
                    or target_top.resolve().parent != results_path):
                raise RuntimeError(f"refusing to delete {target_top}")
            if target_top.exists():
                shutil.rmtree(target_top)
            # Delete old group pages for this top-level dir
            for old_md in results_path.glob(f"{top_dir}_*.md"):
                old_md.unlink()

        # 5. Populate variants in working tree
        render_tasks: List[Tuple[Path, Path, Path]] = []
        for rf, r in results_data:
            variant = r.get("variant", "")
            retrosh_path = r.get("retrosh_path", "")
            test_dir_rel = Path(retrosh_path).parent  # e.g. Agnus/Blitter/bbusy/bbusy0
            dest_dir = results_path / test_dir_rel
            dest_dir.mkdir(parents=True, exist_ok=True)

            # The result as the pages show it (labels, machine), as <variant>.result.json
            dest_json = dest_dir / f"{variant}.result.json"
            dest_json.write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")

            # Find capture .frame.vga.gz (or .frame.vga)
            vga_gz = rf.parent / f"{variant}.frame.vga.gz"
            vga_raw = rf.parent / f"{variant}.frame.vga"
            dest_vga_gz = dest_dir / f"{variant}.frame.vga.gz"

            if vga_gz.is_file():
                shutil.copy2(vga_gz, dest_vga_gz)
                capture_to_render = dest_vga_gz
            elif vga_raw.is_file():
                # Gzip into dest_vga_gz
                with open(vga_raw, "rb") as f_in, gzip.open(dest_vga_gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                capture_to_render = dest_vga_gz
            else:
                capture_to_render = None

            if capture_to_render and r.get("class") in ("PASS", "FAIL"):
                # Render from the run's capture: its result.json sits next to it and
                # names the .retrosh, so ambiguous variant names (timing1) resolve.
                src_capture = vga_gz if vga_gz.is_file() else vga_raw
                render_tasks.append((src_capture, dest_dir, old_dir if with_old else None))

        # Run side-by-side renders; a skipped image is an error, not a silent gap
        if render_tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                futures = [ex.submit(_render_worker, task) for task in render_tasks]
                outcomes = [f.result() for f in concurrent.futures.as_completed(futures)]
            skipped = [o for o in outcomes if "skipped" in o]
            if skipped:
                raise RuntimeError("side-by-side images skipped:\n" + "\n".join(skipped))

        # 6. Generate Group pages
        group_agreements: Dict[str, Dict[str, int]] = {}
        for grp, r_list in by_group.items():
            grp_manual = manual_verdicts_by_group.get(grp, {})

            # Group variants by test_dir for agreement computation
            variants_by_td: Dict[str, List[Dict[str, Any]]] = {}
            for r in r_list:
                td = str(Path(r.get("retrosh_path", "")).parent)
                variants_by_td.setdefault(td, []).append(r)

            agr = compute_agreement(grp_manual, variants_by_td)
            group_agreements[grp] = agr

            md_content = generate_group_markdown(
                group=grp,
                results=r_list,
                manual_verdicts=grp_manual,
                nanomig_sha=nanomig_sha,
                vamigats_sha=vamigats_sha,
            )
            md_path = results_path / f"{grp.replace('/', '_')}.md"
            md_path.write_text(md_content, encoding="utf-8")

        # 7. Generate TestReport.md
        all_results_flat = [r for _, r in results_data]
        report_content = generate_test_report_markdown(
            results=all_results_flat,
            group_agreements=group_agreements,
            nanomig_sha=nanomig_sha,
            vamigats_sha=vamigats_sha,
            regression_sha=regression_sha,
            build_profile=build_profile,
            date_str=date_str,
            synccpu_fails=sum(1 for r in all_results_flat
                              if r.get("class") == "FAIL" and calls_synccpu(test_dir_of(r))),
        )
        (results_path / "TestReport.md").write_text(report_content, encoding="utf-8")

        # 8. Generate README.md
        readme_content = generate_readme_markdown()
        (results_path / "README.md").write_text(readme_content, encoding="utf-8")
        shutil.copyfile(REPO_ROOT / "docs" / "REFERENCES.md", results_path / "REFERENCES.md")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("run_dir", type=Path, help="Directory containing run results")
    parser.add_argument("results_repo", type=Path, help="Results repository working tree")
    parser.add_argument(
        "--tag",
        default="manual-2024-08",
        help="Git tag in results repo containing historical comparison (default: manual-2024-08)",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=8,
        help="Number of parallel render workers (default: 8)",
    )
    parser.add_argument(
        "--with-old",
        action="store_true",
        help="Add the tag's old screenshots to the images (6 panels instead of 3)",
    )
    args = parser.parse_args()

    publish(
        run_dir=args.run_dir,
        results_repo=args.results_repo,
        tag=args.tag,
        jobs=args.jobs,
        with_old=args.with_old,
    )


if __name__ == "__main__":
    main()
