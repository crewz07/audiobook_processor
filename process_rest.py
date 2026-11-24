"""
process_rest.py - process every book in Audiobooks/ that is not in Plex_audiobooks yet, using audiobook.py.

    process_rest --list                  show what would be processed, what is already done, and what can't be
    process_rest                         pick the Audible match for each book, then process them all unattended
    process_rest --only "Mitch Rapp"     just the books whose path contains this text
    process_rest --auto                  no prompts: take the Audible match whose runtime matches (*), else skip
    process_rest --dry-run               pick matches and show the plan, write nothing

How books are found in Audiobooks:
  - an audio file directly in Audiobooks is one book;
  - a folder with audio files is one book (its files are the chapters/parts), EXCEPT when its files have
    different names (ignoring numbers and words like "part"/"disc") and each name group is big (>= 120 MB):
    then each group is its own book (e.g. "Mitch Rapp 01 - American Assassin.m4b", "... 02 - Kill Shot.m4b";
    "WoT 04 ... p1.2" + "WoT 04 ... p2.2" stay together);
  - a folder of sub-folders is a series folder and each sub-folder is looked at the same way, unless the
    sub-folders are just parts of one book ("Part 1", "Part 2", "Disc1"...).
A book counts as done when its source is in processed_log.csv / rechapter_report.csv, or when its name contains
the title of a book already in Plex_audiobooks. --list shows which Plex book it matched, so a wrong match is easy
to spot; force it with --only "<words>" --include-done.
"""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

import audiobook as ab
from rechapter import free_up, has_word, is_online_only, words

SOURCES = ab.MUSIC / "Audiobooks"
REPORT = Path(__file__).with_name("rechapter_report.csv")
PART_WORDS = {"p", "pt", "part", "parts", "disc", "disk", "cd", "of", "track", "chapter", "ch", "mp3", "m4b",
              "m4a", "unabridged", "audiobook"}
MULTI_BOOK_MIN_BYTES = 120e6   # every file group must be at least this big to count as a separate book
MULTI_BOOK_MAX_GROUPS = 30     # more groups than this is a folder of chapter files, not of books


# ----------------------------------------------------------------------------- finding books

class Unit:
    """One book to process: a folder, a single file, or a group of files in a folder."""

    def __init__(self, source, files=None, name=None, note=""):
        self.source = source
        self.files = files            # None: the whole folder / the file itself
        self.name = name or (source.stem if source.is_file() else source.name)
        self.note = note
        self.done = ""                # why it counts as done

    def audio(self):
        if self.files:
            return self.files
        if self.source.is_file():
            return [self.source]
        return [p for p in self.source.rglob("*") if p.is_file() and p.suffix.lower() in ab.AUDIO_EXT]

    def label(self):
        rel = self.source.relative_to(SOURCES)
        if self.files and self.name != self.source.name:
            label = f"{rel}\\{self.name}" + (f"  ({len(self.files)} parts)" if len(self.files) > 1 else "")
        else:
            label = str(rel)
        return label + (f"  [{self.note}]" if self.note else "")


def name_key(name):
    """A name without numbers and part words: 'Part 2 - American Assassin' -> ('american', 'assassin')."""
    return tuple(w for w in words(re.sub(r"\d+", " ", name)) if w not in PART_WORDS)


def has_audio_below(d):
    return any(p.suffix.lower() in ab.AUDIO_EXT for p in d.rglob("*") if p.is_file())


