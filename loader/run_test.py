#!/usr/bin/env python3
"""Runs a vAmigaTS test under NanoMig Verilator harness and compares the result."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.capture import NotCalibratedError, capture_from_vga
from loader.compare import compare_frames
from loader.mask import build_mask
from loader.retrosh import parse_retrosh

BUILD_PROFILE = "tang_nano_20k"


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


def run_test(
    retrosh_path: Path | str,
    out_dir: Path | str | None = None,
    harness_path: Path | str | None = None,
    rom_path: Path | str | None = None,
) -> Dict[str, Any]:
    """Executes a single test script end-to-end and writes result.json.
    
    Returns the result dict.
    """
    retrosh_path = Path(retrosh_path).resolve()
    config = parse_retrosh(retrosh_path)

    if out_dir is None:
        out_path = REPO_ROOT / "runs" / config.variant
    else:
        out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    nanomig_sha = get_git_sha(REPO_ROOT / "external" / "NanoMig")
    vamigats_sha = get_git_sha(REPO_ROOT / "external" / "vAmigaTS")

    result: Dict[str, Any] = {
        "variant": config.variant,
        "class": "ERROR",
        "detail": "",
        "labels": config.labels,
        "nanomig_sha": nanomig_sha,
        "vamigats_sha": vamigats_sha,
        "build_profile": BUILD_PROFILE,
        "wall_time": 0.0,
    }

    # If configuration is unsupported / deviating, do not simulate
    if not config.runnable:
        result["class"] = "DEVIATING_CONFIG"
        result["detail"] = config.deviating_reason or "deviating configuration"
        result_file = out_path / "result.json"
        result_file.write_text(json.dumps(result, indent=2))
        return result

    # Locate harness binary and ROM
    harness_bin = Path(harness_path) if harness_path else (REPO_ROOT / "build" / "nanomig_harness")
    if not harness_bin.is_file():
        result["detail"] = f"Harness binary not found at {harness_bin}"
        (out_path / "result.json").write_text(json.dumps(result, indent=2))
        return result

    kick_rom = Path(rom_path) if rom_path else (REPO_ROOT / "roms" / "kick13.rom")
    if not kick_rom.is_file():
        # Also check NanoMig/sim/kick13.rom if present
        alt_rom = REPO_ROOT / "NanoMig" / "sim" / "kick13.rom"
        if alt_rom.is_file():
            kick_rom = alt_rom
        else:
            result["detail"] = f"Kickstart ROM not found at {kick_rom}"
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result

    if not config.adf or not config.adf.is_file():
        result["detail"] = f"ADF disk image not found for {retrosh_path}"
        (out_path / "result.json").write_text(json.dumps(result, indent=2))
        return result

    # Expected wall time ~ 100x simulation time; timeout = 3x expected wall time
    expected_wall = config.wait_s * 100.0
    timeout_s = max(60.0, 3.0 * expected_wall)

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

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(tmp_path),
                timeout=timeout_s,
                capture_output=True,
                text=True,
            )
            wall_time = time.time() - t0
            result["wall_time"] = round(wall_time, 3)

            if proc.returncode != 0:
                result["class"] = "ERROR"
                result["detail"] = f"Harness failed (code {proc.returncode}): {proc.stderr[-300:]}"
                (out_path / "result.json").write_text(json.dumps(result, indent=2))
                return result

        except subprocess.TimeoutExpired:
            result["wall_time"] = round(time.time() - t0, 3)
            result["class"] = "ERROR"
            result["detail"] = f"Harness timed out after {timeout_s:.0f}s"
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result
        except Exception as e:
            result["wall_time"] = round(time.time() - t0, 3)
            result["class"] = "ERROR"
            result["detail"] = f"Execution error: {e}"
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result

        # Check VGA output
        vga_file = tmp_path / f"{config.variant}.frame.vga"
        if not vga_file.is_file():
            result["class"] = "ERROR"
            result["detail"] = f"Expected output {vga_file} not found"
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result

        try:
            captured_raster = capture_from_vga(vga_file)
        except NotCalibratedError as e:
            result["class"] = "NOT_CALIBRATED"
            result["detail"] = f"uncalibrated geometry: {e.geometry}"
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result
        except Exception as e:
            result["class"] = "ERROR"
            result["detail"] = f"VGA capture error: {e}"
            (out_path / "result.json").write_text(json.dumps(result, indent=2))
            return result

        # Compare with reference .raw
        ref_raw_path = retrosh_path.with_suffix(".raw")
        mask = build_mask(config.cutout)
        cmp_result = compare_frames(
            captured_raster,
            ref_raw_path,
            mask,
            config.variant,
            out_dir=out_path,
        )

        result["class"] = cmp_result["class"]
        result["detail"] = cmp_result["detail"]

    # Write final result.json
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
