#!/usr/bin/env python3
"""
Krok 1 — stáhne nové horory z Wikidat + Wikipedie do SQLite (data/krevzone.db)
a jejich plakáty do media/posters/.

Stejný zdroj, ze kterého byl ručně sestaven původní web (plakáty a odkazy
`wiki` v rozbory/2026-05-12/manifest.json jsou z anglické Wikipedie).

Usage:
  python3 scripts/fetch_films.py                 # okno dnes-7 .. dnes+60 dní
  python3 scripts/fetch_films.py --since 2026-09-01 --until 2026-11-01
"""
from __future__ import annotations

import argparse
import re
import urllib.parse
from datetime import date as Date, datetime, timedelta

import db
from common import POSTERS_DIR, http, log, monday_of, slugify

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
HORROR = "Q200092"
CZECHIA = "Q213"

SPARQL = """
SELECT DISTINCT ?film WHERE {{
  ?film wdt:P31 wd:Q11424;
        wdt:P136/wdt:P279* wd:{horror};
        wdt:P577 ?date.
  FILTER(?date >= "{since}T00:00:00Z"^^xsd:dateTime && ?date <= "{until}T23:59:59Z"^^xsd:dateTime)
  ?article schema:about ?film.
  VALUES ?wiki {{ <https://en.wikipedia.org/> <https://cs.wikipedia.org/> }}
  ?article schema:isPartOf ?wiki.
}}
"""


# ---------------------------------------------------------------------------
# Wikidata
# ---------------------------------------------------------------------------

def find_film_ids(since: Date, until: Date) -> list:
    q = SPARQL.format(horror=HORROR, since=since.isoformat(), until=until.isoformat())
    res = http(WIKIDATA_SPARQL, params={"query": q, "format": "json"},
               headers={"Accept": "application/sparql-results+json"}, timeout=60)
    ids = {b["film"]["value"].rsplit("/", 1)[-1] for b in res["results"]["bindings"]}
    return sorted(ids)


def get_entities(ids: list, props: str) -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        res = http(WIKIDATA_API, params={
            "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]),
            "props": props, "languages": "cs|en", "format": "json"})
        out.update(res.get("entities", {}))
    return out


def label(entity: dict) -> str:
    labels = entity.get("labels", {})
    for lang in ("cs", "en"):
        if lang in labels:
            return labels[lang]["value"]
    return ""


def claims(entity: dict, prop: str) -> list:
    return [c for c in entity.get("claims", {}).get(prop, [])
            if c.get("rank") != "deprecated" and c["mainsnak"].get("snaktype") == "value"]


def item_ids(entity: dict, prop: str) -> list:
    return [c["mainsnak"]["datavalue"]["value"]["id"] for c in claims(entity, prop)]


def pick_premiere(entity: dict, since: Date, until: Date) -> str:
    """Prefer a Czech release date (P577 qualified with P291=Q213), else the
    earliest date inside the window, else the earliest date overall."""
    dates = []
    for c in claims(entity, "P577"):
        v = c["mainsnak"]["datavalue"]["value"]
        if v.get("precision", 0) < 11:  # need day precision
            continue
        try:
            d = Date.fromisoformat(v["time"][1:11])
        except ValueError:
            continue
        places = [q["datavalue"]["value"]["id"] for q in c.get("qualifiers", {}).get("P291", [])
                  if q.get("snaktype") == "value"]
        dates.append((d, CZECHIA in places))
    if not dates:
        return ""
    cz = [d for d, is_cz in dates if is_cz]
    if cz:
        return min(cz).isoformat()
    in_window = [d for d, _ in dates if since <= d <= until]
    return min(in_window or [d for d, _ in dates]).isoformat()


def runtime_minutes(entity: dict):
    for c in claims(entity, "P2047"):
        v = c["mainsnak"]["datavalue"]["value"]
        try:
            amount = float(v["amount"])
        except (KeyError, ValueError):
            continue
        unit = v.get("unit", "")
        if unit.endswith("Q11574"):  # seconds
            amount /= 60
        elif unit.endswith("Q25235"):  # hours
            amount *= 60
        return int(round(amount))
    return None


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------

