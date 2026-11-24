"""
rechapter.py - add chapters to books already in Plex_audiobooks that have none, by rebuilding them from Audiobooks/.

    rechapter --list                 which books lack chapters, and the source each one would be rebuilt from
    rechapter                        rebuild them all, unattended
    rechapter --only "Wheel of Time" just the books whose path contains these words

For every Plex book with 0-1 chapters (and not already processed by audiobook.py, see processed_log.csv):
  1. its ASIN, title, author, year and series are read from the book's own tags;
  2. the source is found in Audiobooks by name, then confirmed by runtime (within 1%), so a wrong match is never used;
  3. it is rebuilt exactly like audiobook.py does (same chapter rules), into the SAME folder and file name, keeping
     those tags. If the rebuild would still have only one chapter, the book is left as it is;
  4. the old file and its cover/desc/reader go to Music\\_replaced\\ (same folders), the new .m4b takes its place.
Sources that were online-only in OneDrive are set back to online-only afterwards, so the disk doesn't fill up.
A summary is written to rechapter_report.csv.
"""
import argparse
import csv
import difflib
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import audiobook as ab

SOURCES = ab.MUSIC / "Audiobooks"
REPLACED = ab.MUSIC / "_replaced"
REPORT = Path(__file__).with_name("rechapter_report.csv")
PLEX_EXT = {".m4b", ".m4a", ".mp3"}
STOP = {"the", "a", "an", "of", "and", "book", "by", "in", "to", "s", "novel", "unabridged", "series"}
MAX_TRIES = 4          # name candidates confirmed by runtime before giving up
MIN_SCORE = 0.75       # share of the title's words a source name must contain


# ----------------------------------------------------------------------------- finding books and sources

def words(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z0-9]+", s) if w not in STOP and not w.isdigit()]


def has_word(w, pool):
    return w in pool or any(difflib.SequenceMatcher(None, w, p).ratio() >= 0.85 for p in pool if len(p) > 3)


def is_online_only(path):
    attrs = getattr(os.stat(path), "st_file_attributes", 0)
    return bool(attrs & 0x400000 or attrs & 0x1000)


def free_up(paths):
    """Make OneDrive files online-only again ('Free up space')."""
    for p in paths:
        subprocess.run(["attrib", "+U", "-P", str(p)], capture_output=True)


def chapterless_books():
    logged = set()
    if ab.LOG.exists():
        with open(ab.LOG, encoding="utf-8") as f:
            logged = {r["destination"].lower() for r in csv.DictReader(f)}
    for p in sorted(ab.PLEX.rglob("*"), key=ab.natural_key):
        if p.suffix.lower() not in PLEX_EXT or str(p).lower() in logged:
            continue
        info = ab.probe(p)
        if len(info["chapters"]) <= 1:
            yield p, info


class Candidate:
    """A possible source: a set of audio files under one folder, with the name used to match the title."""
    KIND_ORDER = {"dir": 0, "dir-ext": 0, "group": 1, "file": 2}

    def __init__(self, kind, source, files, name):
        self.kind, self.source, self.files, self.name = kind, source, sorted(files, key=ab.natural_key), name
        self.depth = len(source.relative_to(SOURCES).parts)
        parent = source.parent if kind == "file" else source.parent if kind in ("dir", "dir-ext") else source
        self.own = set(words(name))
        self.context = set(words(parent.name)) if parent != SOURCES else set()

    def label(self):
        rel = self.source.relative_to(SOURCES)
        if self.kind == "dir-ext":
            return f"{rel}\*{self.files[0].suffix}"
        if self.kind == "group":
            return f"{rel}\{self.name} ({len(self.files)} parts)"
        return str(rel)


PART = re.compile(r"[\s\-_.]*(\(?\b(p|pt|part|disc|cd)?\s*\d+(\.\d+)?\)?)\s*$", re.I)


