#!/usr/bin/env python3
"""Writes a copy of NanoMig's sim/nanomig_tb.v whose joystick, mouse and keyboard
inputs come from the harness.

    python3 harness/inputs_tb.py <NanoMig>/sim/nanomig_tb.v <out>/nanomig_tb.v

NanoMig's testbench ties joystick0/1 to zero and leaves the mouse and keyboard
inputs of the core open, so no input can reach it. This copy routes them to new
top-level ports, which the harness drives (harness.cpp, HARNESS_INPUTS). Only the
testbench wiring differs; the NanoMig sources (src/) are used as they are. Every
replacement must match exactly once, so a changed testbench fails here instead of
being wired wrongly.
"""
import sys
from pathlib import Path

PORTS = """   input [7:0]	 joystick0_in,     // harness: [fire4,fire3,fire2,fire,up,down,left,right]
   input [7:0]	 joystick1_in,
   input [2:0]	 mouse_buttons_in, // harness: [middle,right,left]
   input	 kbd_mouse_level_in,     // harness: toggles once per keyboard byte
   input [1:0]	 kbd_mouse_type_in,
   input [7:0]	 kbd_mouse_data_in,
"""

REPLACEMENTS = [
    # new top-level ports, right after clk85
    ("   input	 clk85, // 85mhz, 3x clk, edge aligned (like the PLL on the real board)\n",
     "   input	 clk85, // 85mhz, 3x clk, edge aligned (like the PLL on the real board)\n" + PORTS),
    ("		 .joystick0(6'b000000),\n",
     "		 .joystick0(joystick0_in),\n"),
    ("		 .joystick1(6'b000000),\n",
     "		 .joystick1(joystick1_in),\n"
     "		 .mouse_buttons(mouse_buttons_in),\n"
     "		 .kbd_mouse_level(kbd_mouse_level_in),\n"
     "		 .kbd_mouse_type(kbd_mouse_type_in),\n"
     "		 .kbd_mouse_data(kbd_mouse_data_in),\n"),
]


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    text = src.read_text()
    for old, new in REPLACEMENTS:
        n = text.count(old)
        if n != 1:
            sys.exit(f"inputs_tb: expected exactly one match in {src}, found {n}:\n{old}")
        text = text.replace(old, new)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)


if __name__ == "__main__":
    main()
