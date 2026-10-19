#!/usr/bin/env python3
"""Runs a vAmigaTS test under NanoMig Verilator harness and compares the result."""

import argparse
from datetime import datetime
import gzip
import json
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.capture import NotCalibratedError, read_capture
from loader.compare import compare_frames
from loader.mask import build_mask
from loader.retrosh import parse_retrosh

BUILD_PROFILE = "tang_nano_20k"


def to_repo_rel(path_val: Path | str | None) -> Optional[str]:
    """Converts a path to be relative to REPO_ROOT."""
    if path_val is None:
        return None
    p = Path(path_val)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def get_git_sha(repo_dir: Path) -> str:
    """Returns the current git HEAD commit SHA of repo_dir."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def get_git_revision(repo_dir: Path) -> str:
    """HEAD SHA of repo_dir, with "-dirty" appended when tracked files have
    uncommitted changes (submodules ignored).

    A dirty SHA does not say which code produced a result, so run_all never
    resumes such a result: commit a change before relying on a run of it.
    """
    sha = get_git_sha(repo_dir)
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain",
             "--untracked-files=no", "--ignore-submodules=all"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return sha
    return f"{sha}-dirty" if res.stdout.strip() else sha


VAMIGATS_ROOT = REPO_ROOT / "external" / "vAmigaTS"


def this_machine() -> str:
    """The CPU this process runs on, as results record it ("<model>, <n> cores")."""
    model, cores = "?", set()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name") and model == "?":
                model = line.split(":", 1)[1].strip()
        out = subprocess.run(["lscpu", "-p=core,socket"], capture_output=True, text=True).stdout
        cores = {line for line in out.splitlines() if line and not line.startswith("#")}
    except Exception:
        pass
    return f"{model}, {len(cores) or '?'} cores"


def prepare_out_dir(out_path: Path) -> None:
    """Empty the variant's output directory before a run (stale files / diff PNGs must not survive)."""
    if out_path.exists():
        for item in out_path.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    else:
        out_path.mkdir(parents=True, exist_ok=True)


def new_result(config: Any, retrosh_path: Path, nanomig_sha: str, vamigats_sha: str,
               regression_sha: str, start_time: str) -> Dict[str, Any]:
    """The result.json skeleton every simulated variant gets."""
    try:
        rel_retrosh = str(retrosh_path.relative_to(VAMIGATS_ROOT).as_posix())
    except ValueError:
        rel_retrosh = str(retrosh_path.as_posix())
    return {
        "variant": config.variant,
        "class": "ERROR",
        "detail": "",
        "labels": config.labels,
        "nanomig_sha": nanomig_sha,
        "vamigats_sha": vamigats_sha,
        "regression_sha": regression_sha,
        "build_profile": BUILD_PROFILE,
        "retrosh_path": rel_retrosh,
        "harness_args": None,
        "harness_exit_code": None,
        "start_time": start_time,
        "end_time": None,
        "wall_time": 0.0,
        "differing_pixels": None,
        "raw_status": None,
    }


def harness_args_for(kick_rom: Path, config: Any) -> List[str]:
    """The harness arguments of a run as recorded in result.json (paths repo-relative)."""
    return [
        to_repo_rel(kick_rom),
        to_repo_rel(config.adf),
        str(config.wait_s),
        f"{config.variant}.frame",
        "--chipset",
        f"0x{config.chipset:02x}",
        "--memory",
        f"0x{config.memory:02x}",
    ] + [a for t, kind, value in config.events for a in ("--event", f"{t},{kind},0x{value:02x}")]


def simulation_limits(config: Any) -> tuple:
    """(CPU-time limit in s, wall-clock backstop in s) of a simulation.

    The simulation runs about 100x slower than real time. A hung simulator keeps
    burning CPU, while an overloaded machine only stretches the wall time. So the
    limit is CPU time (3x the expected ~100x simulated time); the wall-clock
    timeout (10x) is only a backstop for a process that hangs without using the CPU.
    """
    expected_cpu = config.wait_s * 100.0
    return int(max(60.0, 3.0 * expected_cpu)), max(600.0, 10.0 * expected_cpu)


