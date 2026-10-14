# nanomig-vamigats-regression

Runs the [vAmiga Test Suite](https://github.com/dirkwhoffmann/vAmigaTS) (vAmigaTS)
against [NanoMig](https://github.com/harbaum/NanoMig) in a Verilator simulation
and compares every resulting frame with the vAmigaTS reference image.

**Results:** [nanomig-vamigats-results](https://github.com/darius600/nanomig-vamigats-results),
start at its [TestReport.md](https://github.com/darius600/nanomig-vamigats-results/blob/main/TestReport.md).

## What it does

vAmigaTS tests itself the same way: a `.retrosh` script boots Kickstart 1.3,
inserts the test's ADF, waits, and dumps the visible frame into a `.raw` file;
`diff` against the stored reference decides pass or fail. This repository does
exactly that for NanoMig:

1. **NanoMig, unmodified and pinned.** `external/NanoMig` is a submodule at a
   fixed commit. Nothing in it is patched. The harness builds NanoMig's own
   simulation top (`sim/nanomig_tb.v`, `sim/sd_card.cpp`) out of tree with the
   Tang Nano 20k board profile (`src/tang/nano20k/top.sv`: fx68k, no cache).
2. **The test as vAmiga runs it.** `loader/retrosh.py` reads the script
   (configuration, ADF, wait time, screenshot cutout) and maps it onto
   NanoMig's configuration inputs.
3. **The frame.** The harness records NanoMig's VGA output. The comparison
   crops it to vAmiga's dump area (716 × 285) and undoes the scan doubling.
4. **The comparison.** Pixels are compared at the Amiga's real colour depth,
   4 bits per channel, inside the script's cutout. Outside the cutout vAmiga
   writes a checkerboard, so those pixels are not compared. There is no
   tolerance and no threshold. A PASS also means that the `.raw` rebuilt from
   the capture is byte-identical to the vAmigaTS reference, the criterion
   vAmigaTS applies to vAmiga itself.

## Result classes

| class | meaning |
|---|---|
| `PASS` | identical to the reference inside the cutout |
| `FAIL` | differs; `result.json` holds the number of differing pixels |
| `DEVIATING_CONFIG` | the script asks for something NanoMig on a Tang Nano 20k cannot be configured to (a 68010, joystick/mouse/keyboard input); not simulated |
| `NOT_CALIBRATED` | the frame's raster geometry has no verified crop yet (NTSC, interlace); simulated, not compared |
| `NO_REFERENCE` | vAmigaTS ships no reference `.raw` for this variant (13 scripts at the pinned commit); not simulated |
| `ERROR` | the simulation itself failed or timed out |

`denise-ecs-sensitive` is a label, not a class. vAmiga's `A500_ECS_1MB` is an
ECS Agnus with an OCS Denise, as in the A500 rev. 6A. NanoMig has one ECS
switch for both chips, so its ECS mode also enables the ECS-only Denise
features (KILLEHB, DIWHIGH, DENISEID, SuperHires, BRDRBLNK). Tests whose
sources touch one of them carry the label.

## Pinned versions

| | |
|---|---|
| NanoMig | `external/NanoMig` at `0f3d2fd` (harbaum/NanoMig `main`, 2026-10-09) |
| vAmigaTS | `external/vAmigaTS` at `e807e32` |
| Verilator | 5.052 |

## Requirements

- Linux, or Ubuntu in WSL2 on Windows. On Windows, use git inside WSL:
  vAmigaTS contains symlinks that Git for Windows does not check out.
- Verilator 5.052, `g++`, `make`.
- Python 3 with `numpy` and `opencv` (`apt install python3-numpy python3-opencv`).
- **Kickstart 1.3** as `roms/kick13.rom`. It is not part of this repository and
  must never be committed.

## Usage

```sh
git clone --recursive https://github.com/darius600/nanomig-vamigats-regression.git
cd nanomig-vamigats-regression
cp /path/to/kick13.rom roms/
harness/build.sh                                   # -> build/nanomig_harness

# one test
python3 -m loader.run_test external/vAmigaTS/Agnus/Copper/cross/cross1/cross1_ocs.retrosh --out runs/cross1

# a subtree, 8 simulations in parallel; resumes where it stopped
python3 -m loader.run_all Agnus/Copper --jobs 8 --out runs/copper

# images: NanoMig | reference | differences
python3 -m loader.side_by_side $(find runs/copper -name '*.frame.vga.gz') --out side-by-side/copper

# write a run into a working copy of the results repository (no commit, no push)
python3 -m loader.publish runs/copper ../nanomig-vamigats-results

# selftests (compare every reference with itself, mutation, parser, colour table)
python3 -m unittest discover -s tests
```

One test with the usual 9 seconds of simulated time takes 12–15 minutes.
`run_all` writes `report.md` and `report.json` into its output directory. Each
variant keeps its `result.json` and the capture as `.frame.vga.gz` (about
3 KB), so a comparison can be repeated without simulating again.

`run_all --harness <binary> --nanomig-dir <checkout>` runs a harness built from
another NanoMig checkout (`NANOMIG_DIR=<checkout> harness/build.sh <dir>`) and
records that checkout's commit in every result. This is for experiments with
NanoMig changes; the submodule stays as it is.

## Layout

| path | |
|---|---|
| `harness/` | `harness.cpp`, `build.sh`, `taps.vlt` |
| `loader/` | script parser, capture, mask, comparison, `run_test`, `run_all`, report, `side_by_side`, `publish`, `vs_manual` |
| `tests/` | selftests |
| `external/` | the two pinned submodules |
| `roms/`, `build/`, `runs/`, `side-by-side/` | local only, ignored by git |

## Limitations

- NTSC (908 × 526 on OCS, 908 × 524 on ECS) and interlaced rasters have no
  verified crop yet and are reported as `NOT_CALIBRATED`.
- A `FAIL` says that NanoMig's frame differs from the reference. It does not
  say why. The side-by-side images show where.
