import importlib.util
import json
from pathlib import Path


def load():
    path = Path(__file__).parent.parent / "scripts" / "asks_report.py"
    spec = importlib.util.spec_from_file_location("asks_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROWS = [
    {"ts": "2026-11-04T10:56:24.029+04:00", "event": "ask_timing", "ask_id": "A", "listen_ms": 2383, "heard_ms": 2095,
     "posters_ms": 20494},
    {"ts": "2026-11-04T10:56:25.110+04:00", "event": "ask", "ask_id": "A", "source": "voice", "outcome": "shown",
     "question": "Something like the Expanse", "stt_ms": 937, "find_ms": 343, "think_ms": 11485, "claude_api_ms": 9100,
     "claude_turns": 3, "claude_total_ms": 19594, "upload_ms": 40},
    {"ts": "2026-11-04T10:57:00.000+04:00", "event": "loop_stall", "ms": 5200,
     "where": ["lumitv/a.py:1 outer", "lumitv/catalog.py:88 rank"]},
    {"ts": "2026-11-04T10:58:06.000+04:00", "event": "ask", "ask_id": "B", "source": "typed", "outcome": "shown",
     "question": "suits", "find_ms": 810, "claude_total_ms": 5000},
    {"ts": "2026-11-04T10:58:07.000+04:00", "event": "ask_timing", "ask_id": "C", "posters_ms": 1},  # no server line
]  # fmt: skip


def test_report_joins_tv_and_server_timings_per_ask_with_medians_and_stalls(tmp_path):
    m = load()
    log = tmp_path / "l.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in ROWS) + "\nnot json\n", encoding="utf-8")
    out = m.report(m.read(log, None), last=20)
    lines = out.splitlines()
    a = next(x for x in lines if x.startswith("10:56:25"))
    assert all(str(v) in a for v in (2383, 40, 937, 2095, 343, 11485, 9100, 19594, 20494))
    assert any(x.startswith("10:58:06") and "suits" in x for x in lines)
    assert not any(x.split()[:1] == ["C"] for x in lines)  # TV-only timings are not a row
    median = next(x for x in lines if x.startswith("median"))
    assert "n=2" in median and "576" in median  # find: median of 343 and 810 (576.5 rounds to even)
    assert "server stalls since 10:56:25: 1" in out and "5200 ms  at lumitv/catalog.py:88 rank" in out


def test_since_filters_by_time(tmp_path):
    m = load()
    log = tmp_path / "l.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in ROWS), encoding="utf-8")
    assert [a["ask_id"] for a in m.join(m.read(log, "2026-11-04T10:58"))] == ["B"]


def test_picks_report_lists_what_each_answer_showed_and_why():
    picks_report = load().picks_report

    rows = [
        {"ts": "2026-11-04T15:00:00", "event": "ask", "ask_id": "a1", "question": "something like Star Trek"},
        {
            "ts": "2026-11-04T15:00:09",
            "event": "tool_call",
            "tool": "show_on_tv",
            "status": "ok",
            "ask_id": "a1",
            "headline": "Three Trek-style picks",
            "subline": "Crew-in-space adventures",
            "picks": [
                {"title": "Star Trek: Picard", "service": "prime", "reason": "Trek itself"},
                {"title": "Away", "service": "netflix", "reason": None},
            ],
        },
        {
            "ts": "2026-11-04T15:05:00",
            "event": "tool_call",
            "tool": "find_titles",
            "turn_id": "t2",
            "question": "funny",
        },
        {
            "ts": "2026-11-04T15:05:04",
            "event": "tool_call",
            "tool": "show_on_tv",
            "status": "ok",
            "turn_id": "t2",
            "headline": "h",
            "subline": "",
            "picks": [{"title": "Seinfeld", "service": "netflix", "reason": "x"}],
        },
    ]
    out = picks_report(rows, 10)
    assert "[tv] “something like Star Trek”" in out and "Three Trek-style picks" in out
    assert "- Star Trek: Picard (prime): Trek itself" in out and "- Away (netflix): (no reason)" in out
    assert "[desktop] “funny”" in out and "- Seinfeld (netflix): x" in out


def test_target_line_reports_p50_and_p95_against_the_target():
    m = load()
    assert m.percentile([1, 2, 3, 4, 100], 0.5) == 3 and m.percentile([1, 2, 3, 4, 100], 0.95) == 100
    assert m.target_line([6000] * 19 + [14000]).endswith("-> met")
    assert "MISSED" in m.target_line([6000] * 10 + [20000])
    assert m.target_line([]) == "target: no asks with poster timings"
