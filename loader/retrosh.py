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
    machine: Optional[str] = None   # vAmiga's machine, e.g. A500_OCS_1MB
    # input events for the input harness: (simulated time in s, kind, value), see harness.cpp
    events: List[Tuple[float, str, int]] = field(default_factory=list)


# vAmiga's input commands as NanoMig's input bits. Joystick bits as on nanomig's
# joystick0/1 ports, [fire4,fire3,fire2,fire,up,down,left,right]. vAmiga's joystick 1 is
# Amiga port 1 (JOY0DAT), joystick 2 port 2 (JOY1DAT). In NanoMig they cross: nanomig.v
# feeds joystick0 into userio's _joy1 and joystick1 into _joy2, and userio (joy_swap = 0)
# reads port 1 from _joy2 and port 2 from _joy1. So port 1 is joystick1, port 2 joystick0.
JOY_PORT = {1: "joy1", 2: "joy0"}
JOY_X = {"left": 0x02, "right": 0x01}
JOY_Y = {"up": 0x08, "down": 0x04}
MOUSE_BUTTONS = {"left": 0x1, "right": 0x2, "middle": 0x4}   # hid.v order
# vAmiga's "press" commands release after half a second (Keyboard KEY_RELEASE delay 0.5,
# Mouse::pressAndRelease* duration SEC(0.5))
PRESS_S = 0.5


# The .raw references are vAmiga screenshots; the vAmigaTS hardware photos of these
# tests show something else (see docs/REFERENCES.md): a real A500 with ECS Agnus
# does not draw the clean image vAmiga drew. A FAIL here says nothing about real
# hardware. The label never changes PASS/FAIL.
REFERENCE_CONTRADICTS_PHOTO = (
    "Agnus/DIW/DIWV/diwv9/diwv9_ecs.retrosh",
    "Agnus/DIW/DIWV/diwv12/diwv12_ecs.retrosh",
    "Agnus/DIW/DIWV/diwv13/diwv13_ecs.retrosh",
    "Agnus/DIW/DIWV/diwv14/diwv14_ecs.retrosh",
)


# Tests of the keyboard's own controller (the handshake from the CPU, and the resync the
# keyboard starts when it is missing). NanoMig has no such controller: its key codes come
# from the USB/HID path, where the harness feeds them. The label never changes PASS/FAIL.
KEYBOARD_CONTROLLER = (
    "Mainboard/Ports/Keyboard/noack1/noack1.retrosh",
)


def parse_retrosh(path: Path | str) -> RunConfig:
    """Parses a .retrosh file into a RunConfig.

    Every command the script uses must map onto NanoMig's configuration inputs;
    anything else (a 68010, input devices, more drives, an unknown command) sets
    runnable=False with deviating_reason, and the variant is DEVIATING_CONFIG.
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
    elapsed = 0.0          # simulated seconds up to the current line (sum of the waits)
    events: List[Tuple[float, str, int]] = []
    joy = {1: [0, 0], 2: [0, 0]}   # per vAmiga port: x bits, y bits

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

        # Input: joystick <1|2> pull <dir>, mouse 1 press <button>, keyboard press <code>
        m = re.match(r"^joystick\s+([12])\s+pull\s+(left|right|up|down)$", line, re.IGNORECASE)
        if m:
            port, direction = int(m.group(1)), m.group(2).lower()
            if direction in JOY_X:
                joy[port][0] = JOY_X[direction]
            else:
                joy[port][1] = JOY_Y[direction]
            events.append((elapsed, JOY_PORT[port], joy[port][0] | joy[port][1]))
            continue
        m = re.match(r"^mouse\s+1\s+press\s+(left|right|middle)$", line, re.IGNORECASE)
        if m:
            events.append((elapsed, "mouse", MOUSE_BUTTONS[m.group(1).lower()]))
            events.append((elapsed + PRESS_S, "mouse", 0))
            continue
        m = re.match(r"^keyboard\s+press\s+(\S+)$", line, re.IGNORECASE)
        if m:
            code = int(m.group(1), 0) if not m.group(1).startswith("$") else int(m.group(1)[1:], 16)
            events.append((elapsed, "key", code & 0x7F))
            events.append((elapsed + PRESS_S, "key", (code & 0x7F) | 0x80))
            continue
        if re.match(r"^(joystick|mouse|keyboard)\b", line, re.IGNORECASE):
            runnable = False
            if not deviating_reason:
                deviating_reason = f"unsupported input: {line}"
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
                    # vAmiga: ECS Agnus 2 MB + ECS Denise, 512 KB chip + 512 KB slow
                    chipset = 0x08
                    memory = 0x04
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
                    # waits add up; input commands between them happen at the sum so far
                    elapsed += float(parts[1])
                    wait_s = elapsed
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

    if any(path.as_posix().endswith(p) for p in REFERENCE_CONTRADICTS_PHOTO):
        labels.append("reference-contradicts-photo")
    if any(path.as_posix().endswith(p) for p in KEYBOARD_CONTROLLER):
        labels.append("keyboard-controller")

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
        machine=model,
        events=events,
    )
