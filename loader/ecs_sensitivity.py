"""ECS sensitivity scanner for vAmigaTS test source files.

For A500_ECS_1MB scripts, scans the test's .s files and the files they include
(resolving relative paths) for:
  - DIWHIGH
  - BPLCON3
  - DENISEID
  - BPLCON2 writes whose literal value has bit 9 set (non-literal writes count too).
"""

from pathlib import Path
import re
from typing import Optional, Set


_TOKEN = re.compile(r"\s*(\$[0-9a-fA-F]+|0[xX][0-9a-fA-F]+|%[01]+|\d+|<<|>>|[|&+\-~()])")


def _parse_asm_int(val_str: str) -> Optional[int]:
    """Parses an assembly constant into an integer: a literal ($hex, 0x, %bin,
    decimal) or a constant expression of literals with | & + - ~ << >> and
    parentheses, e.g. (1<<12)|$240. Anything else (symbols, registers) -> None."""
    s = val_str.strip().lstrip("#").strip()
    if not s:
        return None
    parts, pos = [], 0
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            return None
        tok = m.group(1)
        if tok.startswith("$"):
            tok = str(int(tok[1:], 16))
        elif tok[:2] in ("0x", "0X"):
            tok = str(int(tok[2:], 16))
        elif tok.startswith("%"):
            tok = str(int(tok[1:], 2))
        parts.append(tok)
        pos = m.end()
    try:
        # only integer literals and operators reach eval, checked by _TOKEN above
        value = eval(" ".join(parts), {"__builtins__": {}}, {})
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _check_line(line: str) -> bool:
    """Checks whether a single line of assembly indicates Denise ECS sensitivity."""
    # Strip comments
    line = line.split(";")[0].strip()
    if not line or line.startswith("*"):
        return False

    # Skip register definition equates (e.g. 'BPLCON2 equ bplcon2')
    if re.search(r"\bequ\b", line, re.IGNORECASE):
        # Unless it defines DIWHIGH or DENISEID
        if re.search(r"\b(DIWHIGH|DENISEID)\b", line, re.IGNORECASE):
            return True
        return False

    # Check for direct mentions/writes of ECS-only registers
    if re.search(r"\b(DIWHIGH|BPLCON3|DENISEID)\b", line, re.IGNORECASE):
        return True

    # DENISEID read by address instead of by name (e.g. move.w $DFF07C,d0)
    if re.search(r"\$0*dff07c\b", line, re.IGNORECASE):
        return True

    # SuperHires through BPLCON0 bit 6. Literal values only: every test writes BPLCON0.
    if re.search(r"\bBPLCON0\b", line, re.IGNORECASE):
        dc_match = re.search(r"\bdc\.w\s+bplcon0\s*,\s*([^,\s]+)", line, re.IGNORECASE)
        move_match = re.search(r"\bmove(?:\.w)?\s+(#[^,]+)\s*,\s*bplcon0\b", line, re.IGNORECASE)
        for m in (dc_match, move_match):
            if m:
                parsed = _parse_asm_int(m.group(1).strip())
                if parsed is not None and (parsed >> 6) & 1:
                    return True

    # Check for writes to BPLCON2
    if re.search(r"\bBPLCON2\b", line, re.IGNORECASE):
        # Copper write: dc.w BPLCON2, <val>
        dc_match = re.search(r"\bdc\.[wb]\s+bplcon2\s*,\s*([^,\s]+)", line, re.IGNORECASE)
        if dc_match:
            val_token = dc_match.group(1)
            parsed = _parse_asm_int(val_token)
            if parsed is None:
                # Non-literal or unknown literal expression -> counts as write
                return True
            return bool((parsed >> 9) & 1)

        # CPU instruction: move(.w/.l) <src>, bplcon2(...)
        move_match = re.search(
            r"\bmove(?:\.[wb])?\s+([^,]+)\s*,\s*bplcon2(?:\([^\)]+\))?",
            line,
            re.IGNORECASE,
        )
        if move_match:
            src = move_match.group(1).strip()
            if src.startswith("#"):
                parsed = _parse_asm_int(src)
                if parsed is None:
                    return True
                return bool((parsed >> 9) & 1)
            else:
                # Non-literal write (e.g. move.w d0, bplcon2(a6)) counts
                return True

    return False


def _scan_file(file_path: Path, visited: Set[Path]) -> bool:
    """Recursively scans an assembly file and its includes."""
    real_path = file_path.resolve()
    if real_path in visited or not real_path.is_file():
        return False
    visited.add(real_path)

    try:
        content = real_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    lines = content.splitlines()
    for raw_line in lines:
        if _check_line(raw_line):
            return True

        # Check for include directives
        # e.g.: include "path", include 'path', include path
        clean = raw_line.split(";")[0].strip()
        inc_match = re.match(r'^\s*include\s+["\']?([^"\'\s]+)["\']?', clean, re.IGNORECASE)
        if inc_match:
            inc_rel = inc_match.group(1)
            inc_target = real_path.parent / inc_rel
            if _scan_file(inc_target, visited):
                return True

    return False


def is_ecs_sensitive(test_dir: Path | str) -> bool:
    """Returns True if any .s file in test_dir (or included files) triggers ECS sensitivity."""
    test_dir = Path(test_dir).resolve()
    s_files = list(test_dir.glob("*.s"))
    visited: Set[Path] = set()
    for s_file in s_files:
        if _scan_file(s_file, visited):
            return True
    return False
