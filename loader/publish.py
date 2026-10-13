#!/usr/bin/env python3
"""Publish a vAmigaTS regression run into the results repository."""

import argparse
import concurrent.futures
from datetime import datetime
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

from loader.report import get_vamigats_group
from loader.run_test import get_git_sha
from loader.side_by_side import render


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


def generate_group_markdown(
    group: str,
    results: List[Dict[str, Any]],
    manual_verdicts: Dict[str, Tuple[str, str]],
    nanomig_sha: str,
    vamigats_sha: str,
) -> str:
    """Generate markdown page for a group (e.g. Agnus_Blitter.md)."""
    group_file_name = group.replace("/", "_")
    classes = ["PASS", "FAIL", "DEVIATING_CONFIG", "NOT_CALIBRATED", "ERROR"]
    counts = {c: 0 for c in classes}
    for r in results:
        c = r.get("class", "ERROR")
        if c in counts:
            counts[c] += 1
        else:
            counts["ERROR"] += 1

    total = len(results)

    lines = [
        f"# {group_file_name}",
        "",
        f"- **NanoMig SHA:** [{nanomig_sha}](https://github.com/harbaum/NanoMig/commit/{nanomig_sha})",
        f"- **vAmigaTS SHA:** [{vamigats_sha}](https://github.com/dirkwhoffmann/vAmigaTS/commit/{vamigats_sha})",
        f"- **Total:** {total} | **PASS:** {counts['PASS']} | **FAIL:** {counts['FAIL']} | **DEVIATING_CONFIG:** {counts['DEVIATING_CONFIG']} | **NOT_CALIBRATED:** {counts['NOT_CALIBRATED']} | **ERROR:** {counts['ERROR']}",
        "",
        "| Test | Variant | Result | Differing px | Hand 2024 | Hand comment | Image |",
        "|---|---|---|---|---|---|---|",
    ]

    # Sort results by (test_dir, variant)
    def sort_key(r: Dict[str, Any]) -> Tuple[str, str]:
        retrosh = r.get("retrosh_path", "")
        test_dir = str(Path(retrosh).parent)
        variant = r.get("variant", "")
        return (test_dir, variant)

    sorted_results = sorted(results, key=sort_key)
    for r in sorted_results:
        retrosh = r.get("retrosh_path", "")
        test_dir = str(Path(retrosh).parent)
        variant = r.get("variant", "")
        res_class = r.get("class", "ERROR")

        if res_class == "PASS":
            diff_px = "0"
        elif res_class == "FAIL":
            diff_px = str(r.get("differing_pixels", 0))
        elif res_class == "NOT_CALIBRATED":
            geom = r.get("geometry")
            if not geom:
                detail = r.get("detail", "")
                if "uncalibrated geometry:" in detail:
                    geom = detail.split("uncalibrated geometry:")[-1].strip()
                else:
                    geom = detail
            diff_px = geom or "uncalibrated"
        else:
            diff_px = "—"

        hand_res, hand_comment = manual_verdicts.get(test_dir, ("", ""))

        if res_class == "NOT_CALIBRATED":
            image_cell = "—"
        else:
            # Relative link from results repo root to image
            rel_image_path = f"{test_dir}/{variant}.png"
            image_cell = f"[{variant}.png]({rel_image_path})"

        lines.append(
            f"| {test_dir} | {variant} | {res_class} | {diff_px} | {hand_res} | {hand_comment} | {image_cell} |"
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
) -> str:
    """Generate TestReport.md content."""
    classes = ["PASS", "FAIL", "DEVIATING_CONFIG", "NOT_CALIBRATED", "ERROR"]
    summary = {c: 0 for c in classes}
    by_group: Dict[str, Dict[str, int]] = {}

    for r in results:
        res_class = r.get("class", "ERROR")
        if res_class in summary:
            summary[res_class] += 1
        else:
            summary["ERROR"] += 1

        grp = get_vamigats_group(r.get("retrosh_path", ""))
        if grp not in by_group:
            by_group[grp] = {c: 0 for c in classes}
            by_group[grp]["total"] = 0
        by_group[grp]["total"] += 1
        if res_class in by_group[grp]:
            by_group[grp][res_class] += 1

    lines = [
        "# NanoMig vAmigaTS Test Report",
        "",
        f"- **NanoMig SHA:** [{nanomig_sha}](https://github.com/harbaum/NanoMig/commit/{nanomig_sha})",
        f"- **vAmigaTS SHA:** [{vamigats_sha}](https://github.com/dirkwhoffmann/vAmigaTS/commit/{vamigats_sha})",
        f"- **Regression repo commit SHA:** `{regression_sha}`",
        f"- **Build profile:** {build_profile}",
        f"- **Date:** {date_str}",
        "",
        "## Summary",
        "",
        "| Total | PASS | FAIL | DEVIATING_CONFIG | NOT_CALIBRATED | ERROR |",
        "|---|---|---|---|---|---|",
        f"| {len(results)} | {summary['PASS']} | {summary['FAIL']} | {summary['DEVIATING_CONFIG']} | {summary['NOT_CALIBRATED']} | {summary['ERROR']} |",
        "",
        "## Groups and Agreement with Hand Verdicts (2024)",
        "",
        "| Group | Variants | PASS | FAIL | NOT_CALIBRATED | Both identical | Both different | Hand ident / auto diff | Hand diff / auto ident | N/A |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    total_agr = {
        "both_identical": 0,
        "both_different": 0,
        "hand_ident_auto_diff": 0,
        "hand_diff_auto_ident": 0,
        "na": 0,
    }

    for grp in sorted(by_group.keys()):
        grp_counts = by_group[grp]
        grp_md = f"{grp.replace('/', '_')}.md"
        agr = group_agreements.get(grp, {
            "both_identical": 0,
            "both_different": 0,
            "hand_ident_auto_diff": 0,
            "hand_diff_auto_ident": 0,
            "na": 0,
        })
        for k in total_agr:
            total_agr[k] += agr[k]

        lines.append(
            f"| [{grp}]({grp_md}) | {grp_counts['total']} | {grp_counts['PASS']} | {grp_counts['FAIL']} | {grp_counts['NOT_CALIBRATED']} | {agr['both_identical']} | {agr['both_different']} | {agr['hand_ident_auto_diff']} | {agr['hand_diff_auto_ident']} | {agr['na']} |"
        )

    lines.append(
        f"| **Total** | **{len(results)}** | **{summary['PASS']}** | **{summary['FAIL']}** | **{summary['NOT_CALIBRATED']}** | **{total_agr['both_identical']}** | **{total_agr['both_different']}** | **{total_agr['hand_ident_auto_diff']}** | **{total_agr['hand_diff_auto_ident']}** | **{total_agr['na']}** |"
    )

    lines.extend([
        "",
        "## How to read",
        "",
        "- **PASS:** The rendered frame is identical to the vAmigaTS reference at the Amiga's 4 bits per channel inside the cutout, which here also means the rebuilt vAmiga `.raw` is byte-identical.",
        "- **FAIL:** Differences were detected inside the compared area (cutout).",
        "- **NOT_CALIBRATED:** The raster geometry has not been calibrated yet; no image is produced.",
        "- **Hand verdicts (2024):** The hand verdicts of August 2024 are kept in tag `manual-2024-08`, whose `_ecs` screenshots came from a simulation without an ECS mode.",
        "",
    ])

    return "\n".join(lines)


