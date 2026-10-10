#!/usr/bin/env python3
"""Parallel runner and report generator for NanoMig vAmigaTS regression suite."""

import argparse
import concurrent.futures
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.retrosh import parse_retrosh
from loader.run_test import BUILD_PROFILE, get_git_sha, run_test
from loader.report import write_reports

VAMIGATS_ROOT = REPO_ROOT / "external" / "vAmigaTS"


def collect_retrosh_files(subtrees: List[str]) -> List[Path]:
    """Collects every .retrosh file below the given subtrees."""
    collected: Set[Path] = set()

    for sub in subtrees:
        sub_p = Path(sub)
        resolved_path = None
        if sub_p.is_absolute() and sub_p.exists():
            resolved_path = sub_p
        elif (REPO_ROOT / sub).exists():
            resolved_path = (REPO_ROOT / sub).resolve()
        elif (VAMIGATS_ROOT / sub).exists():
            resolved_path = (VAMIGATS_ROOT / sub).resolve()
        else:
            resolved_path = (VAMIGATS_ROOT / sub).resolve()

        if not resolved_path.exists():
            print(f"Warning: subtree not found: {sub}", file=sys.stderr)
            continue

        if resolved_path.is_file() and resolved_path.suffix == ".retrosh":
            collected.add(resolved_path)
        elif resolved_path.is_dir():
            for p in resolved_path.rglob("*.retrosh"):
                collected.add(p)

    return sorted(collected)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel runner for vAmigaTS test subtrees under NanoMig."
    )
    parser.add_argument(
        "subtrees",
        nargs="+",
        help="Subtrees below external/vAmigaTS to run (e.g. Agnus/Copper/cross/cross1)",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=8,
        help="Number of parallel simulations (default: 8)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-running simulations even if matching result.json exists",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory for reports and results (default: runs)",
    )
    args = parser.parse_args()

    t_start = time.time()
    out_dir = Path(args.out).resolve() if args.out else (REPO_ROOT / "runs")
    out_dir.mkdir(parents=True, exist_ok=True)

    nanomig_sha = get_git_sha(REPO_ROOT / "external" / "NanoMig")
    vamigats_sha = get_git_sha(VAMIGATS_ROOT)

    retrosh_files = collect_retrosh_files(args.subtrees)
    if not retrosh_files:
        print("No .retrosh test files found in given subtrees.", file=sys.stderr)
        write_reports(
            out_dir=out_dir,
            results=[],
            subtrees=args.subtrees,
            nanomig_sha=nanomig_sha,
            vamigats_sha=vamigats_sha,
            build_profile=BUILD_PROFILE,
            wall_time=0.0,
        )
        return

    print(
        f"Found {len(retrosh_files)} variants across {len(args.subtrees)} subtree(s)."
    )
    print(f"NanoMig SHA: {nanomig_sha} | vAmigaTS SHA: {vamigats_sha}")
    print(f"Parallel jobs: {args.jobs} | Output dir: {out_dir}")

    results_map: Dict[str, Dict[str, Any]] = {}
    to_simulate: List[Tuple[Path, Path, Any]] = []

    # 1. Inspect all tests: handle resume & classify DEVIATING_CONFIG without simulation
    for rf in retrosh_files:
        try:
            rel_retrosh = rf.relative_to(VAMIGATS_ROOT)
        except ValueError:
            rel_retrosh = Path(rf.name)

        config = parse_retrosh(rf)
        variant_out = out_dir / rel_retrosh.parent / config.variant

        result_json_path = variant_out / "result.json"
        if not result_json_path.is_file() and (out_dir / config.variant / "result.json").is_file():
            result_json_path = out_dir / config.variant / "result.json"

        # Check resume condition
        can_resume = False
        if not args.force and result_json_path.is_file():
            try:
                cached = json.loads(result_json_path.read_text(encoding="utf-8"))
                if (
                    cached.get("nanomig_sha") == nanomig_sha
                    and cached.get("vamigats_sha") == vamigats_sha
                    and cached.get("class")
                    in ("PASS", "FAIL", "DEVIATING_CONFIG", "NOT_CALIBRATED", "ERROR")
                ):
                    can_resume = True
            except Exception:
                can_resume = False

        if can_resume:
            print(f"[RESUME] {config.variant}: skipping simulation (already completed: {cached.get('class')})")
            results_map[str(rf)] = cached
            continue

        # If deviating configuration: classify immediately without simulating
        if not config.runnable:
            res = run_test(
                rf,
                out_dir=variant_out,
                nanomig_sha=nanomig_sha,
                vamigats_sha=vamigats_sha,
            )
            print(f"[{res['class']}] {config.variant}: {res.get('detail', '')}")
            results_map[str(rf)] = res
            continue

        # Runnable simulation candidate
        to_simulate.append((rf, variant_out, config))

    # 2. Run simulations in parallel
    interrupted = False
    try:
        if to_simulate:
            print(f"Simulating {len(to_simulate)} variants with {args.jobs} worker(s)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
                future_to_info = {
                    executor.submit(
                        run_test,
                        rf,
                        var_out,
                        nanomig_sha=nanomig_sha,
                        vamigats_sha=vamigats_sha,
                    ): (rf, var_out, cfg)
                    for (rf, var_out, cfg) in to_simulate
                }

                for future in concurrent.futures.as_completed(future_to_info):
                    rf, var_out, cfg = future_to_info[future]
                    try:
                        res = future.result()
                        print(f"[{res['class']}] {cfg.variant}: {res.get('detail', '')}")
                        results_map[str(rf)] = res
                    except Exception as e:
                        print(f"[ERROR] {cfg.variant}: Worker error: {e}", file=sys.stderr)
                        err_res = {
                            "variant": cfg.variant,
                            "class": "ERROR",
                            "detail": f"Worker error: {e}",
                            "labels": cfg.labels,
                            "nanomig_sha": nanomig_sha,
                            "vamigats_sha": vamigats_sha,
                            "build_profile": BUILD_PROFILE,
                            "retrosh_path": str(rf.relative_to(VAMIGATS_ROOT).as_posix()),
                            "harness_args": None,
                            "harness_exit_code": None,
                            "start_time": datetime.now().astimezone().isoformat(),
                            "end_time": datetime.now().astimezone().isoformat(),
                            "wall_time": 0.0,
                            "differing_pixels": None,
                            "raw_status": None,
                        }
                        var_out.mkdir(parents=True, exist_ok=True)
                        (var_out / "result.json").write_text(json.dumps(err_res, indent=2))
                        results_map[str(rf)] = err_res
    except KeyboardInterrupt:
        print("\nInterrupted by user! Generating partial report...", file=sys.stderr)
        interrupted = True
    finally:
        wall_time = time.time() - t_start
        ordered_results = [results_map[str(rf)] for rf in retrosh_files if str(rf) in results_map]
        md_path, json_path = write_reports(
            out_dir=out_dir,
            results=ordered_results,
            subtrees=args.subtrees,
            nanomig_sha=nanomig_sha,
            vamigats_sha=vamigats_sha,
            build_profile=BUILD_PROFILE,
            wall_time=wall_time,
        )
        print(f"\nReport written to:\n  {md_path}\n  {json_path}")


if __name__ == "__main__":
    main()
