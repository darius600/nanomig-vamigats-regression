#!/usr/bin/env python3
"""Report aggregation and formatting for NanoMig vAmigaTS regression runs."""

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent


def get_vamigats_group(retrosh_rel_path: str) -> str:
    """Extracts the 2-level vAmigaTS group from a retrosh relative path.
    
    e.g. 'Agnus/Copper/cross/cross1/cross1_ocs.retrosh' -> 'Agnus/Copper'
         'CPU/68010/Loopmode/colorloop/colorloop_68010.retrosh' -> 'CPU/68010'
    """
    parts = Path(retrosh_rel_path).parts
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    elif len(parts) == 1:
        return parts[0]
    return "Unknown"


def to_repo_rel(path_val: Path | str | None) -> Optional[str]:
    """Converts a path to be relative to REPO_ROOT without any absolute prefixes."""
    if path_val is None:
        return None
    p = Path(path_val)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def build_report_data(
    results: List[Dict[str, Any]],
    subtrees: List[str],
    nanomig_sha: str,
    vamigats_sha: str,
    build_profile: str,
    wall_time: float,
    report_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregates test results into structured report data."""
    if report_date is None:
        report_date = datetime.now().astimezone().isoformat()

    classes = ["PASS", "FAIL", "DEVIATING_CONFIG", "NOT_CALIBRATED", "NO_REFERENCE", "ERROR"]

    # Normalize subtrees so they don't contain absolute paths
    clean_subtrees = []
    for s in subtrees:
        sub_str = str(s).replace("\\", "/")
        if "external/vAmigaTS/" in sub_str:
            sub_str = sub_str.split("external/vAmigaTS/")[-1]
        clean_subtrees.append(sub_str)

    summary = {c: 0 for c in classes}
    summary["total"] = len(results)

    ecs_sensitive = {"PASS": 0, "FAIL": 0}
    by_group: Dict[str, Dict[str, int]] = {}

    failures = []
    deviating_config = []
    not_calibrated = []

    for r in results:
        res_class = r.get("class", "ERROR")
        if res_class in summary:
            summary[res_class] += 1

        retrosh_path = r.get("retrosh_path", "")
        group = get_vamigats_group(retrosh_path)

        if group not in by_group:
            by_group[group] = {c: 0 for c in classes}
            by_group[group]["total"] = 0
        by_group[group]["total"] += 1
        if res_class in by_group[group]:
            by_group[group][res_class] += 1

        # Check denise-ecs-sensitive label
        labels = r.get("labels", [])
        if "denise-ecs-sensitive" in labels:
            if res_class in ecs_sensitive:
                ecs_sensitive[res_class] += 1

        variant = r.get("variant", "")
        if res_class == "FAIL":
            diff_img = to_repo_rel(r.get("diff_image"))
            failures.append({
                "group": group,
                "variant": variant,
                "differing_pixels": r.get("differing_pixels", 0),
                "diff_image": diff_img,
            })
        elif res_class == "DEVIATING_CONFIG":
            deviating_config.append({
                "group": group,
                "variant": variant,
                "reason": r.get("detail", "deviating configuration"),
            })
        elif res_class == "NOT_CALIBRATED":
            geom = r.get("geometry")
            if not geom:
                detail = r.get("detail", "")
                if "uncalibrated geometry:" in detail:
                    geom = detail.split("uncalibrated geometry:")[-1].strip()
                else:
                    geom = detail
            not_calibrated.append({
                "group": group,
                "variant": variant,
                "geometry": geom,
            })

    # Sort failures by group, then variant
    failures.sort(key=lambda x: (x["group"], x["variant"]))
    deviating_config.sort(key=lambda x: (x["group"], x["variant"]))
    not_calibrated.sort(key=lambda x: (x["group"], x["variant"]))

    # Sort groups alphabetically
    sorted_groups = {g: by_group[g] for g in sorted(by_group.keys())}

    return {
        "nanomig_sha": nanomig_sha,
        "vamigats_sha": vamigats_sha,
        "build_profile": build_profile,
        "date": report_date,
        "subtrees": clean_subtrees,
        "wall_time": round(wall_time, 3),
        "summary": summary,
        "denise_ecs_sensitive": ecs_sensitive,
        "by_group": sorted_groups,
        "failures": failures,
        "deviating_config": deviating_config,
        "not_calibrated": not_calibrated,
    }


def generate_report_markdown(data: Dict[str, Any]) -> str:
    """Generates markdown report content from structured report data."""
    subtrees_str = ", ".join(f"`{s}`" for s in data["subtrees"])
    summary = data["summary"]
    ecs = data["denise_ecs_sensitive"]

    lines = [
        "# NanoMig vAmigaTS Regression Report",
        "",
        f"- **Date:** {data['date']}",
        f"- **NanoMig SHA:** `{data['nanomig_sha']}`",
        f"- **vAmigaTS SHA:** `{data['vamigats_sha']}`",
        f"- **Build Profile:** `{data['build_profile']}`",
        f"- **Subtrees:** {subtrees_str}",
        f"- **Wall Time:** {data['wall_time']:.2f}s",
        "",
        "## Summary",
        "",
        "| Total | PASS | FAIL | DEVIATING_CONFIG | NOT_CALIBRATED | NO_REFERENCE | ERROR |",
        "|---|---|---|---|---|---|---|",
        f"| {summary['total']} | {summary['PASS']} | {summary['FAIL']} | {summary['DEVIATING_CONFIG']} | {summary['NOT_CALIBRATED']} | {summary['NO_REFERENCE']} | {summary['ERROR']} |",
        "",
        "### Denise-ECS Sensitivity",
        f"- **PASS:** {ecs['PASS']}",
        f"- **FAIL:** {ecs['FAIL']}",
        "",
        "## Results by Group",
        "",
        "| Group | Total | PASS | FAIL | DEVIATING_CONFIG | NOT_CALIBRATED | NO_REFERENCE | ERROR |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for grp, counts in data["by_group"].items():
        lines.append(
            f"| {grp} | {counts['total']} | {counts['PASS']} | {counts['FAIL']} | {counts['DEVIATING_CONFIG']} | {counts['NOT_CALIBRATED']} | {counts['NO_REFERENCE']} | {counts['ERROR']} |"
        )

    lines.append("")
    lines.append("## Failures")
    lines.append("")
    if not data["failures"]:
        lines.append("*None*")
    else:
        lines.append("| Group | Variant | Differing Pixels | Diff Image |")
        lines.append("|---|---|---|---|")
        for f in data["failures"]:
            diff_img = f["diff_image"] or "N/A"
            lines.append(f"| {f['group']} | {f['variant']} | {f['differing_pixels']} | `{diff_img}` |")

    lines.append("")
    lines.append("## Deviating Configurations")
    lines.append("")
    if not data["deviating_config"]:
        lines.append("*None*")
    else:
        lines.append("| Group | Variant | Reason |")
        lines.append("|---|---|---|")
        for d in data["deviating_config"]:
            lines.append(f"| {d['group']} | {d['variant']} | {d['reason']} |")

    lines.append("")
    lines.append("## Not Calibrated")
    lines.append("")
    if not data["not_calibrated"]:
        lines.append("*None*")
    else:
        lines.append("| Group | Variant | Geometry |")
        lines.append("|---|---|---|")
        for nc in data["not_calibrated"]:
            lines.append(f"| {nc['group']} | {nc['variant']} | {nc['geometry']} |")

    lines.append("")
    return "\n".join(lines)


def write_reports(
    out_dir: Path | str,
    results: List[Dict[str, Any]],
    subtrees: List[str],
    nanomig_sha: str,
    vamigats_sha: str,
    build_profile: str,
    wall_time: float,
    report_date: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Generates and writes report.md and report.json to out_dir."""
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    data = build_report_data(
        results=results,
        subtrees=subtrees,
        nanomig_sha=nanomig_sha,
        vamigats_sha=vamigats_sha,
        build_profile=build_profile,
        wall_time=wall_time,
        report_date=report_date,
    )

    md_content = generate_report_markdown(data)
    json_path = out_path / "report.json"
    md_path = out_path / "report.md"

    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    md_path.write_text(md_content, encoding="utf-8")

    return md_path, json_path