def generate_readme_markdown() -> str:
    """Generate updated README.md for the results repository."""
    lines = [
        "# nanomig-vamigats-results",
        "",
        "Results of test executions from the [vAmiga Test Suite](https://github.com/dirkwhoffmann/vAmigaTS), generated by the [NanoMig](https://github.com/harbaum/NanoMig) Verilator simulation.",
        "",
        "The images generated by the test executions validate the accuracy of the NanoMig simulation by comparing its output against reference images from the vAmiga Test Suite, which has been validated against real Amiga hardware.",
        "",
        "👉 **The status of test executions and detailed results can be found here: [TestReport.md](./TestReport.md)**",
        "",
        "## Contents",
        "",
        "This repository contains automated regression test results and side-by-side review images:",
        "- **Side-by-side comparisons (`.png`):** For each calibrated test, shows NanoMig capture, vAmigaTS reference, differing pixels highlighted in red (with cutout shaded in blue), and the earlier NanoMig screenshots from 2024 for visual comparison.",
        "- **Captures (`.frame.vga.gz`):** Gzipped raw VGA frame captures from the simulation harness.",
        "- **Result metadata (`.result.json`):** Full execution metadata, pixel differences, and timing for each test variant.",
        "- **Group Reports (`Agnus_*.md`):** Detailed breakdown of test variants per functional group.",
        "",
        "## Historical Hand Comparison (2024-08)",
        "",
        "The previous manual comparison from August 2024 is preserved in git tag `manual-2024-08`. Note that `_ecs` screenshots in that tag originated from a simulation run without an ECS mode.",
        "",
    ]
    return "\n".join(lines)


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
    for rf in result_files:
        data = json.loads(rf.read_text(encoding="utf-8"))
        results_data.append((rf, data))

    # Read run metadata from report.json if available
    report_json_path = run_path / "report.json"
    if report_json_path.exists():
        rep_info = json.loads(report_json_path.read_text(encoding="utf-8"))
        nanomig_sha = rep_info.get("nanomig_sha", results_data[0][1].get("nanomig_sha", "unknown"))
        vamigats_sha = rep_info.get("vamigats_sha", results_data[0][1].get("vamigats_sha", "unknown"))
        date_str = rep_info.get("date", datetime.now().astimezone().isoformat())
    else:
        nanomig_sha = results_data[0][1].get("nanomig_sha", "unknown")
        vamigats_sha = results_data[0][1].get("vamigats_sha", "unknown")
        date_str = results_data[0][1].get("start_time", datetime.now().astimezone().isoformat())

    build_profile = "Tang Nano 20k, fx68k"
    regression_sha = get_git_sha(REPO_ROOT)

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

            # Copy result.json as <variant>.result.json
            dest_json = dest_dir / f"{variant}.result.json"
            shutil.copy2(rf, dest_json)

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

            if capture_to_render and r.get("class") != "NOT_CALIBRATED":
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
        )
        (results_path / "TestReport.md").write_text(report_content, encoding="utf-8")

        # 8. Generate README.md
        readme_content = generate_readme_markdown()
        (results_path / "README.md").write_text(readme_content, encoding="utf-8")


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
