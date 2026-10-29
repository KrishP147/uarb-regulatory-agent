"""Fetch a UARB matter: metadata, per-tab counts, and up to N documents of one type."""
import asyncio, re, zipfile
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
DOC_TYPES = ["Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings"]
# FileMaker WebDirect object ids on the matter overview page
FIELDS = {"matter": 286, "type": 287, "status": 289, "date_received": 292,
          "decision_date": 294, "outcome": 295, "category": 298}


async def _text(page, sel):
    loc = page.locator(sel)
    return (await loc.first.inner_text()).strip() if await loc.count() else ""


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', " ", s).strip()[:70]


def _rows(page):
    return page.locator("tr.v-grid-row-has-data:visible").filter(
        has=page.get_by_role("button", name="GO GET IT", exact=True))


async def _open_tab(page, doc_type):
    """Click a tab and wait until the grid shows that tab's rows, not the previous tab's."""
    rows = _rows(page)
    before = await rows.first.inner_text() if await rows.count() else None
    await page.get_by_role("button", name=re.compile(rf"^{doc_type} -")).click()
    for _ in range(60):
        await page.wait_for_timeout(500)
        if await rows.count() and (before is None or await rows.first.inner_text() != before):
            return
    raise TimeoutError(f"{doc_type} tab never loaded")


async def _download_tab(page, dialog, doc_type, total, out_dir: Path, limit):
    """Download the first `limit` rows of the open tab; one bad row is recorded, not fatal."""
    docs, errors = [], []
    rows = _rows(page)
    await rows.first.wait_for()
    want = min(limit, total)
    for _ in range(30):  # the grid fills in over a few hundred ms; don't start on a half-rendered list
        if await rows.count() >= want:
            break
        await page.wait_for_timeout(500)
    # shortcut: only rows the virtual grid renders up front (enough for 10), scroll the grid if limit grows
    n = min(want, await rows.count())
    for i in range(n):
        row = rows.nth(i)
        lines = [l.strip() for l in (await row.inner_text()).splitlines() if l.strip()]
        lines = lines[:lines.index("Preview")] if "Preview" in lines else lines
        doc_no = next((l for l in lines if re.fullmatch(r"\d{4,}", l)), "")
        date = next((l for l in lines if re.fullmatch(r"\d\d/\d\d/\d{4}", l)), "")
        title = next((l for l in lines if l not in (doc_no, date, "Public") and not re.fullmatch(r"[\d.\-]+", l)), doc_no)
        try:
            await row.get_by_role("button", name="GO GET IT", exact=True).click()
            dl_btn = page.locator(".v-window .fm-download-button")
            # recordings first show an "Export Field to File" dialog that needs OK
            export_ok = page.locator(".v-window").filter(has_text="Export Field to File").get_by_role("button", name="OK")
            await dl_btn.first.or_(export_ok).first.wait_for(timeout=180000)  # big PDFs (40MB+) take a while to prepare
            if not await dl_btn.count() and await export_ok.count():
                await export_ok.click()
                await dl_btn.first.wait_for(timeout=300000)
            btn_name = (await dl_btn.first.inner_text()).strip()
            async with page.expect_download(timeout=300000) as info:
                await dl_btn.first.click(no_wait_after=True)
            dl = await info.value
            src = dl.suggested_filename if dl.suggested_filename != "download" else btn_name
            path = out_dir / f"{i + 1:02d} {_safe(title)}{' (' + doc_no + ')' if doc_no and doc_no not in title else ''}{Path(src).suffix}"
            await dl.save_as(path)
            docs.append({"path": path, "title": title, "date": date, "doc_no": doc_no})
        except Exception as e:
            errors.append(f"{doc_type} #{i + 1} ({title}): {type(e).__name__}"[:200])
        finally:
            close = dialog.last.get_by_role("button", name="Close")
            if await close.count():
                await close.click()
                await page.wait_for_timeout(300)
    return docs, errors


async def fetch(matter: str, doc_types: list, out_dir: Path, limit: int = 10) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1200}, accept_downloads=True)
            page = await ctx.new_page()
            page.set_default_timeout(60000)
            await page.goto(URL, wait_until="networkidle")

            # the "Go Directly to Matter" box is a contenteditable div, so type into it; Tab submits
            await page.locator(".fm_object_254").click()
            await page.wait_for_timeout(1000)
            await page.keyboard.type(matter, delay=80)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(500)
            tab0 = page.get_by_role("button", name=re.compile(r"^Exhibits -"))
            dialog = page.locator(".v-window")
            await tab0.or_(dialog).first.wait_for()
            if not await tab0.count():
                raise LookupError(f"Matter {matter} not found")

            meta = {k: await _text(page, f".fm_object_{v} .text") for k, v in FIELDS.items()}
            if meta["matter"] != matter:
                raise LookupError(f"Site showed {meta['matter']!r}, expected {matter}")
            meta["title"] = await _text(page, "[id='b0p0o290i0i0r1']") or await page.evaluate(
                "() => [...document.querySelectorAll('.v-label, .text')].map(e => e.innerText.trim())"
                ".filter(t => t.includes(' - ')).sort((a, b) => b.length - a.length)[0] || ''")

            buttons = await page.get_by_role("button").all_inner_texts()
            counts = {}
            for t in DOC_TYPES:
                m = next((re.match(rf"^{t} - ([\d,]+)$", b.strip()) for b in buttons
                          if re.match(rf"^{t} - ([\d,]+)$", b.strip())), None)
                counts[t] = int(m.group(1).replace(",", "")) if m else 0

            docs, errors = {}, []
            for t in doc_types:
                docs[t] = []
                if not counts[t]:
                    continue
                try:
                    await _open_tab(page, t)
                except TimeoutError as e:
                    errors.append(str(e))
                    continue
                sub = out_dir / t
                sub.mkdir(exist_ok=True)
                docs[t], errs = await _download_tab(page, dialog, t, counts[t], sub, limit)
                errors += errs
        finally:
            await browser.close()
    return {"matter": matter, "doc_types": doc_types, "meta": meta, "counts": counts,
            "docs": docs, "errors": errors}


def make_zip(docs: dict, zip_path: Path) -> Path:
    """One folder per document type when several were asked for, flat otherwise."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for t, items in docs.items():
            for d in items:
                for f in [d["path"]] + ([d["transcript"]] if d.get("transcript") else []):
                    z.write(f, f"{t}/{f.name}" if len(docs) > 1 else f.name)
    return zip_path


if __name__ == "__main__":
    import json, sys
    matter = sys.argv[1] if len(sys.argv) > 1 else "M12205"
    types = sys.argv[2:] or ["Other Documents"]
    out = Path("downloads") / f"{matter}_cli"
    r = asyncio.run(fetch(matter, types, out))
    z = make_zip(r["docs"], out.with_suffix(".zip"))
    r["docs"] = {t: [{**d, "path": d["path"].name} for d in v] for t, v in r["docs"].items()}
    print(json.dumps({**r, "zip": str(z)}, indent=2))
