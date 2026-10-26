"""SQLite catalogue, accounts, and Project Gutenberg excerpts."""

from __future__ import annotations

import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

from books_data import BOOKS

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    student_id TEXT,
    faculty TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    short_title TEXT,
    author TEXT NOT NULL,
    year INTEGER,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    publisher TEXT,
    pages INTEGER,
    language TEXT DEFAULT 'English',
    call_number TEXT,
    featured INTEGER DEFAULT 0,
    popular INTEGER DEFAULT 0,
    downloadable INTEGER DEFAULT 0,
    cover_layout TEXT DEFAULT 'classic',
    cover_bg TEXT,
    cover_fg TEXT,
    cover_accent TEXT,
    cover_author TEXT,
    reads INTEGER DEFAULT 0,
    downloads INTEGER DEFAULT 0,
    text_format TEXT DEFAULT 'prose',
    source_note TEXT,
    gutenberg_id INTEGER,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, book_id)
);
CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_pos INTEGER NOT NULL DEFAULT 1,
    percent INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, book_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_flag INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_books_category ON books(category);
CREATE INDEX IF NOT EXISTS idx_books_added ON books(added_at);
CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, position);
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    count = conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
    if count == 0:
        seed(conn)
    conn.close()


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_gutenberg(eid):
    url = f"https://www.gutenberg.org/cache/epub/{eid}/pg{eid}.txt"
    req = urllib.request.Request(url, headers={"User-Agent": "OnlineLibraryReadingRoom/1.0"})
    with urllib.request.urlopen(req, timeout=40) as response:
        raw = response.read()
    return raw.decode("utf-8", "replace")


