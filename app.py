"""Online Library — a digital reading room.

Run:  python app.py
Then open http://127.0.0.1:8080
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from fpdf import FPDF

from books_data import CATEGORIES, PALETTES
from db import DB_PATH, connect, init_db, now

app = Flask(__name__)
app.secret_key = "online-library-reading-room-2026"
app.permanent_session_lifetime = timedelta(days=14)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "data" / "pdfs"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
CATEGORY_BY_SLUG = {item["slug"]: item for item in CATEGORIES}
CATEGORY_BY_NAME = {item["name"]: item for item in CATEGORIES}


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def query_one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return query_one("SELECT * FROM users WHERE id = ? AND active = 1", (uid,))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            flash("The curator’s desk is restricted.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def safe_next(url):
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return url_for("home")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def csrf_protect():
    if request.method != "POST":
        return None
    token = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
    if token and token == session.get("csrf"):
        return None
    if request.is_json or request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=False, error="Session expired. Refresh the page."), 400
    flash("Your session expired. Please try that again.", "error")
    return redirect(request.referrer or url_for("home"))


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "categories": CATEGORIES,
        "csrf": csrf_token(),
        "year": 2026,
        "palettes": PALETTES,
    }


@app.template_filter("title_lines")
def title_lines(title, width=16):
    words = (title or "").replace("—", "— ").split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if len(trial) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:6] or [""]


@app.template_filter("title_size")
def title_size(lines):
    longest = max((len(line) for line in lines), default=0)
    count = len(lines)
    size = 34 if count <= 2 else 28 if count <= 3 else 24 if count <= 4 else 20
    if longest > 16:
        size -= 4
    if longest > 22:
        size -= 2
    return max(size, 18)


def bookmarked_ids(user):
    if not user:
        return set()
    rows = query("SELECT book_id FROM bookmarks WHERE user_id = ?", (user["id"],))
    return {row["book_id"] for row in rows}


def chapters_for(book_id):
    return query(
        "SELECT * FROM chapters WHERE book_id = ? ORDER BY position",
        (book_id,),
    )


def reading_minutes(chapters):
    words = sum(len((chapter["content"] or "").split()) for chapter in chapters)
    return max(1, round(words / 220)) or 1


@app.route("/")
def home():
    featured = query(
        """SELECT * FROM books
           WHERE featured = 1 OR popular = 1
           ORDER BY featured DESC, reads DESC, title
           LIMIT 8"""
    )
    featured_ids = [row["id"] for row in featured] or [0]
    placeholders = ",".join("?" for _ in featured_ids)
    recent = query(
        f"""SELECT * FROM books
            WHERE id NOT IN ({placeholders})
            ORDER BY added_at DESC, id DESC
            LIMIT 8""",
        featured_ids,
    )
    counts = {
        item["name"]: query_one(
            "SELECT COUNT(*) AS n FROM books WHERE category = ?", (item["name"],)
        )["n"]
        for item in CATEGORIES
    }
    stats = query_one(
        """SELECT
             (SELECT COUNT(*) FROM books) AS titles,
             (SELECT COUNT(*) FROM books WHERE downloadable = 1) AS downloads,
             (SELECT COUNT(*) FROM users WHERE role = 'student') AS readers"""
    )
    return render_template(
        "index.html",
        featured=featured,
        recent=recent,
        counts=counts,
        stats=stats,
        saved=bookmarked_ids(current_user()),
    )


@app.route("/books")
def books():
    return render_book_list(
        category=request.args.get("category", "").strip(),
        heading="All Books",
        lede="Search the shelves by title, author, or category.",
    )


@app.route("/category/<slug>")
def category(slug):
    item = CATEGORY_BY_SLUG.get(slug)
    if not item:
        abort(404)
    return render_book_list(
        category=item["name"],
        heading=item["name"],
        lede=item["blurb"],
    )


def render_book_list(category, heading, lede):
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "title")
    view = request.args.get("view", "grid")
    page = max(request.args.get("page", 1, type=int), 1)
    sql = "SELECT * FROM books WHERE 1=1"
    params = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    for word in q.split()[:8]:
        like = f"%{word}%"
        sql += " AND (title LIKE ? OR author LIKE ? OR category LIKE ? OR description LIKE ? OR call_number LIKE ?)"
        params.extend([like, like, like, like, like])
    sort_map = {
        "title": "title COLLATE NOCASE",
        "newest": "added_at DESC, id DESC",
        "year": "year DESC, title COLLATE NOCASE",
        "popular": "reads DESC, title COLLATE NOCASE",
    }
    if q:
        like = f"%{q.split()[0]}%"
        order = "CASE WHEN title LIKE ? THEN 0 WHEN author LIKE ? THEN 1 ELSE 2 END, title COLLATE NOCASE"
        params.extend([like, like])
    else:
        order = sort_map.get(sort, sort_map["title"])
    rows = query(sql + f" ORDER BY {order}", params)
    per_page = 500 if view == "shelf" else 12
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    visible = rows[start:start + per_page]
    grouped = []
    if view == "shelf":
        by_name = {}
        for book in rows:
            by_name.setdefault(book["category"], []).append(book)
        grouped = [(item["name"], by_name.get(item["name"], [])) for item in CATEGORIES if by_name.get(item["name"])]
    return render_template(
        "books.html",
        books=visible,
        grouped=grouped,
        heading=heading,
        lede=lede,
        q=q,
        sort=sort,
        view=view,
        category=category,
        total=total,
        page=page,
        pages=pages,
        saved=bookmarked_ids(current_user()),
    )


@app.route("/book/<int:book_id>")
def book_detail(book_id):
    book = query_one("SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        abort(404)
    chapters = chapters_for(book_id)
    related = query(
        """SELECT * FROM books
           WHERE category = ? AND id != ?
           ORDER BY popular DESC, reads DESC LIMIT 4""",
        (book["category"], book_id),
    )
    user = current_user()
    saved = False
    progress = None
    if user:
        saved = query_one(
            "SELECT 1 FROM bookmarks WHERE user_id = ? AND book_id = ?",
            (user["id"], book_id),
        ) is not None
        progress = query_one(
            "SELECT * FROM progress WHERE user_id = ? AND book_id = ?",
            (user["id"], book_id),
        )
    slug = CATEGORY_BY_NAME.get(book["category"], {}).get("slug", "")
    return render_template(
        "book.html",
        book=book,
        chapters=chapters,
        related=related,
        saved=saved,
        progress=progress,
        minutes=reading_minutes(chapters),
        category_slug=slug,
    )


@app.route("/categories")
def categories_page():
    counts = {
        item["name"]: query_one(
            "SELECT COUNT(*) AS n FROM books WHERE category = ?", (item["name"],)
        )["n"]
        for item in CATEGORIES
    }
    return render_template("categories.html", counts=counts)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        if not name or not email or "@" not in email or not subject or not body:
            flash("Please complete every field with a valid email.", "error")
        else:
            get_db().execute(
                "INSERT INTO messages (name, email, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (name[:80], email[:120], subject[:140], body[:4000], now()),
            )
            get_db().commit()
            flash("Your note is on the curator’s desk.", "success")
            return redirect(url_for("contact"))
    return render_template("contact.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("account"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = query_one("SELECT * FROM users WHERE email = ?", (email,))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Those details do not match our register.", "error")
        elif not user["active"]:
            flash("This account has been paused. Write to the reading room for help.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["csrf"] = secrets.token_hex(16)
            session.permanent = bool(request.form.get("remember"))
            flash(f"Welcome back, {user['name'].split()[0]}.", "success")
            dest = safe_next(request.form.get("next") or request.args.get("next"))
            if user["role"] == "admin" and dest == url_for("home"):
                dest = url_for("admin")
            return redirect(dest)
    return render_template("login.html", next_url=request.args.get("next", ""))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("account"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        faculty = request.form.get("faculty", "").strip()
        student_id = request.form.get("student_id", "").strip()
        if len(name) < 2 or "@" not in email or len(password) < 8 or password != confirm or not faculty:
            flash("Check your name, email, faculty, and a password of at least 8 characters.", "error")
        elif not request.form.get("terms"):
            flash("Please accept the reading-room terms.", "error")
        elif query_one("SELECT 1 FROM users WHERE email = ?", (email,)):
            flash("That email already has a shelf. Try logging in.", "error")
        else:
            get_db().execute(
                """INSERT INTO users (name, email, password_hash, role, student_id, faculty, active, created_at)
                   VALUES (?, ?, ?, 'student', ?, ?, 1, ?)""",
                (name[:80], email[:120], generate_password_hash(password), student_id[:32], faculty, now()),
            )
            get_db().commit()
            user = query_one("SELECT id, name FROM users WHERE email = ?", (email,))
            session.clear()
            session["user_id"] = user["id"]
            session["csrf"] = secrets.token_hex(16)
            flash("Your shelf is ready.", "success")
            return redirect(url_for("account"))
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have left the reading room. The shelves stay open.", "success")
    return redirect(url_for("home"))


@app.route("/account")
@login_required
def account():
    user = current_user()
    saved = query(
        """SELECT books.* FROM bookmarks
           JOIN books ON books.id = bookmarks.book_id
           WHERE bookmarks.user_id = ?
           ORDER BY bookmarks.created_at DESC""",
        (user["id"],),
    )
    continue_rows = query(
        """SELECT books.*, progress.chapter_pos, progress.percent
           FROM progress JOIN books ON books.id = progress.book_id
           WHERE progress.user_id = ?
           ORDER BY progress.updated_at DESC LIMIT 4""",
        (user["id"],),
    )
    recommended = query(
        """SELECT * FROM books WHERE category = ?
           ORDER BY popular DESC, reads DESC LIMIT 4""",
        (user["faculty"],),
    )
    return render_template(
        "account.html",
        saved=saved,
        continue_rows=continue_rows,
        recommended=recommended,
    )


@app.post("/bookmark/<int:book_id>")
@login_required
def bookmark(book_id):
    user = current_user()
    book = query_one("SELECT id, title FROM books WHERE id = ?", (book_id,))
    if not book:
        abort(404)
    existing = query_one(
        "SELECT 1 FROM bookmarks WHERE user_id = ? AND book_id = ?",
        (user["id"], book_id),
    )
    db = get_db()
    if existing:
        db.execute("DELETE FROM bookmarks WHERE user_id = ? AND book_id = ?", (user["id"], book_id))
        state = False
        message = f"Removed “{book['title']}” from your shelf."
    else:
        db.execute(
            "INSERT INTO bookmarks (user_id, book_id, created_at) VALUES (?, ?, ?)",
            (user["id"], book_id, now()),
        )
        state = True
        message = f"Saved “{book['title']}” to your shelf."
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, saved=state, message=message)
    flash(message, "success")
    return redirect(request.referrer or url_for("book_detail", book_id=book_id))


@app.route("/read/<int:book_id>")
def read(book_id):
    book = query_one("SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        abort(404)
    chapters = chapters_for(book_id)
    if not chapters:
        flash("This title has no reading-room text yet.", "error")
        return redirect(url_for("book_detail", book_id=book_id))
    requested = request.args.get("chapter", 1, type=int)
    user = current_user()
    gated = user is None and requested > 1
    position = 1 if gated else min(max(requested, 1), len(chapters))
    chapter = chapters[position - 1]
    saved = False
    if user:
        saved = query_one(
            "SELECT 1 FROM bookmarks WHERE user_id = ? AND book_id = ?",
            (user["id"], book_id),
        ) is not None
        get_db().execute("UPDATE books SET reads = reads + 1 WHERE id = ?", (book_id,))
        get_db().commit()
    else:
        # Count a guest opening once per session so the shelf does not inflate wildly.
        seen = session.setdefault("opened", [])
        if book_id not in seen:
            seen.append(book_id)
            session["opened"] = seen[-40:]
            get_db().execute("UPDATE books SET reads = reads + 1 WHERE id = ?", (book_id,))
            get_db().commit()
    return render_template(
        "read.html",
        book=book,
        chapters=chapters,
        chapter=chapter,
        position=position,
        gated=gated,
        saved=saved,
    )


@app.post("/read/<int:book_id>/progress")
def save_progress(book_id):
    user = current_user()
    if not user:
        return jsonify(ok=False), 401
    payload = request.get_json(silent=True) or {}
    chapter_pos = int(payload.get("chapter") or request.form.get("chapter") or 1)
    percent = max(0, min(100, int(payload.get("percent") or request.form.get("percent") or 0)))
    get_db().execute(
        """INSERT INTO progress (user_id, book_id, chapter_pos, percent, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, book_id) DO UPDATE SET
             chapter_pos = excluded.chapter_pos,
             percent = excluded.percent,
             updated_at = excluded.updated_at""",
        (user["id"], book_id, chapter_pos, percent, now()),
    )
    get_db().commit()
    return jsonify(ok=True)


@app.route("/download/<int:book_id>")
@login_required
def download(book_id):
    book = query_one("SELECT * FROM books WHERE id = ?", (book_id,))
    if not book or not book["downloadable"]:
        flash("This title is not offered for download.", "error")
        return redirect(url_for("book_detail", book_id=book_id))
    chapters = chapters_for(book_id)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", book["title"].lower()).strip("-") or f"book-{book_id}"
    path = PDF_DIR / f"{slug}.pdf"
    build_pdf(path, book, chapters)
    get_db().execute("UPDATE books SET downloads = downloads + 1 WHERE id = ?", (book_id,))
    get_db().commit()
    return send_file(path, as_attachment=True, download_name=f"{slug}.pdf")


def build_pdf(path, book, chapters):
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("DejaVu", "", FONT)
    pdf.add_font("DejaVu", "B", FONT_BOLD)
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 12)
    pdf.set_text_color(110, 31, 54)
    pdf.cell(0, 8, "ONLINE LIBRARY", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("DejaVu", "B", 20)
    pdf.set_text_color(42, 24, 22)
    pdf.multi_cell(0, 10, book["title"], align="C")
    pdf.ln(2)
    pdf.set_font("DejaVu", "", 12)
    pdf.set_text_color(90, 60, 50)
    pdf.cell(0, 8, book["author"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"{book['year'] or ''}  ·  {book['category']}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(50, 40, 36)
    pdf.multi_cell(0, 6, book["source_note"] or "")
    for chapter in chapters:
        pdf.add_page()
        pdf.set_font("DejaVu", "B", 16)
        pdf.set_text_color(110, 31, 54)
        pdf.multi_cell(0, 8, chapter["title"])
        pdf.ln(3)
        pdf.set_font("DejaVu", "", 11)
        pdf.set_text_color(30, 24, 22)
        content = chapter["content"].replace("[code]", "").replace("[/code]", "")
        for paragraph in content.split("\n\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            pdf.multi_cell(0, 6, paragraph)
            pdf.ln(2)
    pdf.output(str(path))


@app.route("/admin")
@admin_required
def admin():
    stats = query_one(
        """SELECT
            (SELECT COUNT(*) FROM books) AS books,
            (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM bookmarks) AS bookmarks,
            (SELECT COUNT(*) FROM messages) AS messages,
            (SELECT COALESCE(SUM(reads), 0) FROM books) AS reads,
            (SELECT COALESCE(SUM(downloads), 0) FROM books) AS downloads"""
    )
    books = query("SELECT * FROM books ORDER BY added_at DESC, title")
    users = query("SELECT id, name, email, role, faculty, student_id, active, created_at FROM users ORDER BY created_at DESC")
    messages = query("SELECT * FROM messages ORDER BY created_at DESC")
    return render_template("admin.html", stats=stats, books=books, users=users, messages=messages)


@app.route("/admin/books/new", methods=["GET", "POST"])
@admin_required
def admin_new_book():
    if request.method == "POST":
        book_id = save_book_from_form()
        flash("Title added to the catalogue.", "success")
        return redirect(url_for("admin_edit_book", book_id=book_id))
    return render_template("admin_book.html", book=None, chapters=[])


@app.route("/admin/books/<int:book_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_book(book_id):
    book = query_one("SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        abort(404)
    if request.method == "POST":
        save_book_from_form(book_id)
        flash("Catalogue record updated.", "success")
        return redirect(url_for("admin_edit_book", book_id=book_id))
    return render_template("admin_book.html", book=book, chapters=chapters_for(book_id))


def save_book_from_form(book_id=None):
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    if not title or not author or category not in CATEGORY_BY_NAME or not description:
        abort(400)
    palette_name = request.form.get("palette") or "burgundy"
    palette = PALETTES.get(palette_name, PALETTES["burgundy"])
    year = request.form.get("year", type=int)
    pages = request.form.get("pages", type=int)
    fields = (
        title, request.form.get("short_title", "").strip() or title[:28], author, year, category,
        description, request.form.get("publisher", "").strip() or "Online Library Press", pages,
        request.form.get("call_number", "").strip(), 1 if request.form.get("featured") else 0,
        1 if request.form.get("popular") else 0, 1 if request.form.get("downloadable") else 0,
        request.form.get("cover_layout") or "classic", palette[0], palette[1], palette[2],
        request.form.get("cover_author", "").strip() or author, request.form.get("source_note", "").strip(),
    )
    db = get_db()
    if book_id:
        db.execute(
            """UPDATE books SET title=?, short_title=?, author=?, year=?, category=?, description=?,
               publisher=?, pages=?, call_number=?, featured=?, popular=?, downloadable=?,
               cover_layout=?, cover_bg=?, cover_fg=?, cover_accent=?, cover_author=?, source_note=?
               WHERE id=?""",
            (*fields, book_id),
        )
    else:
        cur = db.execute(
            """INSERT INTO books (
                title, short_title, author, year, category, description, publisher, pages,
                call_number, featured, popular, downloadable, cover_layout, cover_bg, cover_fg,
                cover_accent, cover_author, source_note, language, reads, downloads, text_format, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'English', 0, 0, 'prose', ?)""",
            (*fields, now()[:10]),
        )
        book_id = cur.lastrowid
    chapter_title = request.form.get("chapter_title", "").strip()
    chapter_content = request.form.get("chapter_content", "").strip()
    if chapter_title and chapter_content:
        position = query_one("SELECT COALESCE(MAX(position), 0) + 1 AS n FROM chapters WHERE book_id = ?", (book_id,))["n"]
        db.execute(
            "INSERT INTO chapters (book_id, position, title, content) VALUES (?, ?, ?, ?)",
            (book_id, position, chapter_title, chapter_content),
        )
    db.commit()
    clear_pdf(book_id)
    return book_id


@app.post("/admin/books/<int:book_id>/delete")
@admin_required
def admin_delete_book(book_id):
    get_db().execute("DELETE FROM books WHERE id = ?", (book_id,))
    get_db().commit()
    clear_pdf(book_id)
    flash("Title removed from the catalogue.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/chapters/<int:chapter_id>/delete")
@admin_required
def admin_delete_chapter(chapter_id):
    row = query_one("SELECT book_id FROM chapters WHERE id = ?", (chapter_id,))
    if not row:
        abort(404)
    get_db().execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
    get_db().commit()
    clear_pdf(row["book_id"])
    flash("Chapter removed.", "success")
    return redirect(url_for("admin_edit_book", book_id=row["book_id"]))


@app.post("/admin/users/<int:user_id>/toggle")
@admin_required
def admin_toggle_user(user_id):
    actor = current_user()
    if actor["id"] == user_id:
        flash("You cannot pause your own desk.", "error")
        return redirect(url_for("admin"))
    get_db().execute("UPDATE users SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,))
    get_db().commit()
    flash("Reader status updated.", "success")
    return redirect(url_for("admin"))


def clear_pdf(book_id):
    if PDF_DIR.exists():
        for path in PDF_DIR.glob("*.pdf"):
            # Titles can change; drop cached files so the next download is fresh.
            path.unlink(missing_ok=True)


@app.route("/api/suggest")
def suggest():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify(results=[])
    like = f"%{q}%"
    rows = query(
        """SELECT id, title, author, category FROM books
           WHERE title LIKE ? OR author LIKE ? OR category LIKE ?
           ORDER BY CASE WHEN title LIKE ? THEN 0 ELSE 1 END, reads DESC
           LIMIT 6""",
        (like, like, like, like),
    )
    return jsonify(results=[dict(row) for row in rows])


@app.route("/sitemap.xml")
def sitemap():
    base = request.url_root.rstrip("/")
    urls = [base + path for path in ("/", "/books", "/categories", "/about", "/contact", "/login", "/register")]
    for item in CATEGORIES:
        urls.append(base + f"/category/{item['slug']}")
    for row in query("SELECT id FROM books"):
        urls.append(base + f"/book/{row['id']}")
    body = ["<?xml version='1.0' encoding='UTF-8'?>", "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"]
    body.extend(f"<url><loc>{url}</loc></url>" for url in urls)
    body.append("</urlset>")
    return "\n".join(body), 200, {"Content-Type": "application/xml"}


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nAllow: /\n", 200, {"Content-Type": "text/plain"}


@app.errorhandler(404)
def not_found(_exc):
    return render_template("404.html"), 404


@app.errorhandler(400)
def bad_request(_exc):
    flash("That form could not be read. Please try again.", "error")
    return redirect(request.referrer or url_for("home"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=False)