def find_units(folder, problems):
    """Yields Units below `folder`; notes folders/files that can't be processed in `problems`."""
    entries = sorted(folder.iterdir(), key=ab.natural_key)
    files = [p for p in entries if p.is_file() and p.suffix.lower() in ab.AUDIO_EXT]
    subdirs = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
    for p in entries:
        if p.is_file() and p.suffix.lower() == ".aax":
            problems.append((p, "Audible .aax (DRM): convert it to .m4b first"))

    if folder == SOURCES:
        for f in files:
            yield Unit(f)
    elif files:
        note, exts = "", {}
        for f in files:
            exts.setdefault(f.suffix.lower(), []).append(f)
        if len(exts) > 1:  # e.g. an .m4b copy and an .mp3 copy of the same book: use the biggest copy only
            keep = max(exts, key=lambda e: sum(f.stat().st_size for f in exts[e]))
            files = exts[keep]
            note = f"mixed formats: using the {keep} files, ignoring " + ", ".join(e for e in exts if e != keep)
        groups = {}
        for f in files:
            groups.setdefault((name_key(f.stem), f.suffix.lower()), []).append(f)
        sizes = [sum(f.stat().st_size for f in fs) for fs in groups.values()]
        if 1 < len(groups) <= MULTI_BOOK_MAX_GROUPS and min(sizes) >= MULTI_BOOK_MIN_BYTES:
            for fs in groups.values():
                name = re.sub(r"[\s\-_.]*\b(p|pt|part)\s*\d+(\.\d+)?\s*$", "", fs[0].stem, flags=re.I)
                yield Unit(folder, fs, name if len(fs) > 1 else fs[0].stem, note)
            subdirs = [d for d in subdirs if has_audio_below(d)]  # rare: books AND sub-folders
        else:
            yield Unit(folder, files if note else None, note=note)
            return  # sub-folders are part of this book (Book() reads audio from all of them)

    with_audio = [d for d in subdirs if has_audio_below(d)]
    for d in subdirs:
        if d not in with_audio:
            problems.append((d, "no audio files"))
    if folder != SOURCES and len(with_audio) > 1 and len({name_key(d.name) for d in with_audio}) == 1:
        yield Unit(folder)  # "Book Part 1" + "Book Part 2", "Disc1" + "Disc2": one book
        return
    for d in with_audio:
        yield from find_units(d, problems)


# ----------------------------------------------------------------------------- what is done already

def logged_sources():
    done = set()
    if ab.LOG.exists():
        with open(ab.LOG, encoding="utf-8") as f:
            done |= {Path(r["source"]).resolve() for r in csv.DictReader(f) if r.get("source")}
    if REPORT.exists():
        with open(REPORT, encoding="utf-8") as f:
            done |= {(SOURCES / r["source"]).resolve() for r in csv.DictReader(f)
                     if r.get("status") == "rebuilt" and r.get("source")}
    return done


def plex_titles():
    """(title words, author words, label) for every book in Plex_audiobooks."""
    out = []
    for p in ab.PLEX.rglob("*"):
        if p.suffix.lower() not in ab.AUDIO_EXT:
            continue
        title = re.split(r"\s\(\d{4}\)|\s\[", p.stem)[0]
        title = re.sub(r",?\s*Book \d+$", "", title)          # "Harry Potter and the ..., Book 2"
        series = re.search(r"\[(.+?)\s*[\d.]*\]\s*$", p.stem)
        author = p.relative_to(ab.PLEX).parts[0]
        part = re.search(r"\s([\d.]+)\]\s*$", p.stem)
        out.append((words(title), set(words(author)), set(words(series.group(1))) if series else set(),
                    float(part.group(1)) if part else None, str(p.relative_to(ab.PLEX))))
    return out


def plex_match(unit, titles):
    audio = unit.audio()
    names = [unit.name] + ([audio[0].stem] if len(audio) == 1 else [])
    own = {w for n in names for w in words(n)}
    numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", " ".join(names))}
    best = None
    for t_words, author, series, part, label in titles:
        if not t_words or not all(has_word(w, own) for w in t_words):
            continue
        if set(t_words) == series or len(t_words) == 1:
            # "Zodiac Academy" (book 1 of Zodiac Academy) must not match "Zodiac Academy The Big A.S.S. Party":
            # the name must have the same book number, or hold nothing besides title, author and series words.
            extra = own - set(t_words) - author - series - {"book", "books", "series", "novel"}
            if extra and part not in numbers:
                continue
        if not best or len(t_words) > len(best[0]):
            best = (t_words, label)
    return best[1] if best else ""


# ----------------------------------------------------------------------------- choosing and processing

