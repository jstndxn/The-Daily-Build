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
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text())
UA = CONFIG.get("user_agent") or "daily-build/1.0"
ATOM = "{http://www.w3.org/2005/Atom}"
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)


def log(msg):
    print(msg, file=sys.stderr)


REDDIT_GAP = CONFIG.get("reddit_seconds_between_requests", 7)
_last_reddit = 0.0


def http_get(url, headers=None, data=None, timeout=25, tries=3):
    """GET with a polite pace for Reddit and retries when a site says to slow down (HTTP 429)."""
    global _last_reddit
    is_reddit = "reddit.com" in url
    for attempt in range(tries):
        if is_reddit:
            wait = REDDIT_GAP - (time.time() - _last_reddit)
            if wait > 0:
                time.sleep(wait)
            _last_reddit = time.time()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                retry_after = e.headers.get("Retry-After")
                pause = min(int(retry_after), 60) if (retry_after or "").isdigit() else 15 * (attempt + 1)
                log(f"  {urllib.parse.urlparse(url).netloc} asked us to slow down; waiting {pause}s")
                time.sleep(pause)
                continue
            raise


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
            content = node_text(e, ATOM + "content") or node_text(e, ATOM + "summary") or ""
            target, image = rss_link_and_image(content)
            items.append({
                "title": html.unescape(node_text(e, ATOM + "title") or "Untitled"),
                "url": link.get("href") if link is not None else None,
                "link": target,
                "domain": urllib.parse.urlparse(target).netloc if target else None,
                "author": re.sub(r"^/?u/", "", author) or None,
                "created": node_text(e, ATOM + "published") or node_text(e, ATOM + "updated"),
                "excerpt": clean_excerpt(strip_html(content)),
                "score": None,
                "comments": None,
                "image_url": image,
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


def rss_link_and_image(content):
    """Reddit's RSS puts the post's target in a "[link]" anchor; images live on i.redd.it / preview.redd.it."""
    target = image = None
    m = re.search(r'<a href="([^"]+)">\s*\[link\]\s*</a>', content)
    if m:
        target = html.unescape(m.group(1))
        if IMG_EXT.search(target) or "i.redd.it" in target:
            image, target = target, None
        elif "reddit.com" in target:
            target = None  # text post or gallery: nothing to link out to
    if not image:
        m = re.search(r'https://(?:preview|i)\.redd\.it/[^"\'\s<>]+', content)
        if m:
            image = html.unescape(m.group(0))
    return target, image


# ---------- reddit ----------

_token = None
_json_blocked = False  # once Reddit refuses the public .json listing, stop asking for it


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
    global _json_blocked
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
        if _json_blocked:
            raise RuntimeError("public listing blocked earlier in this run")
        try:
            raw = http_get(f"https://www.reddit.com/r/{sub}/top.json?{query}")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                _json_blocked = True
            raise
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
        "image_url": image_from_post_json(p),
    }


def image_from_post_json(p):
    url = p.get("url_overridden_by_dest") or p.get("url") or ""
    if p.get("post_hint") == "image" or IMG_EXT.search(url):
        return url
    try:
        return html.unescape(p["preview"]["images"][0]["source"]["url"])
    except (KeyError, IndexError, TypeError):
        pass
    for m in (p.get("media_metadata") or {}).values():
        u = (m.get("s") or {}).get("u")
        if u:
            return html.unescape(u)
    return None


def reddit_top(sub, period, min_score=0):
    """Top posts for a subreddit: scored listing if Reddit allows it, otherwise RSS (no scores)."""
    try:
        posts, method = reddit_listing(sub, period)
        posts = [p for p in posts if not p.get("stickied") and not p.get("over_18")]
        items = [from_reddit(p) for p in posts if (p.get("score") or 0) >= min_score]
    except Exception as e:
        if not _json_blocked or "earlier in this run" not in str(e):
            log(f"r/{sub}: scored listing unavailable ({e}); using RSS")
        items = parse_feed(http_get(f"https://www.reddit.com/r/{sub}/top/.rss?t={period}&limit=50"))
        method = "rss"
    items.sort(key=lambda i: i.get("score") or 0, reverse=True)
    return items, method


def collect_reddit(src):
    sub, period = src["subreddit"], src.get("period", "day")
    items, method = reddit_top(sub, period, src.get("min_score", 0))
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


# ---------- sports ----------
# Results and schedules: ESPN's public site API (unofficial; no key needed).
# Headlines: Google News RSS search, one query per team.

def espn_score(c):
    sc = c.get("score")
    if isinstance(sc, dict):
        return sc.get("displayValue") or (str(int(sc["value"])) if sc.get("value") is not None else None)
    return sc


def espn_team(path):
    sport, league, team = path.split("/")
    raw = json.loads(http_get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team}/schedule"))
    t = raw.get("team") or {}
    my_id = str(t.get("id"))
    games = []
    for ev in raw.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        status = (comp.get("status") or {}).get("type") or {}
        us = them = None
        for c in comp.get("competitors", []):
            if str((c.get("team") or {}).get("id")) == my_id:
                us = c
            else:
                them = c
        if not (us and them):
            continue
        rank = ((them.get("curatedRank") or {}).get("current")) or 99
        opp = them.get("team") or {}
        tv = None
        for b in comp.get("broadcasts") or []:
            tv = ((b.get("media") or {}).get("shortName")) or tv
            if tv:
                break
        games.append({
            "date": ev.get("date"),
            "state": status.get("state"),            # pre / in / post
            "completed": bool(status.get("completed")),
            "detail": status.get("shortDetail"),
            "home": us.get("homeAway") == "home",
            "neutral": bool(comp.get("neutralSite")),
            "opponent": opp.get("shortDisplayName") or opp.get("displayName") or opp.get("abbreviation"),
            "opponent_rank": rank if rank <= 25 else None,
            "us": espn_score(us),
            "them": espn_score(them),
            "won": us.get("winner"),
            "tv": tv,
        })
    games.sort(key=lambda g: g["date"] or "")
    done = [g for g in games if g["completed"]]
    upcoming = [g for g in games if g["state"] in ("pre", "in") and not g["completed"]]
    kind = "id" if team.isdigit() else "name"
    return {
        "record": t.get("recordSummary"),
        "standing": t.get("standingSummary"),
        "espn_url": f"https://www.espn.com/{league}/team/schedule/_/{kind}/{team}",
        "last": done[-1] if done else None,
        "next": upcoming[0] if upcoming else None,
    }


def google_news(query, days, limit):
    q = urllib.parse.urlencode({"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(http_get(f"https://news.google.com/rss/search?{q}"))
    seen, out = set(), []
    for item in root.iter("item"):
        publisher = node_text(item, "source")
        title = html.unescape(node_text(item, "title") or "")
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        key = re.sub(r"\W+", "", title.lower())[:60]
        if not title or key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": node_text(item, "link"), "publisher": publisher,
                    "created": to_iso(node_text(item, "pubDate"))})
    out.sort(key=lambda i: i["created"] or "", reverse=True)
    return out[:limit]


def collect_sports(src):
    teams = []
    for t in src["teams"]:
        team = {"name": t["name"], "errors": []}
        if t.get("espn"):
            try:
                team.update(espn_team(t["espn"]))
            except Exception as e:
                team["errors"].append(f"scores: {e}")
        if t.get("news"):
            try:
                team["headlines"] = google_news(t["news"], src.get("news_days", 2), src.get("headlines", 3))
            except Exception as e:
                team["errors"].append(f"news: {e}")
        if t.get("subreddit"):
            try:
                posts, _ = reddit_top(t["subreddit"], "day", src.get("fan_min_score", 50))
                team["fans"] = posts[: src.get("fan_posts", 2)]
                team["subreddit"] = t["subreddit"]
            except Exception as e:
                team["errors"].append(f"r/{t['subreddit']}: {e}")
        teams.append(team)
        time.sleep(1)
    if all(len(t["errors"]) >= 2 or (t["errors"] and not t.get("headlines") and not t.get("last")) for t in teams):
        raise RuntimeError("no team data could be collected")
    meta = {"type": "sports", "teams": teams, "method": "espn+news",
            "source": src.get("source_note", "Results from ESPN, headlines from Google News")}
    return [], meta


# ---------- research papers (OpenAlex) ----------
# Each person's name (and any alternate spellings) is matched to OpenAlex author
# profiles at the chosen institution. A profile is kept only if the name matches
# (first name, last name, and middle initial when given) and its research topics
# fall in the allowed fields. Matches are cached in data/openalex_authors.json for
# a week; set "openalex_ids" on a person to pin their profiles by hand.

OPENALEX = "https://api.openalex.org"


def openalex(path, params):
    if os.environ.get("OPENALEX_API_KEY"):
        params = {**params, "api_key": os.environ["OPENALEX_API_KEY"]}
    return json.loads(http_get(f"{OPENALEX}/{path}?" + urllib.parse.urlencode(params), tries=3))


def name_parts(name):
    # Fold accents (José -> jose) and drop every kind of hyphen (Grill‐Spector -> grillspector).
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))
    folded = re.sub(r"[\-\u2010-\u2015\u2212]", "", folded.lower().replace(",", " "))
    tokens = [t for t in re.split(r"[\s.]+", folded) if t]
    return (tokens[0], tokens[1:-1], tokens[-1]) if len(tokens) >= 2 else (None, [], None)


