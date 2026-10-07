#!/usr/bin/env python3
"""
Krok 2 — zpracuje surová data filmů do české redakční podoby pomocí
FREE modelů z OpenRouteru.

Při každém spuštění si stáhne aktuální seznam modelů z
https://openrouter.ai/api/v1/models, vybere ty zdarma a zkouší je postupně
(nejlepší nejdřív), dokud jeden nevrátí validní JSON. Každý pokus se
zapisuje do tabulky ai_runs — úspěšnost se pak bere v potaz při řazení.

Usage:
  python3 scripts/enrich_ai.py                # všechny filmy bez AI textů
  python3 scripts/enrich_ai.py --limit 3 -v
  python3 scripts/enrich_ai.py --force --slug hope
  python3 scripts/enrich_ai.py --list-models  # jen vypíše free modely
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
from datetime import date as Date, datetime

import db
from common import czech_date, die, http, load_env, log, monday_of

OPENROUTER = "https://openrouter.ai/api/v1"
MIN_CONTEXT = 16000
SKIP_PATTERNS = ("content-safety", "guard", "code", "coder", "lyria", "embed", "omni", "vision", "audio", "tts")
PAUSE_S = 3
TIMEOUT_S = 90

SYSTEM_PROMPT = """Jsi redaktor českého nezávislého hororového magazínu KREVZONE.cz.
Píšeš svižnou, atmosférickou češtinou pro fanoušky hororu. Vycházíš VÝHRADNĚ
z faktů, která dostaneš na vstupu — nevymýšlej herce, data, ocenění ani čísla,
která tam nejsou. Odpovídáš POUZE jedním JSON objektem bez dalšího textu."""

FILM_PROMPT = """Zpracuj tento film pro magazín. Vrať JSON s přesně těmito klíči:

{{
  "title_cs": "český distribuční název, pokud ho ve vstupu znáš, jinak původní název",
  "synopsis": "česká synopse bez spoilerů, 3–5 vět (300–700 znaků)",
  "pullquote": "jedna úderná věta pro citaci (max 120 znaků)",
  "review": "krátký redakční verdikt 3–5 vět; tituly jiných filmů obal do <em></em>; pokud film ještě nikdo neviděl, piš o očekáváních",
  "stars": celé číslo 1–10 (redakční odhad kvality/očekávání),
  "rating_label": "2–5 slov shrnutí hodnocení",
  "genre_cs": "žánr česky, max 2 položky oddělené ' · '",
  "status": "kina" | "stream" | "upcoming",
  "trivia": ["3–4 zajímavosti česky, POUZE z faktů ve vstupu"],
  "tags": ["4–6 krátkých štítků: subžánr, motiv, režisér/herec, studio"],
  "roles": {{"Jméno herce": "česky role", ...}}  // jen pro herce ze vstupu, kde role zjistíš z textu
}}

Dnešní datum: {today}. Premiéra: {premiere} → status {status_hint}, pokud text
neříká, že film vyšel jen na streamovací službě (pak "stream").

=== FAKTA ===
Název: {title} (Wikidata česky: {title_cs})
Rok: {year} · Stopáž: {runtime} · Země: {country}
Režie: {director} · Scénář: {writer} · Studio: {studio}
Žánr (Wikidata): {genre}
Obsazení: {cast}

=== ČLÁNEK NA WIKIPEDII (EN) ===
{summary_en}

=== ČLÁNEK NA WIKIPEDII (CS) ===
{summary_cs}
"""

WEEK_PROMPT = """Napiš úvod týdenního hororového rozboru pro KREVZONE.cz na týden od {week_cz}.
Zaměř se hlavně na filmy, které mají premiéru právě teď nebo brzy (status kina/upcoming
s nejbližším datem); starší tituly zmiň nanejvýš okrajově. Měsíc v titulku musí odpovídat
týdnu rozboru. Filmy v balíku (nejrelevantnější první):

{films}

