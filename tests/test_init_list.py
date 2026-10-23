"""Unit tests for the EXPERIMENTAL FPGA start values (--init fpga:<list>): the list, the
build files made from it, and that such results never mix with others."""

import json
from pathlib import Path
import tempfile
import unittest

from loader import init_list
from loader.run_all import claim_out_dir

LIST = """# nanomig a34b8646fec63cc250c679fb1f042ebf1db2507f (bundle/wave12-up)
# gowin 1.9.12.04 (standard edition), synthesis only
# netlist nanomig.vg, root nanomig, 4 of 9 start at 1
\treset_d\t-
minimig.AGNUS1.bpd1\tddfrun\t-
minimig.AGNUS1.bc1\thtotal_reg\t2
minimig.AGNUS1.bc1\thtotal_reg_2\t-
amiga_clk\tcck_Z\t-
nowhere\tx\t-
"""


def module(addr, name, variables, cells=()):
    return {"type": "MODULE", "addr": addr, "name": name, "origName": name,
            "stmtsp": [{"type": "VAR", "name": v} for v in variables]
            + [{"type": "CELL", "name": c, "modp": m} for c, m in cells]}


# What Verilator's --json-only tree holds of the hierarchy, cut down to the parts used
TREE = {"modulesp": [
    module("(A)", "nanomig_tb", ["clk"], [("nanomig", "(B)")]),
    module("(B)", "nanomig", ["reset_d", "clk"], [("minimig", "(C)"), ("amiga_clk", "(F)")]),
    module("(C)", "minimig", ["ovl"], [("AGNUS1", "(D)")]),
    module("(D)", "agnus", [], [("bpd1", "(E)"), ("bc1", "(G)")]),
    module("(E)", "agnus_bitplanedma", ["ddfrun", "ddfend"]),
    module("(F)", "amiga_clk", ["cck"]),
    module("(G)", "agnus_beamcounter", ["htotal_reg", "hpos"]),
]}


class TestInitList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.list = self.dir / "fix4.tsv"
        self.list.write_text(LIST)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_reads_the_header_and_the_rows(self):
        lst = init_list.parse(self.list)
        self.assertEqual(lst.nanomig, "a34b8646fec63cc250c679fb1f042ebf1db2507f")
        self.assertEqual(lst.gowin, "1.9.12.04 (standard edition), synthesis only")
        self.assertEqual(lst.entries[0], ("", "reset_d", None))   # empty instance path: the core itself
        self.assertEqual(lst.entries[2], ("minimig.AGNUS1.bc1", "htotal_reg", 2))
        self.assertEqual(len(lst.entries), 6)
        self.assertEqual(len(lst.sha256), 64)

    def test_a_list_without_its_nanomig_commit_is_refused(self):
        self.list.write_text("# gowin 1.9.12.04\n\tddfrun\t-\n")
        with self.assertRaisesRegex(ValueError, "nanomig"):
            init_list.parse(self.list)
        self.list.write_text("# nanomig a34b864\nminimig\tddfrun\n")
        with self.assertRaisesRegex(ValueError, "expected"):
            init_list.parse(self.list)

    def test_the_list_must_match_the_checkout(self):
        lst = init_list.parse(self.list)
        self.assertIsNone(init_list.nanomig_mismatch(lst, "a34b8646fec63cc250c679fb1f042ebf1db2507f"))
        self.assertIsNone(init_list.nanomig_mismatch(lst, "a34b8646fec63cc250c679fb1f042ebf1db2507f-dirty"))
        self.assertIn("own netlist", init_list.nanomig_mismatch(lst, "1e2d205bd2c1c312acafb6326a8440a50e65eae2"))
        lst.nanomig = "a34b86"   # too short to say which commit
        self.assertIsNotNone(init_list.nanomig_mismatch(lst, "a34b8646fec63cc250c679fb1f042ebf1db2507f"))

    def test_generate_makes_only_the_listed_registers_public_and_reports_the_rest(self):
        tree = self.dir / "tree.json"
        tree.write_text(json.dumps(TREE))
        unresolved = init_list.generate(init_list.parse(self.list), tree, self.dir / "init")
        vlt = (self.dir / "init" / "init.vlt").read_text()
        self.assertIn('public_flat_rd -module "agnus_bitplanedma" -var "ddfrun"', vlt)
        self.assertIn('public_flat_rd -module "agnus_beamcounter" -var "htotal_reg"', vlt)
        self.assertIn('public_flat_rd -module "nanomig" -var "reset_d"', vlt)
        self.assertNotIn("ddfend", vlt)
        # synthesis names are reported, not guessed
        self.assertEqual([(i, r) for i, r, _, _ in unresolved],
                         [("minimig.AGNUS1.bc1", "htotal_reg_2"), ("amiga_clk", "cck_Z"), ("nowhere", "x")])
        header = (self.dir / "init" / "init_list.h").read_text()
        self.assertIn('{"minimig.AGNUS1.bc1", "htotal_reg", 2},', header)
        self.assertIn('{"", "reset_d", -1},', header)
        self.assertIn(init_list.parse(self.list).sha256, header)

    def test_signature(self):
        self.assertEqual(init_list.signature({"class": "PASS"}), ("zero", None))   # results from before --init
        fields = init_list.result_fields(init_list.parse(self.list), {"set": 3, "unresolved": [[], [], []]})
        self.assertEqual(fields["init"], "fpga (experimental)")
        self.assertEqual(fields["init_list"]["unresolved"], 3)
        self.assertEqual(init_list.signature(fields), ("fpga", init_list.parse(self.list).sha256))

    def test_runs_from_different_start_states_never_share_an_out_dir(self):
        fields = init_list.result_fields(init_list.parse(self.list), {"set": 3})
        zero_run = self.dir / "zero"
        (zero_run / "Agnus" / "x").mkdir(parents=True)
        (zero_run / "Agnus" / "x" / "result.json").write_text('{"class": "PASS"}')
        claim_out_dir(zero_run, None)
        with self.assertRaises(SystemExit):
            claim_out_dir(zero_run, fields)
        fpga_run = self.dir / "fpga"
        fpga_run.mkdir()
        claim_out_dir(fpga_run, fields)   # empty: takes any start state and is marked
        self.assertTrue((fpga_run / "init.json").is_file())
        claim_out_dir(fpga_run, fields)   # resumes with the same list
        with self.assertRaises(SystemExit):
            claim_out_dir(fpga_run, None)
        other = dict(fields, init_list=dict(fields["init_list"], sha256="0" * 64))
        with self.assertRaises(SystemExit):
            claim_out_dir(fpga_run, other)


if __name__ == "__main__":
    unittest.main()