def name_matches(wanted, candidate):
    w_first, w_mid, w_last = name_parts(wanted)
    c_first, c_mid, c_last = name_parts(candidate)
    if c_mid and c_mid[-1] + c_last == w_last:  # "Grill Spector" written with a space
        c_mid, c_last = c_mid[:-1], w_last
    if not (w_first and c_first) or w_last != c_last or w_first != c_first:
        return False
    if w_mid and c_mid and w_mid[0][0] != c_mid[0][0]:
        return False  # "Emily M Chen" must not match "Emily J. Chen"
    return True


def resolve_people(src):
    cache_path = DATA / "openalex_authors.json"
    people = src.get("people", [])
    fingerprint = hashlib.sha1(json.dumps([people, src.get("institution"), src.get("fields")], sort_keys=True).encode()).hexdigest()
    try:
        cache = json.loads(cache_path.read_text())
        fresh = datetime.fromisoformat(cache["resolved_at"]) > datetime.now(timezone.utc) - timedelta(days=7)
        if cache.get("fingerprint") == fingerprint and fresh:
            return cache["people"]
    except Exception:
        pass

    allowed = {f.lower() for f in src.get("fields", ["Psychology", "Neuroscience"])}
    resolved = []
    for person in people:
        entry = {"name": person["name"], "ids": list(person.get("openalex_ids", [])), "profiles": [], "rejected": []}
        if not entry["ids"]:
            seen = set()
            for variant in person.get("variants") or [person["name"]]:
                try:
                    found = openalex("authors", {
                        "search": variant, "per_page": 25,
                        "filter": f"affiliations.institution.id:{src['institution']}",
                        "select": "id,display_name,display_name_alternatives,works_count,topics,orcid"})
                except Exception as e:
                    log(f"  OpenAlex search failed for {variant}: {e}")
                    continue
                for a in found.get("results", []):
                    aid = a["id"].rsplit("/", 1)[-1]
                    if aid in seen:
                        continue
                    seen.add(aid)
                    names = [a.get("display_name") or ""] + (a.get("display_name_alternatives") or [])
                    if not any(name_matches(v, n) for v in (person.get("variants") or [person["name"]]) for n in names):
                        continue
                    topics = a.get("topics") or []
                    fields = [((t.get("field") or {}).get("display_name") or "") for t in topics[:8]]
                    summary = {"id": aid, "name": a.get("display_name"), "works": a.get("works_count"),
                               "fields": sorted({f for f in fields if f}), "orcid": a.get("orcid")}
                    if topics and not any(f.lower() in allowed for f in fields):
                        entry["rejected"].append(summary)
                        continue
                    entry["profiles"].append(summary)
                    entry["ids"].append(aid)
        state = f"{len(entry['ids'])} profile(s)" if entry["ids"] else "no Stanford profile found"
        log(f"  {person['name']}: {state}" + (f", {len(entry['rejected'])} rejected" if entry["rejected"] else ""))
        resolved.append(entry)

    DATA.mkdir(exist_ok=True)
    write_json(cache_path, {"resolved_at": datetime.now(timezone.utc).isoformat(), "fingerprint": fingerprint, "people": resolved})
    return resolved


