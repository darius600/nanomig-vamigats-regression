// NanoMig regression harness: drives NanoMig's simulation top (nanomig_tb.v)
// without SDL or tracing, boots an ADF and dumps one native frame indexed by
// the Agnus beam counter and one VGA-captured frame.
//
// Usage:
//   nanomig_harness <kick.rom> <disk.adf> <wait_s> <out.frame> [--chipset 0xNN] [--memory 0xNN]
//                   [--event <t_s>,<kind>,<value> ...]
//   --chipset and --memory go to nanomig_tb's chipset_config and memory_config inputs
//   (loader/retrosh.py maps each vAmigaTS machine onto them).
//   --event exists only in the input build (HARNESS_INPUTS=1 harness/build.sh, see
//   harness/inputs_tb.py): from simulated time t_s on, kind joy0 / joy1 / mouse sets
//   the joystick or mouse button bits to value; kind key sends value as one keyboard
//   byte (keycode, bit 7 set for a release).
//   nanomig_harness --init-info prints the start state as JSON and exits: {"init": "zero"},
//   or for an EXPERIMENTAL build with FPGA start values (HARNESS_INIT=<list> harness/build.sh,
//   see loader/init_list.py) the list, how many of its bits were set and which names could
//   not be resolved. Such a build sets the listed bits to 1 before the first evaluation.
//
// Output files:
//   <out.frame>.vga: VGA-captured frame (width, height header + RGB payload); the
//                    comparison uses only this one (loader/capture.py)
//   <out.frame>:     FRAME_H x FRAME_W x 4 bytes {r, g, b, flags}, indexed by the beam
//                    counter; kept for diagnosis only

#include <algorithm>
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

#ifdef HARNESS_INPUTS
// Input events from the test script, sorted by time
struct Event { double t; std::string kind; unsigned value; };
static std::vector<Event> events;
static size_t next_event = 0;

static void apply_events() {
  while (next_event < events.size() && simulation_time >= events[next_event].t) {
    const Event &e = events[next_event++];
    if (e.kind == "joy0") tb->joystick0_in = e.value;
    else if (e.kind == "joy1") tb->joystick1_in = e.value;
    else if (e.kind == "mouse") tb->mouse_buttons_in = e.value;
    else if (e.kind == "key") {
      // the way NanoMig's hid.v hands a keyboard byte to the core
      tb->kbd_mouse_type_in = 2;
      tb->kbd_mouse_data_in = e.value;
      tb->kbd_mouse_level_in = !tb->kbd_mouse_level_in;
    }
    printf("%.3fms event %s 0x%02x\n", simulation_time * 1000, e.kind.c_str(), e.value);
  }
}
#endif

#ifdef HARNESS_INIT
#include "init_list.h"

struct InitMiss { const InitBit *e; const char *why; };
static int init_set = 0;
static std::vector<InitMiss> init_missed;

// Sets one listed bit to 1, or says why it cannot
static const char *set_init_bit(const InitBit &e) {
  std::string scope = std::string("TOP.nanomig_tb.nanomig") + (e.path[0] ? "." : "") + e.path;
  const VerilatedScope *s = Verilated::threadContextp()->scopeFind(scope.c_str());
  if (!s) return "no such scope";
  VerilatedVar *v = s->varFind(e.var);
  if (!v) return "no such public variable";
  if (v->udims() != 0 || v->pdims() > 1) return "array, not supported";
  int pos = 0;
  if (e.bit < 0) {
    if (v->entBits() != 1) return "no bit given for a vector";
  } else {
    if (v->pdims() != 1) return "bit given for a one-bit register";
    const VerilatedRange &r = v->packedRanges()[0];
    if (e.bit < r.low() || e.bit > r.high()) return "bit out of range";
    pos = r.left() >= r.right() ? e.bit - r.right() : r.right() - e.bit;
  }
  void *p = v->datap();
  switch (v->vltype()) {
    case VLVT_UINT8:  *(CData *)p |= (CData)(1u << pos); break;
    case VLVT_UINT16: *(SData *)p |= (SData)(1u << pos); break;
    case VLVT_UINT32: *(IData *)p |= 1u << pos; break;
    case VLVT_UINT64: *(QData *)p |= 1ull << pos; break;
    case VLVT_WDATA:  ((EData *)p)[pos / 32] |= 1u << (pos % 32); break;
    default: return "type not supported";
  }
  return nullptr;
}

