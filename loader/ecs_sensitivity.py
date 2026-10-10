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


def _parse_asm_int(val_str: str) -> Optional[int]:
    """Parses an assembly literal string into an integer."""
    s = val_str.strip().lstrip("#").strip()
    if not s:
        return None
    try:
        if s.startswith("$"):
            return int(s[1:], 16)
        if s.startswith("0x") or s.startswith("0X"):
            return int(s[2:], 16)
        if s.startswith("%"):
            return int(s[1:], 2)
        return int(s, 10)
    except ValueError:
        return None


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
