"""Clocks for ORFS Flow Runner: clock ports from the top module's RTL, and the clocks an SDC defines.

The RTL side reads the top module's ports (ANSI or non-ANSI Verilog/SystemVerilog) and marks the inputs that
act as clocks. The SDC side runs the file in tclsh with the SDC commands replaced by stubs that record what
they define, so variables, expr and foreach in the SDC work as they do in OpenROAD.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

BEGIN = "# >>> ORFS Flow Runner: clocks. Set on the Clocks page; the lines up to the end marker are rewritten."
END = "# <<< ORFS Flow Runner: clocks"
RELATIONS = {"asynchronous": "Asynchronous", "logically_exclusive": "Logically exclusive",
             "physically_exclusive": "Physically exclusive"}
CLOCK_NAME = re.compile(r"cl(?:oc)?k", re.I)
NOT_CLOCK = re.compile(r"(?:^|_)(?:en|enable|sel|select|gate|gated|div|cnt|count|mux|stop|req|ack|valid|out)(?:_|$)",
                       re.I)
RESET_NAME = re.compile(r"(?:^|_)(?:rst|reset|rstn|resetn)", re.I)

# Tcl that defines the SDC commands as stubs and reports what the SDC defines, one tab-separated line each.
READER = r"""
set ::PORTS {}
set ::CLOCKS {}
set f [open [lindex $argv 1]]
foreach ln [split [read $f] \n] {
    if {[llength $ln] == 2} { lassign $ln d n; lappend ::PORTS $n; set ::DIR($n) $d }
}
close $f
proc ::line {} {
    for {set i [expr {[info frame] - 1}]} {$i > 0} {incr i -1} {
        set fr [info frame $i]
        if {[dict get $fr type] eq "source" && [dict exists $fr line] && [dict exists $fr file]
            && [file tail [dict get $fr file]] eq "in.sdc"} { return [dict get $fr line] }
    }
    return 0
}
proc ::flat {l} {
    set out {}
    foreach x $l { if {[llength $x] > 1} { lappend out {*}[::flat $x] } else { lappend out $x } }
    return $out
}
proc ::names {objs} { return [lmap o [::flat $objs] {regsub {^(port|clock|pin|net):} $o {}}] }
proc ::opts {spec argl} {
    # Split args into {option value ...} (options in spec take a value) and the remaining words.
    set o [dict create]; set rest {}
    for {set i 0} {$i < [llength $argl]} {incr i} {
        set a [lindex $argl $i]
        if {[string index $a 0] eq "-" && ![string is double -strict $a]} {
            if {$a in $spec} { dict lappend o $a [lindex $argl [incr i]] } else { dict set o $a 1 }
        } else { lappend rest $a }
    }
    return [list $o $rest]
}
proc ::match {pats pool kind} {
    set out {}
    foreach p [::names $pats] {
        set hit 0
        foreach n $pool { if {[string match $p $n]} { lappend out $kind:$n; set hit 1 } }
        if {!$hit} { lappend out $kind:$p }
    }
    return $out
}
proc get_ports {args} { lassign [::opts {-of_objects} $args] o rest; return [::match $rest $::PORTS port] }
proc get_clocks {args} { lassign [::opts {-of_objects} $args] o rest; return [::match $rest $::CLOCKS clock] }
proc all_clocks {} { return [lmap c $::CLOCKS {string cat clock: $c}] }
proc all_inputs {args} {
    set out {}
    foreach n $::PORTS {
        if {$::DIR($n) ni {input inout}} continue
        if {"-no_clocks" in $args && [info exists ::CLKPORT($n)]} continue
        lappend out port:$n
    }
    return $out
}
proc all_outputs {args} {
    set out {}
    foreach n $::PORTS { if {$::DIR($n) in {output inout}} { lappend out port:$n } }
    return $out
}
proc get_full_name {o} { return [regsub {^(port|clock|pin|net):} $o {}] }
proc get_name {o} { return [get_full_name $o] }
proc get_property {o p} { return "" }
proc current_design {args} { return "" }
proc create_clock {args} {
    lassign [::opts {-name -period -waveform -comment} $args] o rest
    set ports [::names $rest]
    set name [expr {[dict exists $o -name] ? [lindex [dict get $o -name] 0] : [lindex $ports 0]}]
    set period [expr {[dict exists $o -period] ? [lindex [dict get $o -period] 0] : ""}]
    if {$name ni $::CLOCKS} { lappend ::CLOCKS $name }
    foreach p $ports { set ::CLKPORT($p) 1 }
    puts "CLOCK\t$name\t$period\t[join $ports ,]\t[::line]"
    return clock:$name
}
proc create_generated_clock {args} {
    lassign [::opts {-name -source -master_clock -divide_by -multiply_by -edges -edge_shift -duty_cycle -comment} \
             $args] o rest
    set ports [::names $rest]
    set name [expr {[dict exists $o -name] ? [lindex [dict get $o -name] 0] : [lindex $ports 0]}]
    set master [expr {[dict exists $o -master_clock] ? [::names [dict get $o -master_clock]] : ""}]
    set src [expr {[dict exists $o -source] ? [::names [dict get $o -source]] : ""}]
    set div [expr {[dict exists $o -divide_by] ? [dict get $o -divide_by] : ""}]
    set mul [expr {[dict exists $o -multiply_by] ? [dict get $o -multiply_by] : ""}]
    if {$name ni $::CLOCKS} { lappend ::CLOCKS $name }
    puts "GENCLOCK\t$name\t$master\t[join $src ,]\t$div\t$mul\t[join $ports ,]\t[::line]"
    return clock:$name
}
proc set_clock_groups {args} {
    lassign [::opts {-name -group -comment} $args] o rest
    set kind asynchronous
    foreach k {-logically_exclusive -physically_exclusive} {
        if {[dict exists $o $k]} { set kind [string range $k 1 end] }
    }
    set groups [lmap g [expr {[dict exists $o -group] ? [dict get $o -group] : {}}] {join [::names $g] ,}]
    puts "GROUPS\t$kind\t[join $groups |]\t[::line]"
}
proc set_input_delay {args} {
    lassign [::opts {-clock -reference_pin} $args] o rest
    puts "DELAY\tinput\t[expr {[dict exists $o -clock] ? [::names [dict get $o -clock]] : {}}]\t[::line]"
}
proc set_output_delay {args} {
    lassign [::opts {-clock -reference_pin} $args] o rest
    puts "DELAY\toutput\t[expr {[dict exists $o -clock] ? [::names [dict get $o -clock]] : {}}]\t[::line]"
}
rename unknown ::orig_unknown
proc unknown {args} { return "" }
if {[catch {source [lindex $argv 0]} err]} { puts "ERROR\t[string map {\n { }} $err]\t[::line]" }
"""


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def find_top(files, top):
    """The file among the design's sources that defines module `top`."""
    rx = re.compile(r"^\s*module\s+" + re.escape(top) + r"\b", re.M)
    for f in files:
        p = Path(f)
        if p.suffix.lower() in (".v", ".sv", ".vh", ".svh") and p.is_file():
            try:
                if rx.search(p.read_text(errors="replace")):
                    return p
            except OSError:
                continue
    return None


