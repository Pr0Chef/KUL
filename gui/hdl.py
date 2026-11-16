"""Read the structure of Verilog and SystemVerilog sources, and write new ones from templates.

Not a full parser: it finds modules, their ports and parameters, and the instances inside them, which is what the
hierarchy view, the diagram generator, the testbench generator and the New design dialog need. Generate blocks,
interfaces and macros are read as far as their text allows.
"""

import re
from pathlib import Path

import sdc_clocks as sc

HDL_SUFFIXES = (".v", ".sv", ".vh", ".svh")
KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "logic", "assign", "always", "always_ff",
    "always_comb", "always_latch", "if", "else", "case", "casez", "casex", "endcase", "begin", "end", "for",
    "generate", "endgenerate", "genvar", "parameter", "localparam", "function", "endfunction", "task", "endtask",
    "initial", "typedef", "struct", "enum", "import", "package", "endpackage", "interface", "endinterface",
    "assert", "assume", "cover", "property", "return", "while", "repeat", "forever", "integer", "real", "bit",
    "signed", "unsigned", "posedge", "negedge", "or", "and", "not", "defparam", "specify", "primitive", "final",
    "unique", "priority", "default", "wait", "fork", "join", "disable", "modport", "class", "program", "time",
    "string", "void", "int", "byte", "shortint", "longint", "var", "automatic", "static", "const", "event",
}
MODULE_RE = re.compile(r"^[ \t]*module\s+([A-Za-z_][\w$]*)", re.M)
INSTANCE_RE = re.compile(r"(?<![\w$.`])([A-Za-z_][\w$]*)\s*(#\s*\()?")
CLOCK_NAME = re.compile(r"(^|_)(clk|clock|ck)(_|$|\d)", re.I)
RESET_NAME = re.compile(r"(^|_)(rst|reset|arst|srst)(_|$|n|\d)", re.I)


class Module:
    """One module: where it is, its ports, parameters and the instances inside it."""

    def __init__(self, name, file, line):
        self.name, self.file, self.line = name, Path(file), line
        self.ports = []       # (direction, name, bits) with bits as sdc_clocks.bits_of gives them
        self.params = []      # (name, default text)
        self.instances = []   # (module type, instance name, {port: connected expression})
        self.clocks = {}      # {input port: why it looks like a clock}
        self.ranges = {}      # {port: its packed range as written, such as "[WIDTH-1:0]"}
        self.assigns = []     # (names on the left, names on the right) of each continuous assignment

    def width_text(self, bits, name=None):
        """The range to declare a signal of this port with; parameterised ranges are kept as written."""
        if isinstance(bits, tuple):
            return f"[{bits[0]}:{bits[1]}] "
        if bits == "scalar":
            return ""
        return (self.ranges.get(name) or "/* width? */") + " "


def _params(text):
    """(name, default) of the parameters in a module header '#( ... )'."""
    out = []
    for item in sc._split_top(text):
        m = re.search(r"([A-Za-z_][\w$]*)\s*=\s*(.+)$", re.sub(r"^\s*(parameter|localparam)\b", "", item).strip(),
                      re.S)
        if m:
            out.append((m.group(1), " ".join(m.group(2).split())))
    return out


def _instances(body, defined):
    """Instances in a module body: (type, name, connections). A statement '<type> [#(...)] <name> ( ... );'."""
    out = []
    for stmt in re.split(r";", body):
        m = re.match(r"\s*(?:end\w*\s+|begin\s*(?::\s*\w+)?\s+|generate\s+|else\s+)*([A-Za-z_][\w$]*)\s*", stmt)
        if not m or m.group(1) in KEYWORDS:
            continue
        kind, i = m.group(1), m.end()
        if stmt[i:i + 1] == "#":
            j = stmt.find("(", i)
            if j < 0:
                continue
            i = sc._balanced(stmt, j)
        n = re.match(r"\s*([A-Za-z_][\w$]*)\s*(\[[^\]]*\])?\s*\(", stmt[i:])
        if not n or n.group(1) in KEYWORDS:
            continue
        # A known module, or something shaped like an instance of an unknown cell (a macro or library cell).
        start = i + n.end() - 1
        inside = stmt[start + 1:sc._balanced(stmt, start) - 1]
        named = re.findall(r"\.\s*([A-Za-z_][\w$]*)\s*\(", inside)
        if kind not in defined and not named:
            continue
        conns = {}
        for pm in re.finditer(r"\.\s*([A-Za-z_][\w$]*)\s*\(", inside):
            end = sc._balanced(inside, pm.end() - 1)
            conns[pm.group(1)] = " ".join(inside[pm.end():end - 1].split())
        out.append((kind, n.group(1), conns))
    return out