def wiki_api(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def wiki_extract(lang: str, title: str, limit: int = 6000) -> str:
    res = http(wiki_api(lang), params={
        "action": "query", "prop": "extracts", "explaintext": 1, "titles": title,
        "redirects": 1, "format": "json", "formatversion": 2})
    pages = res.get("query", {}).get("pages", [])
    text = (pages[0].get("extract") or "") if pages else ""
    # drop reference/link sections at the end
    text = re.split(r"\n==+ (References|External links|See also|Reference|Externí odkazy|Odkazy) ==+", text)[0]
    return text.strip()[:limit]


def wiki_poster(lang: str, title: str):
    """Return (url, filename) of the film poster used in the article's infobox."""
    res = http(wiki_api(lang), params={
        "action": "query", "prop": "images|pageimages", "piprop": "name", "imlimit": 50,
        "titles": title, "redirects": 1, "format": "json", "formatversion": 2})
    pages = res.get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    names = [i["title"] for i in page.get("images", [])]
    raster = [n for n in names if re.search(r"\.(jpe?g|png|webp)$", n, re.I)]
    candidates = [n for n in raster if re.search(r"poster|plak[aá]t", n, re.I)]
    if page.get("pageimage"):
        candidates.append("File:" + page["pageimage"])
    if not candidates and len(raster) == 1:
        candidates = raster
    for name in candidates:
        info = http(wiki_api(lang), params={
            "action": "query", "prop": "imageinfo", "iiprop": "url", "titles": name,
            "format": "json", "formatversion": 2})
        ip = info.get("query", {}).get("pages", [])
        if ip and ip[0].get("imageinfo"):
            url = ip[0]["imageinfo"][0]["url"].split("?")[0]
            return url, name.split(":", 1)[1]
    return None


def wiki_youtube(lang: str, title: str) -> str:
    res = http(wiki_api(lang), params={
        "action": "query", "prop": "extlinks", "ellimit": 500, "titles": title,
        "redirects": 1, "format": "json", "formatversion": 2})
    pages = res.get("query", {}).get("pages", [])
    for link in (pages[0].get("extlinks", []) if pages else []):
        m = re.search(r"youtube\.com/watch\?v=([\w-]{11})|youtu\.be/([\w-]{11})", link.get("url", ""))
        if m:
            return m.group(1) or m.group(2)
    return ""


def commons_poster(entity: dict):
    """Fallback: P3383 (film poster) or P18 (image) on Wikimedia Commons."""
    for prop in ("P3383", "P18"):
        for c in claims(entity, prop):
            name = c["mainsnak"]["datavalue"]["value"]
            url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(name)
            return url, name
    return None


def download_poster(url: str, slug: str) -> str:
    ext = (re.search(r"\.(jpe?g|png|webp)$", url, re.I) or [None, "jpg"])[1].lower()
    ext = "jpg" if ext == "jpeg" else ext
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTERS_DIR / f"{slug}.{ext}"
    if not path.exists():
        path.write_bytes(http(url, raw=True, timeout=60))
    return path.name


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=Date.fromisoformat)
    ap.add_argument("--until", type=Date.fromisoformat)
    args = ap.parse_args()
    today = Date.today()
    since = args.since or today - timedelta(days=7)
    until = args.until or today + timedelta(days=60)
    week = monday_of(today).isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    log(f"→ Hledám horory na Wikidatech: {since} … {until}")
    ids = find_film_ids(since, until)
    log(f"  nalezeno {len(ids)} filmů s článkem na Wikipedii")
    if not ids:
        return

    films = get_entities(ids, "labels|claims|sitelinks")
    refs = set()
    for e in films.values():
        for p in ("P57", "P58", "P161", "P495", "P272", "P750", "P136"):
            refs.update(item_ids(e, p))
        for c in claims(e, "P161"):  # character items used as role qualifiers
            for q in c.get("qualifiers", {}).get("P453", []):
                if q.get("snaktype") == "value":
                    refs.add(q["datavalue"]["value"]["id"])
    ref_labels = {k: label(v) for k, v in get_entities(sorted(refs), "labels").items()}

    def names(e, prop, n=None):
        out = [ref_labels.get(i, "") for i in item_ids(e, prop)]
        out = [x for x in dict.fromkeys(out) if x]
        return out[:n] if n else out

    conn = db.connect()
    new = updated = 0
    for qid, e in films.items():
        sitelinks = e.get("sitelinks", {})
        en_title = sitelinks.get("enwiki", {}).get("title")
        cs_title = sitelinks.get("cswiki", {}).get("title")
        title_en = e.get("labels", {}).get("en", {}).get("value") or label(e)
        title_cs = e.get("labels", {}).get("cs", {}).get("value") or ""
        premiere = pick_premiere(e, since, until)
        if not premiere:
            log(f"  – {title_en}: bez přesného data premiéry, přeskočeno")
            continue

        existing = conn.execute("SELECT * FROM films WHERE wikidata_id = ?", (qid,)).fetchone()
        slug = existing["slug"] if existing else db.unique_slug(conn, slugify(title_en), qid)

        try:
            summary_en = wiki_extract("en", en_title) if en_title else ""
            summary_cs = wiki_extract("cs", cs_title) if cs_title else ""
            youtube = ""
            for c in claims(e, "P1651"):
                youtube = c["mainsnak"]["datavalue"]["value"]
                break
            if not youtube and en_title:
                youtube = wiki_youtube("en", en_title)

            poster_file = existing["poster_file"] if existing else None
            poster_url = existing["poster_source_url"] if existing else None
            if not poster_file:
                found = (wiki_poster("en", en_title) if en_title else None) \
                    or (wiki_poster("cs", cs_title) if cs_title else None) \
                    or commons_poster(e)
                if found:
                    poster_url = found[0]
                    poster_file = download_poster(poster_url, slug)
        except Exception as exc:  # one bad film must not kill the weekly run
            log(f"  ✗ {title_en}: {exc}")
            continue

        status = "upcoming" if Date.fromisoformat(premiere) > today else "kina"
        imdb = next((c["mainsnak"]["datavalue"]["value"] for c in claims(e, "P345")), "")
        wiki_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(en_title.replace(' ', '_'))}" if en_title \
            else f"https://cs.wikipedia.org/wiki/{urllib.parse.quote(cs_title.replace(' ', '_'))}"

        row = dict(
            wikidata_id=qid, slug=slug, title=title_en, title_cs=title_cs,
            orig_title=title_en, year=premiere[:4], runtime_min=runtime_minutes(e),
            director=", ".join(names(e, "P57")), writer=", ".join(names(e, "P58")),
            studio=", ".join(names(e, "P272", 2) or names(e, "P750", 2)),
            country=" · ".join(names(e, "P495")), genre=" · ".join(names(e, "P136", 3)),
            premiere_date=premiere, status=status,
            raw_summary_en=summary_en, raw_summary_cs=summary_cs,
            poster_file=poster_file, poster_source_url=poster_url,
            youtube_id=youtube, wiki_url=wiki_url, imdb_id=imdb,
            first_seen_week=existing["first_seen_week"] if existing else week,
            fetched_at=existing["fetched_at"] if existing else now, updated_at=now,
        )
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO films ({cols}) VALUES ({', '.join('?' * len(row))}) "
            f"ON CONFLICT(wikidata_id) DO UPDATE SET "
            + ", ".join(f"{k}=excluded.{k}" for k in row if k != "wikidata_id"),
            list(row.values()))

        # cast: actor + role (P453 "character role" qualifier, if present)
        conn.execute("DELETE FROM cast_members WHERE wikidata_id = ?", (qid,))
        for i, c in enumerate(claims(e, "P161")[:8]):
            actor = ref_labels.get(c["mainsnak"]["datavalue"]["value"]["id"], "")
            if not actor:
                continue
            role = ""
            for q in c.get("qualifiers", {}).get("P453", []) + c.get("qualifiers", {}).get("P4633", []):
                if q.get("snaktype") != "value":
                    continue
                v = q["datavalue"]["value"]
                role = v if isinstance(v, str) else ref_labels.get(v.get("id", ""), "")
                if role:
                    break
            conn.execute("INSERT INTO cast_members VALUES (?, ?, ?, ?)", (qid, i, actor, role))

        conn.commit()
        if existing:
            updated += 1
        else:
            new += 1
        log(f"  ✓ {title_en} ({premiere}) plakát={'ano' if poster_file else 'NE'} trailer={'ano' if youtube else 'NE'}")

    log(f"✅ Hotovo: {new} nových, {updated} aktualizovaných")


if __name__ == "__main__":
    main()