def _balanced(text, i):
    """Index just past the parenthesised group that starts at text[i] == '('."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def _split_top(s):
    """Split on commas that are not inside (), [] or {}."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _decl_names(decl):
    """Names declared by 'logic [7:0] a, b' or 'my_t x [3]' (the identifier before any unpacked dimension)."""
    decl = re.sub(r"\[[^\]]*\]", " ", decl.split("=")[0])
    words = re.findall(r"[A-Za-z_\\][\w$\\.]*", decl)
    return words[-1:] if words else []


def module_text(text, top):
    """The top module's text from 'module top' to its 'endmodule', comments removed."""
    text = strip_comments(text)
    m = re.search(r"^\s*module\s+" + re.escape(top) + r"\b", text, re.M)
    if not m:
        return ""
    end = re.search(r"\bendmodule\b", text[m.end():])
    return text[m.start(): m.end() + end.end() if end else len(text)]


def rtl_ports(mod):
    """[(direction, name)] of a module's text, ANSI or non-ANSI style."""
    m = re.match(r"\s*module\s+\w+\s*(?:import\b[^;]*;\s*)*", mod)
    i = m.end() if m else 0
    if mod[i:i + 1] == "#":
        i = _balanced(mod, mod.index("(", i))
    j = mod.find("(", i)
    if j < 0 or mod[i:j].strip():
        return []
    header = mod[j + 1:_balanced(mod, j) - 1]
    ports, direction = [], None
    items = _split_top(header)
    ansi = any(re.match(r"(input|output|inout)\b", it) for it in items)
    if ansi:
        for it in items:
            d = re.match(r"(input|output|inout)\b\s*(.*)", it, re.S)
            if d:
                direction, it = d.group(1), d.group(2)
            if direction:
                ports += [(direction, n) for n in _decl_names(it)]
        return ports
    # Non-ANSI: names in the header, directions in the body.
    names = [re.sub(r"\W", "", it) for it in items]
    body = mod[_balanced(mod, j):]
    dirs = {}
    for d, rest in re.findall(r"\b(input|output|inout)\b([^;]*);", body):
        rest = re.sub(r"\[[^\]]*\]", " ", rest)
        for part in rest.split(","):
            words = re.findall(r"[A-Za-z_][\w$]*", part)
            if words:
                dirs[words[-1]] = d
    return [(dirs.get(n, "input"), n) for n in names if n]