def _port_ranges(body):
    """{port: packed range text} from a module's header (ANSI) or its port declarations (non-ANSI)."""
    ranges = {}
    m = re.match(r"\s*module\s+\w+\s*(?:import\b[^;]*;\s*)*", body)
    i = m.end() if m else 0
    if body[i:i + 1] == "#":
        i = sc._balanced(body, body.index("(", i))
    j = body.find("(", i)
    if j < 0:
        return ranges
    current = None
    for item in sc._split_top(body[j + 1:sc._balanced(body, j) - 1]):
        if re.match(r"(input|output|inout)\b", item):
            current = None
        found = re.search(r"\[[^\]]*\]", item.split("=")[0])
        if found:
            current = found.group(0)
        names = sc._decl_names(item)
        if names and current:
            ranges[names[-1]] = current
    for _d, rest in re.findall(r"\b(input|output|inout)\b([^;]*);", body[sc._balanced(body, j):]):
        found = re.search(r"\[[^\]]*\]", rest)
        if found:
            for part in re.sub(r"\[[^\]]*\]", " ", rest).split(","):
                words = re.findall(r"[A-Za-z_][\w$]*", part)
                if words:
                    ranges[words[-1]] = found.group(0)
    return ranges


def names_in(expr):
    """Signal names an expression uses (ranges, numbers and keywords dropped)."""
    expr = re.sub(r"\[[^\]]*\]|\d*'[sS]?[bhdoBHDO][0-9a-fA-FxXzZ_?]+|\b\d+\b", " ", expr)
    return {w for w in re.findall(r"[A-Za-z_\\][\w$]*", expr) if w not in KEYWORDS}


def parse_text(text, file="<text>", defined=None):
    """[Module] for every module in one file's text."""
    clean = sc.strip_comments(text)
    names = [m.group(1) for m in MODULE_RE.finditer(clean)]
    known = set(defined or ()) | set(names)
    mods = []
    for m in MODULE_RE.finditer(clean):
        name = m.group(1)
        body = sc.module_text(clean, name)
        mod = Module(name, file, clean.count("\n", 0, m.start()) + 1)
        try:
            mod.ports = sc.port_decls(body)
            mod.ranges = _port_ranges(body)
        except (ValueError, IndexError):
            mod.ports = []
        head = re.match(r"\s*module\s+\w+\s*(?:import\b[^;]*;\s*)*#\s*\(", body)
        if head:
            mod.params = _params(body[head.end():sc._balanced(body, head.end() - 1) - 1])
        after = body.find(";")
        inner = body[after + 1:] if after >= 0 else ""
        have = {n for n, _ in mod.params}
        mod.params += [p for p in _params(",".join(re.findall(r"\bparameter\b([^;]*);", inner))) if p[0] not in have]
        mod.instances = _instances(inner, known)
        mod.assigns = [(names_in(lhs), names_in(rhs)) for lhs, rhs in
                       re.findall(r"\bassign\s+(.+?)\s*=\s*(.+?);", inner, re.S)]
        mod.clocks = sc.rtl_clocks(body, [(d, n) for d, n, _ in mod.ports])
        mods.append(mod)
    return mods


def parse_files(files):
    """{module name: Module} for all the files (first definition wins), reading each file once."""
    texts = {}
    for f in files:
        p = Path(f)
        if p.suffix.lower() in HDL_SUFFIXES and p.is_file():
            try:
                texts[p] = p.read_text(errors="replace")
            except OSError:
                pass
    defined = {m.group(1) for t in texts.values() for m in MODULE_RE.finditer(sc.strip_comments(t))}
    mods = {}
    for p, text in texts.items():
        for mod in parse_text(text, p, defined):
            mods.setdefault(mod.name, mod)
    return mods


def tops(mods):
    """Modules no other module instantiates, the one with the most instances and ports first."""
    used = {kind for m in mods.values() for kind, _, _ in m.instances}
    free = [m for m in mods.values() if m.name not in used]
    return [m.name for m in sorted(free, key=lambda m: (-len(m.instances), -len(m.ports), m.name))] or sorted(mods)


def is_testbench(mod):
    """A module with no ports that instantiates something, or one named like a testbench."""
    return (not mod.ports and bool(mod.instances)) or bool(re.search(r"(^tb_|_tb$|^test_|_test$|testbench)",
                                                                       mod.name, re.I))


def hierarchy(mods, top, _seen=()):
    """Nested instances under top: {"module", "instance", "known", "children": [...]}; recursion is cut."""
    mod = mods.get(top)
    node = {"module": top, "instance": top, "known": mod is not None, "children": []}
    if mod is None or top in _seen:
        return node
    for kind, name, _ in mod.instances:
        child = hierarchy(mods, kind, _seen + (top,))
        child["instance"] = name
        node["children"].append(child)
    return node


