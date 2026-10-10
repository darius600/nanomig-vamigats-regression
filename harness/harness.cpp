// NanoMig regression harness: drives NanoMig's simulation top (nanomig_tb.v)
// without SDL or tracing, boots an ADF and dumps one native frame indexed by
// the Agnus beam counter and one VGA-captured frame.
//
// Usage:
//   nanomig_harness <kick.rom> <disk.adf> <wait_s> <out.frame> [ocs|ecs]
//
// Output files:
//   <out.frame>:     FRAME_H x FRAME_W x 4 bytes {r, g, b, flags}
//   <out.frame>.vga: VGA-captured frame (width, height header + RGB payload)

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <ctime>
#include <string>
#include <vector>

#include "Vnanomig_tb.h"
#include "Vnanomig_tb__Syms.h"
#include "verilated.h"
#include "verilated_syms.h"
#include "sd_card_config.h"

Vnanomig_tb *tb;
double simulation_time = 0;
extern const char *file_image[8];

char *sector_string(int, uint32_t) { static char s[1] = ""; return s; }

static unsigned short ram[8 * 512 * 1024];

static void *sig(const char *scope, const char *var) {
  const VerilatedScope *s = Verilated::threadContextp()->scopeFind(scope);
  if (!s) { fprintf(stderr, "scope not found: %s\n", scope); exit(2); }
  VerilatedVar *v = s->varFind(var);
  if (!v) { fprintf(stderr, "var not found: %s.%s\n", scope, var); exit(2); }
  return v->datap();
}

static void load_kick(const char *path) {
  FILE *fd = fopen(path, "rb");
  if (!fd) { perror(path); exit(2); }
  int len = fread(ram + (0x780000 / 2), 1024, 512, fd);
  if (len == 256) {
    fseek(fd, 0, SEEK_SET);
    if (fread(ram + (0x780000 / 2) + 128 * 1024, 1024, 256, fd) != 256) exit(2);
  } else if (len != 512) { fprintf(stderr, "kick read failed\n"); exit(2); }
  fclose(fd);
}

static const int FRAME_W = 2048, FRAME_H = 320;
static std::vector<uint8_t> frame(FRAME_W * FRAME_H * 4);

static uint8_t *p_red, *p_green, *p_blue, *p_hbl, *p_vbl;
static uint16_t *p_hpos, *p_vpos;

static double wait_s;
static const char *out_path;
static long frames = 0;

// VGA capture: scandoubled output captured the way the testbench does
// (lines split at rising hs_n, frames at rising vs_n, one sample per tick).
static const int VGA_W = 1024, VGA_H = 700;
static std::vector<uint8_t> vga(VGA_W * VGA_H * 3), last_vga(VGA_W * VGA_H * 3);
static int last_vga_w = 0, last_vga_h = 0;

static void capture_vga() {
  static int last_hs = -1, last_vs = -1, sx = 0, sy = 0, line_len = 0;
  if (sx < VGA_W && sy < VGA_H) {
    uint8_t *p = &vga[(sy * VGA_W + sx) * 3];
    p[0] = tb->red << 4; p[1] = tb->green << 4; p[2] = tb->blue << 4;
  }
  sx++;
  if (tb->hs_n != last_hs) {
    last_hs = tb->hs_n;
    if (tb->hs_n) { line_len = sx; sx = 0; sy++; }
  }
  if (tb->vs_n != last_vs) {
    last_vs = tb->vs_n;
    if (tb->vs_n) {
      last_vga.swap(vga);
      last_vga_w = line_len; last_vga_h = sy;
      std::fill(vga.begin(), vga.end(), 0);
      sy = 0;
    }
  }
}

static void dump_vga() {
  std::string p = std::string(out_path) + ".vga";
  FILE *f = fopen(p.c_str(), "wb");
  uint32_t hdr[2] = { (uint32_t)last_vga_w, (uint32_t)last_vga_h };
  fwrite(hdr, sizeof hdr, 1, f);
  for (int y = 0; y < last_vga_h; y++) fwrite(&last_vga[y * VGA_W * 3], 3, last_vga_w, f);
  fclose(f);
}

static void capture() {
  capture_vga();
  static int last_h = -1, last_v = -1, sub = 0;
  int h = *p_hpos, v = *p_vpos;
  if (h != last_h) { sub = 0; last_h = h; } else sub++;
  if (last_v >= 0 && v < last_v) {
    // Frame wrapped: the buffer holds the complete previous frame
    frames++;
    if (frames % 50 == 0)
      printf("%.3fms frame %ld (%d lines)\n", simulation_time * 1000, frames, last_v + 1);
    if (simulation_time >= wait_s) {
      FILE *f = fopen(out_path, "wb");
      fwrite(frame.data(), 1, frame.size(), f);
      fclose(f);
      dump_vga();
      printf("%.3fms dumped frame %ld (%d lines) to %s, vga %dx%d\n", simulation_time * 1000, frames, last_v + 1, out_path, last_vga_w, last_vga_h);
      exit(0);
    }
    std::fill(frame.begin(), frame.end(), 0);
  }
  last_v = v;
  int x = 4 * h + sub;
  if (x < FRAME_W && v < FRAME_H) {
    uint8_t *p = &frame[(v * FRAME_W + x) * 4];
    p[0] = *p_red; p[1] = *p_green; p[2] = *p_blue;
    p[3] = 0x80 | (*p_hbl ? 1 : 0) | (*p_vbl ? 2 : 0);
  }
}