def source_candidates():
    """Every folder with audio below it (also split by file type when it mixes e.g. .m4b and .mp3 copies),
    every multi-part group inside a folder ("... p1.2", "... p2.2"), and every single audio file."""
    below = {}
    for root, _, files in os.walk(SOURCES):
        audio = [Path(root) / f for f in files if Path(f).suffix.lower() in ab.AUDIO_EXT]
        for f in audio:
            for d in [Path(root), *Path(root).parents]:
                if d == SOURCES or SOURCES not in d.parents:
                    break
                below.setdefault(d, []).append(f)
        groups = {}
        for f in audio:
            groups.setdefault((PART.sub("", f.stem).strip().lower(), f.suffix.lower()), []).append(f)
        for (base, _), fs in groups.items():
            if 1 < len(fs) < len(audio) and words(base):
                yield Candidate("group", Path(root), fs, PART.sub("", fs[0].stem).strip())
        for f in audio:
            yield Candidate("file", f, [f], f.stem)
    for d, fs in below.items():
        exts = {f.suffix.lower() for f in fs}
        if len(exts) > 1:
            for e in sorted(exts):
                yield Candidate("dir-ext", d, [f for f in fs if f.suffix.lower() == e], d.name)
        if len(fs) > 1 or d != fs[0].parent:
            yield Candidate("dir", d, fs, d.name)


def rank_sources(title, candidates):
    want = set(words(title))
    if not want:
        return []
    ranked = []
    for c in candidates:
        if not c.own:
            continue
        cover = sum(has_word(w, c.own | c.context) for w in want) / len(want)
        if cover >= MIN_SCORE:
            jac = len(want & c.own) / len(want | c.own)
            # equal coverage: whole folders before part groups before single files; mixed-type folders last
            mixed = c.kind == "dir" and len({f.suffix.lower() for f in c.files}) > 1
            ranked.append(((-round(cover, 2), mixed, Candidate.KIND_ORDER[c.kind], -round(jac, 2), -c.depth), c))
    out, seen = [], set()
    for _, c in sorted(ranked, key=lambda r: r[0]):
        if frozenset(c.files) not in seen:
            seen.add(frozenset(c.files))
            out.append(c)
    return out


def find_source(title, plex_dur, plex_size, candidates):
    """Returns (Book, note). Only a source whose runtime matches the Plex copy is accepted.
    Candidates far off in size are skipped unprobed, so a wrong guess doesn't download a whole series."""
    tried = []
    for cand in rank_sources(title, candidates)[:MAX_TRIES]:
        size = sum(f.stat().st_size for f in cand.files)
        if not 0.25 <= size / plex_size <= 4:
            tried.append(f"{cand.label()}: size {size / 1e6:.0f} MB vs {plex_size / 1e6:.0f} MB")
            continue
        was_online = [f for f in cand.files if is_online_only(f)]
        try:
            book = ab.Book(cand.source, files=cand.files if cand.kind in ("group", "dir-ext") else None)
        except Exception as e:  # noqa: BLE001
            free_up(was_online)
            tried.append(f"{cand.label()}: {e}")
            continue
        if abs(book.total - plex_dur) <= max(60, plex_dur * 0.01):
            book.was_online = was_online
            book.label = cand.label()
            return book, ""
        free_up(was_online)
        tried.append(f"{cand.label()}: runtime {ab.hms(book.total)} vs {ab.hms(plex_dur)}")
    return None, "no source found" + (" (tried " + "; ".join(tried) + ")" if tried else " by name")


# ----------------------------------------------------------------------------- rebuilding one book

def meta_from_tags(plex_file, tags):
    """Audible data for summary/narrator/cover, but title/author/year/series as the book has them now."""
    product = ab.audible_product(tags["asin"])
    year = (tags.get("date") or tags.get("year") or "")[:4]
    meta = ab.Meta(product, series_pref=tags.get("series") or None,
                   author_override=tags.get("album_artist") or None,
                   year_override=year if year.isdigit() else None,
                   part_override=tags.get("series-part") or None)
    if tags.get("album"):
        meta.title = tags["album"]
    if not tags.get("series"):
        meta.series, meta.part = "", ""
    meta.keep_existing(plex_file)
    return meta


