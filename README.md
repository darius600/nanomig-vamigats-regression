# nanomig-vamigats-regression

Runs the [vAmiga Test Suite](https://github.com/dirkwhoffmann/vAmigaTS) (vAmigaTS)
against [NanoMig](https://github.com/MiSTle-Dev/NanoMig) in a Verilator simulation
and compares every resulting frame with the vAmigaTS reference image.

**Results:** [nanomig-vamigats-results](https://github.com/darius600/nanomig-vamigats-results),
start at its [TestReport.md](https://github.com/darius600/nanomig-vamigats-results/blob/main/TestReport.md).

NanoMig is based on the [MiSTer Minimig-AGA](https://github.com/MiSTer-devel/Minimig-AGA_MiSTer)
code, so its chip set RTL (`src/minimig-aga`) is shared with other Minimig-derived cores; see
[Another Minimig-derived core](#another-minimig-derived-core).

## What it does

The `.raw` references are **screenshots made by vAmiga**; only the photos in the vAmigaTS test
folders show real hardware. A `PASS` means identical to vAmiga. [docs/REFERENCES.md](docs/REFERENCES.md)
explains the references, the compared machines and the known cases where a reference
contradicts the photo.

vAmigaTS tests itself the same way: a `.retrosh` script boots Kickstart 1.3,
inserts the test's ADF, waits, and dumps the visible frame into a `.raw` file;
`diff` against the stored reference decides pass or fail. This repository does
exactly that for NanoMig:

1. **NanoMig, unmodified and pinned.** `external/NanoMig` is a submodule at a
   fixed commit. Nothing in it is patched. The harness builds NanoMig's own
   simulation top (`sim/nanomig_tb.v`, `sim/sd_card.cpp`) out of tree with the
   Tang Nano 20k board profile (`src/tang/nano20k/top.sv`: fx68k, no cache).
   The 13 variants that use a joystick, the mouse or the keyboard need a second
   build: NanoMig's testbench ties these inputs to zero, so the input harness
   compiles a copy of `sim/nanomig_tb.v` whose input ports the harness drives
   (`harness/inputs_tb.py`); the NanoMig sources themselves are the same.
2. **The test as vAmiga runs it.** `loader/retrosh.py` reads the script
   (configuration, ADF, wait time, screenshot cutout) and maps it onto
   NanoMig's configuration inputs. The other inputs are fixed in
   `harness/harness.cpp`, as NanoMig's own `sim/nanomig_tb.cpp` sets them: two
   floppy drives with NanoMig's floppy turbo, which also gives disk DMA the
   refresh slots (`floppy_config = 0x5`; on the board, the menu's drive count
   and turbo switch), no fast RAM, no IDE, a 68000 without CPU turbo. Every
   result records `floppy_config`.
3. **The frame.** The harness records NanoMig's VGA output. The comparison
   crops it to vAmiga's dump area (716 × 285) and undoes the scan doubling.
4. **The comparison.** Pixels are compared at the Amiga's real colour depth,
   4 bits per channel, inside the script's cutout. Outside the cutout vAmiga
   writes a checkerboard, so those pixels are not compared. There is no
   tolerance and no threshold. Each result also records whether the `.raw`
   rebuilt from the capture is byte-identical to the vAmigaTS reference
   (`raw_status`), the criterion vAmigaTS applies to vAmiga itself; in every
   published PASS it is.

## Result classes

A **test** is a vAmigaTS test directory (one program, one ADF); a **variant**
is one of its `.retrosh` scripts, usually one per machine (`cross1_ocs`,
`cross1_ecs`). Every count here and in the results is a count of variants.

| class | meaning |
|---|---|
| `PASS` | identical to the reference inside the cutout |
| `FAIL` | differs; `result.json` holds the number of differing pixels |
| `DEVIATING_CONFIG` | the script asks for something this harness does not set NanoMig up for (for example a 68010, a drive beyond the two it connects, a preset CPU register value, or input when no input harness was given); not simulated |
| `NOT_CALIBRATED` | the frame's raster geometry has no verified crop yet (the ERSY test's 908 × 627); simulated, not compared |
| `NO_REFERENCE` | vAmigaTS ships no reference `.raw` for this variant; not simulated. 13 scripts at the pinned commit have none; one of them, the unnamed leftover `CPU/IPL/TAS/TAS1/.retrosh`, also asks for a 68010 and counts as `DEVIATING_CONFIG` |
| `ERROR` | the simulation itself failed or timed out |

Labels never change a class. `reference-contradicts-photo` marks the variants whose vAmigaTS
hardware photo shows something else than the reference (see docs/REFERENCES.md); it puts the
reference in doubt, it does not explain a FAIL. `keyboard-controller` marks a test of the
keyboard's own controller (handshake, resync), which NanoMig does not have: its key codes come
from the USB/HID path, where the harness feeds them. `denise-ecs-sensitive` is a label, too. vAmiga's `A500_ECS_1MB` is an
ECS Agnus with an OCS Denise, as in the A500 rev. 6A. NanoMig has one ECS
switch for both chips, so its ECS mode also enables the ECS-only Denise
features (KILLEHB, DIWHIGH, DENISEID, SuperHires, BRDRBLNK). Tests whose
sources touch one of them carry the label.

## Pinned versions

| | |
|---|---|
| NanoMig | `external/NanoMig` at `4315acb` (MiSTle-Dev/NanoMig `main`, 2026-10-16; `ENABLE_AT_MODEM` left undefined, see Limitations) |
| vAmigaTS | `external/vAmigaTS` at `e807e32` |
| Verilator | 5.052 |

## Requirements

- Linux, or Ubuntu in WSL2 on Windows. On Windows, use git inside WSL:
  vAmigaTS contains symlinks that Git for Windows does not check out.
- Verilator 5.052, `g++`, `make`. The published results were made with 5.052;
  other versions are untested and could give different frames. Distributions
  often ship an older one; [build it from source](https://verilator.org/guide/latest/install.html).
- Python 3 with `numpy` and `opencv` (`apt install python3-numpy python3-opencv`).
- **Kickstart 1.3** as `roms/kick13.rom`: the 512 KB image the published results
  were made with has SHA-1 `90933936cce43ca9bc6bf375662c076b27e3c458`
  (`sha1sum roms/kick13.rom`; results made since 20 October 2026 record it as
  `kick_sha1`). The harness accepts any image of 256 KB or more,
  so a different one does not stop it; every test would just fail. It is not
  part of this repository and must never be committed.

## Usage

**Sizes and durations.** The clone with both submodules takes about 6 GB
(vAmigaTS alone 4.3 GB). One variant simulates 9 seconds of Amiga time, about
100 times slower than real time: 12 to 16 minutes per variant. A group such as
`Agnus/Copper` (223 variants to simulate) takes about 7 hours with `--jobs 8`
on an 8-core machine, the whole suite about 66 hours. To try it, start with
one test, or a small group such as `Agnus/DIW` (62 variants, about 2 hours).

```sh
git clone --recursive https://github.com/darius600/nanomig-vamigats-regression.git
cd nanomig-vamigats-regression
mkdir -p roms && cp /path/to/kick13.rom roms/
harness/build.sh                                   # -> build/nanomig_harness
HARNESS_INPUTS=1 harness/build.sh                  # -> build-inputs/, for the input tests

# one test (its two variants, _ocs and _ecs, in parallel)
python3 -m loader.run_all Agnus/Copper/cross/cross1 --out runs/cross1

# a subtree, 8 simulations in parallel; resumes where it stopped
python3 -m loader.run_all Agnus/Copper --jobs 8 --out runs/copper

# images: NanoMig | reference | differences
python3 -m loader.side_by_side $(find runs/copper -name '*.frame.vga.gz') --out side-by-side/copper

# compare stored captures again after a change to the comparison (no simulation)
python3 -m loader.recompare runs/copper --class NOT_CALIBRATED

# selftests (every reference against itself, single changed pixels must be
# found, the script parser, the colour table); takes a few minutes
python3 -m unittest discover -s tests
```

`run_all` writes `report.md` and `report.json` into its output directory. Each
variant keeps its `result.json` and the capture as `.frame.vga.gz` (about
3 KB), so a comparison can be repeated without simulating again. `run_all`
takes a machine-wide lock (`/tmp/nanomig-vamigats-run_all.lock`), so a second
run waits instead of overloading the cores.

If a job queue that spreads simulations over several machines answers at
`http://127.0.0.1:8765` (nanomig-vamigats-farm, a separate project; or
`$VAMIGATS_FARM_URL`, `--queue URL`), `run_all` hands its simulations to it and
compares the returned captures here as usual; `--local` simulates here anyway,
and `--allow-offline` lets its workers take the run into offline batches.
Without such a server, the normal case, `run_all` simulates itself.

## Testing a change to NanoMig

Build a harness from your own NanoMig checkout, run a group against it and
compare the outcome with the published results:

```sh
NANOMIG_DIR=~/NanoMig harness/build.sh build-mine        # -> build-mine/nanomig_harness
NANOMIG_DIR=~/NanoMig HARNESS_INPUTS=1 harness/build.sh build-mine-inputs   # for the input tests
python3 -m loader.run_all Agnus/Copper --jobs 8 --out runs/mine-copper \
    --harness build-mine/nanomig_harness --nanomig-dir ~/NanoMig
git clone https://github.com/darius600/nanomig-vamigats-results.git ../nanomig-vamigats-results
python3 -m loader.vs_published runs/mine-copper ../nanomig-vamigats-results
```

`run_all` takes the input harness from the directory next to `--harness` with
`-inputs` appended (or from `--harness-inputs`). Without one, the 13 variants
with joystick, mouse or keyboard input are not simulated and are reported as
`DEVIATING_CONFIG`, not as failures.

For a first check, run only the test directories that exercise what you
changed (a directory has two to six variants and takes 15 to 30 minutes with
`--jobs 8`), then the whole group.

`vs_published` lists every variant that got **better** (a FAIL turned PASS, or
fewer differing pixels) or **worse** (a PASS turned FAIL, more differing
pixels, or an ERROR), and exits with status 1 if anything got worse. A FAIL
with as many differing pixels as before but a different capture is listed as
**moved**: the difference now sits elsewhere, so look at its image.

Every result records the NanoMig commit it ran against, and `run_all` resumes
by that commit. **Commit your change before the run:** with uncommitted
changes the checkout is recorded as `<sha>-dirty` and nothing is resumed, so
an interrupted run starts over. After the next change, commit again; a new
commit means a new run, also in the same `--out`.

`python3 -m loader.publish <run> ../nanomig-vamigats-results` writes a run into
a working copy of the results repository (no commit, no push); it is how the
published results are made. Some variants differ from their script's name only
in case (`vblank1_ecs.retrosh` saves `vblank1_ECS`); on a case-insensitive file
system (Windows, macOS) stage with `git rm -r -q --cached . && git add -A`, so
such renames reach git.

## Adding a test

The tests are vAmigaTS's. A new or changed test needs, in a vAmigaTS
checkout, a folder with the program's ADF (vAmigaTS ships its ADFs prebuilt
next to the `.s` sources), a `.retrosh` script like its neighbours' and the
reference `.raw`, which vAmiga writes when it runs the script (vAmigaTS's
`Scripts/Makefile3`, rule `%.raw`, with the vAmiga executable in `$VAMIGA` and
`kick13.rom` in `/tmp`). Run it here by path:

```sh
python3 -m loader.run_all /path/to/vAmigaTS/Agnus/Copper/mytest --out runs/mytest
```

A variant without a `.raw` is reported as `NO_REFERENCE`.

## Another Minimig-derived core

NanoMig's own simulation top is what makes it work unchanged. Pointing the
suite at another core (MiSTer's Minimig-AGA, for example) means replacing
these places:

| file | what is NanoMig-specific |
|---|---|
| `harness/build.sh` | the list of NanoMig source files and the board defines |
| `harness/harness.cpp` | the testbench top `nanomig_tb` it drives: clock sequencing, SDRAM model, ADF via the SD-card model, configuration inputs, the VGA outputs it captures |
| `harness/taps.vlt` | the internal signals it reads (`nanomig.red/green/blue`, the beam counter) |
| `harness/inputs_tb.py` | how the testbench copy for the input tests routes joystick, mouse and keyboard to the harness |
| `loader/capture.py` | `CROP_TABLE`: where vAmiga's dump area lies in the captured frame; must be measured again for another video path |
| `loader/retrosh.py` | how each vAmigaTS machine maps onto the core's `chipset_config` and `memory_config` bits |

The comparison itself (`loader/compare.py`, `loader/mask.py`) depends only on
vAmigaTS.

## Layout

| path | |
|---|---|
| `harness/` | `harness.cpp`, `build.sh`, `taps.vlt`, `inputs_tb.py` |
| `loader/` | script parser, capture, mask, comparison, `run_test`, `run_all`, report, `side_by_side`, `recompare`, `publish`, `vs_published`, `vs_manual` |
| `tests/` | selftests |
| `external/` | the two pinned submodules |
| `roms/`, `build/`, `build-*/`, `runs/`, `side-by-side/` | local only, ignored by git |

## Limitations

- Only what the Tang Nano 20k build of NanoMig contains is tested: the fx68k
  68000 and the OCS/ECS chip set. The board profile leaves `ENABLE_TG68K`
  (68020) and `ENABLE_AGA` undefined (`src/tang/nano20k/top.sv`), and the
  vAmigaTS machines compared here are A500 models; AGA is not covered.
- NTSC frames are compared on the 237 rows an NTSC beam reaches. The crop of
  the ECS NTSC raster (908 × 524) is the unique best fit in all five NTSC
  `_ecs` tests; the one of the OCS NTSC raster (908 × 526) follows the PAL long
  frame, unconfirmed, because every NTSC `_ocs` reference at the pinned commit
  is black. The 908 × 627 raster of `Agnus/Registers/VPOS/ersy2` has no
  verified crop and is reported as `NOT_CALIBRATED`.
- Input follows vAmiga's commands: `joystick <n> pull` holds the direction,
  `mouse 1 press` and `keyboard press` release after 0.5 s. They reach the
  core where NanoMig's USB/HID module would put them (`hid.v` outputs), not
  through the FPGA companion's protocol.
- A `FAIL` says that NanoMig's frame differs from the reference. It does not
  say why. The side-by-side images show where.
- `ENABLE_AT_MODEM`, which NanoMig's Tang Nano 20k profile sets, stays
  undefined: it routes Paula's UART through a 16-byte FIFO to the FPGA
  companion, which the simulation does not have, so a UART test sending more
  than 16 bytes would stall. NanoMig's own `sim/Makefile` leaves it undefined
  too.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). The two submodules keep their own
terms: vAmigaTS is under GPL-3.0, and NanoMig's Minimig chip set sources carry
GPL headers.
