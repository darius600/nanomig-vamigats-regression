#!/bin/bash
# Builds the NanoMig regression harness against external/NanoMig.
# Tang Nano 20k profile: fx68k unpatched with --no-assert, defines INFER_DPRAM DISABLE_ROM_LOADER.
# HARNESS_INPUTS=1 builds the input harness (default dir build-inputs) for the tests that
# use joystick, mouse or keyboard: same sources, but a copy of sim/nanomig_tb.v whose
# input ports the harness drives (harness/inputs_tb.py).
set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
NANOMIG_DIR="${NANOMIG_DIR:-$REPO_ROOT/external/NanoMig}"

# Resolve target build directory: the argument, else build/ (build-inputs/) in the repository,
# wherever the script is called from
if [ -n "$1" ]; then
    BUILD_DIR=$(realpath "$1")
elif [ -n "$HARNESS_INPUTS" ]; then
    BUILD_DIR="$REPO_ROOT/build-inputs"
else
    BUILD_DIR="$REPO_ROOT/build"
fi

echo "Building harness..."
echo "  Source root:  $REPO_ROOT"
echo "  NanoMig:      $NANOMIG_DIR"
echo "  Build dir:    $BUILD_DIR"

mkdir -p "$BUILD_DIR"
BUILD_DIR=$(realpath "$BUILD_DIR")
cd "$BUILD_DIR"

# Copy fx68k microcode tables into build directory for simulation runtime
cp "$NANOMIG_DIR"/src/fx68k/*.mem "$BUILD_DIR/"

DEFINES="-DINFER_DPRAM -DDISABLE_ROM_LOADER"   # Tang Nano 20k profile: fx68k, no cache
M="$NANOMIG_DIR/src/minimig-aga"
MINIMIG="$NANOMIG_DIR/src/nanomig.v $M/amiga_clk.v $M/cpu_wrapper.v $M/cpu_cache_new.v $M/dpram.v $M/dpram_be_1024x16.v $M/minimig.v \
 $M/bitplane_ram.v $M/sprite_ram.v \
 $M/ciaa.v $M/ciab.v $M/cia_int.v $M/cia_timera.v $M/cia_timerb.v $M/cia_timerd.v \
 $M/paula.v $M/paula_uart.v $M/paula_audio_channel.v $M/paula_audio_mixer.v $M/paula_audio.v $M/paula_audio_volume.v $M/paula_floppy_fifo.v $M/paula_floppy.v $M/paula_intcontroller.v \
 $M/agnus.v $M/agnus_audiodma.v $M/agnus_blitter_adrgen.v $M/agnus_blitter_minterm.v $M/agnus_diskdma.v $M/agnus_beamcounter.v $M/agnus_blitter_barrelshifter.v $M/agnus_blitter.v $M/agnus_refresh.v $M/agnus_bitplanedma.v $M/agnus_blitter_fill.v $M/agnus_copper.v $M/agnus_spritedma.v \
 $M/denise.v $M/denise_bitplane_shifter.v $M/denise_collision.v $M/denise_colortable.v $M/denise_playfields.v $M/denise_sprites_shifter.v $M/denise_bitplanes.v $M/denise_hamgenerator.v $M/denise_spritepriority.v $M/denise_sprites.v $M/denise_colortable_ram_mf.v \
 $M/gary.v $M/gayle.v $M/ide.v $M/minimig_m68k_bridge.v $M/minimig_bankmapper.v $M/minimig_sram_bridge.v $M/minimig_syscontrol.v \
 $NANOMIG_DIR/src/minimig/Amber.v $M/userio.v"
FX="$NANOMIG_DIR/src/fx68k/fx68k.sv $NANOMIG_DIR/src/fx68k/fx68kAlu.sv $NANOMIG_DIR/src/fx68k/uaddrPla.sv"
MISC="$NANOMIG_DIR/src/misc/sd_card.v $NANOMIG_DIR/src/misc/sd_rw.v $NANOMIG_DIR/src/misc/sdcmd_ctrl.v $NANOMIG_DIR/src/misc/sdram.sv"
TB="$NANOMIG_DIR/sim/nanomig_tb.v"
CDEFINES=""
if [ -n "$HARNESS_INPUTS" ]; then
    TB="$BUILD_DIR/tb/nanomig_tb.v"
    python3 "$SCRIPT_DIR/inputs_tb.py" "$NANOMIG_DIR/sim/nanomig_tb.v" "$TB"
    CDEFINES="-DHARNESS_INPUTS"
    echo "  Testbench:    copy with harness-driven inputs ($TB)"
fi
HDL="$TB $NANOMIG_DIR/sim/sdr_chip_model.v $MINIMIG $FX $MISC $NANOMIG_DIR/sim/TG68KdotC_Kernel.v"

VLT="$SCRIPT_DIR/taps.vlt"

verilator -O3 $DEFINES -Wno-fatal -Wno-BLKANDNBLK -Wno-lint -Wno-style --no-timing --no-assert --threads 1 \
  -I"$NANOMIG_DIR"/sim -I"$NANOMIG_DIR"/src/fx68k \
  -top-module nanomig_tb -cc $HDL "$VLT" \
  --exe "$SCRIPT_DIR/harness.cpp" "$NANOMIG_DIR"/sim/sd_card.cpp \
  -o ../nanomig_harness -CFLAGS "-I$NANOMIG_DIR/sim $DEFINES $CDEFINES ${HARNESS_OPT:--O2}" \
  --Mdir "$BUILD_DIR/obj_dir" > "$BUILD_DIR/verilator.log" 2>&1 || { tail -30 "$BUILD_DIR/verilator.log"; exit 1; }

JOBS=$(nproc 2>/dev/null || echo 4)
make -s -j"$JOBS" -C "$BUILD_DIR/obj_dir" -f Vnanomig_tb.mk > "$BUILD_DIR/make.log" 2>&1 || { grep -m20 -E "error|Error" "$BUILD_DIR/make.log"; exit 1; }

echo "Build complete: $BUILD_DIR/nanomig_harness"
ls -la "$BUILD_DIR/nanomig_harness"