def auto_match(book):
    """Unattended: the first Audible result whose runtime matches the copy, else None."""
    for q in book.guess_queries():
        for p in ab.audible_search(q):
            if ab.runtime_matches(p, book.total):
                print(f"\n  Audible search: \"{q}\"\n  auto-picked: {ab.describe(p, book.total)}")
                return p
    print("\n  no Audible result with a matching runtime: skipped (run it with audiobook.cmd to choose)")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="only show the plan")
    ap.add_argument("--only", help="only books whose path contains this text")
    ap.add_argument("--include-done", action="store_true", help="also process books that look done already")
    ap.add_argument("--auto", action="store_true", help="no prompts; take the runtime-matching Audible result")
    ap.add_argument("--limit", type=int, help="process at most this many books this run")
    ap.add_argument("--chapters", choices=["auto", "files", "audible"], default="auto")
    ap.add_argument("--bitrate", type=int, help="AAC kbps when encoding (default: match source, max 128)")
    ap.add_argument("--dry-run", action="store_true", help="pick matches and show the plan, write nothing")
    ap.add_argument("--keep-downloaded", action="store_true",
                    help="leave sources downloaded (default: online-only OneDrive files are freed up again)")
    args = ap.parse_args()

    problems = []
    units = list(find_units(SOURCES, problems))
    logged, titles = logged_sources(), plex_titles()
    for u in units:
        srcs = [u.source.resolve()] + [f.resolve() for f in u.audio()]
        if any(s in logged for s in srcs):
            u.done = "in processed_log.csv"
        elif m := plex_match(u, titles):
            u.done = f"in Plex: {m}"
    if args.only:
        units = [u for u in units if args.only.lower() in u.label().lower()]
    todo = [u for u in units if args.include_done or not u.done]

    print(f"Already done ({sum(bool(u.done) for u in units)}):")
    for u in units:
        if u.done:
            print(f"  {u.label()}\n      -> {u.done}")
    if problems:
        print(f"\nCan't be processed ({len(problems)}):")
        for p, why in problems:
            print(f"  {p.relative_to(SOURCES)}: {why}")
    print(f"\nTo process ({len(todo)}):")
    for u in todo:
        print(f"  {u.label()}" + (f"   [looks done: {u.done}]" if u.done else ""))
    if args.limit:
        todo = todo[:args.limit]
    if args.list or not todo:
        return
    interactive = sys.stdin.isatty() and not args.auto
    if not interactive and not args.auto:
        sys.exit("\nNo keyboard here: use --auto (or --list).")
    if interactive and input(f"\nPick Audible matches for these {len(todo)} book(s) now? [Y/n] ").strip().lower() \
            in ("n", "no"):
        return

    # same flow as audiobook.py: choose every match first, then process them all unattended
    pick = argparse.Namespace(asin=None, title=None, query=None, yes=False)
    run = argparse.Namespace(chapters=args.chapters, overwrite=False, dry_run=args.dry_run, bitrate=args.bitrate)
    queue = []
    for i, u in enumerate(todo, 1):
        print(f"\n=== [{i}/{len(todo)}] {u.label()}")
        was_online = [f for f in u.audio() if is_online_only(f)]
        try:
            book = ab.Book(u.source, files=u.files)
            book.print_summary()
            product = auto_match(book) if args.auto else ab.choose_match(book, pick)
        except KeyboardInterrupt:
            print("\n  stopped choosing; processing what was picked so far")
            break
        except Exception as e:  # noqa: BLE001 - note it and go on with the next book
            print(f"  SKIPPED: {e}")
            product = None
        if not product:
            print("  skipped")
            if not args.keep_downloaded:
                free_up(was_online)
            continue
        meta = ab.Meta(product)
        print(f"  match: {meta.title} | {meta.author} | read by {meta.narrator} | {meta.year}"
              f" | series: {meta.series or '-'} {meta.part}")
        queue.append((u, book, meta, was_online))

    print(f"\n##### Processing {len(queue)} book(s)")
    done, failed = [], []
    for i, (u, book, meta, was_online) in enumerate(queue, 1):
        print(f"\n=== [{i}/{len(queue)}] {meta.title}")
        try:
            (done if ab.process(book, meta, run) else failed).append((u, "skipped, see above"))
        except Exception as e:  # noqa: BLE001
            print(f"\n  FAILED: {e}")
            failed.append((u, str(e)))
        if not args.keep_downloaded:
            free_up(was_online)

    if args.dry_run:
        print(f"\nDry run: {len(queue)} of {len(todo)} book(s) have a match; nothing was written.")
        return
    print(f"\nFinished: {len(done)} of {len(todo)} book(s) added to Plex_audiobooks.")
    for u, why in failed:
        print(f"  not added: {u.label()}: {why}")
    not_picked = len(todo) - len(queue)
    if not_picked:
        print(f"  {not_picked} book(s) had no Audible match chosen; run them with audiobook.cmd and --query/--title.")


if __name__ == "__main__":
    main()