def strip_boilerplate(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    start = re.search(r"\*\*\* START OF (THIS |THE )?PROJECT GUTENBERG EBOOK[^\n]*\*\*\*", text)
    if start:
        text = text[start.end():]
    end = re.search(r"\*\*\* END OF (THIS |THE )?PROJECT GUTENBERG EBOOK", text)
    if end:
        text = text[:end.start()]
    return text


def pretty_heading(raw):
    raw = re.sub(r"\s+", " ", raw).strip(" .]")
    match = re.match(r"(?i)^(chapter|letter|book)\s+([0-9IVXLC]+)\b(.*)$", raw)
    if not match:
        return raw.title() if raw.isupper() else raw
    kind = match.group(1).title()
    number = match.group(2).upper()
    rest = match.group(3).strip(" .:-")
    if rest:
        if rest.isupper():
            rest = rest.title()
        return f"{kind} {number}. {rest}"
    return f"{kind} {number}"


def clean_prose(text, limit=12000):
    text = re.sub(r"_([^_\n]+)_", r"\1", text)
    text = re.sub(r"\[Illustration:[^\]]*\]", "", text, flags=re.I)
    text = re.sub(r"\[Illustration\]", "", text, flags=re.I)
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        joined = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if len(joined) < 40:
            continue
        paragraphs.append(joined)
    body = "\n\n".join(paragraphs).strip()
    if len(body) <= limit:
        return body
    cut = body.rfind("\n\n", 0, limit)
    if cut < int(limit * 0.55):
        cut = body.rfind(". ", 0, limit)
        if cut > 0:
            cut += 1
    excerpt = body[:cut].strip()
    return excerpt + "\n\nThis reading-room edition stops here. The work continues in the full public-domain text."


def chapters_from_headings(text, pattern, limit=3, min_gap=1800, title_fn=None):
    matches = list(pattern.finditer(text))
    chapters = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = clean_prose(text[match.end():end])
        if len(content) < min_gap:
            continue
        raw = match.group(0)
        title = title_fn(match) if title_fn else pretty_heading(raw)
        chapters.append({"title": title, "content": content})
        if len(chapters) >= limit:
            break
    return chapters


def split_standard(text, limit):
    pattern = re.compile(
        r"(?m)^[ \t]*(?:CHAPTER|Chapter|LETTER|Letter|BOOK|Book)\s+([0-9]+|[IVXLCDM]+)\b[^\n]*$"
    )
    return chapters_from_headings(text, pattern, limit=limit, min_gap=1600)


def split_gatsby(text, limit):
    pattern = re.compile(r"(?m)^[ \t]{8,}([IVXLC]+)[ \t]*$")

    def title(match):
        return f"Chapter {match.group(1)}"

    return chapters_from_headings(text, pattern, limit=limit, min_gap=2000, title_fn=title)


def split_origin(text, limit):
    """Skip the contents list; the real text begins at the later CHAPTER I."""
    markers = list(re.finditer(r"(?m)^CHAPTER I\.?\s*$", text))
    if not markers:
        markers = list(re.finditer(r"(?m)^CHAPTER I\b.*$", text))
    start = markers[-1].start() if markers else 0
    return split_standard(text[start:], limit)


def split_wells(text, limit):
    pattern = re.compile(r"(?m)^([IVXLC]+)\n([A-Z][A-Z0-9 ,.'’:\-]{3,80})\n")

    def title(match):
        words = match.group(2).title().split()
        small = {"Of", "The", "In", "And", "A", "An", "To", "For"}
        titled = [word.lower() if index and word in small else word for index, word in enumerate(words)]
        return f"{match.group(1)}. {' '.join(titled)}"

    return chapters_from_headings(text, pattern, limit=limit, min_gap=1200, title_fn=title)


def split_sonnets(text, groups=3, per=12):
    heads = list(re.finditer(r"(?m)^([IVXLC]+)\s*$", text))
    sonnets = []
    for index, match in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        lines = [line.strip() for line in text[match.end():end].splitlines()]
        body = "\n".join(lines).strip()
        if len(body) < 80:
            continue
        sonnets.append((match.group(1), body))
    chapters = []
    for group in range(groups):
        chunk = sonnets[group * per:(group + 1) * per]
        if not chunk:
            break
        parts = []
        for number, body in chunk:
            parts.append(number)
            parts.append(body)
            parts.append("")
        chapters.append({
            "title": f"Sonnets {chunk[0][0]}–{chunk[-1][0]}",
            "content": "\n".join(parts).strip(),
        })
    return chapters


def excerpts_for(book):
    eid = book.get("gutenberg_id")
    if not eid:
        return book.get("chapters") or []
    try:
        text = strip_boilerplate(fetch_gutenberg(eid))
    except Exception as exc:
        print(f"Gutenberg fetch failed for {eid}: {exc}")
        return book.get("chapters") or [{
            "title": "Text unavailable",
            "content": "The public-domain excerpt could not be fetched when this catalogue was built. The Project Gutenberg link on the book page still points to the full work.",
        }]
    limit = book.get("chapter_limit") or 3
    if book.get("text_format") == "verse":
        chapters = split_sonnets(text)
    elif eid == 64317:
        chapters = split_gatsby(text, limit)
    elif eid == 35461:
        chapters = split_wells(text, limit)
    elif eid == 1228:
        chapters = split_origin(text, limit)
    else:
        chapters = split_standard(text, limit)
    if not chapters:
        print(f"No chapters parsed for {eid}; storing opening excerpt")
        chapters = [{"title": "Opening", "content": clean_prose(text[:8000])}]
    return chapters


def seed(conn):
    stamp = now()
    conn.execute(
        """INSERT INTO users (name, email, password_hash, role, student_id, faculty, active, created_at)
           VALUES (?, ?, ?, 'admin', 'STAFF-01', 'Library and Information Science', 1, ?)""",
        ("Chinedu Okonkwo", "curator@onlinelibrary.ng", generate_password_hash("Library#2026"), stamp),
    )
    conn.execute(
        """INSERT INTO users (name, email, password_hash, role, student_id, faculty, active, created_at)
           VALUES (?, ?, ?, 'student', 'U/2024/1186', 'Computer Science', 1, ?)""",
        ("Amina Bala", "amina.bala@student.ng", generate_password_hash("Reader#2026"), stamp),
    )
    student_id = conn.execute("SELECT id FROM users WHERE email = ?", ("amina.bala@student.ng",)).fetchone()["id"]
    conn.execute(
        """INSERT INTO messages (name, email, subject, body, created_at, read_flag)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (
            "Ibrahim Musa",
            "ibrahim.musa@student.ng",
            "Request for a cataloguing workbook",
            "Good afternoon. Could the desk add a short workbook of practice call numbers for LIS 210? Our seminar starts next month.",
            stamp,
        ),
    )
    for book in BOOKS:
        cur = conn.execute(
            """INSERT INTO books (
                title, short_title, author, year, category, description, publisher, pages,
                language, call_number, featured, popular, downloadable, cover_layout,
                cover_bg, cover_fg, cover_accent, cover_author, reads, downloads,
                text_format, source_note, gutenberg_id, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                book["title"], book.get("short_title") or book["title"], book["author"], book.get("year"),
                book["category"], book["description"], book.get("publisher"), book.get("pages"),
                book.get("language") or "English", book.get("call_number"), book.get("featured", 0),
                book.get("popular", 0), book.get("downloadable", 0), book.get("cover_layout") or "classic",
                book.get("cover_bg"), book.get("cover_fg"), book.get("cover_accent"),
                book.get("cover_author") or book["author"], book.get("reads") or 0,
                book.get("text_format") or "prose", book.get("source_note"), book.get("gutenberg_id"),
                book.get("added_at") or stamp[:10],
            ),
        )
        book_id = cur.lastrowid
        for position, chapter in enumerate(excerpts_for(book), start=1):
            conn.execute(
                "INSERT INTO chapters (book_id, position, title, content) VALUES (?, ?, ?, ?)",
                (book_id, position, chapter["title"], chapter["content"]),
            )
        print(f"seeded {book['title']}")
    cs = conn.execute("SELECT id FROM books WHERE call_number = 'CSC 101 INT'").fetchone()["id"]
    pride = conn.execute("SELECT id FROM books WHERE call_number = 'LIT 1813 AUS'").fetchone()["id"]
    lis = conn.execute("SELECT id FROM books WHERE call_number = 'LIS 100 FOU'").fetchone()["id"]
    for book_id in (cs, pride, lis):
        conn.execute(
            "INSERT INTO bookmarks (user_id, book_id, created_at) VALUES (?, ?, ?)",
            (student_id, book_id, stamp),
        )
    conn.execute(
        """INSERT INTO progress (user_id, book_id, chapter_pos, percent, updated_at)
           VALUES (?, ?, 1, 42, ?)""",
        (student_id, cs, stamp),
    )
    conn.commit()
    print(f"Catalogue ready at {DB_PATH}")
