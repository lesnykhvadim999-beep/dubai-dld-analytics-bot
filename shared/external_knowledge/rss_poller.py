"""RSS poller — every 3 hours.

Free sources:
  - arabianbusiness.com
  - gulfnews.com (Property section)
  - thenationalnews.com/uae
  - propertyfinder.ae/blog
  - bayut.com/mybayut

Pipeline per entry:
  1. dedup by sha1(url + title)
  2. classify (LLM Gemini Flash 2.0 → fallback heuristic)
  3. drop if relevance < 0.3
  4. embed (384-dim hashing vec)
  5. INSERT external_signals
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

try:
    import feedparser  # type: ignore
except ImportError:  # pragma: no cover
    feedparser = None  # type: ignore

from ._db import conn_cursor, ensure_schema
from ._embed import embed
from .relevance_filter import classify

log = logging.getLogger(__name__)

FEEDS: list[dict[str, str]] = [
    # Google News RSS aggregator (free, reliable, covers arabianbusiness/gulfnews/khaleejtimes/etc).
    {"source": "rss:gnews_dubai_real_estate",
     "url": "https://news.google.com/rss/search?q=dubai+real+estate&hl=en-US&gl=US&ceid=US:en"},
    {"source": "rss:gnews_dubai_property_market",
     "url": "https://news.google.com/rss/search?q=dubai+property+market&hl=en-US&gl=US&ceid=US:en"},
    {"source": "rss:gnews_uae_real_estate",
     "url": "https://news.google.com/rss/search?q=uae+real+estate&hl=en-US&gl=US&ceid=US:en"},
    {"source": "rss:gnews_dubai_off_plan",
     "url": "https://news.google.com/rss/search?q=dubai+off+plan&hl=en-US&gl=US&ceid=US:en"},
    {"source": "rss:gnews_dubai_rental",
     "url": "https://news.google.com/rss/search?q=dubai+rental+market&hl=en-US&gl=US&ceid=US:en"},
    {"source": "rss:gnews_dld_dubai",
     "url": "https://news.google.com/rss/search?q=dubai+land+department&hl=en-US&gl=US&ceid=US:en"},
    {"source": "rss:gnews_emaar_damac",
     "url": "https://news.google.com/rss/search?q=emaar+OR+damac+OR+sobha+dubai&hl=en-US&gl=US&ceid=US:en"},
    # Direct feeds confirmed working.
    {"source": "rss:albawaba_business",
     "url": "https://www.albawaba.com/rss/business"},
    {"source": "rss:opp_today",
     "url": "https://www.opp.today/feed"},
]


def _fingerprint(url: str, title: str) -> str:
    h = hashlib.sha1()
    h.update((url or "").encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update((title or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _parse_dt(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _entry_body(entry: Any) -> str:
    for key in ("summary", "description", "content"):
        val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if not val:
            continue
        if isinstance(val, list) and val:
            v0 = val[0]
            if isinstance(v0, dict) and "value" in v0:
                return str(v0["value"])
        if isinstance(val, str):
            return val
    return ""


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def poll_feed(feed_cfg: dict[str, str], min_relevance: float = 0.3) -> dict:
    if feedparser is None:
        log.error("feedparser not installed")
        return {"inserted": 0, "seen": 0, "error": "feedparser missing"}

    source = feed_cfg["source"]
    url = feed_cfg["url"]
    log.info("polling %s -> %s", source, url)

    import urllib.request
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VadimRealtyBot/1.0)",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        parsed = feedparser.parse(raw)
    except Exception as e:
        log.warning("fetch fail %s: %s", source, e)
        return {"inserted": 0, "seen": 0, "error": str(e)}

    entries = list(parsed.entries or [])
    inserted = 0
    seen = len(entries)

    for entry in entries:
        try:
            title = (getattr(entry, "title", None) or entry.get("title", "") or "").strip()
            link = (getattr(entry, "link", None) or entry.get("link", "") or "").strip()
            if not title and not link:
                continue
            body = _strip_html(_entry_body(entry))
            published = _parse_dt(entry)
            fp = _fingerprint(link, title)

            cls = classify(title, body)
            if cls["score"] < min_relevance:
                continue

            vec = embed(f"{title}\n{body}")
            facts = {
                "areas": cls.get("areas", []),
                "summary": cls.get("summary", ""),
            }

            with conn_cursor() as (_, cur):
                cur.execute(
                    """
                    INSERT INTO external_signals
                      (source, source_kind, signal_type, url, title, raw_text,
                       extracted_facts, embedding, relevance_score,
                       published_at, fingerprint, language)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                    ON CONFLICT (fingerprint) DO NOTHING
                    """,
                    (
                        source, "rss", cls.get("signal_type", "news"),
                        link, title, body[:8000],
                        json.dumps(facts, ensure_ascii=False),
                        json.dumps(vec),
                        cls["score"],
                        published, fp, "en",
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
        except Exception as e:
            log.debug("entry skip (%s): %s", source, e)
            continue

    # update source state
    try:
        with conn_cursor() as (_, cur):
            cur.execute(
                """
                INSERT INTO external_source_state
                  (source, last_polled_at, total_fetched, errors_in_row)
                VALUES (%s, NOW(), %s, 0)
                ON CONFLICT (source) DO UPDATE SET
                  last_polled_at = NOW(),
                  total_fetched = external_source_state.total_fetched + EXCLUDED.total_fetched,
                  errors_in_row = 0
                """,
                (source, inserted),
            )
    except Exception:
        pass

    log.info("%s: seen=%s inserted=%s", source, seen, inserted)
    return {"inserted": inserted, "seen": seen}


def poll_all(min_relevance: float = 0.3) -> dict:
    ensure_schema()
    total = {"inserted": 0, "seen": 0, "sources": 0}
    for cfg in FEEDS:
        r = poll_feed(cfg, min_relevance=min_relevance)
        total["inserted"] += r.get("inserted", 0)
        total["seen"] += r.get("seen", 0)
        total["sources"] += 1
        time.sleep(0.5)
    return total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = poll_all()
    print(f"rss_poller: sources={s['sources']} seen={s['seen']} inserted={s['inserted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
