#!/usr/bin/env python3
"""Parallel runner and report generator for NanoMig vAmigaTS regression suite."""

import argparse
import concurrent.futures
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader import farm_client, init_list
from loader.retrosh import parse_retrosh
from loader.run_test import BUILD_PROFILE, get_git_revision, get_git_sha, run_test, select_harness
from loader.report import write_reports

VAMIGATS_ROOT = REPO_ROOT / "external" / "vAmigaTS"

# One simulating run_all per machine: a second one waits instead of overloading the
# cores (and running into timeouts). /tmp is shared by everything in the WSL distro.
LOCK_PATH = Path("/tmp/nanomig-vamigats-run_all.lock")


def acquire_machine_lock(out_dir: Path):
    """Blocks until this process holds the machine lock; returns the open lock file.

    flock is released by the kernel when the process ends, so a killed run never
    leaves a stale lock behind."""
    f = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip() or "another run_all"
        print(f"Waiting for the machine lock, held by: {holder}", flush=True)
        fcntl.flock(f, fcntl.LOCK_EX)
        print(f"Machine lock acquired at {datetime.now().astimezone().isoformat()}", flush=True)
    f.seek(0)
    f.truncate()
    f.write(f"pid {os.getpid()}, out {out_dir}, since {datetime.now().astimezone().isoformat()}\n")
    f.flush()
    return f


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


