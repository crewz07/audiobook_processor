"""
audiobook.py - turn a book from Audiobooks/ into one tagged, chaptered .m4b in Plex_audiobooks/.

Replaces the manual steps: rename_chapters.py -> ffmpeg -> mp3tag (Audible lookup + rename action).

    audiobook "<book folder or file>" [more books...]      interactive: pick the Audible match, then it runs
    audiobook "<book>" --query "title author"              search with your own words
    audiobook "<book>" --asin B002V8LF30 --yes             no prompts (e.g. when the match is already known)
    audiobook "<book>" --dry-run                           show the plan, change nothing

With several books, all matches are chosen first, then every book is processed unattended.
Work files go to C:\\Audiobook_Work and are deleted after the finished book is moved into Plex_audiobooks.
"""
import argparse
import csv
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:  # use the Windows certificate store (this PC's HTTPS is inspected by local security software)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

MUSIC = Path(r"C:\Users\kruse\OneDrive\Music")
PLEX = MUSIC / "Plex_audiobooks"
WORK = Path(r"C:\Audiobook_Work")
LOG = Path(__file__).with_name("processed_log.csv")

AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wma", ".wav"}
AUDIBLE_SEARCH = "https://api.audible.com/1.0/catalog/products"
AUDIBLE_GROUPS = "contributors,product_desc,product_extended_attrs,product_attrs,media,series,category_ladders"
AUDNEXUS_CHAPTERS = "https://api.audnex.us/books/{asin}/chapters"
AUDIBLE_MAX_RUNTIME_DIFF = 0.05   # beyond 5% runtime difference it is surely another edition: skip Audible chapters
SILENCE_NOISE = "-35dB"           # quieter than this counts as a pause when aligning chapters

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


# ----------------------------------------------------------------------------- helpers

def find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    pattern = os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\{name}.exe")
    hits = glob.glob(pattern)
    if hits:
        return hits[0]
    sys.exit(f"Could not find {name}. Install with: winget install --id Gyan.FFmpeg -e --source winget")


FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")


