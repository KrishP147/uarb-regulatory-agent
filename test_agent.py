from pathlib import Path

from agent import _count_phrase, compose, first_name, parse_regex, parse_request


def test_parse_spec_example():
    assert parse_regex("Hi Agent, Can you give me Other Documents files from M12205? Thanks!") == ("M12205", ["Other Documents"])


def test_parse_loose_and_multi():
    assert parse_regex("need the transcript for m12383 pls") == ("M12383", ["Transcripts"])
    assert parse_regex("exhibits and key docs on M12205") == ("M12205", ["Exhibits", "Key Documents"])
    assert parse_regex("all documents for M12205")[1] == ["Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings"]
    assert parse_regex("hello") == (None, [])


def test_thread_followup_without_claude(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # first email had the matter, the reply only names the type
    assert parse_request("can I get files from M12205?\n---\nExhibits please", "Exhibits please")[:2] == ("M12205", ["Exhibits"])


def test_count_phrase():
    c = {"Exhibits": 13, "Key Documents": 5, "Other Documents": 21, "Transcripts": 0, "Recordings": 0}
    assert _count_phrase(c) == "13 Exhibits, 5 Key Documents, 21 Other Documents, and no Transcripts or Recordings"
    assert _count_phrase({"Exhibits": 2, "Key Documents": 0}) == "2 Exhibits and no Key Documents"


def test_first_name():
    assert first_name('"Paula Prado" <paula.prado@senpilot.com>') == "Paula"
    assert first_name("paula.prado@senpilot.com") == "Paula"


def test_compose_spec_shape():
    docs = [{"path": Path(f"{i:02d} Doc.pdf"), "title": "Doc", "date": "04/11/2025", "doc_no": ""} for i in range(1, 11)]
    r = {"matter": "M12205", "doc_types": ["Other Documents"], "docs": {"Other Documents": docs}, "errors": [],
         "counts": {"Exhibits": 13, "Key Documents": 5, "Other Documents": 21, "Transcripts": 0, "Recordings": 0},
         "meta": {"title": "Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,270,000",
                  "type": "Capital Expenditure", "category": "Water", "date_received": "04/07/2025",
                  "decision_date": "10/23/2025", "status": "Closed", "outcome": "Directions given"}}
    body = compose("User", r, "They're attached as a ZIP (7.0 MB).")
    assert "initial filing on April 7, 2025 and a final filing on October 23, 2025" in body
    assert "I found 13 Exhibits, 5 Key Documents, 21 Other Documents, and no Transcripts or Recordings." in body
    assert "I downloaded 10 out of the 21 Other Documents (the first 10 listed). They're attached as a ZIP" in body