Vrať JSON: {{"title": "titulek týdne, max 60 znaků", "subtitle": "jedna věta, max 110 znaků",
"intro": "2–3 věty česky, zmiň konkrétní filmy z výčtu"}}"""


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def is_small(model_id: str) -> bool:
    """Small models tend to write poor Czech — try them only after the big ones."""
    mid = model_id.lower()
    if re.search(r"\b(mini|nano|xs|small|lightning|tiny)\b", re.sub(r"[-_:/.]", " ", mid)):
        return True
    sizes = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)b\b", mid)]
    return bool(sizes) and max(sizes) < 20


def free_models(conn) -> list:
    """Fetch current OpenRouter model list and return usable free text models, best first."""
    data = http(f"{OPENROUTER}/models", timeout=30)["data"]
    stats = db.model_success_rates(conn)
    out = []
    for m in data:
        mid = m["id"]
        pricing = m.get("pricing", {})
        is_free = mid.endswith(":free") or (
            str(pricing.get("prompt")) in ("0", "0.0") and str(pricing.get("completion")) in ("0", "0.0"))
        if not is_free or mid == "openrouter/free":
            continue
        arch = m.get("architecture", {})
        if "text" not in arch.get("input_modalities", ["text"]) or arch.get("output_modalities", ["text"]) != ["text"]:
            continue
        if (m.get("context_length") or 0) < MIN_CONTEXT:
            continue
        if any(p in mid.lower() for p in SKIP_PATTERNS):
            continue
        params = m.get("supported_parameters") or []
        ok, n = stats.get(mid, (0, 0))
        out.append({
            "id": mid,
            "json_mode": "response_format" in params or "structured_outputs" in params,
            "ctx": m.get("context_length") or 0,
            "score": (ok + 1) / (n + 2),  # Laplace-smoothed success rate
        })
    out.sort(key=lambda m: (-m["score"], is_small(m["id"]), not m["json_mode"], -m["ctx"]))
    # OpenRouter's own free router as the very last resort
    out.append({"id": "openrouter/free", "json_mode": False, "ctx": 0, "score": 0})
    return out


# ---------------------------------------------------------------------------
# Calling + validation
# ---------------------------------------------------------------------------

def call_model(model: dict, prompt: str, api_key: str) -> str:
    body = {
        "model": model["id"],
        # system prompt merged into the user turn: some free providers (Gemma) reject the system role
        "messages": [{"role": "user", "content": f"{SYSTEM_PROMPT}\n\n{prompt}"}],
        "temperature": 0.7,
        "max_tokens": 8000,
        # reasoning models otherwise burn the whole budget thinking and return empty content
        "reasoning": {"effort": "low"},
    }
    if model["json_mode"]:
        body["response_format"] = {"type": "json_object"}
    res = http(f"{OPENROUTER}/chat/completions", data=body, timeout=TIMEOUT_S, retries=1, headers={
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://britta710.github.io/horrorweb/",
        "X-Title": "KREVZONE",
    })
    if "error" in res:
        raise RuntimeError(str(res["error"])[:200])
    choice = (res.get("choices") or [{}])[0]
    content = choice.get("message", {}).get("content") or ""
    if not content.strip():
        raise ValueError(f"prázdná odpověď (finish_reason={choice.get('finish_reason')})")
    return content


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("v odpovědi není JSON")
        text = text[start:end + 1]
    return json.loads(text)


CZ_CHARS = re.compile(r"[áčďéěíňóřšťúůýž]", re.I)


def looks_czech(s: str) -> bool:
    return len(CZ_CHARS.findall(s)) >= max(3, len(s) // 60)


def validate_film(d: dict) -> dict:
    for k in ("synopsis", "pullquote", "review", "stars", "rating_label", "trivia", "tags"):
        if k not in d:
            raise ValueError(f"chybí klíč {k}")
    syn, rev = str(d["synopsis"]).strip(), str(d["review"]).strip()
    if not (120 <= len(syn) <= 1500):
        raise ValueError(f"synopse má {len(syn)} znaků")
    if len(rev) < 120:
        raise ValueError("recenze je moc krátká")
    if not (looks_czech(syn) and looks_czech(rev)):
        raise ValueError("text není česky")
    try:
        stars = max(1, min(10, int(round(float(d["stars"])))))
    except (TypeError, ValueError):
        raise ValueError("stars není číslo")
    trivia = [str(t).strip() for t in d["trivia"] if str(t).strip()] if isinstance(d["trivia"], list) else []
    tags = [str(t).strip() for t in d["tags"] if str(t).strip()] if isinstance(d["tags"], list) else []
    if len(trivia) < 2 or len(tags) < 3:
        raise ValueError("málo trivia/tagů")
    status = d.get("status") if d.get("status") in ("kina", "stream", "upcoming") else None
    roles = d.get("roles") if isinstance(d.get("roles"), dict) else {}
    return dict(
        title_cs=str(d.get("title_cs") or "").strip(), synopsis=syn,
        pullquote=str(d["pullquote"]).strip().strip('„“"'), review=rev, stars=stars,
        rating=f"{stars:.1f}" if stars < 10 else "10", rating_label=str(d["rating_label"]).strip(),
        genre_cs=str(d.get("genre_cs") or "").strip(), status=status,
        trivia=trivia[:4], tags=tags[:6], roles={str(k): str(v) for k, v in roles.items()},
    )


def validate_week(d: dict) -> dict:
    for k in ("title", "subtitle", "intro"):
        if not str(d.get(k, "")).strip():
            raise ValueError(f"chybí {k}")
    if not looks_czech(d["intro"]):
        raise ValueError("text není česky")
    return {k: str(d[k]).strip() for k in ("title", "subtitle", "intro")}


def run_with_fallback(conn, models, prompt, validator, target, api_key, verbose):
    """Try models in order; return (validated_dict, model_id) or (None, None).

    Mutates `models`: models that are unusable for this run (HTTP 400/403/404)
    are removed, rate-limited ones (429) are moved to the end.
    """
    for model in list(models):
        t0 = time.time()
        err = None
        result = None
        try:
            result = validator(parse_json(call_model(model, prompt, api_key)))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            err = f"HTTP {e.code} {detail}"
        except Exception as e:  # network, JSON, validation…
            err = f"{type(e).__name__}: {e}"[:300]
        ms = int((time.time() - t0) * 1000)
        conn.execute("INSERT INTO ai_runs (ts, model, target, ok, error, latency_ms) VALUES (datetime('now'), ?, ?, ?, ?, ?)",
                     (model["id"], target, int(result is not None), err, ms))
        conn.commit()
        if result is not None:
            log(f"    ✓ {model['id']} ({ms} ms)")
            return result, model["id"]
        if verbose:
            log(f"    ✗ {model['id']}: {err}")
        if err and err.startswith("HTTP 401"):
            die("OpenRouter odmítl API klíč (401). Zkontroluj OPENROUTER_API_KEY v .env")
        if err and err[:8] in ("HTTP 400", "HTTP 403", "HTTP 404") and model["id"] != "openrouter/free":
            models.remove(model)
        elif err and err.startswith("HTTP 429"):
            models.remove(model)
            models.insert(len(models) - 1, model)  # keep openrouter/free last
        time.sleep(PAUSE_S)
    return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def film_prompt(conn, f) -> str:
    cast = conn.execute("SELECT actor, role FROM cast_members WHERE wikidata_id = ? ORDER BY ord",
                        (f["wikidata_id"],)).fetchall()
    today = Date.today()
    return FILM_PROMPT.format(
        today=today.isoformat(), premiere=f["premiere_date"],
        status_hint="upcoming" if f["premiere_date"] > today.isoformat() else "kina",
        title=f["title"], title_cs=f["title_cs"] or "—", year=f["year"] or "—",
        runtime=f"{f['runtime_min']} min" if f["runtime_min"] else "—",
        country=f["country"] or "—", director=f["director"] or "—", writer=f["writer"] or "—",
        studio=f["studio"] or "—", genre=f["genre"] or "—",
        cast=", ".join(f"{c['actor']} ({c['role']})" if c["role"] else c["actor"] for c in cast) or "—",
        summary_en=f["raw_summary_en"] or "—", summary_cs=f["raw_summary_cs"] or "—",
    )


def save_film(conn, f, d: dict, model_id: str):
    conn.execute(
        "INSERT INTO ai_content (wikidata_id, synopsis, pullquote, review, rating, rating_label, stars, "
        "trivia_json, tags_json, genre_cs, model_used, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(wikidata_id) DO UPDATE SET synopsis=excluded.synopsis, pullquote=excluded.pullquote, "
        "review=excluded.review, rating=excluded.rating, rating_label=excluded.rating_label, stars=excluded.stars, "
        "trivia_json=excluded.trivia_json, tags_json=excluded.tags_json, genre_cs=excluded.genre_cs, "
        "model_used=excluded.model_used, created_at=excluded.created_at",
        (f["wikidata_id"], d["synopsis"], d["pullquote"], d["review"], d["rating"], d["rating_label"], d["stars"],
         json.dumps(d["trivia"], ensure_ascii=False), json.dumps(d["tags"], ensure_ascii=False),
         d["genre_cs"], model_id, datetime.now().isoformat(timespec="seconds")))
    if d["title_cs"] and not f["title_cs"]:
        conn.execute("UPDATE films SET title_cs = ? WHERE wikidata_id = ?", (d["title_cs"], f["wikidata_id"]))
    if d["status"] == "stream" and f["status"] == "kina":
        conn.execute("UPDATE films SET status = 'stream' WHERE wikidata_id = ?", (f["wikidata_id"],))
    for actor, role in d["roles"].items():
        conn.execute("UPDATE cast_members SET role = ? WHERE wikidata_id = ? AND actor = ? AND (role IS NULL OR role = '')",
                     (role, f["wikidata_id"], actor))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="max filmů v tomto běhu")
    ap.add_argument("--force", action="store_true", help="přegenerovat i filmy, které už AI texty mají (kromě locked)")
    ap.add_argument("--slug", help="zpracovat jen jeden film")
    ap.add_argument("--week", help="pondělí týdne pro úvod rozboru (default: tento týden)")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    conn = db.connect()

    models = free_models(conn)
    log(f"→ OpenRouter: {len(models) - 1} použitelných free modelů")
    if args.verbose or args.list_models:
        for m in models:
            log(f"    {m['id']:<55} json={'ano' if m['json_mode'] else 'ne '} úspěšnost={m['score']:.2f}")
    if args.list_models:
        return
    if not api_key:
        die("Chybí OPENROUTER_API_KEY (v .env nebo v prostředí)")

    q = ("SELECT f.* FROM films f LEFT JOIN ai_content a USING (wikidata_id) "
         "WHERE COALESCE(a.locked, 0) = 0")
    params = []
    if not args.force:
        q += " AND a.wikidata_id IS NULL"
    if args.slug:
        q += " AND f.slug = ?"
        params.append(args.slug)
    q += " ORDER BY f.premiere_date DESC"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    films = conn.execute(q, params).fetchall()
    log(f"→ Ke zpracování: {len(films)} filmů")

    done = failed = 0
    for f in films:
        log(f"  • {f['title']}")
        d, model_id = run_with_fallback(conn, models, film_prompt(conn, f), validate_film,
                                        f["slug"], api_key, args.verbose)
        if d:
            save_film(conn, f, d, model_id)
            done += 1
        else:
            log("    ✗ žádný free model neuspěl, zkusí se příští týden")
            failed += 1
        time.sleep(PAUSE_S)

    # Week intro (title/subtitle/intro) for this week's batch
    week = args.week or monday_of(Date.today()).isoformat()
    has_week = conn.execute("SELECT 1 FROM weeks WHERE week = ?", (week,)).fetchone()
    if not args.slug and (not has_week or args.force):
        rows = conn.execute(
            "SELECT f.title, f.premiere_date, f.status, f.director, a.genre_cs, a.pullquote FROM films f "
            "LEFT JOIN ai_content a USING (wikidata_id) WHERE f.first_seen_week = ? "
            "OR f.premiere_date BETWEEN date(?, '-7 days') AND date(?, '+6 days') "
            "ORDER BY abs(julianday(f.premiere_date) - julianday(?)) LIMIT 8",
            (week, week, week, week)).fetchall()
        if rows:
            log(f"  • Úvod týdne {week}")
            lines = "\n".join(f"- {r['title']} ({czech_date(r['premiere_date'])}, {r['status']}, režie {r['director'] or '?'}, "
                              f"{r['genre_cs'] or ''}) — {r['pullquote'] or ''}" for r in rows)
            d, model_id = run_with_fallback(conn, models, WEEK_PROMPT.format(week_cz=czech_date(week), films=lines), validate_week,
                                            f"week:{week}", api_key, args.verbose)
            if d:
                conn.execute("INSERT OR REPLACE INTO weeks (week, title, subtitle, intro, model_used) VALUES (?,?,?,?,?)",
                             (week, d["title"], d["subtitle"], d["intro"], model_id))
                conn.commit()

    log(f"✅ Hotovo: {done} zpracováno, {failed} neúspěšných")


if __name__ == "__main__":
    main()