def clock_and_reset(mod):
    """(clock port or None, reset port or None, True if the reset is active low) guessed from the ports."""
    inputs = [n for d, n, _ in mod.ports if d == "input"]
    clock = next(iter(mod.clocks), None) or next((n for n in inputs if CLOCK_NAME.search(n)), None)
    reset = next((n for n in inputs if RESET_NAME.search(n) and n != clock), None)
    low = bool(reset and re.search(r"(_n|_ni|_b|_l|rstn|resetn)$|^n(rst|reset)", reset, re.I))
    return clock, reset, low


def testbench(mod, period=10, unit="ns", cycles=200):
    """A self-checking testbench skeleton for a module: clock, reset, VCD dump, a stimulus block to fill in and a
    PASS / FAIL line the app reads. Works in Icarus Verilog and Verilator (--binary --timing)."""
    clock, reset, low = clock_and_reset(mod)
    lines = [f"// Testbench for {mod.name}. Generated by ORFS Flow Runner; fill in the stimulus and the checks.",
             f"`timescale 1{unit}/1ps", "", f"module {mod.name}_tb;"]
    # The module's parameters with their defaults, so parameterised port widths resolve here too.
    lines += [f"  localparam {n} = {v};" for n, v in mod.params]
    for d, name, bits in mod.ports:
        kind = "reg " if d == "input" else "wire"
        lines.append(f"  {kind} {mod.width_text(bits, name)}{name};")
    lines += ["  integer errors = 0;", ""]
    if mod.params:
        lines.append(f"  {mod.name} #(")
        lines += [f"    .{n}({n})" + ("," if i < len(mod.params) - 1 else "") for i, (n, _v) in enumerate(mod.params)]
        lines.append("  ) dut (")
    else:
        lines.append(f"  {mod.name} dut (")
    lines += [f"    .{name}({name})" + ("," if i < len(mod.ports) - 1 else "")
              for i, (_, name, _) in enumerate(mod.ports)]
    lines += ["  );", ""]
    if clock:
        lines += [f"  // Clock: {period} {unit} period.", f"  initial {clock} = 1'b0;",
                  f"  always #({period} / 2.0) {clock} = ~{clock};", ""]
    lines += ["  initial begin", f'    $dumpfile("{mod.name}_tb.vcd");', f"    $dumpvars(0, {mod.name}_tb);"]
    for d, name, _ in mod.ports:
        if d == "input" and name not in (clock, reset):
            lines.append(f"    {name} = 0;")
    edge = f"@(posedge {clock})" if clock else f"#{period}"
    if reset:
        lines += [f"    {reset} = 1'b{0 if low else 1};", f"    repeat (4) {edge};",
                  f"    {reset} = 1'b{1 if low else 0};"]
    lines += [f"    {edge};", "", "    // ---- Stimulus and checks: drive the inputs, then compare the outputs. ----",
              "    // Example:",
              "    //   a = 8'd3; " + edge + ";",
              '    //   if (y !== 8\'d6) begin $display("FAIL: y=%0d", y); errors = errors + 1; end',
              f"    repeat ({cycles}) {edge};", "",
              '    if (errors == 0) $display("PASS");',
              '    else $display("FAIL: %0d errors", errors);',
              "    $finish;", "  end", "",
              "  // Stop a simulation that hangs.",
              f"  initial begin #({period} * {cycles * 50}); $display(\"FAIL: timeout\"); $finish; end",
              "endmodule", ""]
    return "\n".join(lines)