def abstract_text(inverted):
    if not inverted:
        return ""
    words = sorted((pos, w) for w, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words)


def collect_papers(src):
    people = resolve_people(src)
    owner = {aid: p["name"] for p in people for aid in p["ids"]}
    if not owner:
        raise RuntimeError("none of the researchers could be matched in OpenAlex")
    since = (datetime.now(timezone.utc) - timedelta(days=src.get("days", 7))).date().isoformat()
    ids = list(owner)
    works = []
    for i in range(0, len(ids), 50):  # OpenAlex accepts up to 100 OR'd values; stay well under
        data = openalex("works", {
            "filter": f"author.id:{'|'.join(ids[i:i + 50])},from_publication_date:{since}",
            "sort": "publication_date:desc", "per_page": 100,
            "select": "id,doi,display_name,publication_date,type,primary_location,authorships,abstract_inverted_index"})
        works += data.get("results", [])

    order = [p["name"] for p in people]
    items, seen = [], {}
    for w in works:
        title = html.unescape(w.get("display_name") or "")
        key = re.sub(r"\W+", "", title.lower())[:80]
        if not key:
            continue
        authorships = w.get("authorships") or []
        tracked = []
        for a in authorships:
            name = owner.get(((a.get("author") or {}).get("id") or "").rsplit("/", 1)[-1])
            if name and name not in tracked:
                tracked.append(name)
        tracked.sort(key=order.index)
        names = [(a.get("author") or {}).get("display_name") for a in authorships]
        names = [n for n in names if n]
        byline = ", ".join(names[:4]) + (f" and {len(names) - 4} others" if len(names) > 4 else "")
        loc = w.get("primary_location") or {}
        venue = (loc.get("source") or {}).get("display_name") or ("Preprint" if w.get("type") == "preprint" else None)
        abstract = abstract_text(w.get("abstract_inverted_index"))
        item = {
            "work_id": (w.get("id") or "").rsplit("/", 1)[-1],
            "title": title,
            "url": w.get("doi") or loc.get("landing_page_url") or w.get("id"),
            "flair": " and ".join(tracked) if len(tracked) <= 2 else f"{', '.join(tracked[:-1])} and {tracked[-1]}",
            "authors": byline or None,
            "publisher": venue or "OpenAlex",
            "published": w.get("publication_date"),
            "forthcoming": False,
            "type": w.get("type"),
            "excerpt": clean_excerpt(abstract, 300),
            "abstract": clean_excerpt(abstract, 4000) or None,
            "score": None,
        }
        # A preprint and its journal version share a title; keep the journal version.
        if key in seen:
            if seen[key]["type"] == "preprint" and item["type"] != "preprint":
                items[items.index(seen[key])] = item
                seen[key] = item
            continue
        seen[key] = item
        items.append(item)
    unmatched = [p["name"] for p in people if not p["ids"]]
    meta = {"type": "papers", "method": "openalex", "tracked": len(people), "unmatched": unmatched,
            "period_days": src.get("days", 7),
            "source": src.get("source_note", f"New papers from OpenAlex, published in the past {src.get('days', 7)} days")}
    return items[: src.get("count", 9)], meta


