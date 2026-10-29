"""Offline tests for search.py: fake embedder, tiny generated PDF, no network, no API key."""
import hashlib, re
import numpy as np
import pytest
import search


def _fake_embed(texts):
    """Hashed bag of words into 256 dims, unit-normalised: deterministic and offline."""
    v = np.zeros((len(texts), 256), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            v[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def _pdf(path, text):
    """Minimal one-page PDF with a text line (no reportlab needed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


@pytest.fixture
def matter(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_DB", str(tmp_path / "search.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(search, "_embed", _fake_embed)
    out = tmp_path / "M99999_1"
    (out / "Key Documents").mkdir(parents=True)
    _pdf(out / "Key Documents" / "01 Board Order (11111).pdf",
         "The Board approves the water main replacement capital expenditure of 5 million dollars")
    (out / "Key Documents" / "02 Letter (22222).txt").write_text("Intervenor letter about hearing schedule dates")
    return {"matter": "M99999",
            "meta": {"matter": "M99999", "title": "Test Utility - Pump Station", "type": "Capital Expenditure Approvals",
                     "category": "Water", "status": "Closed"},
            "counts": {"Key Documents": 2},
            "docs": {"Key Documents": [
                {"path": "01 Board Order (11111).pdf", "title": "Board Order", "date": "01/02/2026", "doc_no": "11111"},
                {"path": "02 Letter (22222).txt", "title": "Letter", "date": "01/03/2026", "doc_no": "22222"}]}}, out


def test_index_count_and_idempotent(matter):
    result, out = matter
    assert search.index_matter(result, out) == 3  # metadata + pdf + txt
    assert search.index_matter(result, out) == 3
    con = search._db()
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 3
    con.close()


def test_search_ranks_relevant_chunk_first(matter):
    search.index_matter(*matter)
    top = search.search("water main replacement capital expenditure approves")[0]
    assert top["doc_no"] == "11111" and top["page"] == 1
    assert search.search("intervenor hearing schedule")[0]["doc_no"] == "22222"


def test_answer_without_key_returns_cited_excerpts(matter):
    search.index_matter(*matter)
    text = search.answer("water main replacement capital expenditure")
    assert text.startswith("[M99999 · 01 Board Order (11111) p.1]")
    assert "water main replacement" in text