def rtl_clocks(mod, ports):
    """{port: evidence} for the inputs that look like clocks."""
    out = {}
    for d, n in ports:
        if d != "input":
            continue
        q = re.escape(n)
        edge = re.search(r"@\s*\(\s*(?:posedge|negedge)\s+" + q + r"\b", mod)
        conn = re.search(r"\.(\w+)\s*\(\s*" + q + r"\s*\)", mod)
        conn = conn if conn and CLOCK_NAME.search(conn.group(1)) and not NOT_CLOCK.search(conn.group(1)) else None
        named = CLOCK_NAME.search(n) and not NOT_CLOCK.search(n)
        if RESET_NAME.search(n) and not named:
            continue
        if edge:
            out[n] = f"{edge.group(0).split('(')[1].split()[0]} {n} in the module"
        elif conn:
            out[n] = f"drives .{conn.group(1)}() of an instance"
        elif named:
            out[n] = "named like a clock"
    return out


def read_rtl(files, top):
    """(file, ports, clocks) for the top module, or (None, [], {})."""
    path = find_top(files, top)
    if not path:
        return None, [], {}
    mod = module_text(path.read_text(errors="replace"), top)
    ports = rtl_ports(mod)
    return path, ports, rtl_clocks(mod, ports)


def read_sdc(text, ports, env=None):
    """What an SDC defines, by running it in tclsh with stub commands.

    Returns {"clocks": [...], "generated": [...], "groups": [...], "delays": [...], "error": str or None}.
    """
    tclsh = shutil.which("tclsh") or shutil.which("tclsh8.6")
    res = {"clocks": [], "generated": [], "groups": [], "delays": [], "error": None}
    if not tclsh:
        res["error"] = "tclsh was not found (sudo apt install tcl), so the clocks in the SDC can't be read."
        return res
    with tempfile.TemporaryDirectory() as tmp:
        sdc, pf, rd = Path(tmp) / "in.sdc", Path(tmp) / "ports.txt", Path(tmp) / "reader.tcl"
        sdc.write_text(text)
        pf.write_text("\n".join(f"{d} {n}" for d, n in ports))
        rd.write_text(READER)
        try:
            out = subprocess.run([tclsh, str(rd), str(sdc), str(pf)], capture_output=True, text=True, timeout=20,
                                 env=dict(os.environ, **(env or {})))
        except (OSError, subprocess.TimeoutExpired) as e:
            res["error"] = str(e)
            return res
    for line in out.stdout.splitlines():
        f = line.split("\t")
        if f[0] == "CLOCK" and len(f) == 5:
            res["clocks"].append({"name": f[1], "period": f[2], "ports": [p for p in f[3].split(",") if p],
                                  "line": int(f[4] or 0)})
        elif f[0] == "GENCLOCK" and len(f) == 8:
            res["generated"].append({"name": f[1], "master": f[2], "source": f[3], "divide_by": f[4],
                                     "multiply_by": f[5], "ports": [p for p in f[6].split(",") if p],
                                     "line": int(f[7] or 0)})
        elif f[0] == "GROUPS" and len(f) == 4:
            res["groups"].append({"kind": f[1], "groups": [g.split(",") for g in f[2].split("|") if g],
                                  "line": int(f[3] or 0)})
        elif f[0] == "DELAY" and len(f) == 4:
            res["delays"].append({"kind": f[1], "clock": f[2], "line": int(f[3] or 0)})
        elif f[0] == "ERROR":
            res["error"] = f"line {f[2] if len(f) > 2 else '?'}: {f[1]}"
    if out.returncode and not res["error"]:
        res["error"] = (out.stderr.strip().splitlines() or ["tclsh failed"])[-1]
    return res


