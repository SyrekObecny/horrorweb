"""SQLite schema and helpers for the KREVZONE film database."""
from __future__ import annotations

import sqlite3

from common import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
  wikidata_id       TEXT PRIMARY KEY,
  slug              TEXT UNIQUE NOT NULL,
  title             TEXT NOT NULL,
  title_cs          TEXT,
  orig_title        TEXT,
  year              TEXT,
  runtime_min       INTEGER,
  director          TEXT,
  writer            TEXT,
  studio            TEXT,
  country           TEXT,
  genre             TEXT,
  premiere_date     TEXT,
  status            TEXT,
  raw_summary_en    TEXT,
  raw_summary_cs    TEXT,
  poster_file       TEXT,
  poster_source_url TEXT,
  youtube_id        TEXT,
  wiki_url          TEXT,
  imdb_id           TEXT,
  first_seen_week   TEXT,
  fetched_at        TEXT,
  updated_at        TEXT
);
CREATE TABLE IF NOT EXISTS cast_members (
  wikidata_id TEXT NOT NULL REFERENCES films(wikidata_id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  actor       TEXT NOT NULL,
  role        TEXT,
  PRIMARY KEY (wikidata_id, ord)
);
CREATE TABLE IF NOT EXISTS ai_content (
  wikidata_id  TEXT PRIMARY KEY REFERENCES films(wikidata_id) ON DELETE CASCADE,
  synopsis     TEXT,
  pullquote    TEXT,
  review       TEXT,
  rating       TEXT,
  rating_label TEXT,
  stars        INTEGER,
  trivia_json  TEXT,
  tags_json    TEXT,
  genre_cs     TEXT,
  model_used   TEXT,
  created_at   TEXT,
  locked       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  model       TEXT NOT NULL,
  target      TEXT NOT NULL,
  ok          INTEGER NOT NULL,
  error       TEXT,
  latency_ms  INTEGER
);
CREATE TABLE IF NOT EXISTS weeks (
  week      TEXT PRIMARY KEY,
  title     TEXT,
  subtitle  TEXT,
  intro     TEXT,
  model_used TEXT
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def unique_slug(conn: sqlite3.Connection, base: str, wikidata_id: str) -> str:
    """Return `base`, or base-2, base-3… if another film already owns it."""
    slug, n = base, 2
    while True:
        row = conn.execute("SELECT wikidata_id FROM films WHERE slug = ?", (slug,)).fetchone()
        if row is None or row["wikidata_id"] == wikidata_id:
            return slug
        slug, n = f"{base}-{n}", n + 1


def model_success_rates(conn: sqlite3.Connection) -> dict:
    """model id -> (successes, attempts) from the last 30 days of ai_runs."""
    rows = conn.execute(
        "SELECT model, SUM(ok) AS s, COUNT(*) AS n FROM ai_runs "
        "WHERE ts >= datetime('now', '-30 days') GROUP BY model"
    ).fetchall()
    return {r["model"]: (r["s"], r["n"]) for r in rows}