def http_get(url, timeout, tries=4):
    """GET with retries: the connection sometimes drops (e.g. 'SSL: UNEXPECTED_EOF_WHILE_READING')."""
    req = urllib.request.Request(url, headers={"User-Agent": "audiobook-organizer/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:  # 404 etc. won't get better
                raise
        except (urllib.error.URLError, OSError):
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def http_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(http_get(url, 30))


def http_bytes(url):
    return http_get(url, 60)


def fix_mojibake(s):
    """Audible returns some text double-encoded (e.g. 'â€œ' instead of a curly quote)."""
    if s and any(m in s for m in ("â€", "Ã", "Â")):
        try:
            return s.encode("cp1252", errors="surrogateescape").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def html_to_text(s):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", fix_mojibake(s)).strip()


def safe_name(s):
    """Make a string safe for a Windows file/folder name."""
    s = s.replace(":", " -")
    s = re.sub(r'[<>"/\\|?*]', "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().rstrip(".")


def natural_key(path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def hms(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


# ----------------------------------------------------------------------------- source analysis

def hydrate(path):
    """Read the whole file so OneDrive downloads an online-only placeholder; returns True if it read fine."""
    try:
        with open(path, "rb") as f:
            while f.read(1 << 20):
                pass
        return True
    except OSError:
        return False


def probe(path):
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
           "-show_chapters", "-select_streams", "a:0", str(path)]
    for attempt in range(3):
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if out.returncode == 0:
            break
        # OneDrive's on-demand download sometimes fails when ffprobe triggers it ("Invalid argument"):
        # fetch the file ourselves and try again.
        if not hydrate(path):
            break
    else:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()}")
    if out.returncode != 0:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
        if attrs & 0x400000 or attrs & 0x1000:  # RECALL_ON_DATA_ACCESS / OFFLINE: online-only OneDrive file
            raise RuntimeError(f"{path.name} is online-only and OneDrive would not download it (Windows says "
                               "'the cloud file provider is not running'). Check the OneDrive tray icon: signed in, "
                               "not paused? Or right-click the book folder > 'Always keep on this device' first.")
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()}")
    d = json.loads(out.stdout)
    st = (d.get("streams") or [{}])[0]
    fmt = d.get("format", {})
    tags = {k.lower(): v for k, v in {**fmt.get("tags", {}), **st.get("tags", {})}.items()}
    return {
        "duration": float(fmt.get("duration") or st.get("duration") or 0),
        "codec": st.get("codec_name"),
        "sample_rate": int(st.get("sample_rate") or 0),
        "channels": int(st.get("channels") or 0),
        "bit_rate": int(st.get("bit_rate") or fmt.get("bit_rate") or 0),
        "tags": tags,
        "chapters": [(float(c["start_time"]), float(c["end_time"]), (c.get("tags") or {}).get("title", ""))
                     for c in d.get("chapters", [])],
    }


def tag_number(value):
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


class Book:
    def __init__(self, source, files=None):
        self.source = Path(source).resolve()
        self.picked_files = files is not None   # a few files out of a shared folder: log the file, not the folder
        if not self.source.exists():
            raise FileNotFoundError(self.source)
        if files:  # an explicit subset of the folder (rechapter.py: e.g. "p1.2" + "p2.2" of one book)
            files = [Path(f).resolve() for f in files]
        elif self.source.is_file():
            files = [self.source]
        else:
            files = [p for p in self.source.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT]
        aax = list(self.source.rglob("*.aax")) if self.source.is_dir() else []
        if not files:
            hint = " (only .aax found: Audible DRM files need converting first)" if aax else ""
            raise ValueError(f"No audio files in {self.source}{hint}")
        print(f"  probing {len(files)} file(s)... (downloads them from OneDrive if needed)")
        self.probes = {f: probe(f) for f in files}
        self.files, self.order_note = self._order(files)
        self.total = sum(self.probes[f]["duration"] for f in self.files)

    def _order(self, files):
        natural = sorted(files, key=lambda p: natural_key(p.relative_to(self.source.parent)))
        keys = {}
        for f in files:
            t = self.probes[f]["tags"]
            track = tag_number(t.get("track"))
            if track is None:
                return natural, "filename order"
            keys[f] = (tag_number(t.get("disc")) or 1, track)
        if len(set(keys.values())) != len(keys):
            return natural, "filename order (track tags have duplicates)"
        by_tag = sorted(files, key=lambda f: keys[f])
        if by_tag == natural:
            return natural, "filename order (matches track tags)"
        return by_tag, "TRACK TAG order (differs from filename order - check the list)"

    @property
    def can_copy(self):
        p = [self.probes[f] for f in self.files]
        return ({x["codec"] for x in p} == {"aac"} and len({x["sample_rate"] for x in p}) == 1
                and len({x["channels"] for x in p}) == 1)

    def encode_settings(self, bitrate_k=None):
        p = [self.probes[f] for f in self.files]
        channels = max(x["channels"] for x in p) or 2
        if not bitrate_k:
            src = max(x["bit_rate"] for x in p) // 1000 or 128
            bitrate_k = next((b for b in (32, 48, 64, 96, 128) if b >= src * 0.9), 128)
        return bitrate_k, channels

    def guess_queries(self):
        """Several search phrasings (tags, folder name, parent + folder); the caller tries each."""
        def clean(s):
            s = re.split(r"narrated by", s, flags=re.I)[0]
            s = re.sub(r"[\(\[].*?[\)\]]", " ", s)                       # (Unabridged), [Series, Book 1], (64kbps)
            s = re.sub(r"\b(unabridged|epub|mp3|m4b|audiobook|disc|cd|part)\b", " ", s, flags=re.I)
            s = re.sub(r"(^|\s)\d+(\.\d+)?(?=[\s.:\-]|$)", " ", s)          # track / book numbers like "01", "5."
            s = re.sub(r"\b(19|20)\d\d\b", " ", s)                       # years
            return re.sub(r"[\s\-_꞉:,.]+", " ", s).strip()

        t = self.probes[self.files[0]]["tags"]
        artist = t.get("album_artist") or t.get("artist") or ""
        name = self.source.stem if self.source.is_file() else self.source.name
        queries = [clean(f"{t.get('album', '')} {artist}") if t.get("album") else "",
                   clean(name), clean(f"{self.source.parent.name} {name}")]
        return [q for i, q in enumerate(queries) if q and q not in queries[:i]]

    def print_summary(self):
        print(f"  {len(self.files)} file(s), {hms(self.total)}, order: {self.order_note}")
        show = self.files if len(self.files) <= 8 else self.files[:4] + [None] + self.files[-3:]
        for f in show:
            print("     ..." if f is None else f"     {f.relative_to(self.source.parent)}")
        if self.can_copy:
            print("  audio: AAC already -> copy without re-encoding (fast, lossless)")
        else:
            codecs = ", ".join(sorted({self.probes[f]['codec'] for f in self.files}))
            b, c = self.encode_settings()
            print(f"  audio: {codecs} -> encode to AAC {b}k {'mono' if c == 1 else 'stereo'}")


# ----------------------------------------------------------------------------- Audible metadata

def audible_search(query, n=8):
    data = http_json(AUDIBLE_SEARCH, {
        "keywords": query, "num_results": n, "products_sort_by": "Relevance",
        "response_groups": AUDIBLE_GROUPS, "image_sizes": "500,1024"})
    return data.get("products", [])


def audible_product(asin):
    data = http_json(f"{AUDIBLE_SEARCH}/{asin}", {"response_groups": AUDIBLE_GROUPS, "image_sizes": "500,1024"})
    return data["product"]


def clean_people(people):
    names = []
    for p in people or []:
        n = fix_mojibake(p.get("name", "")).strip()
        if re.search(r"-\s*(translator|foreword|introduction|afterword|contributor)\b", n, re.I):
            continue
        n = re.sub(r"\s*-\s*editor\s*$", "", n, flags=re.I)
        if n and n not in names:
            names.append(n)
    return names


class Meta:
    def __init__(self, product, series_pref=None, author_override=None, year_override=None, part_override=None):
        p = product
        self.asin = p["asin"]
        self.title = fix_mojibake(p.get("title", "")).strip()
        self.subtitle = fix_mojibake(p.get("subtitle") or "").strip()
        self.authors = clean_people(p.get("authors"))
        self.narrators = clean_people(p.get("narrators"))
        self.author = author_override or (", ".join(self.authors) if len(self.authors) <= 3 else self.authors[0])
        self.narrator = ", ".join(self.narrators)
        self.year = year_override or (p.get("release_date") or p.get("issue_date") or "")[:4]
        self.publisher = fix_mojibake(p.get("publisher_name") or "")
        self.summary = html_to_text(p.get("publisher_summary") or p.get("merchandising_summary") or "")
        self.runtime_min = p.get("runtime_length_min") or 0
        self.language = p.get("language", "")
        imgs = p.get("product_images") or {}
        self.cover_url = imgs.get("1024") or imgs.get("500")
        genres = [lad["ladder"] for lad in p.get("category_ladders") or [] if lad.get("root") == "Genres"]
        self.genre = genres[0][0]["name"] if genres else "Audiobook"
        self.all_series = [(fix_mojibake(s["title"]), s.get("sequence") or "") for s in p.get("series") or []]
        self.series, self.part = self._pick_series(series_pref)
        if part_override:
            self.part = part_override
        self.keep_path = None  # (folder, file stem): rebuild in place instead of the computed layout

    def keep_existing(self, existing_file):
        """Write to an existing book's folder and file name (used by rechapter.py)."""
        self.keep_path = (existing_file.parent, existing_file.stem)

    def _pick_series(self, pref):
        norm = lambda s: re.sub(r"^the\s+", "", s.strip().lower())  # noqa: E731 - "The Empyrean" == "Empyrean"
        if not self.all_series:
            return (pref, "") if pref else ("", "")
        if pref:
            for title, part in self.all_series:
                if norm(title) == norm(pref):
                    return pref, part
            return pref, ""
        author_dir = PLEX / safe_name(self.author)
        existing = [d.name for d in author_dir.iterdir() if d.is_dir()] if author_dir.is_dir() else []
        for title, part in self.all_series:  # prefer a series folder that already exists for this author
            for name in existing:
                if norm(name) == norm(safe_name(title)):
                    return name, part
        return self.all_series[0]

    # Same layout as the mp3tag action "&1 Rename Relocate Extras Title"
    @property
    def series_tag(self):
        return f"[{self.series} {self.part}]" if self.series and self.part else ""

    @property
    def dest_dir(self):
        if self.keep_path:
            return self.keep_path[0]
        folder = f"{self.year} - {self.title}" + (f" {self.series_tag}" if self.series_tag else "")
        parts = [PLEX, safe_name(self.author)] + ([safe_name(self.series)] if self.series else []) + [safe_name(folder)]
        return Path(*parts)

    @property
    def file_stem(self):
        if self.keep_path:
            return self.keep_path[1]
        return safe_name(f"{self.title} ({self.year})" + (f" {self.series_tag}" if self.series_tag else ""))

    @property
    def cover_name(self):
        if self.keep_path:
            return self.keep_path[1] + " - cover.jpg"
        return safe_name(f"{self.title} ({self.year})" + (f" {self.series_tag}" if self.series_tag else "") + " - cover") + ".jpg"


def runtime_matches(p, total_s):
    rt = (p.get("runtime_length_min") or 0) * 60
    return (bool(rt) and abs(rt - total_s) / 60 <= max(3, total_s / 60 * 0.02)
            and p.get("language", "english") == "english")


def describe(p, total_s):
    authors = ", ".join(clean_people(p.get("authors")))[:40]
    narr = ", ".join(clean_people(p.get("narrators")))[:30]
    ser = "; ".join(f"{s['title']} {s.get('sequence', '')}".strip() for s in p.get("series") or [])[:60]
    rt = (p.get("runtime_length_min") or 0) * 60
    diff = (rt - total_s) / 60
    mark = "*" if runtime_matches(p, total_s) else " "
    year = (p.get("release_date") or "")[:4]
    title = fix_mojibake(p.get("title", ""))
    return (f"{mark} {title[:55]}  ({year})  by {authors}  read by {narr}"
            f"\n      {ser or '-'}  |  {hms(rt)} ({diff:+.0f} min vs yours)  |  {p.get('language', '')}  |  {p['asin']}")


def choose_match(book, args):
    if args.asin:
        return audible_product(args.asin)
    if args.title:  # not on Audible: details typed in by hand
        return {"asin": "", "title": args.title, "narrators": [{"name": args.narrator}] if args.narrator else [],
                "release_date": args.year or "", "series": [{"title": args.series, "sequence": args.part or ""}]
                if args.series else []}
    interactive = sys.stdin.isatty() and not args.yes
    if args.query:
        query, results = args.query, audible_search(args.query)
    else:  # try each phrasing; prefer the first whose results include a runtime match
        tried = [(q, audible_search(q)) for q in book.guess_queries()]
        query, results = next(((q, r) for q, r in tried if any(runtime_matches(p, book.total) for p in r)),
                              next(((q, r) for q, r in tried if r), tried[0]))
    while True:
        print(f"\n  Audible search: \"{query}\"   (* = runtime matches yours)")
        for i, p in enumerate(results, 1):
            print(f"  {i}. {describe(p, book.total)}")
        if not results:
            print("  (no results)")
        if not interactive:
            if args.yes and results:
                return results[0]
            print("\n  Not interactive: re-run with --asin <ASIN> (or --query) to choose.")
            return None
        try:
            ans = input("\n  Pick 1-%d, type a new search, 'a <ASIN>', or Enter to skip this book: "
                        % len(results)).strip()
        except EOFError:
            print("\n  No keyboard input available: re-run with --asin <ASIN> to choose.")
            return None
        if not ans:
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(results):
            return results[int(ans) - 1]
        if ans.lower().startswith("a "):
            return audible_product(ans[2:].strip())
        query, results = ans, audible_search(ans)


# ----------------------------------------------------------------------------- chapters

def chapters_from_files(book):
    chapters, t, embedded = [], 0.0, False
    titles = [book.probes[f]["tags"].get("title", "").strip() for f in book.files]
    use_titles = all(titles) and len(set(titles)) == len(titles)
    for i, f in enumerate(book.files):
        p = book.probes[f]
        real = [c for c in p["chapters"] if c[1] - c[0] >= 1.0]  # some rips carry junk zero-length markers
        if len(real) > 1:
            embedded = True
            for s, e, title in real:
                chapters.append([t + s, t + e, title or f"Chapter {len(chapters) + 1}"])
        else:
            chapters.append([t, t + p["duration"], titles[i] if use_titles else f"Chapter {i + 1:02d}"])
        t += p["duration"]
    return chapters, ("embedded chapters from source" if embedded else "one chapter per source file")


def chapters_from_audible(asin):
    """Audible's chapter list (names + times) for this ASIN, via Audnexus."""
    try:
        data = http_json(AUDNEXUS_CHAPTERS.format(asin=asin))
    except Exception as e:  # noqa: BLE001 - any network/parse problem just means "not available"
        return None, f"Audible chapters unavailable ({e})"
    chs = data.get("chapters") or []
    if not chs:
        return None, "Audible chapters unavailable"
    audible_total = (data.get("runtimeLengthMs") or (chs[-1]["startOffsetMs"] + chs[-1]["lengthMs"])) / 1000
    chapters = [[c["startOffsetMs"] / 1000, (c["startOffsetMs"] + c["lengthMs"]) / 1000, c["title"].strip()]
                for c in chs]
    return chapters, audible_total


def fit_to_length(chapters, total_s):
    chapters = [list(c) for c in chapters if c[0] < total_s - 1]
    for a, b in zip(chapters, chapters[1:]):
        a[1] = b[0]
    chapters[-1][1] = total_s
    return chapters


def detect_silences(book, job):
    """Every pause of 0.6s+ in the joined audio, as (start, end) seconds."""
    log = job / "silences.log"
    run_with_progress([FFMPEG, "-hide_banner", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(job / "list.txt"),
                       "-map", "0:a:0", "-af", f"silencedetect=noise={SILENCE_NOISE}:d=0.6", "-f", "null", "-"],
                      book.total, "finding pauses", log)
    text = log.read_text(encoding="utf-8")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", text)]
    return list(zip(starts, ends))


def align_to_silences(chapters, audible_total, total_s, silences):
    """Scale Audible's chapter starts to our length, then snap each to the longest nearby pause.

    Chapter breaks are the longest pauses in a recording (measured on 3 books: typical pause ~0.9s,
    chapter breaks 3-5s; 36-46 of the N longest pauses were exactly the N chapter breaks). A break
    only counts as confirmed ("strong") when its pause is among the book's 2*N longest, so a random
    sentence gap inside the search window can't pass as a chapter break.
    """
    scale = total_s / audible_total
    window = min(300, abs(total_s - audible_total) + 15)
    lengths = sorted(b - a for a, b in silences) or [0]
    strong_len = max(1.0, min(lengths[max(0, len(lengths) - 2 * len(chapters))], lengths[int(len(lengths) * 0.99)]))
    out, strong, shifts = [], 0, []
    for i, (start, _, title) in enumerate(chapters):
        target = start * scale
        new = target
        if i == 0:
            new = 0.0
        else:
            near = [(a, b) for a, b in silences if abs((a + b) / 2 - target) <= window]
            if near:
                a, b = max(near, key=lambda s: (s[1] - s[0]) - 0.005 * abs((s[0] + s[1]) / 2 - target))
                if not out or b - 0.5 > out[-1][0] + 1:  # keep chapters in order
                    new = max(a, b - 0.5)  # just before the voice starts again
                    strong += (b - a) >= strong_len
            if new <= out[-1][0] + 1:  # unsnapped estimate fell behind the previous (snapped) chapter
                new = out[-1][0] + 1
        shifts.append(abs(new - target))
        out.append([new, 0, title])
    return fit_to_length(out, total_s), strong, shifts, strong_len


def pick_chapters(book, meta, mode, job=None):
    """Returns (chapters, note). Without a job folder it only previews (no audio analysis)."""
    file_chs, file_note = chapters_from_files(book)
    if mode == "files" or (mode == "auto" and file_note.startswith("embedded")) or not meta.asin:
        return file_chs, file_note
    aud, aud_total = chapters_from_audible(meta.asin)
    if aud is None:
        return file_chs, f"{file_note} ({aud_total})"
    diff = book.total - aud_total
    if len(book.files) == len(aud) > 1 and abs(diff) <= book.total * AUDIBLE_MAX_RUNTIME_DIFF:  # one file per chapter
        return ([[s, e, a[2]] for (s, e, _), a in zip(file_chs, aud)],
                f"Audible chapter names on the source's {len(aud)} chapter files")
    if mode == "auto" and abs(diff) > book.total * AUDIBLE_MAX_RUNTIME_DIFF:
        return file_chs, (f"{file_note} (Audible chapters skipped: runtime differs by {diff / 60:+.1f} min,"
                          " probably a different edition)")
    if abs(diff) <= 2:
        return fit_to_length(aud, book.total), "Audible chapters (runtime matches exactly)"
    if job is None:
        return fit_to_length(aud, book.total), f"Audible chapters, to be aligned to pauses (runtime differs {diff:+.0f}s)"
    chs, strong, shifts, strong_len = align_to_silences(aud, aud_total, book.total, detect_silences(book, job))
    # a chapter right after a short heading ("Part 1: ...", <60s) follows only a brief pause: don't count those
    breaks = max(1, sum(1 for prev in aud[:-1] if prev[1] - prev[0] >= 60))
    # Within 30s of Audible's runtime it is the same edition, so the times are right even where a short
    # chapter has no long pause; the strict check is for other editions, where times have drifted.
    if mode == "auto" and abs(diff) > 30 and strong < 0.8 * breaks:
        return file_chs, (f"{file_note} (Audible chapters skipped: only {strong}/{breaks} breaks landed on a long"
                          f" pause, so the chapter times can't be trusted)")
    shifts = sorted(shifts[1:]) or [0]
    return chs, (f"Audible chapters, {min(strong, breaks)}/{breaks} breaks on a long pause (>={strong_len:.1f}s); "
                 f"moved median {shifts[len(shifts) // 2]:.0f}s, max {shifts[-1]:.0f}s; runtime differs {diff:+.0f}s")


def write_ffmetadata(chapters, path):
    esc = lambda s: re.sub(r"([=;#\\\n])", r"\\\1", s)  # noqa: E731
    lines = [";FFMETADATA1"]
    for s, e, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(s * 1000)}", f"END={int(e * 1000)}", f"title={esc(title)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- build

def write_concat_list(book, job):
    (job / "list.txt").write_text("".join("file '%s'\n" % str(f).replace("\\", "/").replace("'", r"'\''")
                                          for f in book.files), encoding="utf-8")


def run_with_progress(cmd, total_s, label, log_path):
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]],
                                stdout=subprocess.PIPE, stderr=log, text=True)
        last = -1
        for line in proc.stdout:
            if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                pct = min(100, int(int(line.strip()[12:]) / 1e6 / total_s * 100))
                if pct != last:
                    print(f"\r  {label} {pct:3d}%", end="", flush=True)
                    last = pct
        proc.wait()
    print()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({label}), see {log_path}")


