#!/usr/bin/env python3
"""Compare stored captures again, without simulating.

    python3 -m loader.recompare runs/<name> [--class NOT_CALIBRATED ...]

For every result of the given classes (default NOT_CALIBRATED; a run's
result.json or the results repository's <variant>.result.json) whose
capture (<variant>.frame.vga.gz) is next to it, the capture is cropped and
compared again with the current comparison code, exactly as run_test does after
a simulation. Class, detail, differing pixels and raw_status are replaced; the
simulation fields (versions, harness arguments, times) stay. Use it when the
comparison changed and the simulation did not, e.g. after calibrating a new
raster geometry. Run report.md is not rewritten; run_all over the same --out
does that without simulating.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.retrosh import parse_retrosh
from loader.run_test import evaluate_capture, get_git_revision

VAMIGATS = REPO_ROOT / "external" / "vAmigaTS"


def recompare(run_dir: Path, classes: Iterable[str]) -> List[Dict[str, Any]]:
    """Recompares the matching results in run_dir; returns (old class, new result) pairs."""
    changed = []
    regression_sha = get_git_revision(REPO_ROOT)
    # a run keeps <variant>/result.json, the results repository <variant>.result.json
    files = sorted(set(run_dir.rglob("result.json")) | set(run_dir.rglob("*.result.json")))
    matching = 0
    for f in files:
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("class") not in classes:
            continue
        matching += 1
        capture = f.parent / f"{r['variant']}.frame.vga.gz"
        if not capture.is_file():
            print(f"{r['variant']}: no stored capture, left as {r['class']}")
            continue
        retrosh = VAMIGATS / r["retrosh_path"]
        old_class = r["class"]
        evaluate_capture(capture, retrosh, parse_retrosh(retrosh), f.parent, r)
        r["recompared"] = {"date": datetime.now().astimezone().isoformat(), "regression_sha": regression_sha}
        f.write_text(json.dumps(r, indent=2), encoding="utf-8")
        print(f"{r['variant']}: {old_class} -> {r['class']} ({r['detail']})")
        changed.append((old_class, r))
    if not matching:
        print(f"{len(files)} results in {run_dir}, none of class {', '.join(classes)}")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--class", dest="classes", action="append",
                    help="result class to recompare (repeatable; default NOT_CALIBRATED)")
    args = ap.parse_args()
    recompare(args.run_dir, args.classes or ["NOT_CALIBRATED"])


if __name__ == "__main__":
    main()