def backup_old(plex_file):
    """Move the old audio, and copy its cover/desc/reader, to _replaced (same folders as in Plex_audiobooks)."""
    target = REPLACED / plex_file.parent.relative_to(ab.PLEX)
    target.mkdir(parents=True, exist_ok=True)
    for f in plex_file.parent.iterdir():
        if f.is_file() and f.suffix.lower() not in ab.AUDIO_EXT and not (target / f.name).exists():
            shutil.copy2(f, target / f.name)
    moved = target / plex_file.name
    shutil.move(str(plex_file), str(moved))
    return moved


def rebuild(plex_file, meta, book, args):
    print(f"  source: {book.label}")
    book.print_summary()
    new_file = plex_file.with_suffix(".m4b")
    same_name = new_file == plex_file
    old = backup_old(plex_file) if same_name else None  # it would be overwritten in place
    run = argparse.Namespace(chapters=args.chapters, overwrite=True, dry_run=False, bitrate=None, min_chapters=2)
    try:
        ok = ab.process(book, meta, run)
    except Exception:
        if old:
            shutil.move(str(old), str(plex_file))
        raise
    if not ok:
        if old:
            shutil.move(str(old), str(plex_file))
        return False, "left as is (rebuild would not add chapters)"
    if not same_name:
        old = backup_old(plex_file)
    free_up([old])
    n = len(ab.probe(new_file)["chapters"])
    return True, f"{n} chapters; old file in {old.parent.relative_to(ab.MUSIC)}"


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show the books and their likely sources; change nothing")
    ap.add_argument("--only", help="only books whose Plex path contains this text")
    ap.add_argument("--chapters", choices=["auto", "audible"], default="auto", help="as in audiobook.py")
    args = ap.parse_args()

    candidates = list(source_candidates())
    books = [(p, i) for p, i in chapterless_books()
             if not args.only or args.only.lower() in str(p.relative_to(ab.PLEX)).lower()]
    print(f"{len(books)} book(s) in Plex_audiobooks without chapters")

    if args.list:
        for p, info in books:
            title = info["tags"].get("album") or p.stem
            ranked = rank_sources(title, candidates)[:MAX_TRIES]
            print(f"\n- {p.relative_to(ab.PLEX)}  ({ab.hms(info['duration'])}, ASIN {info['tags'].get('asin', '-')})")
            for c in ranked:
                print(f"     {c.kind:7} {c.label()}")
            if not ranked:
                print("     (no source found by name)")
        return

    results, started = [], datetime.now()
    for n, (plex_file, info) in enumerate(books, 1):
        rel = plex_file.relative_to(ab.PLEX)
        print(f"\n##### [{n}/{len(books)}] {rel}")
        status, note, source = "skipped", "", ""
        try:
            if not info["tags"].get("asin"):
                note = "no ASIN tag"
            else:
                meta = meta_from_tags(plex_file, info["tags"])  # before downloading the source: fails fast
                book, note = find_source(info["tags"].get("album") or plex_file.stem, info["duration"],
                                         plex_file.stat().st_size, candidates)
                if book:
                    source = book.label
                    try:
                        ok, note = rebuild(plex_file, meta, book, args)
                        status = "rebuilt" if ok else "unchanged"
                    finally:
                        free_up(book.was_online)
        except Exception as e:  # noqa: BLE001 - report and carry on with the next book
            status, note = "FAILED", str(e)
        print(f"  => {status}: {note}")
        results.append([datetime.now().strftime("%Y-%m-%d %H:%M"), status, str(rel), source, note])
        with open(REPORT, "w", newline="", encoding="utf-8") as f:  # rewritten after every book
            w = csv.writer(f)
            w.writerow(["date", "status", "plex_book", "source", "note"])
            w.writerows(results)

    print(f"\n##### Finished in {str(datetime.now() - started).split('.')[0]}")
    for s in ("rebuilt", "unchanged", "skipped", "FAILED"):
        hits = [r for r in results if r[1] == s]
        if hits:
            print(f"\n{s}: {len(hits)}")
            for r in hits:
                print(f"   {r[2]}  - {r[4]}")
    print(f"\nReport: {REPORT}")


if __name__ == "__main__":
    main()
