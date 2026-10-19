"""run_all through simqueue gives the same results as run_all simulating itself.

Needs the simqueue repository next to this one (../simqueue) and the stored captures
of runs/control-4315acb and runs/full-4315acb-worker; skipped otherwise. A dummy
harness replays stored captures, so nothing is simulated (hence --no-lock).
"""
import gzip
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SIMQUEUE = REPO_ROOT.parent / "simqueue"
TS = REPO_ROOT / "external" / "vAmigaTS"
CONTROL = REPO_ROOT / "runs" / "control-4315acb"
WORKER_RUN = REPO_ROOT / "runs" / "full-4315acb-worker"

# variant -> (retrosh, capture the dummy harness replays, exit code)
CASES = {
    "cross1_ocs": ("Agnus/Copper/cross/cross1/cross1_ocs.retrosh", CONTROL / "Agnus/Copper/cross/cross1/cross1_ocs", 0),
    "ntsc3_ocs": ("Agnus/Misc/NTSC/ntsc3/ntsc3_ocs.retrosh", CONTROL / "Agnus/Misc/NTSC/ntsc3/ntsc3_ocs", 0),
    # another variant's capture: a FAIL with differing pixels
    "cross1_ecs": ("Agnus/Copper/cross/cross1/cross1_ecs.retrosh", CONTROL / "Agnus/DDF/DDF/ddf1/ddf1_ecs", 0),
    # the harness fails
    "dasdma1_ocs": ("Agnus/Copper/CopDma/dasdma1/dasdma1_ocs.retrosh", None, 3),
    # input events on the command line
    "joy0dat_1": ("Mainboard/Ports/JOYxDAT/joy0dat/joy0dat_1.retrosh",
                  WORKER_RUN / "Mainboard/Ports/JOYxDAT/joy0dat/joy0dat_1", 0),
    # needs no simulation
    "addrerr1_68010": ("CPU/Exceptions/StackFrame/addrerr1/addrerr1_68010.retrosh", None, None),
}
VOLATILE = ("start_time", "end_time", "wall_time", "simulated_by", "diff_image")

