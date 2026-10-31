"""Unit tests for the EXPERIMENTAL extractor of init lists from a Gowin netlist (loader/gowin_init.py),
on cut-down copies of what NanoMig's netlist and Verilator tree hold."""

from pathlib import Path
import tempfile
import unittest

from loader import gowin_init, init_list

# GowinSynthesis names a cell after the RTL register; an output register's Q net after the port's net
NETLIST = r"""//
//Written by GowinSynthesis
//Tool Version: V1.9.12.04(89079)
//Part Number: GW2AR-LV18QN88C8/I7
//file0 "\/src\/nanomig.v"
module busControl (
  clk,
  chip_rw
)
;
  DFFSE rRWn_s0 (
    .Q(chip_rw),
    .D(GND),
    .CLK(clk),
    .SET(n389_4)
);
  DFFRE rIdle_s0 (
    .Q(rIdle),
    .D(GND),
    .CLK(clk)
);
defparam rIdle_s0.INIT=1'b1;
  DFFE quiet_s0 (
    .Q(quiet),
    .D(GND),
    .CLK(clk)
);
endmodule
module ide (
  ide0_data
)
;
  DFFSE io_readdata_15_s0 (
    .Q(ide0_data[7]),
    .D(GND),
    .CLK(clk)
);
  DFFSE io_readdata_15_s1 (
    .Q(ide0_data_7_dup),
    .D(GND),
    .CLK(clk)
);
endmodule
module uart_port (
  txd
)
;
  DFFSE txd_d_s0 (
    .Q(txd),
    .D(GND),
    .CLK(clk)
);
endmodule
module amiga_clk (
  clk7_en
)
;
  DFFP clk7_en_reg_s0 (
    .Q(clk7_en),
    .D(n1),
    .CLK(clk)
);
  DFFP cck_s0 (
    .Q(cck_Z),
    .D(n2),
    .CLK(clk)
);
endmodule
module nanomig (
  clk
)
;
  DFFP reset_s_0_s0 (
    .Q(reset_s[0]),
    .D(GND),
    .CLK(clk)
);
  DFFP n51_s0 (
    .Q(n51),
    .D(GND),
    .CLK(clk)
);
  busControl busControl (
    .clk(clk)
);
  ide ide0 (
    .clk(clk)
);
  uart_port uart_port (
    .clk(clk)
);
  amiga_clk amiga_clk (
    .clk(clk)
);
endmodule
"""

PRIM = """module DFFE (Q, D, CLK, CE);
parameter INIT = 1'b0;
endmodule
module DFFRE (Q, D, CLK, CE, RESET);
parameter INIT = 1'b0;
endmodule
module DFFSE (Q, D, CLK, CE, SET);
parameter INIT = 1'b1;
endmodule
module DFFP (Q, D, CLK, PRESET);
parameter INIT = 1'b1;
endmodule
"""


def var(name, dtype):
    return {"type": "VAR", "name": name, "dtypep": dtype}


def module(addr, name, variables, cells=(), blocks=()):
    return {"type": "MODULE", "addr": addr, "name": name, "origName": name,
            "stmtsp": [var(v, t) for v, t in variables]
            + [{"type": "CELL", "name": c, "modp": m} for c, m in cells]
            + [{"type": "ALWAYS", "stmtsp": [{"type": "BEGIN", "name": b, "declsp": [var(v, t) for v, t in vs]}]}
               for b, vs in blocks]}


# What the harness elaborates (no uart_port: the harness builds without ENABLE_AT_MODEM)
TREE = {
    "modulesp": [
        module("(A)", "nanomig_tb", [], [("nanomig", "(B)")]),
        module("(B)", "nanomig", [("reset_d", "(T1)")],
               [("busControl", "(C)"), ("ide0", "(D)"), ("amiga_clk", "(E)")],
               blocks=[("unnamedblk1", [("reset_s", "(T8)")])]),
        module("(C)", "busControl", [("rRWn", "(T1)"), ("rIdle", "(T1)"), ("chip_rw", "(T1)")]),
        module("(D)", "ide", [("io_readdata", "(T16)"), ("ide0_data", "(T16)")]),
        module("(E)", "amiga_clk", [("clk7_en", "(T1)"), ("clk7_en_reg", "(T1)"), ("cck", "(T1)")]),
    ],
    "miscsp": [{"type": "TYPETABLE", "typesp": [
        {"type": "BASICDTYPE", "addr": "(T1)", "keyword": "logic"},
        {"type": "BASICDTYPE", "addr": "(T8)", "keyword": "logic", "range": "7:0"},
        {"type": "BASICDTYPE", "addr": "(T16)", "keyword": "logic", "range": "15:0"}]}],
}


