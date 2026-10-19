#!/usr/bin/env python3
"""run_all's side of simqueue, the job queue all sessions on this machine share.

When a simqueue server answers at --queue / $SIMQUEUE_URL (default
http://127.0.0.1:8765), run_all hands its simulations to it instead of running them
itself. A job is exactly the harness command line run_test runs (same harness, ROM,
ADF, options and limits); a worker, on this machine or another, returns the capture,
and the comparison and result.json happen here with run_test's code. So a result looks
the same whichever machine simulated it, plus "simulated_by" (the worker's neutral id).

If the server stays unreachable, or no worker is active, for GIVE_UP_S, run_all
withdraws the run and simulates the rest itself (under the machine lock). The server
and the workers live in their own repository (simqueue); only the HTTP protocol
connects the two.
"""
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from loader.run_test import (
    REPO_ROOT,
    evaluate_capture,
    harness_args_for,
    new_result,
    prepare_out_dir,
    run_test,
    simulation_limits,
)

PROTOCOL = 1
DEFAULT_URL = "http://127.0.0.1:8765"
# unreachable, or no active worker, this long: simulate the rest here (shorter in tests)
GIVE_UP_S = int(os.environ.get("SIMQUEUE_GIVE_UP_S", "900"))
POLL_S = 5
PROGRESS_S = 1800    # a progress line in the log at least this often


