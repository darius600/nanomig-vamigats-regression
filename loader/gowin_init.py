#!/usr/bin/env python3
"""EXPERIMENTAL: the init list for --init fpga (loader/init_list.py) from a Gowin netlist.

    python3 -m loader.gowin_init <nanomig.vg> <gowin-dir> <nanomig-dir> [--out <list.tsv>]
        [--tree <Vnanomig_tb.tree.json>] [--note <text>] [--no-source-check]

<nanomig.vg> is the post-synthesis netlist of the Tang Nano 20k project made from exactly the
checkout <nanomig-dir>; GowinSynthesis writes it to impl/gwsynthesis/nanomig.vg. The Education
edition encrypts it, the standard edition does not. <gowin-dir> is the Gowin installation: its
simulation library (IDE/simlib/gw2a/prim_sim.v) holds the primitives' INIT defaults.

A flip-flop below the core is listed when it starts at 1: its INIT defparam says so, or the
default of its primitive does (the set/preset types DFFS, DFFSE, DFFP, DFFPE, ...).
Its register comes from the cell name, which GowinSynthesis derives from the RTL register:
<register>_s<n> or <register>_<bit>_s<n>. The Q net does not name it reliably. The net of a
register that drives an output port is named after the parent's net: busControl's rRWn drives
chip_rw, and ide's io_readdata[15] drives ide0_data[7]. The net can also be a wire assigned
from the register: amiga_clk's clk7_en_reg drives clk7_en.

Every register is resolved against the design as the harness elaborates it, with the build's
own lookup (harness/build.sh with HARNESS_TREE=1, run here unless --tree is given). That
settles whether <name>_<n> is bit n of <name> or a register of its own, and it finds registers
declared in a procedural block, such as nanomig's reset_s. A flip-flop that does not resolve
becomes a comment line with the reason, e.g. one in an instance the harness does not build
(uart_port needs ENABLE_AT_MODEM).

The list records the checkout's commit and the Gowin version from the netlist. The source
files the netlist names are compared with the checkout first, where they can be read.
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

from loader import init_list

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_MODULE = "nanomig"   # the core's module in the netlist (init_list.ROOT_CELL in the harness)

CELL = re.compile(r"^  (\S+) (\\\S+ |\S+) \($")          # "  TYPE name (" ; name may be an escaped identifier
PORT_Q = re.compile(r"^\s*\.Q\((\\\S+ |[^)]+)\)")
DEFPARAM = re.compile(r"^\s*defparam (\\\S+ |\S+?)\.INIT=1'b([01]);")
SOURCE = re.compile(r'^//file\d+ "(.*)"\s*$')
TOOL = re.compile(r"^//Tool Version: V?(\S+)")
PART = re.compile(r"^//Part Number: (\S+)")


@dataclass
class Module:
    ffs: List[Tuple[str, str, str]] = field(default_factory=list)   # (primitive, cell, Q net)
    inits: Dict[str, int] = field(default_factory=dict)             # cell: INIT defparam
    subs: List[Tuple[str, str]] = field(default_factory=list)       # (module, instance)


@dataclass
class Netlist:
    path: Path
    sha256: str
    tool: Optional[str]
    part: Optional[str]
    sources: List[str]
    modules: Dict[str, Module]


def unescape(name: str) -> str:
    name = name.strip()
    return name[1:] if name.startswith("\\") else name


def parse_netlist(path: Path | str) -> Netlist:
    path = Path(path)
    data = path.read_bytes()
    net = Netlist(path, hashlib.sha256(data).hexdigest(), None, None, [], {})
    cur, cell, ctype = None, None, None
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("//"):
            for rx, attr in ((TOOL, "tool"), (PART, "part")):
                m = rx.match(line)
                if m and getattr(net, attr) is None:
                    setattr(net, attr, m.group(1))
            m = SOURCE.match(line)
            if m:
                net.sources.append(m.group(1).replace("\\/", "/"))
            continue
        if line.startswith("module "):
            cur = net.modules.setdefault(line.split()[1], Module())
            continue
        if cur is None:
            continue
        if line.startswith("endmodule"):
            cur = None
            continue
        m = CELL.match(line)
        if m:
            ctype, cell = m.group(1), unescape(m.group(2))
            if not ctype.startswith("DFF"):
                cur.subs.append((ctype, cell))
            continue
        if ctype and ctype.startswith("DFF"):
            m = PORT_Q.match(line)
            if m:
                cur.ffs.append((ctype, cell, unescape(m.group(1))))
                ctype = None
                continue
        m = DEFPARAM.match(line)
        if m:
            cur.inits[unescape(m.group(1))] = int(m.group(2))
    if not net.modules:
        raise ValueError(f"{path}: no readable module: encrypted (Gowin Education edition)?")
    return net


def primitive_inits(gowin_dir: Path | str) -> Dict[str, int]:
    """INIT default of every DFF* primitive in the GW2A simulation library."""
    lib = Path(gowin_dir) / "IDE" / "simlib" / "gw2a" / "prim_sim.v"
    inits, mod = {}, None
    for line in lib.read_text(errors="replace").splitlines():
        m = re.match(r"\s*module\s+(DFF\w*)\b", line)
        if m:
            mod = m.group(1)
            continue
        if mod:
            m = re.search(r"parameter\s+INIT\s*=\s*1'b([01])", line)
            if m:
                inits[mod] = int(m.group(1))
                mod = None
    return inits


def ones(net: Netlist, prim: Dict[str, int], root: str = ROOT_MODULE) -> Tuple[List[Tuple[str, str, str]], int, Counter]:
    """The flip-flops below <root> that start at 1, as (instance path, cell, Q net); the number
    of flip-flops; and the primitives without an INIT default (their flip-flops are left out)."""
    found, missing, total = [], Counter(), 0

    def walk(mtype: str, path: str) -> None:
        nonlocal total
        mod = net.modules[mtype]
        for ctype, cell, q in mod.ffs:
            total += 1
            if cell in mod.inits:
                v = mod.inits[cell]
            elif ctype in prim:
                v = prim[ctype]
            else:
                missing[ctype] += 1
                continue
            if v:
                found.append((path, cell, q))
        for sub, inst in mod.subs:
            if sub in net.modules:
                walk(sub, init_list._join(path, inst))

    if root not in net.modules:
        raise ValueError(f"module {root} not in the netlist")
    walk(root, "")
    return found, total, missing


def register_of(hier, inst: str, cell: str) -> Tuple[Optional[Tuple[str, Optional[int]]], str]:
    """(register, bit or None) that flip-flop <cell> of instance <inst> holds, or None and why not."""
    if inst not in hier:
        return None, "instance not in the simulated design"
    base = re.sub(r"_s\d+$", "", cell)
    cands = []
    var, _ = init_list.lookup(hier, inst, base)
    if var and var.kind != "other" and not var.bit_problem(None):
        cands.append((base, None))
    m = re.match(r"^(.+)_(\d+)$", base)
    if m:
        var, _ = init_list.lookup(hier, inst, m.group(1))
        if var and var.kind != "other" and not var.bit_problem(int(m.group(2))):
            cands.append((m.group(1), int(m.group(2))))
    if len(cands) == 1:
        return cands[0], ""
    if cands:
        return None, "ambiguous: " + " or ".join(f"{r} {'-' if b is None else b}" for r, b in cands)
    tried = base + (f" or {m.group(1)} bit {m.group(2)}" if m else "")
    return None, f"no register {tried} in module {hier[inst][0]}"


def make_list(found: List[Tuple[str, str, str]], hier) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """The list rows (instance, register, bit or -), without repeats (GowinSynthesis may
    duplicate a register), and a comment line for every flip-flop that does not resolve."""
    rows, seen, skipped = [], set(), []
    for inst, cell, q in found:
        reg, why = register_of(hier, inst, cell)
        if reg is None:
            skipped.append(f"- {inst or '(core)'} {cell} (Q {q}): {why}")
            continue
        row = (inst, reg[0], "-" if reg[1] is None else str(reg[1]))
        if row not in seen:
            seen.add(row)
            rows.append(row)
    return rows, skipped


def source_check(net: Netlist, nanomig_dir: Path) -> Tuple[bool, str]:
    """Whether the source files the netlist names equal the checkout's (True when they cannot
    be read here, with a note saying so)."""
    files = [Path(s) for s in net.sources]
    if not files or not all(f.is_file() for f in files):
        return True, "not compared: the netlist's source files are not readable here"
    common = Path(os.path.commonpath([str(f) for f in files]))
    for root in (common, *common.parents):
        rel = [f.relative_to(root) for f in files]
        if all((nanomig_dir / r).is_file() for r in rel):
            break
    else:
        return False, f"the checkout lacks source files of the netlist (below {common})"
    differ = [str(r) for f, r in zip(files, rel) if f.read_bytes() != (nanomig_dir / r).read_bytes()]
    if differ:
        return False, f"{len(differ)} of {len(files)} source files differ from the checkout: {', '.join(differ[:5])}"
    return True, f"the netlist's {len(files)} source files equal the checkout's"


def build_tree(nanomig_dir: Path, work: Path) -> Path:
    env = dict(os.environ, HARNESS_TREE="1", NANOMIG_DIR=str(nanomig_dir))
    env.pop("HARNESS_INIT", None)
    proc = subprocess.run(["bash", str(REPO_ROOT / "harness" / "build.sh"), str(work)], env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"gowin_init: harness/build.sh HARNESS_TREE=1 failed:\n{proc.stdout}{proc.stderr}")
    return work / "tree" / "Vnanomig_tb.tree.json"


def git(nanomig_dir: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(nanomig_dir), *args], capture_output=True, text=True).stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("gowin_dir")
    ap.add_argument("nanomig_dir")
    ap.add_argument("--out", help="the list (default: stdout)")
    ap.add_argument("--tree", help="Vnanomig_tb.tree.json of this checkout (HARNESS_TREE=1 harness/build.sh)")
    ap.add_argument("--note", action="append", default=[], help="a comment line for the list")
    ap.add_argument("--no-source-check", action="store_true",
                    help="list even if the netlist's source files differ from the checkout")
    a = ap.parse_args()
    nanomig_dir = Path(a.nanomig_dir).resolve()

    from loader.run_test import get_git_revision
    sha = get_git_revision(nanomig_dir)
    if sha.endswith("-dirty") or not sha:
        sys.exit(f"gowin_init: {nanomig_dir} has uncommitted changes: a list names a commit, commit first")
    try:
        net = parse_netlist(a.netlist)
        found, total, missing = ones(net, primitive_inits(a.gowin_dir))
    except (OSError, ValueError) as e:
        sys.exit(f"gowin_init: {e}")
    same, sources = source_check(net, nanomig_dir)
    if not same and not a.no_source_check:
        sys.exit(f"gowin_init: {sources}; is the netlist from {nanomig_dir}? (--no-source-check lists anyway)")

    with tempfile.TemporaryDirectory(prefix="gowin_init-") as tmp:
        tree = Path(a.tree) if a.tree else build_tree(nanomig_dir, Path(tmp))
        hier = init_list.hierarchy(json.loads(tree.read_text()))
    rows, skipped = make_list(found, hier)

    branch = git(nanomig_dir, "rev-parse", "--abbrev-ref", "HEAD")
    version = (net.tool or "?").split("(")[0]
    head = [f"nanomig {sha}" + (f" ({branch})" if branch and branch != "HEAD" else ""),
            f"gowin {version} (GowinSynthesis {net.tool or '?'}, {net.part or '?'})",
            f"made by loader/gowin_init.py from {net.path.name} (sha256 {net.sha256[:12]}): "
            f"{total} flip-flops below {ROOT_MODULE}, {len(found)} start at 1",
            f"sources: {sources}",
            *(f"note {n}" for n in a.note),
            f"listed {len(rows)}; not listed {len(skipped)} (lines '# -' below)"]
    if missing:
        head.append(f"no INIT default for {dict(missing)}: their flip-flops are not in the list")
    out = "\n".join([f"# {h}" for h in head + skipped] + ["\t".join(r) for r in rows]) + "\n"
    if a.out:
        Path(a.out).write_text(out)
    else:
        sys.stdout.write(out)

    why = Counter(re.sub(r"^(no register|ambiguous).*", r"\1", s.split(": ", 1)[1]) for s in skipped)
    print(f"gowin_init: {len(found)} of {total} flip-flops start at 1, {len(rows)} listed; "
          f"not listed: {dict(why) or 'none'}", file=sys.stderr)
    if missing:
        print(f"gowin_init: no INIT default for {dict(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