// One 28 MHz half period, same sequencing as NanoMig's sim/nanomig_tb.cpp
static void tick(int c) {
  tb->clk = c;
  tb->eval();
  if (c) {
    if (tb->por && simulation_time > 0.0001) tb->por = 0;
    if (tb->reset && simulation_time > 0.005) tb->reset = 0;
    sd_handle();

    static unsigned char ram_delay = 0;
    if (tb->clk7n_en) ram_delay = 0; else ram_delay++;
    if (ram_delay == 1) {
      unsigned char *ram_b = (unsigned char *)(ram + tb->ram_address);
      if (!tb->_ram_oe) {
        tb->ramdata_in = 256 * ram_b[0] + ram_b[1];
        unsigned char *cb = (unsigned char *)(ram + (tb->ram_address & ~3u));
        tb->chip48 = ((uint64_t)((cb[2] << 8) | cb[3]) << 32) |
                     ((uint64_t)((cb[4] << 8) | cb[5]) << 16) |
                      (uint64_t)((cb[6] << 8) | cb[7]);
      }
      if (!tb->_ram_we) {
        if (!tb->_ram_bhe) ram_b[0] = tb->ram_data >> 8;
        if (!tb->_ram_ble) ram_b[1] = tb->ram_data & 0xff;
      }
    }
  }
  static int clk85 = 0;
  for (int k = 0; k < 3; k++) { clk85 = !clk85; tb->clk85 = clk85; tb->eval(); }
  if (c) capture();
  simulation_time += TICKLEN;
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  std::vector<std::string> pos;
  uint8_t chipset = 0x00;
  uint8_t memory = 0x04;

  for (int i = 1; i < argc; i++) {
    std::string arg = argv[i];
    if (arg == "--chipset" && i + 1 < argc) {
      chipset = (uint8_t)std::strtoul(argv[++i], nullptr, 0);
    } else if (arg.rfind("--chipset=", 0) == 0) {
      chipset = (uint8_t)std::strtoul(arg.c_str() + 10, nullptr, 0);
    } else if (arg == "--memory" && i + 1 < argc) {
      memory = (uint8_t)std::strtoul(argv[++i], nullptr, 0);
    } else if (arg.rfind("--memory=", 0) == 0) {
      memory = (uint8_t)std::strtoul(arg.c_str() + 9, nullptr, 0);
    } else {
      pos.push_back(arg);
    }
  }

  if (pos.size() < 4) {
    fprintf(stderr, "usage: %s <kick> <adf> <wait_s> <out.frame> [--chipset 0xNN] [--memory 0xNN]\n", argv[0]);
    return 2;
  }

  static std::string adf_file = pos[1];
  static std::string out_file = pos[3];
  load_kick(pos[0].c_str());
  file_image[0] = adf_file.c_str();
  wait_s = atof(pos[2].c_str());
  out_path = out_file.c_str();

  tb = new Vnanomig_tb;
  uint32_t *mem = (uint32_t *)sig("TOP.nanomig_tb.sdram_chip", "mem");
  unsigned char *b = (unsigned char *)ram;
  for (unsigned c = 0; c < 2097152; c++)
    mem[c] = ((unsigned)b[4*c] << 24) | ((unsigned)b[4*c+1] << 16) | ((unsigned)b[4*c+2] << 8) | b[4*c+3];

  p_red   = (uint8_t *)sig("TOP.nanomig_tb.nanomig", "red");
  p_green = (uint8_t *)sig("TOP.nanomig_tb.nanomig", "green");
  p_blue  = (uint8_t *)sig("TOP.nanomig_tb.nanomig", "blue");
  p_hbl   = (uint8_t *)sig("TOP.nanomig_tb.nanomig", "hbl");
  p_vbl   = (uint8_t *)sig("TOP.nanomig_tb.nanomig", "vbl");
  p_hpos  = (uint16_t *)sig("TOP.nanomig_tb.nanomig.minimig.AGNUS1.bc1", "hpos");
  p_vpos  = (uint16_t *)sig("TOP.nanomig_tb.nanomig.minimig.AGNUS1.bc1", "vpos");

  sd_init();
  tb->por = 1;
  tb->reset = 1;
  tb->memory_config = memory;
  tb->fastram_config = 0;
  tb->floppy_config = 0x5;
  tb->ide_config = 0;
  tb->chipset_config = chipset;
  tb->cpu_config = 0;                  // 68000
  tb->turbo_config = 0;                // plain A500 timing
  tb->clk85 = 0;

  struct timespec t0, t1;
  clock_gettime(CLOCK_MONOTONIC, &t0);
  double next_report = 0.1;
  while (true) {
    tick(1);
    tick(0);
    if (simulation_time >= next_report) {
      clock_gettime(CLOCK_MONOTONIC, &t1);
      double wall = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
      printf("sim %.1fs wall %.0fs (factor %.0f) pwr_led=%d fdd_led=%d\n",
             simulation_time, wall, wall / simulation_time, tb->pwr_led, tb->fdd_led);
      fflush(stdout);
      next_report += 0.1;
    }
  }
}
