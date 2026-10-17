#!/usr/bin/env python3
"""Zygote runs: boot Kickstart once per configuration and fork every test from there.

A development tool for quick feedback, e.g. while trying an RTL change; off unless
run_all gets --zygote. Every result it produces carries zygote_fork_at_s, and
loader.publish refuses such results: what is published ran on its own, and a change
is confirmed with a normal run.

Kickstart 1.3 does not touch the disk until about 3.9 s of Amiga time (first read of
the boot block at 3.93 s on an A500 with 1 MB). Up to that point the simulation is the
same for every test of one configuration, so the harness boots once with a
placeholder image of the same size and forks one child per test (harness.cpp,
--zygote); each child puts its ADF behind the image's descriptor. For a test of 9 s
that saves about 40 % of the simulation. The harness refuses (exit 3) if the image was
read before the fork time; run_all then runs that configuration's tests one by one.
"""
from datetime import datetime
import gzip
import json
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Tuple

from loader.run_test import evaluate_capture, harness_args_for, new_result, prepare_out_dir

ZYGOTE_FORK_AT = 3.5   # s of Amiga time, before the first disk read (3.93 s)


class ZygoteTooLate(Exception):
    """The image was read before the fork time: no zygote for this configuration."""


def simulate_group(
    items: List[Tuple[Path, Path, Any]],
    harness_bin: Path,
    kick_rom: Path,
    children: int,
    shas: Dict[str, str],
    on_result: Callable[[Path, Dict[str, Any]], None],
    fork_at: float = ZYGOTE_FORK_AT,
) -> None:
    """Simulates items (retrosh, output dir, RunConfig) of one configuration (same
    chipset and memory, no input events) from one zygote; calls on_result per item."""
    cfg0 = items[0][2]
    with tempfile.TemporaryDirectory(prefix="nanomig_zygote_") as tmp:
        tmp = Path(tmp)
        for mem in harness_bin.parent.glob("*.mem"):
            shutil.copy2(mem, tmp / mem.name)
        placeholder = tmp / "placeholder.adf"
        with open(placeholder, "wb") as f:
            f.truncate(cfg0.adf.stat().st_size)

        results, lines = [], []
        for i, (rf, var_out, cfg) in enumerate(items):
            prepare_out_dir(var_out)
            r = new_result(cfg, rf, shas["nanomig"], shas["vamigats"], shas["regression"],
                           datetime.now().astimezone().isoformat())
            r["harness_args"] = harness_args_for(kick_rom, cfg)
            r["zygote_fork_at_s"] = fork_at
            results.append(r)
            job_dir = tmp / str(i)
            job_dir.mkdir()
            expected_cpu = (cfg.wait_s - fork_at) * 100.0
            cpu_limit = int(max(60.0, 3.0 * expected_cpu))
            wall_limit = int(max(600.0, 10.0 * expected_cpu))
            lines.append(f"{cfg.adf}\t{cfg.wait_s}\t{job_dir / (cfg.variant + '.frame')}\t{cpu_limit}\t{wall_limit}")
        jobfile = tmp / "jobs.tsv"
        jobfile.write_text("\n".join(lines) + "\n")

        cmd = [str(harness_bin), "--zygote", str(jobfile), "--children", str(children),
               str(kick_rom), str(placeholder), str(fork_at), "-",
               "--chipset", f"0x{cfg0.chipset:02x}", "--memory", f"0x{cfg0.memory:02x}"]
        boot_cpu = int(3.0 * fork_at * 100.0 + 60)

        def limit_boot():
            # the zygote's own boot; children set their own limits
            resource.setrlimit(resource.RLIMIT_CPU, (boot_cpu, resource.RLIM_INFINITY))

        proc = subprocess.Popen(cmd, cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, preexec_fn=limit_boot)
        done = set()
        for line in proc.stdout:
            if not line.startswith("done "):
                continue
            _, k, code, sig, cpu, wall = line.split()
            k, code, sig = int(k), int(code), int(sig)
            rf, var_out, cfg = items[k]
            r = results[k]
            r["wall_time"] = float(wall)
            r["harness_exit_code"] = code if sig == 0 else -sig
            frame = tmp / str(k) / f"{cfg.variant}.frame"
            vga = frame.with_name(frame.name + ".vga")
            if sig in (signal.SIGXCPU, signal.SIGKILL):
                r["detail"] = f"Harness exceeded its CPU-time limit ({cpu} s used)"
            elif sig == signal.SIGALRM:
                r["detail"] = f"Harness hit the wall-clock backstop after {wall} s"
            elif code != 0 or sig != 0:
                log = frame.with_name(frame.name + ".log")
                tail = log.read_text(errors="replace")[-300:] if log.is_file() else ""
                r["detail"] = f"Harness failed (code {code}, signal {sig}): {tail}"
            elif not vga.is_file():
                r["detail"] = f"Expected output {vga.name} not found"
            else:
                with open(vga, "rb") as f_in, gzip.open(var_out / f"{cfg.variant}.frame.vga.gz", "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                evaluate_capture(vga, rf, cfg, var_out, r)
            r["end_time"] = datetime.now().astimezone().isoformat()
            (var_out / "result.json").write_text(json.dumps(r, indent=2))
            done.add(k)
            on_result(rf, r)
        stderr = proc.stderr.read()
        proc.wait()
        if proc.returncode == 3 and not done:
            raise ZygoteTooLate(stderr.strip())
        for k, (rf, var_out, cfg) in enumerate(items):
            if k in done:
                continue
            r = results[k]
            r["detail"] = f"Zygote ended (code {proc.returncode}) before this job: {stderr.strip()[-300:]}"
            r["end_time"] = datetime.now().astimezone().isoformat()
            (var_out / "result.json").write_text(json.dumps(r, indent=2))
            on_result(rf, r)
