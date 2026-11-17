"""The assistant panel, off screen, with a scripted stand-in for the Claude API (no key or network needed).

The fake client plays back turns: each turn is the text Claude "says" and the tools it calls. The test checks
that reads run at once, that an edit waits for approval and lands on disk, that a denied edit does not, that a
run tool (lint) waits for its result, and that bad tool input goes back to Claude as an error.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402

import agent  # noqa: E402
import workspace as ws  # noqa: E402

COUNTER = """module cnt (input clk, input rst, output reg [3:0] q);
  always @(posedge clk) if (rst) q <= 0; else q <= q + 1;
endmodule
"""


class FakeStream:
    def __init__(self, turn):
        self.turn = turn

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        yield NS(type="thinking", thinking="Planning. ")
        for word in self.turn["text"].split(" "):
            yield NS(type="text", text=word + " ")

    def get_final_message(self):
        blocks = [NS(type="text", text=self.turn["text"])]
        for i, (name, args) in enumerate(self.turn.get("tools", [])):
            blocks.append(NS(type="tool_use", id=f"t{FakeClient.n}_{i}", name=name, input=args))
        stop = "tool_use" if self.turn.get("tools") else "end_turn"
        return NS(content=blocks, stop_reason=stop)


class FakeClient:
    """Stands in for anthropic.Anthropic(): client.beta.messages.stream(**request) plays the next turn."""
    n = 0

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **request):
        FakeClient.n += 1
        self.requests.append({**request, "messages": list(request["messages"])})
        return FakeStream(self.turns.pop(0) if self.turns else {"text": "Done."})


def tool_results(client, i):
    """The tool_result blocks sent with request i."""
    last = client.requests[i]["messages"][-1]
    return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


area = Path(tempfile.mkdtemp(prefix="agent-wa-")) / "area"
ws.create_work_area(area, hn.KUL_FLOW)
proj = ws.new_project(area, "cnt", "cnt")
(proj.dir / "cnt.v").write_text(COUNTER)

import os  # noqa: E402
h = hn.Harness(area, shots=os.environ.get("SHOTS"))
app_size = h.app.resize(1440, 900)
app = h.app
agent.load_sdk = lambda: NS()           # the SDK and a key are "there"; the fake client does the talking
agent.has_credentials = lambda key="": True
app.set_project("cnt")
h.pump(0.5)
panel = app.assistant
panel.update_setup()
app.assistant_btn.set_active(True)
h.pump(0.5)
h.check("assistant panel shows", panel.widget.get_visible())
h.check("welcome suggestions shown", len(panel.welcome) == 2)

# 1. reads, an approved edit, then lint (a run tool, approved for the chat)
new_text = COUNTER.replace("q + 1", "q + 4'd1")
client = FakeClient([
    {"text": "Let me look.", "tools": [("get_overview", {}), ("read_file", {"path": "cnt.v"})]},
    {"text": "I will make the increment width explicit.",
     "tools": [("edit_file", {"path": "cnt.v", "old_text": "q + 1", "new_text": "q + 4'd1"})]},
    {"text": "Now lint.", "tools": [("run_lint", {})]},
    {"text": "The counter is **clean**."},
])
panel.agent.client = client
approvals, handled = [], set()


def approve_when_asked(answer):
    def tick():
        cards = [c for c in panel.list.get_children() if "approval" in c.get_style_context().list_classes()]
        for card in cards:
            if card in handled:
                continue
            handled.add(card)
            approvals.append(card)
            row = card.get_children()[-1]
            button = next(b for b in row.get_children() if getattr(b, "get_label", lambda: "")() == answer)
            button.clicked()
        return panel.busy()
    return tick


hn.GLib.timeout_add(150, approve_when_asked("Allow"))
panel.on_send("Check my counter")
h.check("chat finishes", h.wait(lambda: not panel.busy(), 120))
h.pump(0.5)
h.check("edit landed on disk", (proj.dir / "cnt.v").read_text() == new_text, (proj.dir / "cnt.v").read_text())
h.check("asked twice (edit, lint)", len(approvals) == 2, len(approvals))
reads = tool_results(client, 1)
h.check("overview and file read went back", len(reads) == 2 and "cnt" in reads[0]["content"]
        and "q + 1" in reads[1]["content"], reads)
lint_result = tool_results(client, 3)
h.check("lint result went back", lint_result and "took" in lint_result[0]["content"]
        and not lint_result[0]["is_error"], lint_result)
h.check("app state sent with the message", "<app_state>" in str(client.requests[0]["messages"][0]["content"]))
h.check("request uses opus 5.5, adaptive thinking, eager tools",
        client.requests[0]["model"] == "claude-opus-5-5"
        and client.requests[0]["thinking"]["type"] == "adaptive"
        and all(t["eager_input_streaming"] for t in client.requests[0]["tools"]))
app.show_section("rtl")
h.pump(0.5)
h.shot("assistant_chat")
h.check("reply rendered with bold", "<b>clean</b>" in (panel.reply.get_label() if panel.reply else ""))

# 2. a denied write and bad tool input
client2 = FakeClient([
    {"text": "Writing.", "tools": [("write_file", {"path": "evil.v", "content": "x"}),
                                   ("read_file", {"pth": "cnt.v"})]},
    {"text": "Understood."},
])
panel.agent.client = client2
approvals.clear()
panel.agent.allowed.clear()
hn.GLib.timeout_add(150, approve_when_asked("Deny"))
panel.on_send("Write a file")
h.check("second chat finishes", h.wait(lambda: not panel.busy(), 60))
h.shot("assistant_denied")
res = tool_results(client2, 1)
h.check("denied write not on disk", not (proj.dir / "evil.v").exists())
h.check("denial and bad input reported as errors", len(res) == 2 and all(r["is_error"] for r in res)
        and "denied" in res[0]["content"] and "INVALID_JSON" in res[1]["content"], res)

# 3. a path outside the project is refused without asking
client3 = FakeClient([{"text": "x", "tools": [("read_file", {"path": "../../../../etc/passwd"})]},
                      {"text": "ok"}])
panel.agent.client = client3
panel.on_send("read passwd")
h.wait(lambda: not panel.busy(), 30)
res = tool_results(client3, 1)
h.check("escape outside the work area refused", res and res[0]["is_error"] and "outside" in res[0]["content"], res)

panel.on_new_chat()
h.check("new chat clears history", panel.agent.messages == [])

# 4. the newer tools: plan checklist, a question, a waveform, a diagram it looks at, undo, notes
VCD = """$timescale 1ns $end
$scope module cnt_tb $end
$var wire 1 ! clk $end
$var wire 4 " q [3:0] $end
$upscope $end
$enddefinitions $end
#0
0!
b0000 "
#5
1!
b0001 "
#10
0!
#15
1!
b0010 "
"""
wave = proj.dir / "waves.vcd"
wave.write_text(VCD)
app.section_pages["verify"].wave_file = wave
before = (proj.dir / "cnt.v").read_text()
client4 = FakeClient([
    {"text": "Plan first.", "tools": [("update_tasks", {"tasks": [{"text": "Read the waveform", "status": "doing"},
                                                                  {"text": "Fix the RTL", "status": "pending"}]}),
                                      ("read_waveform", {"signals": ["q", "clk"], "from_time": 0, "to_time": 20})]},
    {"text": "Which width?", "tools": [("ask_user", {"question": "Widen the counter?", "options": ["8 bits",
                                                                                                   "Keep 4"]})]},
    {"text": "Widening.", "tools": [("edit_file", {"path": "cnt.v", "old_text": "reg [3:0] q",
                                                   "new_text": "reg [7:0] q"}),
                                    ("remember", {"note": "Counters in this project are 8 bits wide."})]},
    {"text": "Drawing.", "tools": [("draw_diagram", {"name": "blocks", "shapes": [
        {"id": "a", "kind": "register", "label": "q", "x": 0, "y": 0},
        {"id": "b", "kind": "adder", "x": 220, "y": 0}], "edges": [
        {"src": "b", "dst": "a", "style": "curved", "head": "open", "bus": 8}]}),
        ("look", {"what": "diagram"})]},
    {"text": "Done: **8 bits** now."},
])
panel.agent.client = client4
panel.mode_cb.set_active_id("edits")        # edits run without asking; the question still waits for the user


def answer_question():
    for card in panel.list.get_children():
        if "question" in card.get_style_context().list_classes():
            flow = card.get_children()[1]
            if isinstance(flow, hn.Gtk.FlowBox):
                flow.get_children()[0].get_child().clicked()
                return False
    return panel.busy()


hn.GLib.timeout_add(200, answer_question)
panel.on_send("The counter wraps too early")
h.check("chat with the newer tools finishes", h.wait(lambda: not panel.busy(), 60))
first = client4.requests[0]["messages"][0]["content"]
h.check("a new chat starts with the project overview", "Project overview" in str(first) and '"name": "cnt"' in str(first))
res = tool_results(client4, 1)
h.check("waveform values at each change", res and "0  0  0" in res[1]["content"] and "15  2  1" in res[1]["content"],
        res[1]["content"] if len(res) > 1 else res)
h.check("the plan shows as a checklist", panel.tasks_card is not None and "0 of 2 done" in
        panel.tasks_card.get_children()[0].get_text())
h.check("the user's answer went back", "The user answered: 8 bits" in tool_results(client4, 2)[0]["content"])
h.check("edit ran without asking in 'edit without asking' mode", "reg [7:0] q" in (proj.dir / "cnt.v").read_text())
h.check("note kept in the project", "8 bits wide" in app.project["notes"])
look = tool_results(client4, 4)
h.check("the assistant sees the diagram as a picture", isinstance(look[1]["content"], list)
        and look[1]["content"][1]["type"] == "image" and len(look[1]["content"][1]["source"]["data"]) > 1000, look)
d = app.section_pages["diagram"].d
h.check("diagram with new shape kinds and arrow styles", [s["kind"] for s in d.shapes] == ["register", "adder"]
        and d.edges[0]["style"] == "curved" and d.edges[0]["head"] == "open")
undo_rows = [row for row in panel.tool_rows.values()
             if any(getattr(w, "get_label", lambda: "")() == "Undo" for w in row[2].get_children())]
h.check("the edit has an Undo button", len(undo_rows) == 1)
h.shot("assistant_tools")
next(w for w in undo_rows[0][2].get_children() if getattr(w, "get_label", lambda: "")() == "Undo").clicked()
h.check("Undo puts the file back", (proj.dir / "cnt.v").read_text() == before)
h.check("the mode survives a new chat", (panel.on_new_chat() or True) and panel.agent.allowed == {"edit"})
panel.mode_cb.set_active_id("ask")
h.check("ask mode asks again", panel.agent.allowed == set())

# 5. suggestions from the state of the project
rtl = app.section_pages["rtl"]
rtl.sets["cdc"] = [{"source": "cdc", "severity": "error", "code": "CDC-NOSYNC", "file": "cnt.v", "line": 2,
                    "message": "a (clk_a) to b (clk_b): no synchronizer"}]
app.section_pages["verify"].results["tests"] = {"cnt_tb": {"state": "fail", "why": "FAIL q=3", "test": "cnt_tb"}}
panel.update_chips()
labels = [c.get_child().get_label() for c in panel.chips.get_children()]
h.check("suggestions follow the project's problems", labels[:2] == ["Fix 1 CDC error", "Debug cnt_tb"], labels)
client5 = FakeClient([{"text": "Looking."}])
panel.agent.client = client5
rtl.shown = rtl.sets["cdc"]
rtl.pstore.clear()
rtl.pstore.append(["error", "cnt.v", 2, "m", "#000", "cdc", 0])
rtl.ptv.get_selection().select_path(0)
rtl.on_ask()
h.wait(lambda: not panel.busy(), 30)
h.check("Ask assistant on a finding sends it", "CDC-NOSYNC" in str(client5.requests[0]["messages"][0]["content"]))
h.done()
