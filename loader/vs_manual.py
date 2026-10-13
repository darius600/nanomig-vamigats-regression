#!/usr/bin/env python3
"""Compare run results with the hand-made verdicts of nanomig-vamigats-results (tag manual-2024-08).

    python3 -m loader.vs_manual <results dir> <Agnus_Xxx.md> [--out FILE]

A test directory counts as identical when every variant in it is PASS and as
different when at least one is FAIL; any other class makes it "n/a". The hand
verdict comes from the "Result" column (Identical / Different).
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def manual_verdicts(md):
    out = {}
    for line in md.read_text(encoding="utf-8").splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) > 5 and cols[1].startswith("Agnus/"):
            res = cols[4]
            out[cols[1]] = ("identical" if "Identical" in res else
                            "different" if "Different" in res else res)
    return out


def automatic_verdicts(results):
    per_dir = defaultdict(list)
    for f in results.rglob("result.json"):
        r = json.loads(f.read_text())
        test_dir = str(Path(r["retrosh_path"]).parent)
        px = r.get("differing_pixels")
        per_dir[test_dir].append((r["variant"], r["class"], px))
    return per_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", type=Path)
    ap.add_argument("manual", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    manual = manual_verdicts(a.manual)
    auto = automatic_verdicts(a.results)
    rows, tally = [], defaultdict(int)
    for d in sorted(set(manual) | set(auto)):
        variants = sorted(auto.get(d, []))
        classes = {c for _, c, _ in variants}
        if not variants:
            mine = "not run"
        elif classes == {"PASS"}:
            mine = "identical"
        elif "FAIL" in classes:
            mine = "different"
        else:
            mine = "n/a"
        theirs = manual.get(d, "no verdict")
        agree = "yes" if mine == theirs else ("n/a" if mine in ("n/a", "not run") else "**no**")
        tally[(theirs, mine)] += 1
        detail = ", ".join(f"{v}: {c}" + (f" {px} px" if c == "FAIL" else "") for v, c, px in variants)
        rows.append(f"| `{d}` | {theirs} | {mine} | {agree} | {detail} |")

    lines = ["# Hand verdict vs automatic comparison", "",
             f"Results: `{a.results}` - hand verdicts: `{a.manual.name}`", "",
             "| hand | automatic | test dirs |", "|---|---|---:|"]
    lines += [f"| {t} | {m} | {n} |" for (t, m), n in sorted(tally.items())]
    lines += ["", "| test dir | hand | automatic | agree | variants |", "|---|---|---|---|---|"] + rows
    text = "\n".join(lines) + "\n"
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print("\n".join(lines[:6 + len(tally)]))


if __name__ == "__main__":
    main()
