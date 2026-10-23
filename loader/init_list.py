#!/usr/bin/env python3
"""EXPERIMENTAL: start values from the FPGA for the harness (run_all --init fpga:<list>).

Verilator starts every register at 0. On the Tang Nano 20k, Gowin gives every flip-flop the
INIT value of its primitive: the set/preset types (DFFS, DFFSE, DFFP, DFFPE) start at 1, the
others at 0, and the synthesis decides which register becomes which type. A fix that only
holds from all zeros passes every simulation and can still fail on the board after a cold
start.

An init list names the flip-flops that start at 1 in the Gowin netlist of one NanoMig tree,
one per line: instance path below the core (empty for the core itself), register, bit (or -
for a one-bit register), separated by tabs. Lines starting with # are comments; two of them
are read:

    # nanomig <sha> ...      the NanoMig commit the netlist was made from (required)
    # gowin <version> ...    the Gowin version that made it

HARNESS_INIT=<list> harness/build.sh <dir> builds a harness of its own with the list compiled
in. It sets the listed bits to 1 before the first evaluation, so the reset then acts on them
as it does on the FPGA. Names it cannot resolve (synthesis names such as cck_Z or
htotal_reg_2) are reported, not guessed.

Build steps (called by harness/build.sh):
    python3 -m loader.init_list check <list> <nanomig-dir>
    python3 -m loader.init_list generate <list> <Vnanomig_tb.tree.json> <out-dir>
"""

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

FPGA_LABEL = "fpga (experimental)"
ROOT_SCOPE = "TOP.nanomig_tb.nanomig"   # the core in NanoMig's sim/nanomig_tb.v
ROOT_CELL = "nanomig"


@dataclass
class InitList:
    path: Path
    sha256: str
    nanomig: Optional[str]
    gowin: Optional[str]
    entries: List[Tuple[str, str, Optional[int]]] = field(default_factory=list)   # (instance, register, bit)


def parse(path: Path | str) -> InitList:
    """Reads an init list; ValueError names the first line that is not one."""
    path = Path(path)
    data = path.read_bytes()
    lst = InitList(path=path, sha256=hashlib.sha256(data).hexdigest(), nanomig=None, gowin=None)
    for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("#"):
            words = line[1:].split(None, 1)
            if words and words[0] == "nanomig" and len(words) > 1 and lst.nanomig is None:
                lst.nanomig = words[1].split()[0]
            elif words and words[0] == "gowin" and len(words) > 1 and lst.gowin is None:
                lst.gowin = words[1].strip()
            continue
        cols = line.split("\t")
        if len(cols) != 3 or not cols[1]:
            raise ValueError(f"{path}:{n}: expected <instance> TAB <register> TAB <bit or ->, got {line!r}")
        inst, reg, bit = cols[0].strip(), cols[1].strip(), cols[2].strip()
        if bit != "-" and not bit.isdigit():
            raise ValueError(f"{path}:{n}: bit must be a number or -, got {bit!r}")
        lst.entries.append((inst, reg, None if bit == "-" else int(bit)))
    if not lst.nanomig:
        raise ValueError(f"{path}: no '# nanomig <sha>' line: which NanoMig tree is this list for?")
    return lst


def nanomig_mismatch(lst: InitList, nanomig_sha: str) -> Optional[str]:
    """Why the list does not belong to this NanoMig checkout, or None if it does.

    nanomig_sha is the checkout's revision as results record it (<sha> or <sha>-dirty);
    the list's commit may be abbreviated (7 characters at least)."""
    want = (lst.nanomig or "").lower()
    have = nanomig_sha.lower().removesuffix("-dirty")
    if len(want) < 7 or not have.startswith(want):
        return (f"{lst.path.name} was made from NanoMig {lst.nanomig}, the checkout is at {nanomig_sha}: "
                f"every tree needs a list from its own netlist")
    return None


