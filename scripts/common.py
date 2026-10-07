"""Shared helpers for the KREVZONE weekly pipeline (stdlib only)."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as Date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MEDIA_DIR = ROOT / "media"
POSTERS_DIR = MEDIA_DIR / "posters"
DB_PATH = DATA_DIR / "krevzone.db"

USER_AGENT = "KrevzoneBot/1.0 (https://britta710.github.io/horrorweb/; weekly horror digest)"

MONTHS_GEN = ["ledna", "února", "března", "dubna", "května", "června",
              "července", "srpna", "září", "října", "listopadu", "prosince"]


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env() -> None:
    """Load KEY=VALUE lines from ROOT/.env into os.environ (without overriding)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http(url: str, *, params: dict | None = None, data: dict | None = None,
         headers: dict | None = None, timeout: int = 30, retries: int = 3,
         raw: bool = False):
    """GET (or POST JSON when `data` is given). Returns parsed JSON, or bytes if raw.

    Retries on 429/5xx and network errors with exponential backoff. Other HTTP
    errors are raised as urllib.error.HTTPError.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            return payload if raw else json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            wait = int(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)
        time.sleep(min(wait, 30))
    raise last_exc  # pragma: no cover


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "film"


def monday_of(d: Date) -> Date:
    return d - timedelta(days=d.weekday())


def czech_date(iso: str) -> str:
    try:
        d = Date.fromisoformat(iso)
    except (TypeError, ValueError):
        return "—"
    return f"{d.day}. {MONTHS_GEN[d.month - 1]} {d.year}"


def die(msg: str, code: int = 1) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)