def evaluate_capture(
    vga_file: Path, retrosh_path: Path, config: Any, out_path: Path, result: Dict[str, Any]
) -> Dict[str, Any]:
    """Crops a capture, compares it with the reference and fills in the result's
    class, detail, differing pixels and raw_status. Used after a simulation and by
    loader.recompare on a stored capture."""
    result.pop("geometry", None)
    result.pop("diff_image", None)
    try:
        captured_raster, ntsc_capture = read_capture(vga_file)
    except NotCalibratedError as e:
        result["class"] = "NOT_CALIBRATED"
        result["detail"] = f"uncalibrated geometry: {e.geometry}"
        result["geometry"] = e.geometry
        return result
    except Exception as e:
        result["class"] = "ERROR"
        result["detail"] = f"VGA capture error: {e}"
        return result

    cmp_result = compare_frames(
        captured_raster,
        retrosh_path.with_suffix(".raw"),
        build_mask(config.cutout),
        config.variant,
        out_dir=out_path,
        ntsc_capture=ntsc_capture,
    )
    result["class"] = cmp_result["class"]
    result["detail"] = cmp_result["detail"]
    result["differing_pixels"] = cmp_result["differing_pixels"]
    result["raw_status"] = cmp_result.get("raw_status")
    if cmp_result.get("diff_image"):
        result["diff_image"] = to_repo_rel(cmp_result["diff_image"])
    return result