// The FPGA's start values: before the first eval, so that the reset acts on them as on the board
static void apply_init() {
  for (const InitBit &e : INIT_BITS) {
    const char *why = set_init_bit(e);
    if (why) init_missed.push_back({ &e, why }); else init_set++;
  }
}

static void print_json_str(const char *s) {
  putchar('"');
  for (; *s; s++) {
    if (*s == '"' || *s == '\\') putchar('\\');
    putchar(*s);
  }
  putchar('"');
}
#endif

static int init_info() {
#ifdef HARNESS_INIT
  printf("{\"init\": \"fpga (experimental)\", \"list\": "); print_json_str(INIT_LIST_NAME);
  printf(", \"sha256\": "); print_json_str(INIT_LIST_SHA256);
  printf(", \"nanomig\": "); print_json_str(INIT_LIST_NANOMIG);
  printf(", \"gowin\": "); print_json_str(INIT_LIST_GOWIN);
  printf(", \"bits\": %d, \"set\": %d, \"unresolved\": [", (int)(sizeof INIT_BITS / sizeof INIT_BITS[0]), init_set);
  for (size_t i = 0; i < init_missed.size(); i++) {
    const InitMiss &m = init_missed[i];
    printf(i ? ", [" : "["); print_json_str(m.e->path); printf(", "); print_json_str(m.e->var);
    if (m.e->bit < 0) printf(", \"-\", "); else printf(", \"%d\", ", m.e->bit);
    print_json_str(m.why); printf("]");
  }
  printf("]}\n");
#else
  printf("{\"init\": \"zero\"}\n");
#endif
  return 0;
}

// One 28 MHz half period, same sequencing as NanoMig's sim/nanomig_tb.cpp
static void tick(int c) {
#ifdef HARNESS_INPUTS
  if (c) apply_events();
#endif
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
    if (arg == "--init-info") {
#ifdef HARNESS_INIT
      tb = new Vnanomig_tb;   // resolves the names against the model, nothing is simulated
      apply_init();
#endif
      return init_info();
    } else if (arg == "--chipset" && i + 1 < argc) {
      chipset = (uint8_t)std::strtoul(argv[++i], nullptr, 0);
    } else if (arg.rfind("--chipset=", 0) == 0) {
      chipset = (uint8_t)std::strtoul(arg.c_str() + 10, nullptr, 0);
    } else if (arg == "--memory" && i + 1 < argc) {
      memory = (uint8_t)std::strtoul(argv[++i], nullptr, 0);
    } else if (arg.rfind("--memory=", 0) == 0) {
      memory = (uint8_t)std::strtoul(arg.c_str() + 9, nullptr, 0);
    } else if (arg == "--event" && i + 1 < argc) {
#ifdef HARNESS_INPUTS
      std::string spec = argv[++i];
      size_t a = spec.find(','), b = spec.find(',', a + 1);
      if (a == std::string::npos || b == std::string::npos) {
        fprintf(stderr, "bad --event %s, expected <t_s>,<kind>,<value>\n", spec.c_str());
        return 2;
      }
      events.push_back({ atof(spec.substr(0, a).c_str()), spec.substr(a + 1, b - a - 1),
                         (unsigned)std::strtoul(spec.substr(b + 1).c_str(), nullptr, 0) });
#else
      fprintf(stderr, "--event needs the input build: HARNESS_INPUTS=1 harness/build.sh <dir>\n");
      return 2;
#endif
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
#ifdef HARNESS_INIT
  apply_init();
  printf("init fpga (experimental): %s (sha256 %.12s), %d of %d bits set to 1\n", INIT_LIST_NAME,
         INIT_LIST_SHA256, init_set, (int)(sizeof INIT_BITS / sizeof INIT_BITS[0]));
  for (const InitMiss &m : init_missed)
    printf("init: not set: %s %s %d: %s\n", m.e->path[0] ? m.e->path : "(core)", m.e->var, m.e->bit, m.why);
#endif

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
#ifdef HARNESS_INPUTS
  std::stable_sort(events.begin(), events.end(), [](const Event &x, const Event &y) { return x.t < y.t; });
  tb->joystick0_in = 0;
  tb->joystick1_in = 0;
  tb->mouse_buttons_in = 0;
  tb->kbd_mouse_level_in = 0;
  tb->kbd_mouse_type_in = 0;
  tb->kbd_mouse_data_in = 0;
#endif

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
