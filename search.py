"""Local semantic search + RAG over scraped UARB matters.

python search.py index <folder-or-json>   index a saved matter (folder of PDFs and/or <matter>.json)
python search.py "question"               answer from the indexed filings, with citations
python search.py --matters                list indexed matters and chunk counts
"""
import json, os, re, sqlite3, sys
from pathlib import Path
import numpy as np

HERE = Path(__file__).parent
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
CHUNK, OVERLAP, MAX_PAGES = 800, 150, 30
_embedder = None


def _db() -> sqlite3.Connection:
    path = Path(os.environ.get("SEARCH_DB", HERE / "downloads" / "search.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY, matter TEXT, doc_type TEXT, title TEXT, doc_no TEXT,
        date TEXT, label TEXT, page INTEGER, text TEXT, emb BLOB)""")
    return con


def _embed(texts: list) -> np.ndarray:
    """Unit-normalised embeddings, one row per text (fastembed, local ONNX, no API key)."""
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(EMBED_MODEL)
    v = np.array(list(_embedder.embed(texts)), dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def _pages(path: Path) -> list:
    """(page number, text) for the first MAX_PAGES pages; .txt files count as one page."""
    if path.suffix.lower() == ".txt":
        return [(1, path.read_text(encoding="utf-8", errors="ignore"))]
    from pypdf import PdfReader
    out = []
    for i, p in enumerate(PdfReader(path).pages[:MAX_PAGES], 1):
        try:
            out.append((i, p.extract_text() or ""))
        except Exception:
            continue
    return out


def _chunks(text: str) -> list:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    step = CHUNK - OVERLAP
    return [text[i:i + CHUNK] for i in range(0, max(len(text) - OVERLAP, 1), step)]


def _store(con, rows: list):
    if not rows:
        return
    embs = _embed([r["text"] for r in rows])
    con.executemany(
        "INSERT INTO chunks (matter, doc_type, title, doc_no, date, label, page, text, emb) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["matter"], r["doc_type"], r["title"], r["doc_no"], r["date"], r["label"], r["page"],
          r["text"], e.tobytes()) for r, e in zip(rows, embs)])


def _resolve(p, doc_type: str, out_dir: Path) -> Path:
    p = Path(p)
    for c in (p, out_dir / doc_type / p.name, out_dir / p.name):
        if c.is_file():
            return c
    return p


def index_matter(result: dict, out_dir: Path) -> int:
    """Index one fetched matter: a metadata chunk plus text chunks of each downloaded doc.
    Idempotent per matter + doc_no (re-indexing replaces). Returns the number of chunks stored."""
    out_dir = Path(out_dir)
    meta, matter = result.get("meta", {}), result.get("matter") or result.get("meta", {}).get("matter")
    counts = ", ".join(f"{v} {k}" for k, v in result.get("counts", {}).items() if v)
    summary = (f"{matter}: {meta.get('title', '')}. Type: {meta.get('type', '')}. Category: "
               f"{meta.get('category', '')}. Status: {meta.get('status', '')}, outcome: "
               f"{meta.get('outcome', '')}. Received {meta.get('date_received', '')}, decided "
               f"{meta.get('decision_date', '')}. Documents: {counts}.")
    rows = [dict(matter=matter, doc_type="Matter", title=meta.get("title", ""), doc_no="",
                 date=meta.get("date_received", ""), label="matter record", page=0, text=summary)]
    for doc_type, docs in result.get("docs", {}).items():
        for d in docs:
            # Recordings carry a transcript .txt next to the audio; never read the audio itself
            path = _resolve(d.get("transcript") or d["path"], doc_type, out_dir)
            if not path.is_file() or path.suffix.lower() not in (".pdf", ".txt"):
                continue
            try:
                pages = _pages(path)
            except Exception as e:
                print(f"  skip {path.name}: {type(e).__name__}")
                continue
            for page, text in pages:
                rows += [dict(matter=matter, doc_type=doc_type, title=d.get("title", ""),
                              doc_no=str(d.get("doc_no", "")), date=d.get("date", ""),
                              label=path.stem, page=page, text=c) for c in _chunks(text)]
    con = _db()
    with con:
        for doc_no in {r["doc_no"] for r in rows}:
            con.execute("DELETE FROM chunks WHERE matter = ? AND doc_no = ?", (matter, doc_no))
        _store(con, rows)
    con.close()
    return len(rows)


def _cite(r: dict) -> str:
    return f"[{r['matter']} · {r['label']}" + (f" p.{r['page']}]" if r["page"] else "]")


def search(query: str, k: int = 8) -> list:
    """Top-k chunks by cosine similarity, best first. Each dict carries its citation in 'cite'."""
    con = _db()
    cols = ["matter", "doc_type", "title", "doc_no", "date", "label", "page", "text", "emb"]
    rows = [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM chunks")]
    con.close()
    if not rows:
        return []
    scores = np.stack([np.frombuffer(r.pop("emb"), dtype=np.float32) for r in rows]) @ _embed([query])[0]
    out = []
    for i in np.argsort(-scores)[:k]:
        out.append({**rows[i], "score": float(scores[i]), "cite": _cite(rows[i])})
    return out


def answer(question: str) -> str:
    """Answer from retrieved excerpts only, with citations. No API key: the ranked excerpts."""
    hits = search(question)
    if not hits:
        return "Nothing indexed yet. Run: python search.py index <folder-or-json>"
    excerpts = "\n\n".join(f"{h['cite']} ({h['date']})\n{h['text']}" for h in hits)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return excerpts
    import anthropic
    prompt = ("You help a utility regulatory analyst research past Nova Scotia UARB matters. Answer the "
              "question using ONLY the excerpts below; if they do not contain the answer, say so. Cite "
              "every fact with the bracketed citation of its excerpt, exactly as written, e.g. "
              "[M12205 · 02 Board Order (102674) p.3]. Plain text, no markdown, no preamble.\n\n"
              f"Question: {question}\n\nExcerpts:\n\n{excerpts}")
    r = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=4000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    return next((b.text.strip() for b in r.content if b.type == "text"), "") or excerpts


def _index_path(target: Path) -> int:
    """A matter folder (<matter>_<ts>/<DocType>/*.pdf) or a saved <matter>.json."""
    if target.suffix.lower() == ".json":
        js, folders = target, sorted(target.parent.glob(target.stem + "_*/"))
    else:
        matter = target.name.split("_")[0]
        js = next((p for p in (target / f"{matter}.json", target.parent / f"{matter}.json") if p.is_file()), None)
        folders = [target]
        if js is None:
            raise SystemExit(f"no {matter}.json next to or inside {target}")
    result = json.loads(js.read_text(encoding="utf-8"))
    return index_matter(result, folders[-1] if folders else js.parent)


def main(argv: list):
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    if not argv:
        print(__doc__)
    elif argv[0] == "index" and len(argv) > 1:
        for t in argv[1:]:
            print(f"{t}: {_index_path(Path(t))} chunks")
    elif argv[0] == "--matters":
        con = _db()
        for m, n, title in con.execute("SELECT matter, COUNT(*), MAX(CASE WHEN doc_type='Matter' "
                                       "THEN title END) FROM chunks GROUP BY matter ORDER BY matter"):
            print(f"{m}  {n:>5} chunks  {title or ''}")
        con.close()
    else:
        print(answer(" ".join(argv)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