COLLECTORS = {"reddit": collect_reddit, "rss": collect_rss, "sports": collect_sports, "papers": collect_papers}


# ---------- images ----------
# Each section's top story gets a picture: the post's own image, else the linked
# site's preview image, else (if IMAGE_API_KEY is set) an AI illustration.

DEFAULT_STYLE = (
    "Editorial illustration for a newspaper story. Classic pen-and-ink engraving with fine "
    "crosshatching, black ink on warm off-white paper, one restrained accent color. Show the "
    "subject through objects, tools and scenes. No identifiable real people, no team or brand "
    "logos, and no written words, letters or numbers anywhere in the image."
)


def image_ok(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.headers.get("Content-Type", "").startswith("image/")
    except Exception:
        return False


def og_image(url):
    page = http_get(url, {"Accept": "text/html"}, timeout=12, tries=1)[:400_000].decode("utf-8", "ignore")
    patterns = (
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)',
    )
    for pat in patterns:
        m = re.search(pat, page, re.I)
        if m:
            return urllib.parse.urljoin(url, html.unescape(m.group(1)))
    return None


def ai_image(item, section_title, dest):
    cfg = CONFIG.get("images", {})
    prompt = (f"{cfg.get('style_prompt', DEFAULT_STYLE)}\n\nSection: {section_title}\n"
              f"Story headline: {item['title']}\nContext: {(item.get('excerpt') or '')[:300]}")
    body = json.dumps({
        "model": cfg.get("ai_model", "gpt-image-2"),
        "prompt": prompt,
        "size": cfg.get("ai_size", "1536x1024"),
        "quality": cfg.get("ai_quality", "medium"),
        "n": 1,
        "output_format": "jpeg",
        "output_compression": 80,
    }).encode()
    raw = http_get(cfg.get("ai_endpoint", "https://api.openai.com/v1/images/generations"),
                   {"Authorization": f"Bearer {os.environ['IMAGE_API_KEY']}", "Content-Type": "application/json"},
                   body, timeout=240, tries=2)
    out = json.loads(raw)["data"][0]
    data = base64.b64decode(out["b64_json"]) if out.get("b64_json") else http_get(out["url"], timeout=60)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def add_images(sections, date):
    cfg = CONFIG.get("images", {})
    if cfg.get("enabled") is False:
        for sec in sections:
            for item in sec["items"]:
                item.pop("image_url", None)
        return
    ai_left = cfg.get("ai_max_per_day", 4) if os.environ.get("IMAGE_API_KEY") else 0
    per_section = cfg.get("per_section", 1)
    for sec in sections:
        for i, item in enumerate(sec["items"]):
            found = item.pop("image_url", None)
            if i >= per_section or sec.get("status") != "ok":
                continue
            image = None
            if found and image_ok(found):
                who = f"u/{item['author']}" if item.get("author") else "the poster"
                image = {"src": found, "kind": "post", "credit": f"Image posted by {who}"}
            elif item.get("link"):
                try:
                    og = og_image(item["link"])
                except Exception:
                    og = None
                if og and image_ok(og):
                    image = {"src": og, "kind": "site", "credit": f"Image from {item.get('domain') or 'the linked site'}"}
            if not image and ai_left > 0:
                slug = hashlib.sha1(item["title"].encode()).hexdigest()[:10]
                rel = f"data/img/{date}/{sec['id']}-{slug}.jpg"
                try:
                    if not (ROOT / rel).exists():  # reruns on the same day reuse the drawing
                        ai_image(item, sec["title"], ROOT / rel)
                        ai_left -= 1
                    image = {"src": rel, "kind": "ai", "credit": "AI illustration based on the headline"}
                except Exception as e:
                    log(f"  AI illustration failed for {sec['title']}: {e}")
            if image:
                item["image"] = image
                log(f"  {sec['title']}: {image['kind']} image")