def run_ffmpeg(book, chapters, out_path, job, bitrate_k):
    meta_file = job / "chapters.txt"
    write_ffmetadata(chapters, meta_file)
    if book.can_copy:
        codec = ["-c:a", "copy"]
    else:
        b, ch = book.encode_settings(bitrate_k)
        codec = ["-c:a", "aac", "-b:a", f"{b}k", "-ac", str(ch)]
    run_with_progress([FFMPEG, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                       "-i", str(job / "list.txt"), "-f", "ffmetadata", "-i", str(meta_file), "-map", "0:a:0",
                       "-map_metadata", "1",  # not -1: that also drops chapter titles
                       "-map_chapters", "1", *codec, "-movflags", "+faststart", str(out_path)],
                      book.total, "building m4b", job / "ffmpeg.log")


def write_tags(path, meta, cover):
    m = MP4(path)
    m.delete()
    m = MP4(path)
    ff = lambda v: [MP4FreeForm(v.encode("utf-8"))]  # noqa: E731
    m["\xa9nam"] = [meta.file_stem]          # mp3tag action sets TITLE to the filename for single-file books
    m["\xa9alb"] = [meta.title]
    m["\xa9ART"] = [meta.author]
    m["aART"] = [meta.author]
    if meta.narrator:
        m["\xa9wrt"] = [meta.narrator]
    if meta.year:
        m["\xa9day"] = [meta.year]
    if meta.summary:
        m["\xa9cmt"] = [meta.summary]
        m["desc"] = [meta.summary[:255]]
    m["\xa9gen"] = [meta.genre]
    m["soal"] = [f"{meta.series} {meta.part} - {meta.title}" if meta.series else meta.title]
    m["trkn"] = [(1, 1)]
    m["stik"] = [2]  # media kind: audiobook
    if meta.subtitle:
        m["----:com.apple.iTunes:SUBTITLE"] = ff(meta.subtitle)
    if meta.series:
        m["----:com.apple.iTunes:SERIES"] = ff(meta.series)
        if meta.part:
            m["----:com.apple.iTunes:SERIES-PART"] = ff(meta.part)
    if meta.publisher:
        m["----:com.apple.iTunes:PUBLISHER"] = ff(meta.publisher)
    if meta.asin:
        m["----:com.apple.iTunes:ASIN"] = ff(meta.asin)
    if cover:
        m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()