def run_test(
    retrosh_path: Path | str,
    out_dir: Path | str | None = None,
    harness_path: Path | str | None = None,
    rom_path: Path | str | None = None,
    nanomig_sha: Optional[str] = None,
    vamigats_sha: Optional[str] = None,
    regression_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Executes a single test script end-to-end and writes result.json.
    
    Returns the result dict.
    """
    start_time = datetime.now().astimezone().isoformat()
    t0 = time.time()

    retrosh_path = Path(retrosh_path).resolve()
    config = parse_retrosh(retrosh_path)

    if out_dir is None:
        out_path = REPO_ROOT / "runs" / config.variant
    else:
        out_path = Path(out_dir).resolve()
    prepare_out_dir(out_path)

    if nanomig_sha is None:
        nanomig_sha = get_git_revision(REPO_ROOT / "external" / "NanoMig")
    if vamigats_sha is None:
        vamigats_sha = get_git_sha(VAMIGATS_ROOT)
    if regression_sha is None:
        regression_sha = get_git_revision(REPO_ROOT)
    result = new_result(config, retrosh_path, nanomig_sha, vamigats_sha, regression_sha, start_time)

    # If configuration is unsupported / deviating, do not simulate
    if not config.runnable:
        result["class"] = "DEVIATING_CONFIG"
        result["detail"] = config.deviating_reason or "deviating configuration"
        result["end_time"] = datetime.now().astimezone().isoformat()
        result_file = out_path / "result.json"
        result_file.write_text(json.dumps(result, indent=2))
        return result

    # Without a reference .raw there is nothing to compare against: do not simulate
    if not retrosh_path.with_suffix(".raw").is_file():
        result["class"] = "NO_REFERENCE"
        result["detail"] = "vAmigaTS has no reference .raw for this variant"
        result["end_time"] = datetime.now().astimezone().isoformat()
        (out_path / "result.json").write_text(json.dumps(result, indent=2))
        return result

    # Locate harness binary and ROM; scripts with joystick, mouse or keyboard input need
    # the input harness (HARNESS_INPUTS=1 harness/build.sh)
    default_dir = "build-inputs" if config.events else "build"
    harness_bin = Path(harness_path) if harness_path else (REPO_ROOT / default_dir / "nanomig_harness")
    if not harness_bin.is_file():
        result["detail"] = f"Harness binary not found at {harness_bin}"
        result["end_time"] = datetime.now().astimezone().isoformat()
        (out_path / "result.json").write_text(json.dumps(result, indent=2))
        return result

    kick_rom = Path(rom_path) if rom_path else (REPO_ROOT / "roms" / "kick13.rom")
    if not kick_rom.is_file():
        result["detail"] = f"Kickstart ROM not found at {kick_rom}"
        result["end_time"] = datetime.now().astimezone().isoformat()
        (out_path / "result.json").write_text(json.dumps(result, indent=2))
        return result

    if not config.adf or not config.adf.is_file():
        result["detail"] = f"ADF disk image not found for {retrosh_path}"
        result["end_time"] = datetime.now().astimezone().isoformat()
        (out_path / "result.json").write_text(json.dumps(result, indent=2))
        return result

    cpu_limit_s, timeout_s = simulation_limits(config)

    def limit_cpu():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_s, cpu_limit_s + 10))

    # Private temp directory per run
    with tempfile.TemporaryDirectory(prefix=f"nanomig_{config.variant}_") as tmpdir:
        tmp_path = Path(tmpdir)
        out_frame = tmp_path / f"{config.variant}.frame"

        # Copy fx68k microcode tables into temp directory so Verilator $readmemh finds them
        for mem_file in harness_bin.parent.glob("*.mem"):
            shutil.copy2(mem_file, tmp_path / mem_file.name)

        cmd = [
            str(harness_bin),
            str(kick_rom),
            str(config.adf),
            str(config.wait_s),
            str(out_frame),
            "--chipset",
            f"0x{config.chipset:02x}",
            "--memory",
            f"0x{config.memory:02x}",
        ]
        cmd += [a for t, kind, value in config.events for a in ("--event", f"{t},{kind},0x{value:02x}")]
        result["harness_args"] = harness_args_for(kick_rom, config)
        result["simulated_on"] = this_machine()

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(tmp_path),
                timeout=timeout_s,
                capture_output=True,
                text=True,
                preexec_fn=limit_cpu,
            )
            wall_time = time.time() - t0
            result["wall_time"] = round(wall_time, 3)
            result["harness_exit_code"] = proc.returncode

            if proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
                result["class"] = "ERROR"
                result["detail"] = f"Harness exceeded its CPU-time limit of {cpu_limit_s}s"
                result["end_time"] = datetime.now().astimezone().isoformat()
                (out_path / "result.json").write_text(json.dumps(result, indent=2))
                return result
            if proc.returncode != 0:
                result["class"] = "ERROR"
                result["detail"] = f"Harness failed (code {proc.returncode}): {proc.stderr[-300:]}"
                result["end_time"] = datetime.now().astimezone().isoformat()
                (out_path / "result.json").write_text(json.dumps(result, indent=2))
                return result

        except subprocess.TimeoutExpired:
            result["wall_time"] = round(time.time() - t0, 3)
            result["class"] = "ERROR"
            result["detail"] = f"Harness hit the wall-clock backstop of {timeout_s:.0f}s"
            result["end_time"] = datetime.now().astimezone().isoformat()
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result
        except Exception as e:
            result["wall_time"] = round(time.time() - t0, 3)
            result["class"] = "ERROR"
            result["detail"] = f"Execution error: {e}"
            result["end_time"] = datetime.now().astimezone().isoformat()
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result

        # Check VGA output
        vga_file = tmp_path / f"{config.variant}.frame.vga"
        if not vga_file.is_file():
            result["class"] = "ERROR"
            result["detail"] = f"Expected output {vga_file} not found"
            result["end_time"] = datetime.now().astimezone().isoformat()
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result

        # Keep the capture for audit: <variant>.frame.vga.gz in the output directory
        vga_gz_path = out_path / f"{config.variant}.frame.vga.gz"
        with open(vga_file, "rb") as f_in, gzip.open(vga_gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        evaluate_capture(vga_file, retrosh_path, config, out_path, result)

    # Write final result.json
    result["end_time"] = datetime.now().astimezone().isoformat()
    result_file = out_path / "result.json"
    result_file.write_text(json.dumps(result, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a vAmigaTS test under NanoMig and compare against reference."
    )
    parser.add_argument("retrosh", help="Path to variant.retrosh file")
    parser.add_argument("--out", default=None, help="Output directory for results")
    args = parser.parse_args()

    res = run_test(args.retrosh, out_dir=args.out)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