DUMMY = r'''#!/usr/bin/env python3
# Dummy harness: replays a stored capture and logs what it was called with.
import gzip, hashlib, json, os, sys
from pathlib import Path
CASES = json.loads(%(cases)r)
rom, adf, wait, frame = sys.argv[1:5]
variant = Path(frame).name[: -len(".frame")]
seen = {"tail": sys.argv[3:4] + [Path(frame).name] + sys.argv[5:],
        "rom": hashlib.sha256(Path(rom).read_bytes()).hexdigest(),
        "adf": hashlib.sha256(Path(adf).read_bytes()).hexdigest(),
        "mem": sorted(p for p in os.listdir(".") if p.endswith(".mem"))}
with open(%(log)r, "a") as f:
    f.write(json.dumps({"variant": variant, **seen}) + "\n")
capture, code = CASES[variant]
if code:
    sys.stderr.write("boom: the dummy harness fails here\n")
    sys.exit(code)
Path(frame + ".vga").write_bytes(gzip.decompress(Path(capture).read_bytes()))
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def captures_present() -> bool:
    return all(c is None or any(c.glob("*.frame.vga.gz")) for _, c, _ in CASES.values())


@unittest.skipUnless((SIMQUEUE / "server.py").is_file() and captures_present(),
                     "needs ../simqueue and the stored captures under runs/")
class TestRunAllThroughSimqueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SIMQUEUE))
        cls.srv = importlib.import_module("server")
        cls.wrk = importlib.import_module("worker")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        harness_dir = self.base / "harness"
        harness_dir.mkdir()
        (harness_dir / "microrom.mem").write_text("00\n")
        self.log = self.base / "calls.jsonl"
        cases = {v: (str(next(c.glob("*.frame.vga.gz"))) if c else None, code)
                 for v, (_, c, code) in CASES.items() if code is not None}
        self.harness = harness_dir / "nanomig_harness"
        self.harness.write_text(DUMMY % {"cases": json.dumps(cases), "log": str(self.log)})
        self.harness.chmod(0o755)
        self.scripts = [str(TS / r) for r, _, _ in CASES.values()]

    def tearDown(self):
        self.tmp.cleanup()

    def run_all(self, out: Path, *extra: str, env: dict = None) -> str:
        cmd = [sys.executable, "-m", "loader.run_all", *self.scripts, "--harness", str(self.harness),
               "--out", str(out), "--no-lock", "--jobs", "2", *extra]
        p = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
                           env={**os.environ, **(env or {})})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def results(self, out: Path) -> dict:
        found = {}
        for f in out.rglob("result.json"):
            r = json.loads(f.read_text())
            cap = list(f.parent.glob("*.frame.vga.gz"))
            found[r["variant"]] = ({k: v for k, v in r.items() if k not in VOLATILE},
                                   gzip.decompress(cap[0].read_bytes()) if cap else None)
        return found

    def calls(self) -> dict:
        by_variant = {}
        for line in self.log.read_text().splitlines():
            c = json.loads(line)
            by_variant.setdefault(c.pop("variant"), []).append(c)
        self.log.unlink()
        return by_variant

    def test_same_results_as_local(self):
        local_out = self.base / "local"
        self.run_all(local_out, "--local")
        local, local_calls = self.results(local_out), self.calls()

        state = self.srv.State(self.base / "state")
        port = free_port()
        http = self.srv.serve(state, port, host="127.0.0.1")
        server = self.wrk.Server(f"http://127.0.0.1:{port}",
                                 {"worker": "here", "cpu": self.wrk.cpu_model(),
                                  "cores": self.wrk.physical_cores(), "reference": "1"})
        worker = self.wrk.Worker(server, self.base / "w", self.base / "w" / "cache", 2, poll=0.5)
        worker.start()
        try:
            queue_out = self.base / "queue"
            log = self.run_all(queue_out, "--queue", f"http://127.0.0.1:{port}")
        finally:
            worker.shutdown()
            http.shutdown()
        self.assertIn("through simqueue", log)
        queued, queue_calls = self.results(queue_out), self.calls()

        self.assertEqual(sorted(local), sorted(CASES))
        self.assertEqual(sorted(queued), sorted(CASES))
        for variant in CASES:
            with self.subTest(variant=variant):
                self.assertEqual(queued[variant], local[variant])
        self.assertEqual(local["cross1_ocs"][0]["class"], "PASS")
        self.assertEqual(local["ntsc3_ocs"][0]["class"], "PASS")
        self.assertEqual(local["cross1_ecs"][0]["class"], "FAIL")
        self.assertEqual(local["dasdma1_ocs"][0]["class"], "ERROR")
        self.assertEqual(local["joy0dat_1"][0]["class"], "PASS")
        self.assertEqual(local["addrerr1_68010"][0]["class"], "DEVIATING_CONFIG")
        # the harness was called the same way: arguments, ROM, ADF, .mem files
        self.assertEqual(queue_calls, local_calls)
        self.assertIn("--event", local_calls["joy0dat_1"][0]["tail"])

    def test_without_workers_run_all_simulates_itself(self):
        state = self.srv.State(self.base / "state")
        port = free_port()
        http = self.srv.serve(state, port, host="127.0.0.1")
        try:
            out = self.base / "fallback"
            log = self.run_all(out, "--queue", f"http://127.0.0.1:{port}", env={"SIMQUEUE_GIVE_UP_S": "2"})
        finally:
            http.shutdown()
        self.assertIn("no worker active", log)
        self.assertIn("Simulating the remaining 5 variants here", log)
        self.assertEqual(sorted(self.results(out)), sorted(CASES))
        self.assertEqual(state.runs, {})   # the run was withdrawn


if __name__ == "__main__":
    unittest.main()