def signature(result: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """The start state a result was simulated from: ("zero", None) or ("fpga", list sha256).
    Results from before --init have no such field and started from zero."""
    if result.get("init", "zero") == "zero":
        return ("zero", None)
    return ("fpga", (result.get("init_list") or {}).get("sha256"))


def describe(sig: Tuple[str, Optional[str]]) -> str:
    return "zero" if sig[0] == "zero" else f"fpga, list {str(sig[1])[:12]}"


def harness_info(harness: Path) -> Dict[str, Any]:
    """What a harness binary says about its start state (--init-info). Harnesses from before
    --init do not know the option, print their usage and fail: they start from zero."""
    try:
        proc = subprocess.run([str(harness), "--init-info"], capture_output=True, text=True, timeout=120)
        info = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        info = None
    return info if isinstance(info, dict) and "init" in info else {"init": "zero"}


def result_fields(lst: InitList, info: Dict[str, Any]) -> Dict[str, Any]:
    """What every result of an fpga run records about its start state."""
    return {
        "init": FPGA_LABEL,
        "init_list": {
            "name": lst.path.name,
            "sha256": lst.sha256,
            "nanomig": lst.nanomig,
            "gowin": lst.gowin,
            "bits": len(lst.entries),
            "set": info.get("set"),
            "unresolved": len(info.get("unresolved") or []),
        },
    }


# -- build: harness/build.sh ---------------------------------------------------------------------

def _children(node: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for value in node.values():
        if isinstance(value, list):
            for x in value:
                if isinstance(x, dict):
                    yield x


def hierarchy(tree: Dict[str, Any]) -> Dict[str, Tuple[str, set]]:
    """{instance path below the core: (module name, its variable names)} from Verilator's
    --json-only tree; "" is the core itself."""
    by_addr = {m["addr"]: m for m in tree["modulesp"]}
    top = next(m for m in tree["modulesp"] if m.get("origName") == "nanomig_tb")

    def cells(mod: Dict[str, Any], prefix: str = "") -> Iterator[Tuple[str, Dict[str, Any]]]:
        for x in _children(mod):
            if x.get("type") == "CELL":
                yield prefix + x["name"], by_addr[x["modp"]]
            elif x.get("type") in ("BEGIN", "GENBLOCK", "GENFOR", "GENIF", "GENCASE"):
                yield from cells(x, prefix + (f"{x['name']}." if x.get("name") else ""))

    root = next(m for name, m in cells(top) if name == ROOT_CELL)
    out: Dict[str, Tuple[str, set]] = {}

    def walk(mod: Dict[str, Any], path: str) -> None:
        names = {x["name"] for x in _children(mod) if x.get("type") == "VAR"}
        out[path] = (mod.get("origName") or mod["name"], names)
        for name, sub in cells(mod):
            walk(sub, f"{path}.{name}" if path else name)

    walk(root, "")
    return out


def _c_str(s: str) -> str:
    return json.dumps(s)   # a valid C string literal for the plain names and versions here


def generate(lst: InitList, tree_json: Path, out_dir: Path) -> List[Tuple[str, str, Optional[int], str]]:
    """Writes init.vlt (the listed registers public) and init_list.h (the list for harness.cpp)
    into out_dir; returns the entries that cannot be resolved, with the reason."""
    hier = hierarchy(json.loads(Path(tree_json).read_text()))
    public, unresolved = set(), []
    for inst, reg, bit in lst.entries:
        if inst not in hier:
            unresolved.append((inst, reg, bit, "no such instance"))
        elif reg not in hier[inst][1]:
            unresolved.append((inst, reg, bit, f"no variable {reg} in module {hier[inst][0]}"))
        else:
            public.add((hier[inst][0], reg))
    out_dir.mkdir(parents=True, exist_ok=True)
    vlt = ["`verilator_config",
           f"// Generated by loader/init_list.py from {lst.path.name} (sha256 {lst.sha256}).",
           "// The registers the FPGA start values are written to, made writable from the harness."]
    vlt += [f'public_flat_rw -module "{m}" -var "{v}"' for m, v in sorted(public)]
    (out_dir / "init.vlt").write_text("\n".join(vlt) + "\n")
    rows = [f"  {{{_c_str(inst)}, {_c_str(reg)}, {-1 if bit is None else bit}}},"
            for inst, reg, bit in lst.entries]
    header = [
        f"// Generated by loader/init_list.py from {lst.path.name}. EXPERIMENTAL.",
        f"static const char *const INIT_LIST_NAME = {_c_str(lst.path.name)};",
        f"static const char *const INIT_LIST_SHA256 = {_c_str(lst.sha256)};",
        f"static const char *const INIT_LIST_NANOMIG = {_c_str(lst.nanomig or '')};",
        f"static const char *const INIT_LIST_GOWIN = {_c_str(lst.gowin or '')};",
        "struct InitBit { const char *path; const char *var; int bit; };   // bit -1: a one-bit register",
        "static const InitBit INIT_BITS[] = {",
        *rows,
        "};",
    ]
    (out_dir / "init_list.h").write_text("\n".join(header) + "\n")
    return unresolved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="stop unless the list was made from this NanoMig checkout")
    c.add_argument("list")
    c.add_argument("nanomig_dir")
    g = sub.add_parser("generate", help="write init.vlt and init_list.h for the harness build")
    g.add_argument("list")
    g.add_argument("tree_json")
    g.add_argument("out_dir")
    a = ap.parse_args()
    try:
        lst = parse(a.list)
    except (OSError, ValueError) as e:
        sys.exit(f"init list: {e}")
    if a.cmd == "check":
        from loader.run_test import get_git_revision
        problem = nanomig_mismatch(lst, get_git_revision(Path(a.nanomig_dir)))
        if problem:
            sys.exit(f"init list: {problem}")
        print(f"  Init list:    {lst.path.name} (NanoMig {lst.nanomig}, Gowin {lst.gowin or '?'}, "
              f"{len(lst.entries)} bits start at 1) - EXPERIMENTAL")
        return
    unresolved = generate(lst, Path(a.tree_json), Path(a.out_dir))
    print(f"  Init list:    {len(lst.entries) - len(unresolved)} of {len(lst.entries)} bits resolved")
    for inst, reg, bit, why in unresolved:
        print(f"    not resolvable: {inst or '(core)'} {reg} {'-' if bit is None else bit}: {why}")


if __name__ == "__main__":
    main()