class TestGowinInit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "nanomig.vg").write_text(NETLIST)
        lib = self.dir / "gowin" / "IDE" / "simlib" / "gw2a"
        lib.mkdir(parents=True)
        (lib / "prim_sim.v").write_text(PRIM)
        self.net = gowin_init.parse_netlist(self.dir / "nanomig.vg")
        self.prim = gowin_init.primitive_inits(self.dir / "gowin")
        self.hier = init_list.hierarchy(TREE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_netlist_header_and_the_start_values(self):
        self.assertEqual(self.net.tool, "1.9.12.04(89079)")
        self.assertEqual(self.net.part, "GW2AR-LV18QN88C8/I7")
        self.assertEqual(self.net.sources, ["/src/nanomig.v"])
        self.assertEqual(self.prim, {"DFFE": 0, "DFFRE": 0, "DFFSE": 1, "DFFP": 1})
        found, total, missing = gowin_init.ones(self.net, self.prim)
        self.assertEqual(total, 10)
        cells = [c for _, c, _ in found]
        self.assertIn("rIdle_s0", cells)        # a reset type with INIT=1 from its defparam
        self.assertNotIn("quiet_s0", cells)     # DFFE: starts at 0
        self.assertEqual(missing, {})

    def test_registers_come_from_the_cell_names_and_resolve_against_the_harness(self):
        found, _, _ = gowin_init.ones(self.net, self.prim)
        rows, skipped = gowin_init.make_list(found, self.hier)
        self.assertEqual(rows, [
            ("", "reset_s", "0"),                      # declared in a procedural block
            ("busControl", "rRWn", "-"),               # Q net named after the parent's net chip_rw
            ("busControl", "rIdle", "-"),
            ("ide0", "io_readdata", "15"),             # Q net ide0_data[7]: bytes swapped at the port; the s1 duplicate once
            ("amiga_clk", "clk7_en_reg", "-"),         # Q net clk7_en is a wire assigned from it
            ("amiga_clk", "cck", "-"),                 # Q net cck_Z
        ])
        self.assertEqual(skipped, [
            "- (core) n51_s0 (Q n51): no register n51 in module nanomig",
            "- uart_port txd_d_s0 (Q txd): instance not in the simulated design",
        ])

    def test_a_name_that_is_both_a_register_and_a_bit_is_not_guessed(self):
        tree = {**TREE, "modulesp": [*TREE["modulesp"][:3], module("(D)", "ide", [("io_readdata", "(T16)"), ("io_readdata_15", "(T1)")]), TREE["modulesp"][4]]}
        reg, why = gowin_init.register_of(init_list.hierarchy(tree), "ide0", "io_readdata_15_s0")
        self.assertIsNone(reg)
        self.assertEqual(why, "ambiguous: io_readdata_15 - or io_readdata 15")

    def test_an_encrypted_netlist_is_refused(self):
        (self.dir / "edu.vg").write_text("`pragma protect begin_protected\n...\n")
        with self.assertRaisesRegex(ValueError, "encrypted"):
            gowin_init.parse_netlist(self.dir / "edu.vg")

    def test_the_sources_are_compared_with_the_checkout(self):
        src = self.dir / "gowin-project" / "src"
        src.mkdir(parents=True)
        (src / "nanomig.v").write_text("module nanomig; endmodule\n")
        checkout = self.dir / "NanoMig"
        (checkout / "src").mkdir(parents=True)
        (checkout / "src" / "nanomig.v").write_text("module nanomig; endmodule\n")
        self.net.sources = [str(src / "nanomig.v")]
        self.assertEqual(gowin_init.source_check(self.net, checkout), (True, "the netlist's 1 source files equal the checkout's"))
        (checkout / "src" / "nanomig.v").write_text("module nanomig; wire x; endmodule\n")
        same, why = gowin_init.source_check(self.net, checkout)
        self.assertFalse(same)
        self.assertIn("src/nanomig.v", why)
        self.net.sources = ["/nowhere/src/nanomig.v"]
        self.assertTrue(gowin_init.source_check(self.net, checkout)[0])   # not readable here: not compared


if __name__ == "__main__":
    unittest.main()