def relation(groups, a, b):
    """How clocks a and b are related by set_clock_groups: (kind, line) or (None, None)."""
    for g in groups:
        ia = next((i for i, grp in enumerate(g["groups"]) if a in grp), None)
        ib = next((i for i, grp in enumerate(g["groups"]) if b in grp), None)
        if ia is not None and ib is not None and ia != ib:
            return g["kind"], g["line"]
        # A single -group means: unrelated to every clock outside it.
        if len(g["groups"]) == 1 and (ia is None) != (ib is None):
            return g["kind"], g["line"]
    return None, None


def block_span(lines):
    """(first, last) line indexes of the managed block, or None."""
    try:
        first = next(i for i, ln in enumerate(lines) if ln.strip() == BEGIN)
        last = next(i for i in range(first, len(lines)) if lines[i].strip() == END)
        return first, last
    except StopIteration:
        return None


def render_block(clocks, relations):
    """The managed block for [{name, period, port}] and {(a, b): kind}."""
    lines = [BEGIN]
    for c in clocks:
        port = f" [get_ports {{{c['port']}}}]" if c.get("port") else ""
        lines.append(f"create_clock -name {c['name']} -period {c['period']}{port}")
    for (a, b), kind in sorted(relations.items()):
        lines.append(f"set_clock_groups -name {a}__{b} -{kind} -group [get_clocks {{{a}}}] "
                     f"-group [get_clocks {{{b}}}]")
    lines.append(END)
    return lines


def set_block(text, clocks, relations):
    """Replace or add the managed block; it goes after the file's last clock definition."""
    lines = text.split("\n")
    span = block_span(lines)
    new = render_block(clocks, relations) if (clocks or relations) else []
    if span:
        lines[span[0]:span[1] + 1] = new
        return "\n".join(lines)
    if not new:
        return text
    code = [i for i, ln in enumerate(lines) if not ln.lstrip().startswith("#")]
    defs = [i for i in code if re.match(r"\s*create_(generated_)?clock\b", lines[i])]
    if defs:
        at = defs[-1] + 1
        while at < len(lines) and lines[at - 1].rstrip().endswith("\\"):
            at += 1
    else:
        uses = [i for i in code if re.match(r"\s*(set_(input|output)_delay|set_clock_|set_false_path|set_max_delay|"
                                            r"set_min_delay|set_multicycle_path)\b", lines[i])]
        at = uses[0] if uses else len(lines)
        while at > 0 and at == len(lines) and not lines[at - 1].strip():
            at -= 1
    return "\n".join(lines[:at] + [""] + new + [""] + lines[at:])


def managed(text):
    """Clocks and relations in the managed block, as the page last wrote them."""
    lines = text.split("\n")
    span = block_span(lines)
    clocks, rels = [], {}
    if not span:
        return clocks, rels
    for ln in lines[span[0] + 1:span[1]]:
        m = re.match(r"create_clock -name (\S+) -period (\S+)(?: \[get_ports \{(.*)\}\])?$", ln.strip())
        if m:
            clocks.append({"name": m.group(1), "period": m.group(2), "port": m.group(3) or ""})
        m = re.match(r"set_clock_groups -name \S+ -(\w+) -group \[get_clocks \{(.*?)\}\] -group \[get_clocks "
                     r"\{(.*?)\}\]$", ln.strip())
        if m:
            rels[(m.group(2), m.group(3))] = m.group(1)
    return clocks, rels


def period_source(text, line_no):
    """How a create_clock line sets its period: ("var", name), ("literal", value) or (None, None)."""
    lines = text.split("\n")
    if not 0 < line_no <= len(lines):
        return None, None
    ln, i = lines[line_no - 1], line_no
    while ln.rstrip().endswith("\\") and i < len(lines):
        ln = ln.rstrip()[:-1] + " " + lines[i]
        i += 1
    m = re.search(r"-period\s+(\$\{?(\w+)\}?|-?\d+(?:\.\d+)?)", ln)
    if not m:
        return None, None
    return ("var", m.group(2)) if m.group(2) else ("literal", m.group(1))