# ---------- paper summaries ----------
# A plain-language summary of each paper, written by AI from the paper's abstract only.
# Uses Claude when ANTHROPIC_API_KEY is set, otherwise OpenAI when OPENAI_API_KEY is set
# (the same key as the illustrations). With neither, the reader panel shows the abstract.
# Summaries are cached in data/summaries.json so a paper is only summarized once.

SUMMARY_SYSTEM = (
    "You write plain-language summaries of new research papers for a personal morning newspaper. "
    "Using only the title and abstract you are given, write three or four sentences for a curious "
    "reader who is not a specialist: the question the researchers asked, what they did, and what they "
    "found. Do not add facts, numbers, methods or claims that are not in the abstract, and do not "
    "speculate about implications the abstract doesn't state. Write plain prose with no heading, "
    "list or preamble, and don't begin with 'This paper' or 'This study'."
)


def summary_prompt(item):
    return f"Title: {item['title']}\nPublished in: {item.get('publisher')}\n\nAbstract:\n{item['abstract']}"


def summary_provider(cfg):
    choice = cfg.get("provider", "auto")
    if choice in ("auto", "anthropic") and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if choice in ("auto", "openai") and os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def openai_summary(item, cfg):
    body = json.dumps({
        "model": cfg.get("openai_model", "gpt-6-luna"),
        "instructions": SUMMARY_SYSTEM,
        "input": summary_prompt(item),
        "max_output_tokens": 1500,  # leaves room for the model's own reasoning before the answer
        "store": False,
    }).encode()
    raw = http_get("https://api.openai.com/v1/responses",
                   {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
                   body, timeout=90, tries=2)
    data = json.loads(raw)
    text = data.get("output_text") or "".join(
        c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
        for c in o.get("content", []) if c.get("type") == "output_text")
    return text.strip()


def claude_summary(item, cfg):
    body = json.dumps({
        "model": cfg.get("anthropic_model", "claude-haiku-5-5"),
        "max_tokens": 400,
        "system": SUMMARY_SYSTEM,
        "messages": [{"role": "user", "content":
                      summary_prompt(item)}],
    }).encode()
    raw = http_get("https://api.anthropic.com/v1/messages",
                   {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                    "content-type": "application/json"}, body, timeout=90, tries=2)
    text = "".join(b.get("text", "") for b in json.loads(raw).get("content", []) if b.get("type") == "text")
    return text.strip()


def add_summaries(sections):
    papers = [i for s in sections if s.get("type") == "papers" for i in s["items"]]
    if not papers:
        return
    cache_path = DATA / "summaries.json"
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:
        cache = {}
    cfg = CONFIG.get("summaries", {})
    provider = summary_provider(cfg) if cfg.get("enabled", True) else None
    write = {"anthropic": claude_summary, "openai": openai_summary}.get(provider)
    for item in papers:
        wid = item.get("work_id")
        if wid in cache:
            item["summary"] = cache[wid]["text"]
        elif write and item.get("abstract"):
            try:
                text = write(item, cfg)
                if text:
                    item["summary"] = text
                    cache[wid] = {"text": text, "by": provider, "written": datetime.now(timezone.utc).date().isoformat()}
                    log(f"  summarized with {provider}: {item['title'][:60]}")
                else:
                    log(f"  {provider} returned no summary text for {item['title'][:60]}")
            except Exception as e:
                log(f"  summary failed for {item['title'][:60]}: {e}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    cache = {k: v for k, v in cache.items() if v.get("written", "") >= cutoff}
    DATA.mkdir(exist_ok=True)
    write_json(cache_path, cache)


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
            what = f"{len(meta['teams'])} teams" if "teams" in meta else f"{len(items)} stories"
            log(f"{src['title']}: {what} via {meta['method']}")
        except Exception as e:
            section.update(status="error", error=str(e)[:200], items=[])
            log(f"{src['title']}: failed ({e})")
        sections.append(section)

    if all(s["status"] == "error" for s in sections):
        log("Every section failed, so yesterday's edition stays up.")
        sys.exit(1)

    # A papers section with nothing from the past week is left out of the edition entirely.
    for sec in [s for s in sections if s.get("type") == "papers" and s["status"] == "ok" and not s["items"]]:
        log(f"{sec['title']}: nothing new this week, so it's left out today")
        sections.remove(sec)

    date = now.date().isoformat()
    add_images(sections, date)
    add_summaries(sections)

    # The front-page lead: the most upvoted top story (first section wins ties and RSS days).
    lead = None
    for s in sections:
        if s["status"] == "ok" and s["items"]:
            if lead is None or (s["items"][0].get("score") or -1) > (lead[1].get("score") or -1):
                lead = (s["id"], s["items"][0])
    edition = {
        "title": CONFIG.get("title", "The Daily Build"),
        "motto": CONFIG.get("motto", ""),
        "date": date,
        "generated_at": now.isoformat(),
        "timezone": CONFIG.get("timezone", "UTC"),
        "lead_section": lead[0] if lead else None,
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
        shutil.rmtree(DATA / "img" / old, ignore_errors=True)
    write_json(index_path, {"dates": keep})
    log(f"Printed the edition for {date}.")


if __name__ == "__main__":
    main()
