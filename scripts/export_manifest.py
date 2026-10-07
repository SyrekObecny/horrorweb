#!/usr/bin/env python3
"""
Krok 3 — vyexportuje filmy z DB do rozbory/<pondělí>/manifest.json ve formátu,
který čte _build.py, a zkopíruje plakáty do rozbory/<pondělí>/posters/.

Do týdne patří filmy, které byly poprvé staženy tento týden, nebo mají
premiéru v okně [pondělí-7, pondělí+6]. Ruční úpravy v existujícím
manifestu se zachovají (merge podle slugu — ručně vyplněná pole vyhrávají).

Usage:
  python3 scripts/export_manifest.py
  python3 scripts/export_manifest.py --week 2026-10-05
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import date as Date

import db
from common import POSTERS_DIR, ROOT, czech_date, log, monday_of

ROZBORY_DIR = ROOT / "rozbory"


def film_entry(conn, f) -> dict:
    a = conn.execute("SELECT * FROM ai_content WHERE wikidata_id = ?", (f["wikidata_id"],)).fetchone()
    cast = conn.execute("SELECT actor, role FROM cast_members WHERE wikidata_id = ? ORDER BY ord LIMIT 6",
                        (f["wikidata_id"],)).fetchall()
    title = f["title_cs"] or f["title"]
    trivia = json.loads(a["trivia_json"]) if a and a["trivia_json"] else []
    tags = json.loads(a["tags_json"]) if a and a["tags_json"] else [t for t in (f["genre"] or "").split(" · ") if t]
    stars = a["stars"] if a and a["stars"] else 0
    return {
        "slug": f["slug"],
        "title": title,
        "orig": f"{f['orig_title']} ({f['year']})" if f["orig_title"] else title,
        "rating": a["rating"] if a and a["rating"] else "—",
        "rating_label": a["rating_label"] if a else "",
        "stars": stars,
        "year": f["year"] or "",
        "runtime": f"{f['runtime_min']} min" if f["runtime_min"] else "—",
        "director": f["director"] or "—",
        "writer": f["writer"] or "—",
        "studio": f["studio"] or "—",
        "genre": (a["genre_cs"] if a and a["genre_cs"] else f["genre"]) or "Horor",
        "country": f["country"] or "—",
        "premiere": czech_date(f["premiere_date"]),
        "premiere_date": f["premiere_date"],
        "rt_score": "—",
        "status": f["status"] or "upcoming",
        "tags": tags,
        "poster": f["poster_file"] or "",
        "trailer": "",
        "synopsis": a["synopsis"] if a else "",
        "pullquote": a["pullquote"] if a else "",
        "review": a["review"] if a else "",
        "cast": [[c["actor"], c["role"] or ""] for c in cast],
        "trivia": trivia,
        "youtube": f"https://www.youtube.com/watch?v={f['youtube_id']}" if f["youtube_id"] else "",
        "wiki": f["wiki_url"] or "",
        "source": {"wikidata": f["wikidata_id"], "imdb": f["imdb_id"] or "",
                   "poster": f["poster_source_url"] or "", "ai_model": a["model_used"] if a else ""},
    }


def merge(old: dict, prev_auto: dict, new: dict) -> dict:
    """Keep manual edits: a field in the existing manifest that differs from what
    this script generated last time (prev_auto) was hand-edited and wins."""
    out = dict(new)
    for k, v in old.items():
        if k != "source" and k in prev_auto and v != prev_auto[k]:
            out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", help="pondělí týdne (YYYY-MM-DD), default tento týden")
    args = ap.parse_args()
    week_d = Date.fromisoformat(args.week) if args.week else monday_of(Date.today())
    week = week_d.isoformat()

    conn = db.connect()
    films = conn.execute(
        "SELECT * FROM films WHERE first_seen_week = ? "
        "OR premiere_date BETWEEN date(?, '-7 days') AND date(?, '+6 days') "
        "ORDER BY premiere_date DESC", (week, week, week)).fetchall()
    # a film without a poster would render a broken image on the site
    films = [f for f in films if f["poster_file"]]
    if not films:
        log(f"⚠️  Pro týden {week} nejsou v DB žádné filmy s plakátem — nic neexportuji")
        return

    out_dir = ROZBORY_DIR / week
    (out_dir / "posters").mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    old_films = {f["slug"]: f for f in old.get("films", [])}
    # what this script generated last time — anything that differs from it was edited by hand
    auto_path = out_dir / ".auto.json"
    prev_auto = json.loads(auto_path.read_text(encoding="utf-8")) if auto_path.exists() else {}
    auto = {"films": {}}

    entries = []
    for f in films:
        shutil.copy2(POSTERS_DIR / f["poster_file"], out_dir / "posters" / f["poster_file"])
        e = film_entry(conn, f)
        auto["films"][e["slug"]] = e
        prev = prev_auto.get("films", {}).get(e["slug"], {})
        entries.append(merge(old_films[e["slug"]], prev, e) if e["slug"] in old_films else e)

    w = conn.execute("SELECT * FROM weeks WHERE week = ?", (week,)).fetchone()
    titles = ", ".join(e["title"] for e in entries[:3])
    header = {
        "week": week,
        "week_label": f"Týden {week_d.isocalendar()[1]} · {week_d.year}",
        "title": w["title"] if w else f"{czech_date(week).split(' ', 1)[1].capitalize()} — {len(entries)} hororů",
        "subtitle": w["subtitle"] if w else f"Nové horory: {titles}.",
        "intro": w["intro"] if w else f"Tento týden v kinech a na obzoru: {titles}.",
        "compiled_by": "KREVZONE bot",
    }
    auto["header"] = header
    manifest = merge({k: old[k] for k in header if k in old}, prev_auto.get("header", {}), header)
    manifest["compiled_at"] = Date.today().isoformat()
    manifest["films"] = entries
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    auto_path.write_text(json.dumps(auto, ensure_ascii=False) + "\n", encoding="utf-8")
    missing_ai = sum(1 for e in entries if not e["synopsis"])
    log(f"✅ {manifest_path.relative_to(ROOT)}: {len(entries)} filmů"
        + (f" ({missing_ai} zatím bez AI textů)" if missing_ai else ""))


if __name__ == "__main__":
    main()