class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def call(self, method: str, path: str, obj: Any = None, timeout: float = 60) -> Any:
        body = json.dumps(obj).encode() if obj is not None else None
        req = urllib.request.Request(f"{self.url}/api/v1/{path}", data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return json.loads(data) if data else None

    def put_blob(self, sha: str, data: bytes) -> None:
        req = urllib.request.Request(f"{self.url}/api/v1/blob/{sha}", data=data, method="PUT")
        with urllib.request.urlopen(req, timeout=600) as r:
            r.read()

    def blob(self, sha: str) -> bytes:
        with urllib.request.urlopen(f"{self.url}/api/v1/blob/{sha}", timeout=600) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise OSError(f"capture {sha[:12]} arrived damaged")
        return data


def connect(url: Optional[str] = None) -> Optional[Client]:
    """A client for the simqueue server, or None if none of our protocol answers."""
    client = Client(url or os.environ.get("SIMQUEUE_URL") or DEFAULT_URL)
    try:
        hello = client.call("GET", "hello", timeout=5)
    except (OSError, ValueError):
        return None
    if isinstance(hello, dict) and hello.get("service") == "simqueue" and hello.get("protocol") == PROTOCOL:
        return client
    return None


def harness_for(harness: Optional[str], config: Any) -> Path:
    """The harness run_test would run (input scripts need the input harness)."""
    return Path(harness) if harness else REPO_ROOT / ("build-inputs" if config.events else "build") / "nanomig_harness"


def job_for(jid: int, config: Any, harness_bin: Path, kick_rom: Path, sha: Callable[[Path], str]) -> Dict[str, Any]:
    """run_test's harness call as a simqueue job: the files are put next to each other in
    the job's working directory, as run_test does with the .mem files."""
    files = {"harness": sha(harness_bin), "kick.rom": sha(kick_rom), "disk.adf": sha(Path(config.adf))}
    for mem in sorted(harness_bin.parent.glob("*.mem")):
        files[mem.name] = sha(mem)
    cpu_limit_s, timeout_s = simulation_limits(config)
    argv = ["./harness", "kick.rom", "disk.adf", str(config.wait_s), f"{config.variant}.frame"]
    argv += harness_args_for(kick_rom, config)[4:]   # --chipset, --memory, --event as run_test passes them
    return {"id": jid, "label": config.variant, "files": files, "exec": "harness", "argv": argv,
            "outputs": [f"{config.variant}.frame.vga"], "cpu_limit": cpu_limit_s, "wall_limit": int(timeout_s),
            "lease": max(1800, 2 * cpu_limit_s)}


def result_from(entry: Dict[str, Any], retrosh: Path, config: Any, out_path: Path, kick_rom: Path,
                shas: Dict[str, str], capture: Optional[bytes]) -> Dict[str, Any]:
    """result.json for a job a worker ran, filled in exactly as run_test does."""
    prepare_out_dir(out_path)
    now = datetime.now().astimezone().isoformat()
    result = new_result(config, retrosh, shas["nanomig"], shas["vamigats"], shas["regression"],
                        entry.get("start") or now)
    result["harness_args"] = harness_args_for(kick_rom, config)
    result["simulated_on"] = entry["machine"]
    result["simulated_by"] = entry["worker"]
    result["wall_time"] = round(float(entry["wall"]), 3)
    cpu_limit_s, timeout_s = simulation_limits(config)
    code = -entry["signal"] if entry["signal"] else entry["exit"]
    if entry["timed_out"]:
        result["detail"] = f"Harness hit the wall-clock backstop of {timeout_s:.0f}s"
    else:
        result["harness_exit_code"] = code
        if code in (-signal.SIGXCPU, -signal.SIGKILL):
            result["detail"] = f"Harness exceeded its CPU-time limit of {cpu_limit_s}s"
        elif code != 0:
            result["detail"] = f"Harness failed (code {code}): {entry['stderr'][-300:]}"
        elif capture is None:
            result["detail"] = f"Expected output {config.variant}.frame.vga not found"
        else:
            # Keep the capture for audit, as run_test does: <variant>.frame.vga.gz
            vga_gz_path = out_path / f"{config.variant}.frame.vga.gz"
            with gzip.open(vga_gz_path, "wb") as f:
                f.write(capture)
            evaluate_capture(vga_gz_path, retrosh, config, out_path, result)
    result["end_time"] = datetime.now().astimezone().isoformat()
    (out_path / "result.json").write_text(json.dumps(result, indent=2))
    return result


def simulate(client: Client, items: List[Tuple[Path, Path, Any]], results: Dict[str, Dict[str, Any]], *,
             harness: Optional[str], kick_rom: Path, shas: Dict[str, str], label: str, owner: str,
             priority: int, report: Callable[[Dict[str, Any], Any], None]) -> List[Tuple[Path, Path, Any]]:
    """Simulates items [(retrosh, out dir, config)] through the queue, filling in results.

    Returns the items left undone when it gave up (see GIVE_UP_S); run_all simulates
    those itself."""
    sha_of: Dict[Path, str] = {}

    def sha(path: Path) -> str:
        path = path.resolve()
        if path not in sha_of:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            sha_of[path] = h.hexdigest()
        return sha_of[path]

    jobs: Dict[int, Dict[str, Any]] = {}
    todo: Dict[int, Tuple[Path, Path, Any]] = {}
    for rf, var_out, cfg in items:
        harness_bin = harness_for(harness, cfg)
        if not harness_bin.is_file() or not kick_rom.is_file() or not cfg.adf or not Path(cfg.adf).is_file():
            # run_test reports the missing file without simulating
            res = run_test(rf, var_out, harness_path=harness, nanomig_sha=shas["nanomig"],
                           vamigats_sha=shas["vamigats"], regression_sha=shas["regression"])
            results[str(rf)] = res
            report(res, cfg)
            continue
        jid = len(jobs)
        jobs[jid] = job_for(jid, cfg, harness_bin, kick_rom, sha)
        todo[jid] = (rf, var_out, cfg)
    if not jobs:
        return []
    paths = {s: p for p, s in sha_of.items()}

    def submit(ids: List[int]) -> str:
        wanted = sorted({s for j in ids for s in jobs[j]["files"].values()})
        for s in client.call("POST", "blobs/missing", {"shas": wanted})["missing"]:
            client.put_blob(s, paths[s].read_bytes())
        spec = {"label": label, "owner": owner, "key": owner, "priority": priority,
                "jobs": [jobs[j] for j in ids]}
        return client.call("POST", "runs", spec, timeout=300)["run"]

    rid = submit(sorted(jobs))
    print(f"simqueue run {rid}: {len(jobs)} jobs at {client.url}", flush=True)
    outstanding = set(jobs)
    cursor = 0
    trouble_since: Optional[float] = None   # server unreachable or no worker active since
    last_line = time.time()
    try:
        while outstanding:
            status = None
            try:
                status = client.call("GET", f"runs/{rid}/results?after={cursor}")
            except urllib.error.HTTPError as e:
                if e.code == 404:   # the server was restarted: submit what is left again
                    print(f"simqueue no longer knows run {rid} (restarted?); submitting the "
                          f"{len(outstanding)} remaining jobs again", flush=True)
                    try:
                        rid, cursor = submit(sorted(outstanding)), 0
                        print(f"simqueue run {rid}: {len(outstanding)} jobs", flush=True)
                        continue
                    except (OSError, ValueError):
                        pass
            except (OSError, ValueError):
                pass
            if status is not None:
                for entry in status["results"]:
                    jid = entry["id"]
                    if jid in outstanding:
                        rf, var_out, cfg = todo[jid]
                        name = f"{cfg.variant}.frame.vga"
                        try:
                            capture = client.blob(entry["outputs"][name]) if name in entry["outputs"] else None
                        except OSError:
                            break   # fetch it with the next poll
                        try:
                            res = result_from(entry, rf, cfg, var_out, kick_rom, shas, capture)
                        except Exception as e:
                            res = new_result(cfg, rf, shas["nanomig"], shas["vamigats"], shas["regression"],
                                             entry.get("start") or datetime.now().astimezone().isoformat())
                            res["detail"] = f"Worker error: {e}"
                            var_out.mkdir(parents=True, exist_ok=True)
                            (var_out / "result.json").write_text(json.dumps(res, indent=2))
                        outstanding.discard(jid)
                        results[str(rf)] = res
                        report(res, cfg)
                    cursor += 1
                stuck = status["workers_active"] == 0 and status["running"] == 0
                trouble_since = (trouble_since or time.time()) if stuck else None
                if time.time() - last_line > PROGRESS_S:
                    print(f"simqueue run {rid}: {status['done']}/{status['total']} done, {status['running']} "
                          f"running, {status['pending']} pending, {status['workers_active']} workers active",
                          flush=True)
                    last_line = time.time()
            else:
                trouble_since = trouble_since or time.time()
            if trouble_since and time.time() - trouble_since > GIVE_UP_S:
                print(f"simqueue: {'server not reachable' if status is None else 'no worker active'} for "
                      f"{GIVE_UP_S // 60} min; withdrawing run {rid}", flush=True)
                cancel(client, rid)
                return [todo[j] for j in sorted(outstanding)]
            if outstanding:
                time.sleep(POLL_S)
    except KeyboardInterrupt:
        cancel(client, rid)
        raise
    cancel(client, rid)   # all fetched: the server can forget the run
    return []


def cancel(client: Client, rid: str) -> None:
    try:
        client.call("DELETE", f"runs/{rid}", timeout=10)
    except (OSError, ValueError):
        pass
