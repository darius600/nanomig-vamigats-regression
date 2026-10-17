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
//
// Zygote mode, to boot Kickstart once for many tests of one configuration:
//   nanomig_harness --zygote <jobfile> --children N <kick.rom> <placeholder.adf> <fork_at_s> -
//                   [--chipset 0xNN] [--memory 0xNN]
//   Boots with the placeholder image until fork_at_s, checks that the image has not been
//   read yet, then forks one child per line of jobfile ("<adf>\t<wait_s>\t<out.frame>
//   \t<cpu_limit_s>\t<wall_limit_s>"), at most N at a time. Each child puts its ADF
//   behind the descriptor the SD card model opened and simulates on, exactly as a run
//   started with that ADF: nothing in the simulation depends on the image's contents
//   before it is first read (all vAmigaTS ADFs have the same size). The child's output
//   goes to <out.frame>.log; the parent prints "done <job> <exit> <signal> <cpu_s>
//   <wall_s>" per child. Exit 3: the image was read before fork_at_s.
//
// Output files:
//   <out.frame>.vga: VGA-captured frame (width, height header + RGB payload); the
//                    comparison uses only this one (loader/capture.py)
//   <out.frame>:     FRAME_H x FRAME_W x 4 bytes {r, g, b, flags}, indexed by the beam
//                    counter; kept for diagnosis only

#include <algorithm>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <ctime>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include <dirent.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

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

// ---- zygote mode ----
static const char *zygote_jobfile = nullptr;
static int zygote_children = 1;
static double zygote_fork_at = 0;
static std::string zygote_placeholder;

struct Job { std::string adf, out; double wait, cpu_limit, wall_limit; };

static double now_s() {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

// The descriptor the SD card model opened for the drive image (the placeholder)
static int image_fd() {
  char want[PATH_MAX], link[PATH_MAX], path[64];
  if (!realpath(zygote_placeholder.c_str(), want)) return -1;
  DIR *d = opendir("/proc/self/fd");
  if (!d) return -1;
  int found = -1;
  while (struct dirent *e = readdir(d)) {
    snprintf(path, sizeof path, "/proc/self/fd/%s", e->d_name);
    ssize_t n = readlink(path, link, sizeof link - 1);
    if (n <= 0) continue;
    link[n] = 0;
    if (!strcmp(link, want)) found = atoi(e->d_name);
  }
  closedir(d);
  return found;
}

// Returns only in a child, which then simulates its job; the parent exits here.
static void run_zygote() {
  int fd = image_fd();
  if (fd < 0) { fprintf(stderr, "zygote: drive image is not open\n"); exit(2); }
  if (lseek(fd, 0, SEEK_CUR) != 0) {
    fprintf(stderr, "zygote: the image was read before %.3fs\n", zygote_fork_at);
    exit(3);
  }
  std::vector<Job> jobs;
  std::ifstream in(zygote_jobfile);
  for (std::string line; std::getline(in, line);) {
    if (line.empty()) continue;
    std::stringstream s(line);
    Job j; std::string w, c, wl;
    std::getline(s, j.adf, '\t'); std::getline(s, w, '\t'); std::getline(s, j.out, '\t');
    std::getline(s, c, '\t'); std::getline(s, wl, '\t');
    j.wait = atof(w.c_str()); j.cpu_limit = atof(c.c_str()); j.wall_limit = atof(wl.c_str());
    jobs.push_back(j);
  }
  printf("%.3fms zygote: forking %zu jobs, %d at a time\n", simulation_time * 1000, jobs.size(), zygote_children);
  fflush(stdout);
  fflush(stderr);

  std::map<pid_t, std::pair<size_t, double>> running;
  size_t next = 0;
  while (next < jobs.size() || !running.empty()) {
    if (next < jobs.size() && (int)running.size() < zygote_children) {
      const Job &j = jobs[next];
      pid_t pid = fork();
      if (pid < 0) { perror("fork"); exit(2); }
      if (pid == 0) {
        struct stat a, b;
        int nfd = open(j.adf.c_str(), O_RDONLY);
        if (nfd < 0 || fstat(nfd, &a) || fstat(fd, &b) || a.st_size != b.st_size) {
          fprintf(stderr, "zygote child: %s missing or not the placeholder's size\n", j.adf.c_str());
          _exit(4);
        }
        dup2(nfd, fd);
        close(nfd);
        std::string log = j.out + ".log";
        if (!freopen(log.c_str(), "w", stdout)) _exit(4);
        struct rlimit cpu = { (rlim_t)j.cpu_limit, (rlim_t)j.cpu_limit + 10 };
        setrlimit(RLIMIT_CPU, &cpu);
        alarm((unsigned)j.wall_limit);
        wait_s = j.wait;
        out_path = strdup(j.out.c_str());
        printf("%.3fms zygote child: %s, wait %.1fs\n", simulation_time * 1000, j.adf.c_str(), j.wait);
        return;
      }
      running[pid] = { next, now_s() };
      next++;
      continue;
    }
    int status;
    struct rusage ru;
    pid_t pid = wait4(-1, &status, 0, &ru);
    if (pid < 0) { perror("wait4"); exit(2); }
    auto it = running.find(pid);
    if (it == running.end()) continue;
    double cpu = ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 + ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6;
    printf("done %zu %d %d %.1f %.1f\n", it->second.first,
           WIFEXITED(status) ? WEXITSTATUS(status) : -1, WIFSIGNALED(status) ? WTERMSIG(status) : 0,
           cpu, now_s() - it->second.second);
    fflush(stdout);
    running.erase(it);
  }
  exit(0);
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
    } else if (arg == "--zygote" && i + 1 < argc) {
      zygote_jobfile = argv[++i];
    } else if (arg == "--children" && i + 1 < argc) {
      zygote_children = atoi(argv[++i]);
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
  if (zygote_jobfile) {
    // positional wait_s is the fork time; the zygote itself never dumps a frame
    zygote_placeholder = adf_file;
    zygote_fork_at = wait_s;
    wait_s = 1e30;
  }

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
  bool forked = false;
  while (true) {
    tick(1);
    tick(0);
    if (zygote_jobfile && !forked && simulation_time >= zygote_fork_at) {
      forked = true;
      run_zygote();   // returns only in a child
    }
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