def process(book, meta, args):
    dest = meta.dest_dir
    final = dest / (meta.file_stem + ".m4b")
    chapters, ch_note = pick_chapters(book, meta, args.chapters)
    print(f"  -> {final.relative_to(MUSIC)}")
    print(f"  chapters: {len(chapters)} - {ch_note}")
    for other in PLEX.glob(f"*/{glob.escape(safe_name(meta.series))}") if meta.series else []:
        if other.is_dir() and other.parent.name != safe_name(meta.author):
            print(f"  NOTE: series folder also exists under a different author: {other.relative_to(PLEX)}")
    if any(p.suffix.lower() in AUDIO_EXT for p in dest.glob("*")) and not args.overwrite:
        print("  SKIPPED: destination already has audio (use --overwrite to replace)")
        return False
    if not meta.year and not meta.keep_path:
        print("  SKIPPED: Audible has no year for this book; re-run with --year YYYY")
        return False
    min_ch = getattr(args, "min_chapters", 0)  # rechapter.py: only rebuild if it gains chapters
    if min_ch and "to be aligned" not in ch_note and len(chapters) < min_ch:
        print(f"  SKIPPED: would only have {len(chapters)} chapter(s)")
        return False
    if args.dry_run:
        print("  (dry run - nothing written)")
        return False

    job = WORK / (meta.asin or "manual-" + safe_name(meta.title))
    job.mkdir(parents=True, exist_ok=True)
    out = job / "book.m4b"
    started = datetime.now()
    write_concat_list(book, job)
    if "to be aligned" in ch_note:
        chapters, ch_note = pick_chapters(book, meta, args.chapters, job)
        print(f"  chapters: {len(chapters)} - {ch_note}")
        if min_ch and len(chapters) < min_ch:
            shutil.rmtree(job, ignore_errors=True)
            print(f"  SKIPPED: would only have {len(chapters)} chapter(s)")
            return False
    run_ffmpeg(book, chapters, out, job, args.bitrate)

    check = probe(out)
    if abs(check["duration"] - book.total) > max(3, book.total * 0.002):
        raise RuntimeError(f"Output is {hms(check['duration'])} but sources total {hms(book.total)}; kept {job}")
    cover = None
    if meta.cover_url:
        try:
            cover = http_bytes(meta.cover_url)
        except Exception as e:  # noqa: BLE001
            print(f"  cover download failed: {e}")
    if not cover:  # fall back to the picture embedded in the source files
        jpg = job / "cover.jpg"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(book.files[0]), "-an", "-frames:v", "1",
                        "-c:v", "mjpeg", str(jpg)], capture_output=True)
        cover = jpg.read_bytes() if jpg.exists() and jpg.stat().st_size > 1000 else None
    write_tags(out, meta, cover)
    got = [c[2] for c in probe(out)["chapters"]]
    if got != [c[2] for c in chapters]:
        raise RuntimeError(f"Chapters missing or unnamed in output ({len(got)} found); kept {job}")

    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(out), str(final))
    if cover:
        (dest / meta.cover_name).write_bytes(cover)
    (dest / "desc.txt").write_text(meta.summary, encoding="utf-8")
    (dest / "reader.txt").write_text(meta.narrator, encoding="utf-8")
    shutil.rmtree(job, ignore_errors=True)

    new_log = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["date", "source", "asin", "destination", "audio", "chapters", "minutes_taken"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"),
                    str(book.files[0] if book.picked_files else book.source), meta.asin, str(final),
                    "copy" if book.can_copy else "encode", f"{len(chapters)} ({ch_note})",
                    round((datetime.now() - started).total_seconds() / 60, 1)])
    print(f"  DONE in {datetime.now() - started}".split(".")[0])
    return True


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="book folder(s) or single audio file(s)")
    ap.add_argument("--query", help="Audible search words (default: guessed from tags/folder name)")
    ap.add_argument("--asin", help="use this Audible ASIN directly (single book only)")
    ap.add_argument("--series", help="pick this series when Audible lists several (e.g. 'Harry Potter')")
    ap.add_argument("--author", help="override the author folder/tag")
    ap.add_argument("--title", help="book is not on Audible: use this title (needs --author and --year)")
    ap.add_argument("--narrator", help="narrator, with --title")
    ap.add_argument("--year", help="override the year")
    ap.add_argument("--part", help="override the series number (e.g. 15)")
    ap.add_argument("--chapters", choices=["auto", "files", "audible"], default="auto",
                    help="auto: source chapters > Audible (if same runtime) > one per file")
    ap.add_argument("--bitrate", type=int, help="AAC kbps when encoding (default: match source, max 128)")
    ap.add_argument("--yes", action="store_true", help="no prompts; take --asin or the top search result")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing book in Plex_audiobooks")
    args = ap.parse_args()
    if args.title and not (args.author and args.year):
        ap.error("--title needs --author and --year")
    if args.asin and len(args.sources) > 1:
        ap.error("--asin only works with a single book")

    queue = []
    for src in args.sources:
        print(f"\n=== {src}")
        try:
            book = Book(src)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"  SKIPPED: {e}")
            continue
        book.print_summary()
        product = choose_match(book, args)
        if not product:
            print("  skipped")
            continue
        meta = Meta(product, args.series, args.author, args.year, args.part)
        print(f"  match: {meta.title} | {meta.author} | read by {meta.narrator} | {meta.year}"
              f" | series: {meta.series or '-'} {meta.part}")
        if len(meta.all_series) > 1:
            print(f"  (Audible lists several series: {meta.all_series}; choose with --series)")
        queue.append((book, meta))

    if len(queue) > 1:
        print(f"\n##### Processing {len(queue)} books")
    done = 0
    for book, meta in queue:
        print(f"\n=== {meta.title}")
        try:
            done += bool(process(book, meta, args))
        except Exception as e:  # noqa: BLE001 - report and continue with the next book
            print(f"\n  FAILED: {e}")
    print(f"\nFinished: {done} of {len(args.sources)} book(s) added to Plex_audiobooks.")


if __name__ == "__main__":
    main()