def formal_wrapper(mod):
    """A formal top for a module: it drives the module's inputs freely, instantiates it as dut, and has a place
    for assumptions, assertions and covers. SymbiYosys checks it with the wrapper as the top (no bind needed, so
    every reader accepts it)."""
    clock, reset, low = clock_and_reset(mod)
    active = f"!{reset}" if low else reset
    lines = [f"// Formal properties for {mod.name}. Generated by ORFS Flow Runner; replace the examples with the",
             "// design's real invariants. Checked by SymbiYosys from the Verify section (top: "
             f"{mod.name}_formal).", "",
             f"module {mod.name}_formal" + (" #(" if mod.params else " (")]
    if mod.params:
        lines += [f"  parameter {n} = {v}" + ("," if i < len(mod.params) - 1 else "")
                  for i, (n, v) in enumerate(mod.params)]
        lines.append(") (")
    inputs = [(n, b) for d, n, b in mod.ports if d == "input"]
    lines += [f"  input {mod.width_text(b, n)}{n}" + ("," if i < len(inputs) - 1 else "")
              for i, (n, b) in enumerate(inputs)]
    lines.append(");")
    lines += [f"  wire {mod.width_text(b, n)}{n};" for d, n, b in mod.ports if d != "input"]
    passed = " #(" + ", ".join(f".{n}({n})" for n, _ in mod.params) + ")" if mod.params else ""
    lines += ["", f"  {mod.name}{passed} dut ("]
    lines += [f"    .{n}({n})" + ("," if i < len(mod.ports) - 1 else "") for i, (_, n, _) in enumerate(mod.ports)]
    lines += ["  );", ""]
    if clock:
        lines += ["  reg past_valid = 1'b0;", f"  always @(posedge {clock}) past_valid <= 1'b1;", ""]
        if reset:
            lines += ["  // Start every proof in reset.", f"  always @(*) if (!past_valid) assume ({active});", ""]
        in_reset = active or "1'b0"
        lines += [f"  always @(posedge {clock}) begin", f"    if (past_valid && !({in_reset})) begin",
                  "      // Examples: a value that must always hold, and a state that must be reachable.",
                  "      // assert (count <= MAX);", "      // cover (done);", "    end", "  end"]
    else:
        lines += ["  always @* begin", "    // assert (y == a + b);", "  end"]
    lines += ["endmodule", ""]
    return "\n".join(lines)


# New-file templates of the RTL editor: label -> (default file name, text with {name} for the module name).
TEMPLATES = {
    "Empty module": ("{name}.v", """// {name}: describe what this module does.
module {name} (
  input  wire clk,
  input  wire rst_n,
  input  wire [7:0] a,
  output reg  [7:0] y
);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) y <= 8'd0;
    else        y <= a;
  end

endmodule
"""),
    "Counter": ("{name}.v", """// {name}: counts up when enabled, wraps at MAX.
module {name} #(
  parameter WIDTH = 8,
  parameter MAX = 255
) (
  input  wire clk,
  input  wire rst_n,
  input  wire en,
  output reg  [WIDTH-1:0] count,
  output wire wrap
);

  assign wrap = en && (count == MAX);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)    count <= {{WIDTH{{1'b0}}}};
    else if (wrap) count <= {{WIDTH{{1'b0}}}};
    else if (en)   count <= count + 1'b1;
  end

endmodule
"""),
    "State machine": ("{name}.v", """// {name}: a three-state machine (IDLE -> RUN -> DONE).
module {name} (
  input  wire clk,
  input  wire rst_n,
  input  wire start,
  input  wire finished,
  output wire busy,
  output wire done
);

  localparam IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2;
  reg [1:0] state, next_state;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) state <= IDLE;
    else        state <= next_state;
  end

  always @* begin
    next_state = state;
    case (state)
      IDLE: if (start)    next_state = RUN;
      RUN:  if (finished) next_state = DONE;
      DONE:               next_state = IDLE;
      default:            next_state = IDLE;
    endcase
  end

  assign busy = (state == RUN);
  assign done = (state == DONE);

endmodule
"""),
    "Synchronous FIFO": ("{name}.v", """// {name}: a synchronous FIFO with full and empty flags.
module {name} #(
  parameter WIDTH = 8,
  parameter DEPTH_LOG2 = 4
) (
  input  wire clk,
  input  wire rst_n,
  input  wire wr_en,
  input  wire [WIDTH-1:0] wr_data,
  input  wire rd_en,
  output wire [WIDTH-1:0] rd_data,
  output wire full,
  output wire empty
);

  reg [WIDTH-1:0] mem [0:(1 << DEPTH_LOG2) - 1];
  reg [DEPTH_LOG2:0] wr_ptr, rd_ptr;

  assign empty = (wr_ptr == rd_ptr);
  assign full  = (wr_ptr[DEPTH_LOG2] != rd_ptr[DEPTH_LOG2]) &&
                 (wr_ptr[DEPTH_LOG2-1:0] == rd_ptr[DEPTH_LOG2-1:0]);
  assign rd_data = mem[rd_ptr[DEPTH_LOG2-1:0]];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wr_ptr <= 0;
      rd_ptr <= 0;
    end else begin
      if (wr_en && !full) begin
        mem[wr_ptr[DEPTH_LOG2-1:0]] <= wr_data;
        wr_ptr <= wr_ptr + 1'b1;
      end
      if (rd_en && !empty) rd_ptr <= rd_ptr + 1'b1;
    end
  end

endmodule
"""),
    "Empty file": ("{name}.v", ""),
}


def template(label, name):
    """(file name, text) of a template for a module called name."""
    fname, text = TEMPLATES[label]
    return fname.format(name=name), text.format(name=name)