def start_state(init_arg: str, nanomig_sha: str, harnesses: List[Path]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """(fields every result records, report entry) for --init; exits when the list, the
    checkout and the harnesses do not belong together.

    zero: Verilator's all-zero start, as always; a harness built with an init list is refused.
    fpga:<list>: EXPERIMENTAL, see loader/init_list.py; every harness must have been built
    with exactly this list, and the list must come from the NanoMig commit being run."""
    infos = {h: init_list.harness_info(h) for h in harnesses if h.is_file()}
    if init_arg == "zero":
        built = [h for h, i in infos.items() if i.get("init") != "zero"]
        if built:
            sys.exit(f"{built[0]} was built with an FPGA init list ({infos[built[0]].get('list')}): "
                     f"pass --init fpga:<that list>, or use a harness built without HARNESS_INIT")
        return None, {"init": "zero"}
    if not init_arg.startswith("fpga:"):
        sys.exit(f"--init {init_arg}: expected zero or fpga:<list>")
    try:
        lst = init_list.parse(init_arg[len("fpga:"):])
    except (OSError, ValueError) as e:
        sys.exit(f"--init: {e}")
    problem = init_list.nanomig_mismatch(lst, nanomig_sha)
    if problem:
        sys.exit(f"--init: {problem}")
    if not infos:
        sys.exit(f"--init: no harness found ({', '.join(str(h) for h in harnesses)})")
    for h, info in infos.items():
        if info.get("sha256") != lst.sha256:
            sys.exit(f"--init: {h} was not built with {lst.path.name}: "
                     f"HARNESS_INIT={lst.path} NANOMIG_DIR=<checkout> harness/build.sh <dir>")
    info = next(iter(infos.values()))
    fields = init_list.result_fields(lst, info)
    print(f"Start state: {init_list.FPGA_LABEL} from {lst.path.name} (sha256 {lst.sha256[:12]}, "
          f"NanoMig {lst.nanomig}, Gowin {lst.gowin or '?'}): {info.get('set')} of {len(lst.entries)} bits set")
    for inst, reg, bit, why in info.get("unresolved") or []:
        print(f"  not set: {inst or '(core)'} {reg} {bit}: {why}")
    return fields, {**fields, "init_list": {**fields["init_list"], "unresolved": info.get("unresolved") or []}}


INIT_MARKER = "init.json"   # in an --out of an EXPERIMENTAL fpga run; normal runs have none


def claim_out_dir(out_dir: Path, fields: Optional[Dict[str, Any]]) -> None:
    """Results from different start states never share an --out: exits if this one holds
    results of another, and marks it for an fpga run."""
    marker = out_dir / INIT_MARKER
    here = init_list.signature(json.loads(marker.read_text())) if marker.is_file() else ("zero", None)
    want = init_list.signature(fields or {})
    if here == ("zero", None) and want != here and next(out_dir.rglob("result.json"), None) is None:
        here = want   # an empty --out takes any start state
    if here != want:
        sys.exit(f"{out_dir} holds results simulated from start state {init_list.describe(here)}, this run "
                 f"starts from {init_list.describe(want)}: use another --out")
    if fields:
        marker.write_text(json.dumps(fields, indent=2))


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
    parser.add_argument(
        "--harness",
        default=None,
        help="Harness binary to run (default: build/nanomig_harness). For experiments "
             "with a NanoMig checkout other than external/NanoMig.",
    )
    parser.add_argument(
        "--harness-inputs",
        default=None,
        help="Input harness for the variants with joystick, mouse or keyboard input, built from the "
             "same checkout with HARNESS_INPUTS=1 (default: build-inputs/; with --harness, "
             "<its directory>-inputs if it exists). Without one these variants are not simulated.",
    )
    parser.add_argument(
        "--nanomig-dir",
        default=None,
        help="NanoMig checkout the harness was built from (default: external/NanoMig); "
             "its HEAD SHA is recorded in every result and used for resume. With "
             "uncommitted changes it is recorded as <sha>-dirty and never resumed.",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help=f"Do not wait for the machine lock ({LOCK_PATH}); only one run_all should simulate at a time.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Simulate here even when a nanomig-vamigats-farm server is running.",
    )
    parser.add_argument(
        "--queue",
        default=None,
        help=f"nanomig-vamigats-farm server to hand the simulations to (default: $VAMIGATS_FARM_URL or "
             f"{farm_client.DEFAULT_URL}); without an answering server run_all simulates here.",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=1,
        help="nanomig-vamigats-farm priority: 1 (default); 2 only gets workers when nothing of priority 1 waits.",
    )
    parser.add_argument(
        "--allow-offline",
        action="store_true",
        help="let nanomig-vamigats-farm workers take this run's jobs into offline batches; such jobs "
             "come back only when that machine is back (up to twice the hours of its batch).",
    )
    parser.add_argument(
        "--init",
        default="zero",
        metavar="zero|fpga:LIST",
        help="start state of the registers: zero (default, Verilator's) or, EXPERIMENTAL, fpga:<list> "
             "with the start values of the Gowin netlist of this NanoMig commit (see loader/init_list.py); "
             "needs a harness built with HARNESS_INIT=<list>. Such results are never mixed with others "
             "in one --out and never published.",
    )
    args = parser.parse_args()

    t_start = time.time()
    out_dir = Path(args.out).resolve() if args.out else (REPO_ROOT / "runs")
    out_dir.mkdir(parents=True, exist_ok=True)

    nanomig_dir = Path(args.nanomig_dir).resolve() if args.nanomig_dir else REPO_ROOT / "external" / "NanoMig"
    nanomig_sha = get_git_revision(nanomig_dir)
    vamigats_sha = get_git_sha(VAMIGATS_ROOT)
    regression_sha = get_git_revision(REPO_ROOT)
    nanomig_dirty = nanomig_sha.endswith("-dirty")
    if nanomig_dirty:
        print(f"Warning: {nanomig_dir} has uncommitted changes. Results are recorded as "
              f"{nanomig_sha} and every variant is simulated again; commit the change "
              f"to make a run resumable.", file=sys.stderr)

    harnesses = [select_harness(SimpleNamespace(events=events), args.harness, args.harness_inputs)
                 for events in ([], [(0.0, "key", 0)])]
    init_fields, init_report = start_state(args.init, nanomig_sha, [h for h in harnesses if h])
    claim_out_dir(out_dir, init_fields)

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
            init=init_report,
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
        if not args.force and not nanomig_dirty and result_json_path.is_file():
            try:
                cached = json.loads(result_json_path.read_text(encoding="utf-8"))
                if (
                    cached.get("nanomig_sha") == nanomig_sha
                    and cached.get("vamigats_sha") == vamigats_sha
                    and cached.get("class")
                    in ("PASS", "FAIL", "DEVIATING_CONFIG", "NOT_CALIBRATED", "NO_REFERENCE", "ERROR")
                    # a variant that could not be simulated before but can now (e.g. input
                    # tests) is simulated, and one that no longer can is classified again
                    and (cached.get("class") == "DEVIATING_CONFIG") == (not config.runnable)
                    and init_list.signature(cached) == init_list.signature(init_fields or {})
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
                regression_sha=regression_sha,
                init=init_fields,
            )
            print(f"[{res['class']}] {config.variant}: {res.get('detail', '')}")
            results_map[str(rf)] = res
            continue

        # An input variant with a harness of one's own but no input harness: classified, not simulated
        if select_harness(config, args.harness, args.harness_inputs) is None:
            res = run_test(
                rf,
                out_dir=variant_out,
                harness_path=args.harness,
                nanomig_sha=nanomig_sha,
                vamigats_sha=vamigats_sha,
                regression_sha=regression_sha,
                init=init_fields,
            )
            print(f"[{res['class']}] {config.variant}: {res.get('detail', '')}")
            results_map[str(rf)] = res
            continue

        # Runnable simulation candidate
        to_simulate.append((rf, variant_out, config))

    # 2. Simulate: through nanomig-vamigats-farm when its server answers, otherwise here in parallel -
    # one simulating run_all per machine
    queue = farm_client.connect(args.queue) if (to_simulate and not args.local) else None
    machine_lock = None
    interrupted = False
    try:
        if queue:
            print(f"Simulating {len(to_simulate)} variants through nanomig-vamigats-farm at {queue.url} "
                  f"(priority {args.priority}; its workers decide how many run at a time)...", flush=True)
            to_simulate = farm_client.simulate(
                queue,
                to_simulate,
                results_map,
                harness=args.harness,
                inputs_harness=args.harness_inputs,
                offline_ok=args.allow_offline,
                init=init_fields,
                kick_rom=REPO_ROOT / "roms" / "kick13.rom",
                shas={"nanomig": nanomig_sha, "vamigats": vamigats_sha, "regression": regression_sha},
                label=" ".join(args.subtrees),
                owner=str(out_dir),
                priority=args.priority,
                report=lambda res, cfg: print(f"[{res['class']}] {cfg.variant}: {res.get('detail', '')}",
                                              flush=True),
            )
            if to_simulate:
                print(f"Simulating the remaining {len(to_simulate)} variants here.", flush=True)
        if to_simulate and not args.no_lock:
            machine_lock = acquire_machine_lock(out_dir)
        if to_simulate:
            print(f"Simulating {len(to_simulate)} variants with {args.jobs} worker(s)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
                future_to_info = {
                    executor.submit(
                        run_test,
                        rf,
                        var_out,
                        harness_path=args.harness,
                        inputs_harness_path=args.harness_inputs,
                        nanomig_sha=nanomig_sha,
                        vamigats_sha=vamigats_sha,
                        regression_sha=regression_sha,
                        init=init_fields,
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
                            "regression_sha": regression_sha,
                            "build_profile": BUILD_PROFILE,
                            "retrosh_path": str(rf.relative_to(VAMIGATS_ROOT).as_posix()),
                            "harness_args": None,
                            "harness_exit_code": None,
                            "start_time": datetime.now().astimezone().isoformat(),
                            "end_time": datetime.now().astimezone().isoformat(),
                            "wall_time": 0.0,
                            "differing_pixels": None,
                            "raw_status": None,
                            **(init_fields or {}),
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
            init=init_report,
        )
        print(f"\nReport written to:\n  {md_path}\n  {json_path}")


if __name__ == "__main__":
    main()
