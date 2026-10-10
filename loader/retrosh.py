#!/usr/bin/env python3
"""Parser for vAmigaTS .retrosh test scripts."""

from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.ecs_sensitivity import is_ecs_sensitive


@dataclass
class RunConfig:
    variant: str
    adf: Optional[Path]
    wait_s: float
    chipset: int
    memory: int
    cutout: Optional[Tuple[int, int, int, int]]
    labels: List[str] = field(default_factory=list)
    runnable: bool = True
    deviating_reason: Optional[str] = None
    retrosh_path: Optional[Path] = None


def parse_retrosh(path: Path | str) -> RunConfig:
    """Parses a .retrosh file into a RunConfig.
    
    Unsupported features or deviating configs set runnable=False with
    deviating_reason populated.
    """
    path = Path(path).resolve()
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    variant = path.stem
    wait_s = 9.0
    chipset = 0x00
    memory = 0x04
    cutout = None
    labels: List[str] = []
    runnable = True
    deviating_reason = None
    model = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Check for explicitly deviating configuration commands first
        # cpu set revision 68010 (case-insensitive)
        if re.match(r"^cpu\s+set\s+revision\s+68010", line, re.IGNORECASE):
            runnable = False
            if not deviating_reason:
                deviating_reason = "cpu 68010"
            continue

        # joystick, mouse, keyboard
        if re.match(r"^(joystick|mouse|keyboard)\b", line, re.IGNORECASE):
            runnable = False
            if not deviating_reason:
                deviating_reason = "input"
            continue

        # df1-3 connect
        if re.match(r"^df[1-3]\s+connect\b", line, re.IGNORECASE):
            runnable = False
            if not deviating_reason:
                deviating_reason = "df1-3"
            continue

        # amiga reset
        if line.lower() == "amiga reset":
            runnable = False
            if not deviating_reason:
                deviating_reason = "amiga reset"
            continue

        # Supported commands:
        if line.startswith("regression setup"):
            parts = line.split()
            if len(parts) >= 3:
                model = parts[2]
                if model == "A500_OCS_1MB":
                    chipset = 0x00
                    memory = 0x04
                elif model == "A500_ECS_1MB":
                    chipset = 0x08
                    memory = 0x04
                elif model == "A500_PLUS_1MB":
                    chipset = 0x08
                    memory = 0x01
                else:
                    runnable = False
                    if not deviating_reason:
                        deviating_reason = f"unsupported model: {model}"
            continue

        if line.startswith("regression run"):
            # ADF is resolved from directory
            continue

        if line.startswith("wait"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    wait_s = float(parts[1])
                except ValueError:
                    runnable = False
                    if not deviating_reason:
                        deviating_reason = f"unsupported: {line}"
            continue

        if line.startswith("screenshot set cutout"):
            parts = line.split()
            if len(parts) >= 7:
                try:
                    cutout = (int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6]))
                except ValueError:
                    runnable = False
                    if not deviating_reason:
                        deviating_reason = f"unsupported: {line}"
            continue

        if line.startswith("screenshot save"):
            parts = line.split()
            if len(parts) >= 3:
                variant = parts[2]
            continue

        if line == "mem set SLOW_RAM 0":
            memory = 0x00
            continue

        if line == "amiga set VIDEO_FORMAT NTSC":
            chipset |= 0x02
            continue

        # Any unrecognized command -> DEVIATING_CONFIG "unsupported: <line>"
        runnable = False
        if not deviating_reason:
            deviating_reason = f"unsupported: {line}"

    # Resolve ADF in the test directory
    test_dir = path.parent
    adfs = list(test_dir.glob("*.adf"))
    adf_path = adfs[0] if adfs else None

    # ECS sensitivity scan for A500_ECS_1MB
    if model == "A500_ECS_1MB":
        if is_ecs_sensitive(test_dir):
            labels.append("denise-ecs-sensitive")

    return RunConfig(
        variant=variant,
        adf=adf_path,
        wait_s=wait_s,
        chipset=chipset,
        memory=memory,
        cutout=cutout,
        labels=labels,
        runnable=runnable,
        deviating_reason=deviating_reason,
        retrosh_path=path,
    )
