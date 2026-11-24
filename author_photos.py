"""
author_photos.py - put an author photo (artist.jpg) in each Plex_audiobooks author folder that lacks one,
and in each series folder under it (Plex reads the photo from the folder directly above the book folder).

Plex's music agent finds photos only for well-known authors; it uses a local artist.jpg for the rest
(needs "Use local assets" on in the library's advanced settings, which is the default).
Photos come from Audible via Audnexus. For a folder with several authors, the first author's photo is used.

    author_photos              fill in missing photos
    author_photos --overwrite  fetch them all again
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

PLEX = Path(r"C:\Users\kruse\OneDrive\Music\Plex_audiobooks")
UA = {"User-Agent": "audiobook-organizer/1.0"}
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read()


def norm(name):
    return re.sub(r"[^a-z]", "", name.lower())


def first_author(folder_name):
    name = folder_name.split(",")[0]
    return re.sub(r"\s+-\s+(editor|translator|foreword|introduction).*$", "", name, flags=re.I).strip()


def author_photo(name):
    hits = json.loads(get("https://api.audnex.us/authors?region=us&name=" + urllib.parse.quote(name)))
    for asin in dict.fromkeys(h["asin"] for h in hits if norm(h["name"]) == norm(name)):
        info = json.loads(get(f"https://api.audnex.us/authors/{asin}?region=us"))
        if info.get("image"):
            return info["image"]
    return None


def main():
    overwrite = "--overwrite" in sys.argv
    for folder in sorted(p for p in PLEX.iterdir() if p.is_dir()):
        target = folder / "artist.jpg"
        if target.exists() and not overwrite:
            continue
        name = first_author(folder.name)
        try:
            url = author_photo(name)
        except Exception as e:
            print(f"  error   {name}: {e}")
            continue
        if not url:
            print(f"  none    {name}")
            continue
        target.write_bytes(get(url))
        print(f"  saved   {name}")
    # Plex takes the folder directly above a book's folder as the artist folder, which is the series
    # folder in Author/Series/Book. Copy the photo there too.
    audio = {".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus"}
    for book in {f.parent for f in PLEX.rglob("*") if f.suffix.lower() in audio}:
        artist_dir = book.parent
        author_dir = PLEX / book.relative_to(PLEX).parts[0]
        src, dst = author_dir / "artist.jpg", artist_dir / "artist.jpg"
        if artist_dir != author_dir and src.exists() and (overwrite or not dst.exists()):
            dst.write_bytes(src.read_bytes())
            print(f"  copied  {artist_dir.relative_to(PLEX)}")


if __name__ == "__main__":
    main()
