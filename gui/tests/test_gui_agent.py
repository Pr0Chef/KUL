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
h.done()
