"""Turn downloaded hearing recordings into searchable text with Deepgram (optional: needs DEEPGRAM_API_KEY)."""
import json, os, urllib.request
from pathlib import Path

AUDIO = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".mp4": "video/mp4", ".wma": "audio/x-ms-wma"}
URL = "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&paragraphs=true&diarize=true"


def _key():
    return os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM_KEY")


def transcribe(path: Path) -> Path:
    """Write <recording>.txt next to the audio (speaker-labelled paragraphs) and return its path."""
    with open(path, "rb") as f:
        req = urllib.request.Request(URL, data=f, method="POST", headers={
            "Authorization": f"Token {_key()}", "Content-Type": AUDIO[path.suffix.lower()],
            "Content-Length": str(path.stat().st_size)})
        with urllib.request.urlopen(req, timeout=1800) as r:  # a full hearing day is ~3 h of audio
            res = json.load(r)
    alt = res["results"]["channels"][0]["alternatives"][0]
    text = (alt.get("paragraphs") or {}).get("transcript") or alt["transcript"]
    out = path.with_suffix(".transcript.txt")
    out.write_text(text.strip() + "\n", encoding="utf-8")
    return out


def transcribe_all(docs: list) -> list:
    """Transcribe every audio file in a list of downloaded docs; returns per-file error strings."""
    if not _key():
        return []
    from concurrent.futures import ThreadPoolExecutor
    audio = [d for d in docs if d["path"].suffix.lower() in AUDIO]
    errors = []

    def one(d):
        try:
            d["transcript"] = transcribe(d["path"])
        except Exception as e:
            errors.append(f"transcript for {d['title']}: {type(e).__name__}")

    with ThreadPoolExecutor(4) as pool:  # each call is minutes of server-side work, so run a few at once
        list(pool.map(one, audio))
    return errors


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    t = transcribe(Path(sys.argv[1]))
    print(t, t.stat().st_size, "bytes")
    print(t.read_text(encoding="utf-8")[:800])
