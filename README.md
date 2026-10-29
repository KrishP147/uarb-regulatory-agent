# UARB Regulatory Agent

An email agent for Nova Scotia Utility and Review Board (UARB) filings. Email it a matter number and a document type. It finds the matter in the UARB public documents database, downloads the documents, and replies in the same thread with a ZIP, the document counts, and a summary of the matter.

**Try it:** email **krish-uarb-agent@agentmail.to**

> **You:** Hi Agent, can you give me Other Documents files from M12205? Thanks!
>
> **Agent** (a few seconds later): On it: pulling Other Documents for M12205 from the UARB database...
>
> **Agent** (about a minute later):
> M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000. It relates to Capital Expenditure Approvals within the Water category. The matter had an initial filing on April 7, 2025 and a final filing on October 23, 2025 (closed, directions given).
>
> I found 13 Exhibits, 6 Key Documents, 44 Other Documents, and no Transcripts or Recordings.
> I downloaded 10 out of the 44 Other Documents (the first 10 listed). They're attached as a ZIP (6.7 MB).
>
> What these documents cover: Halifax Water's semi-annual compliance update reports a forecast total cost of $59,143,000, matching the Board-approved construction cost, with construction about 6% complete. The Consumer Advocate questioned whether the revised estimate is supported...
>
> In the ZIP:
> &nbsp;&nbsp;01 Utility (Board) - Update (104160).pdf (10/28/2026)
> &nbsp;&nbsp;02 Board Order (102674).pdf (07/08/2026)
> &nbsp;&nbsp;...

## What it handles

| | |
|---|---|
| **Request types** | Exhibits, Key Documents, Other Documents, Transcripts, Recordings. Several in one email, or "all documents" |
| **Loose wording** | "could I grab the key docs for matter 12205" works. Claude interprets requests that don't match the standard format |
| **Follow-ups** | Reply "Exhibits please" in the same thread and it remembers the matter. Reply "retry" to re-run a failed download |
| **Large files** | ZIPs that fit are attached. Bigger ones (Exhibits can exceed 100 MB) come as a private 7-day download link |
| **Readable output** | Files are named by title and document number (`05 Board Decision (99761).pdf`), with a dated list of contents in the email |
| **Document summary** | Claude reads the first pages of the downloaded filings and summarizes them in 2-3 sentences. It uses only facts stated in the text |
| **Errors** | Unknown matter numbers, unclear requests, and failed downloads each get a clear reply instead of silence |

Every fact in the reply (title, amounts, dates, counts) comes straight from the UARB site. Claude is only used to interpret requests and summarize documents, never to fill in facts.

## How it works

```
email ──► AgentMail inbox ──websocket──► agent.py
                                          ├─ understand request (regex, Claude for loose wording / thread context)
                                          ├─ "on it" reply
                                          ├─ uarb.py: Playwright drives the UARB site
                                          │    matter search → metadata + tab counts → GO GET IT × up to 10
                                          ├─ ZIP → attach, or S3 + presigned link when too large
                                          ├─ Claude: summary of the downloaded filings
                                          └─ reply in thread  (each step traced in Langfuse)
```

- **`uarb.py`**: the scraper. The UARB database is a FileMaker WebDirect app with no plain links, so Playwright operates it like a person would. One failed document is reported without failing the whole request.
- **`agent.py`**: the email loop, request parsing, delivery and reply text. Requests are processed one at a time so the public site isn't overloaded. Mail that arrives while the agent is offline is processed when it starts.
- **Observability**: each email is one Langfuse trace, grouped by thread, with the scrape, storage and Claude steps (including token cost) nested under it.
- **Data**: each matter's scraped metadata is saved as JSON, the seed of a searchable matter database.

## Run it

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env                       # add your keys
python agent.py --create-inbox my-agent    # once; puts the address in the output
python agent.py
```

Scraper only, no email: `python uarb.py M12205 "Other Documents"`
Tests: `python -m pytest -q`

**Deploy:** `docker build -t uarb-agent . && docker run -d --restart unless-stopped --env-file .env --shm-size=1g uarb-agent`. It needs no public port, because the agent connects out to AgentMail over a websocket. The live agent runs this way on a small AWS EC2 instance.

## Configuration

See `.env.example`. Only AgentMail is required. Claude (request parsing and summaries), S3-compatible storage (large ZIPs) and Langfuse (tracing) are each optional, and the agent still works without them.

## Notes and limits

- It downloads the first 10 documents in the order the site lists them.
- "Initial filing" and "final filing" use the site's Date Received and Decision Date fields.
- The UARB site prepares large PDFs slowly, so a request for big exhibits can take several minutes. The agent replies right away to say it's working on it.

## Searching across matters

The challenge frames this agent as the data ingestion side of a Regulatory Agent: the searchable database under its research, where an analyst looks for examples, precedents and approval patterns in past cases. `search.py` is that layer. It indexes every scraped matter (the saved metadata plus the text of the downloaded filings) into a local database, so one question can be asked across all matters at once instead of one email per matter. The agent indexes every matter it fetches once the one-line hook in `agent.py` (`search.index_matter(r, out)`, before the download folder is deleted) is wired.

```bash
python search.py index downloads/M12205_1793235393   # a saved matter folder, or its M12205.json
python search.py "what did the Board decide about the Windsor Street Exchange costs?"
python search.py --matters                           # indexed matters and chunk counts
```

Real output on the M12205 filings (no `ANTHROPIC_API_KEY` set, so it prints the ranked excerpts; with a key, Claude writes a short answer that cites them the same way):

```
[M12205 · 06 Board Order (102674) p.1] (07/08/2026)
Document: 325280 ORDER M12205 NOVA SCOTIA REGULATORY AND APPEALS BOARD ... Halifax Water applied to the
Board for approval of the Windsor Street Exchange Redevelopment Project - Construction for a cost of
$64,769,000. In its Decision dated October 23, 2025, the Board approved the project with the changes
directed in the Decision, subject to a Compliance Filing ...

[M12205 · 02 Hearing Order (97291) p.1] (04/11/2025)
...
```

How it works:
- Each filing's first 30 pages are split into overlapping 800-character chunks, plus one chunk per matter for its metadata (title, type, category, status, counts).
- Chunks are embedded locally with fastembed (`BAAI/bge-small-en-v1.5`, no API key) and stored in SQLite at `downloads/search.db` (or `SEARCH_DB`).
- A question is embedded the same way and ranked by cosine similarity; Claude answers only from the top excerpts and cites each fact as `[M12205 · 02 Board Order (102674) p.3]`.
