# Online Library

A digital reading room. Search the catalogue by title, author, or category, read in the browser, bookmark a shelf, and download works that are free to keep.

**Unlimited Access to Thousands of Books.**

## Run locally

```bash
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8080

The catalogue lives in `data/library.db` (SQLite). If that file is missing, the app builds it on first launch. Public-domain excerpts are fetched from Project Gutenberg during that first build, so the machine needs a network connection then.

## Demonstration accounts

| Role | Email | Password |
| --- | --- | --- |
| Curator | curator@onlinelibrary.ng | Library#2026 |
| Student | amina.bala@student.ng | Reader#2026 |

Guests may browse and read a first chapter. A free student account continues the text, saves bookmarks, and downloads legal files. The curator’s desk adds, edits, and removes titles and can pause accounts.

## Collection policy

Downloads are limited to public-domain excerpts and original open educational texts written for this reading room. Links to Project Gutenberg point to the complete public-domain source. Works still in copyright are not offered as files.

Online Library is an independent demonstration. It is not affiliated with the National Library of Nigeria.
