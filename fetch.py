#!/usr/bin/env python3
"""Collect today's edition from the sources listed in config.json.

Writes data/<date>.json, data/latest.json and data/index.json.
Standard library only, so the GitHub Action needs no installs.

Reddit is read three ways, in order:
  1. Reddit's API, if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET are set (most reliable)
  2. The public .json listing (no setup, has scores, sometimes blocked from cloud servers)
  3. The public RSS feed (no scores, but rarely blocked)
"""
import base64
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text())
UA = CONFIG.get("user_agent") or "daily-build/1.0"
ATOM = "{http://www.w3.org/2005/Atom}"


def log(msg):
    print(msg, file=sys.stderr)


def http_get(url, headers=None, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- text helpers ----------

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return html.unescape(s)


def clean_excerpt(text, limit=320):
    text = text or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # markdown images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)       # markdown links -> text
    text = re.sub(r"https?://\S+", "", text)                    # bare urls
    text = re.sub(r"submitted by\s+/u/\S+.*$", "", text, flags=re.S)  # reddit rss boilerplate
    text = re.sub(r"[*_`#>~|]+", "", text)
    text = re.sub(r"&#x200B;|\u200b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    return text


def to_iso(value):
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return value  # already ISO (Atom)


def node_text(el, path):
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else None


# ---------- feeds (RSS and Atom) ----------

def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    if root.tag == ATOM + "feed":
        for e in root.findall(ATOM + "entry"):
            link = e.find(ATOM + "link")
            author = node_text(e, f"{ATOM}author/{ATOM}name") or ""
            items.append({
                "title": html.unescape(node_text(e, ATOM + "title") or "Untitled"),
                "url": link.get("href") if link is not None else None,
                "author": re.sub(r"^/?u/", "", author) or None,
                "created": node_text(e, ATOM + "published") or node_text(e, ATOM + "updated"),
                "excerpt": clean_excerpt(strip_html(node_text(e, ATOM + "content") or node_text(e, ATOM + "summary"))),
                "score": None,
                "comments": None,
            })
    else:
        for e in root.iter("item"):
            items.append({
                "title": html.unescape(node_text(e, "title") or "Untitled"),
                "url": node_text(e, "link"),
                "author": node_text(e, "author") or node_text(e, "{http://purl.org/dc/elements/1.1/}creator"),
                "created": to_iso(node_text(e, "pubDate")),
                "excerpt": clean_excerpt(strip_html(node_text(e, "description"))),
                "score": None,
                "comments": None,
            })
    return items


# ---------- reddit ----------

_token = None


def reddit_token():
    global _token
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return None
    if _token is None:
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        raw = http_get("https://www.reddit.com/api/v1/access_token", {"Authorization": f"Basic {auth}"}, body)
        _token = json.loads(raw)["access_token"]
    return _token


def reddit_listing(sub, period):
    query = f"t={period}&limit=50&raw_json=1"
    token = None
    try:
        token = reddit_token()
    except Exception as e:  # bad secrets shouldn't stop the paper
        log(f"Reddit sign-in failed ({e}); using the public listing instead")
    if token:
        raw = http_get(f"https://oauth.reddit.com/r/{sub}/top?{query}", {"Authorization": f"Bearer {token}"})
        method = "api"
    else:
        raw = http_get(f"https://www.reddit.com/r/{sub}/top.json?{query}")
        method = "json"
    return [c["data"] for c in json.loads(raw)["data"]["children"]], method


def from_reddit(p):
    is_self = p.get("is_self")
    return {
        "title": html.unescape(p.get("title") or "Untitled"),
        "url": "https://www.reddit.com" + p["permalink"],
        "link": None if is_self else (p.get("url_overridden_by_dest") or p.get("url")),
        "domain": None if is_self else p.get("domain"),
        "author": p.get("author"),
        "score": p.get("score"),
        "comments": p.get("num_comments"),
        "created": datetime.fromtimestamp(p["created_utc"], timezone.utc).isoformat(),
        "excerpt": clean_excerpt(p.get("selftext")),
        "flair": p.get("link_flair_text"),
    }


def collect_reddit(src):
    sub, period = src["subreddit"], src.get("period", "day")
    try:
        posts, method = reddit_listing(sub, period)
        posts = [p for p in posts if not p.get("stickied") and not p.get("over_18")]
        items = [from_reddit(p) for p in posts if (p.get("score") or 0) >= src.get("min_score", 0)]
    except Exception as e:
        log(f"r/{sub}: listing unavailable ({e}); falling back to RSS")
        items = parse_feed(http_get(f"https://www.reddit.com/r/{sub}/top/.rss?t={period}&limit=50"))
        method = "rss"
    items.sort(key=lambda i: i.get("score") or 0, reverse=True)
    meta = {
        "source": f"r/{sub}",
        "source_url": f"https://www.reddit.com/r/{sub}/top/?t={period}",
        "period": period,
        "min_score": src.get("min_score", 0),
        "method": method,
    }
    return items[: src.get("count", 8)], meta


def collect_rss(src):
    items = parse_feed(http_get(src["url"]))
    meta = {"source": src.get("source_name") or urllib.parse.urlparse(src["url"]).netloc,
            "source_url": src.get("home_url") or src["url"], "method": "rss"}
    return items[: src.get("count", 8)], meta


COLLECTORS = {"reddit": collect_reddit, "rss": collect_rss}


# ---------- main ----------

def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def main():
    tz = ZoneInfo(CONFIG.get("timezone", "UTC"))
    now = datetime.now(tz)
    sections = []
    for i, src in enumerate(CONFIG["sections"]):
        if i:
            time.sleep(2)  # be polite to the sources
        section = {"id": src["id"], "title": src["title"]}
        try:
            items, meta = COLLECTORS[src["type"]](src)
            section.update(meta, status="ok", items=items)
            log(f"{src['title']}: {len(items)} stories via {meta['method']}")
        except Exception as e:
            section.update(status="error", error=str(e)[:200], items=[])
            log(f"{src['title']}: failed ({e})")
        sections.append(section)

    if all(s["status"] == "error" for s in sections):
        log("Every section failed, so yesterday's edition stays up.")
        sys.exit(1)

    date = now.date().isoformat()
    edition = {
        "title": CONFIG.get("title", "The Daily Build"),
        "motto": CONFIG.get("motto", ""),
        "date": date,
        "generated_at": now.isoformat(),
        "timezone": CONFIG.get("timezone", "UTC"),
        "sections": sections,
    }
    DATA.mkdir(exist_ok=True)
    write_json(DATA / f"{date}.json", edition)
    write_json(DATA / "latest.json", edition)

    index_path = DATA / "index.json"
    dates = json.loads(index_path.read_text()).get("dates", []) if index_path.exists() else []
    dates = sorted(set(dates) | {date}, reverse=True)
    keep = dates[: CONFIG.get("archive_days", 90)]
    for old in dates[len(keep):]:
        (DATA / f"{old}.json").unlink(missing_ok=True)
    write_json(index_path, {"dates": keep})
    log(f"Printed the edition for {date}.")


if __name__ == "__main__":
    main()
