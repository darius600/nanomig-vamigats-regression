#!/usr/bin/env python3
"""Compare a local run with the published results, variant by variant.

    python3 -m loader.vs_published runs/<name> ../nanomig-vamigats-results

For every variant of the run that the results repository also has:

  better     FAIL -> PASS, or a FAIL with fewer differing pixels
  worse      PASS -> FAIL, a FAIL with more differing pixels, or a new ERROR
  moved      a FAIL with the same number of differing pixels whose capture
             differs from the published one: the difference sits elsewhere
  unchanged  same class and the same number of differing pixels (for a FAIL,
             also the same capture, when both captures are there)
  not compared  not PASS/FAIL on one of the two sides (DEVIATING_CONFIG,
             NOT_CALIBRATED, NO_REFERENCE)

The exit status is 1 when anything got worse, so the command can gate a change;
a moved FAIL is listed but does not count as worse: look at its image.
"""
import argparse
import gzip
import json
from collections import Counter
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

COMPARED = ("PASS", "FAIL")


def pixels(r: Dict[str, Any]) -> Optional[int]:
    """Differing pixels of a compared result (0 for PASS), None if not compared."""
    if r.get("class") == "PASS":
        return 0
    if r.get("class") == "FAIL":
        return int(r.get("differing_pixels") or 0)
    return None


def verdict(new: Dict[str, Any], old: Dict[str, Any]) -> str:
    """better / worse / unchanged / not compared, for one variant."""
    if new.get("class") == "ERROR":
        return "unchanged" if old.get("class") == "ERROR" else "worse"
    p_new, p_old = pixels(new), pixels(old)
    if p_new is None or p_old is None:
        return "not compared"
    if p_new < p_old:
        return "better"
    if p_new > p_old:
        return "worse"
    return "unchanged"


def published_result(results_repo: Path, r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The results repository keeps <test dir>/<variant>.result.json."""
    p = results_repo / Path(r.get("retrosh_path", "")).parent / f"{r.get('variant', '')}.result.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def capture_moved(result_file: Path, r: Dict[str, Any], results_repo: Path) -> bool:
    """Whether the run's capture differs from the published one (both must exist)."""
    name = f"{r.get('variant', '')}.frame.vga.gz"
    mine = result_file.parent / name
    published = results_repo / Path(r.get("retrosh_path", "")).parent / name
    if not (mine.is_file() and published.is_file()):
        return False
    return gzip.decompress(mine.read_bytes()) != gzip.decompress(published.read_bytes())


def describe(r: Dict[str, Any]) -> str:
    p = pixels(r)
    return r.get("class", "?") if p in (None, 0) else f"FAIL {p} px"


def compare_run(run_dir: Path, results_repo: Path) -> Tuple[Counter, List[Tuple[str, str, str, str, int]], Dict[str, set]]:
    counts: Counter = Counter()
    changed: List[Tuple[str, str, str, str, int]] = []
    shas: Dict[str, set] = {"nanomig run": set(), "nanomig published": set(),
                            "vamigats run": set(), "vamigats published": set()}
    for f in sorted(run_dir.rglob("result.json")):
        new = json.loads(f.read_text(encoding="utf-8"))
        old = published_result(results_repo, new)
        if old is None:
            counts["not published"] += 1
            continue
        shas["nanomig run"].add(new.get("nanomig_sha"))
        shas["nanomig published"].add(old.get("nanomig_sha"))
        shas["vamigats run"].add(new.get("vamigats_sha"))
        shas["vamigats published"].add(old.get("vamigats_sha"))
        v = verdict(new, old)
        if v == "unchanged" and new.get("class") == "FAIL" and capture_moved(f, new, results_repo):
            v = "moved"
        counts[v] += 1
        if v in ("better", "worse", "moved"):
            delta = (pixels(new) or 0) - (pixels(old) or 0)
            changed.append((v, str(Path(new["retrosh_path"]).parent), new["variant"],
                            f"{describe(old)} -> {describe(new)}", delta))
    return counts, changed, shas


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path, help="output directory of run_all or run_test")
    ap.add_argument("results_repo", type=Path, help="working copy of nanomig-vamigats-results")
    args = ap.parse_args()

    counts, changed, shas = compare_run(args.run_dir, args.results_repo)
    if shas["vamigats run"] != shas["vamigats published"]:
        print(f"Warning: different vAmigaTS commits (run {sorted(shas['vamigats run'])}, published "
              f"{sorted(shas['vamigats published'])}); the references may differ.", file=sys.stderr)
    print(f"NanoMig: run {', '.join(sorted(map(str, shas['nanomig run'])))} | "
          f"published {', '.join(sorted(map(str, shas['nanomig published'])))}")
    print(" | ".join(f"{k}: {counts[k]}" for k in ("better", "worse", "moved", "unchanged", "not compared", "not published")))
    for kind in ("worse", "moved", "better"):
        rows = sorted((c for c in changed if c[0] == kind), key=lambda c: (-abs(c[4]), c[1], c[2]))
        if rows:
            print(f"\n{kind}:")
            for _, test_dir, variant, change, delta in rows:
                print(f"  {test_dir}/{variant}: {change} ({delta:+d} px)")
    sys.exit(1 if counts["worse"] else 0)


if __name__ == "__main__":
    main()
