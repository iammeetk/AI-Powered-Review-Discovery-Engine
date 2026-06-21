from __future__ import annotations

import argparse
import csv
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class CollectedRecord:
    source: str
    record_id: str
    date: date
    text: str
    rating: str = ""
    url: str = ""
    author: str = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Spotify review/discussion records.")
    parser.add_argument("--links", default="source_links.json", help="JSON file containing source links.")
    parser.add_argument("--output", default="data/raw/scraped_reviews.csv", help="CSV output path.")
    parser.add_argument("--status", default="outputs/source_status.json", help="Collection status output path.")
    parser.add_argument("--months", type=int, default=12, help="Lookback window in months.")
    parser.add_argument("--max-per-source", type=int, default=500, help="Maximum records per source.")
    parser.add_argument("--sleep", type=float, default=0.4, help="Delay between paginated requests.")
    return parser.parse_args(argv)


def http_get(url: str, *, accept: str = "application/json,text/html;q=0.9") -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_any_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value[:25], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def cutoff_date(months: int) -> date:
    return date.today() - timedelta(days=round(months * 30.4375))


def is_recent(record_date: date, cutoff: date) -> bool:
    return record_date >= cutoff


def collect_play_store(link: dict[str, Any], cutoff: date, max_records: int) -> tuple[list[CollectedRecord], str]:
    try:
        from google_play_scraper import Sort, reviews  # type: ignore
    except ImportError:
        return [], "skipped: install google-play-scraper to collect Play Store reviews"

    app_id = link.get("app_id") or "com.spotify.music"
    country = link.get("country") or "us"
    language = link.get("language") or "en"
    records: list[CollectedRecord] = []
    token = None

    while len(records) < max_records:
        batch, token = reviews(
            app_id,
            lang=language,
            country=country,
            sort=Sort.NEWEST,
            count=min(200, max_records - len(records)),
            continuation_token=token,
        )
        if not batch:
            break
        for item in batch:
            if len(records) >= max_records:
                return records[:max_records], "ok"
            at = item.get("at")
            record_date = at.date() if hasattr(at, "date") else parse_any_date(str(at))
            if not record_date:
                continue
            if not is_recent(record_date, cutoff):
                return records, "ok"
            records.append(
                CollectedRecord(
                    source="Play Store",
                    record_id=str(item.get("reviewId") or f"play-{len(records) + 1}"),
                    date=record_date,
                    text=str(item.get("content") or "").strip(),
                    rating=str(item.get("score") or ""),
                    url=str(link.get("url") or ""),
                    author=str(item.get("userName") or ""),
                )
            )
        if token is None:
            break
    return records, "ok"


def collect_app_store(link: dict[str, Any], cutoff: date, max_records: int, sleep: float) -> tuple[list[CollectedRecord], str]:
    app_id = link.get("app_id") or "324684580"
    country = link.get("country") or "us"
    records: list[CollectedRecord] = []

    for page in range(1, 11):
        url = f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
        payload = json.loads(http_get(url).decode("utf-8"))
        entries = payload.get("feed", {}).get("entry", [])
        if page == 1 and entries:
            entries = entries[1:]
        if not entries:
            break
        for item in entries:
            updated = item.get("updated", {}).get("label")
            record_date = parse_any_date(updated)
            if not record_date:
                continue
            if not is_recent(record_date, cutoff):
                return records[:max_records], "ok"
            text = item.get("content", {}).get("label") or item.get("title", {}).get("label") or ""
            records.append(
                CollectedRecord(
                    source="App Store",
                    record_id=str(item.get("id", {}).get("label") or f"appstore-{page}-{len(records)}"),
                    date=record_date,
                    text=str(text).strip(),
                    rating=str(item.get("im:rating", {}).get("label") or ""),
                    url=str(link.get("url") or ""),
                    author=str(item.get("author", {}).get("name", {}).get("label") or ""),
                )
            )
            if len(records) >= max_records:
                return records, "ok"
        time.sleep(sleep)
    if not records:
        return [], "no public App Store review entries returned; Apple may require authenticated/API access for this app"
    return records[:max_records], "ok"


def reddit_json_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if "/comments/" in path:
        return urllib.parse.urlunparse(("https", "www.reddit.com", f"{path}.json", "", "limit=500&sort=new", ""))
    return urllib.parse.urlunparse(("https", "www.reddit.com", f"{path}/new.json", "", "limit=100", ""))


def collect_reddit(link: dict[str, Any], cutoff: date, max_records: int, sleep: float) -> tuple[list[CollectedRecord], str]:
    url = str(link.get("url") or "")
    records: list[CollectedRecord] = []
    after = None

    while len(records) < max_records:
        json_url = reddit_json_url(url)
        if after and "/comments/" not in json_url:
            sep = "&" if "?" in json_url else "?"
            json_url = f"{json_url}{sep}after={urllib.parse.quote(after)}"
        try:
            payload = json.loads(http_get(json_url).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429}:
                return collect_reddit_rss_or_html(link, cutoff, max_records)
            raise
        children = []
        if isinstance(payload, list):
            for listing in payload:
                children.extend(listing.get("data", {}).get("children", []))
            after = None
        else:
            data = payload.get("data", {})
            children = data.get("children", [])
            after = data.get("after")

        if not children:
            break
        stop_for_age = False
        for child in children:
            data = child.get("data", {})
            created = data.get("created_utc")
            if created is None:
                continue
            record_date = datetime.fromtimestamp(float(created), tz=timezone.utc).date()
            if not is_recent(record_date, cutoff):
                stop_for_age = True
                continue
            title = str(data.get("title") or "").strip()
            body = str(data.get("selftext") or data.get("body") or "").strip()
            text = f"{title}. {body}".strip(". ").strip()
            if not text or data.get("stickied"):
                continue
            permalink = data.get("permalink") or ""
            records.append(
                CollectedRecord(
                    source="Reddit",
                    record_id=str(data.get("id") or f"reddit-{len(records) + 1}"),
                    date=record_date,
                    text=text,
                    rating=str(data.get("score") or ""),
                    url=f"https://www.reddit.com{permalink}" if permalink else url,
                    author=str(data.get("author") or ""),
                )
            )
            if len(records) >= max_records:
                break
        if isinstance(payload, list) or not after or stop_for_age:
            break
        time.sleep(sleep)
    return records[:max_records], "ok"


def collect_reddit_rss_or_html(link: dict[str, Any], cutoff: date, max_records: int) -> tuple[list[CollectedRecord], str]:
    url = str(link.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    candidates = []
    if "/comments/" in path:
        candidates.append(urllib.parse.urlunparse(("https", "www.reddit.com", f"{path}.rss", "", "limit=500&sort=new", "")))
        candidates.append(urllib.parse.urlunparse(("https", "old.reddit.com", path, "", "", "")))
    else:
        candidates.append(urllib.parse.urlunparse(("https", "www.reddit.com", f"{path}/new.rss", "", "limit=100", "")))
        candidates.append(urllib.parse.urlunparse(("https", "old.reddit.com", f"{path}/new/", "", "", "")))

    errors = []
    for candidate in candidates:
        try:
            body = http_get(candidate, accept="application/rss+xml,text/html").decode("utf-8", errors="replace")
            if candidate.endswith(".rss") or ".rss?" in candidate:
                records = parse_reddit_rss(body, cutoff, max_records)
            else:
                records = parse_old_reddit_html(body, cutoff, max_records)
            if records:
                return records, "ok via RSS/HTML fallback"
        except urllib.error.HTTPError as exc:
            errors.append(f"HTTP {exc.code}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return [], "failed: Reddit JSON blocked and fallback returned no records" + (f" ({'; '.join(errors)})" if errors else "")


def parse_reddit_rss(body: str, cutoff: date, max_records: int) -> list[CollectedRecord]:
    root = ET.fromstring(body)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
    }
    records: list[CollectedRecord] = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", default="", namespaces=ns)
        content = entry.findtext("atom:content", default="", namespaces=ns)
        updated = parse_any_date(entry.findtext("atom:updated", default="", namespaces=ns))
        author_node = entry.find("atom:author/atom:name", ns)
        link_node = entry.find("atom:link", ns)
        link_url = link_node.attrib.get("href", "") if link_node is not None else ""
        if not updated or not is_recent(updated, cutoff):
            continue
        clean_content = strip_tags(content)
        text = f"{strip_tags(title)}. {clean_content}".strip(". ").strip()
        if not text:
            continue
        records.append(
            CollectedRecord(
                source="Reddit",
                record_id=f"reddit-rss-{len(records) + 1}",
                date=updated,
                text=text[:4000],
                url=link_url,
                author=author_node.text if author_node is not None and author_node.text else "",
            )
        )
        if len(records) >= max_records:
            break
    return records


def parse_old_reddit_html(body: str, cutoff: date, max_records: int) -> list[CollectedRecord]:
    records: list[CollectedRecord] = []
    things = re.findall(r'(?is)<div[^>]+class="[^"]*\bthing\b[^"]*"[^>]*>(.*?)</div>\s*</div>', body)
    if not things:
        things = re.findall(r'(?is)<div[^>]+class="[^"]*\bthing\b[^"]*"[^>]*>(.*?)(?=<div[^>]+class="[^"]*\bthing\b|</body>)', body)
    for block in things:
        title_match = re.search(r'(?is)<a[^>]+class="[^"]*\btitle\b[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        body_match = re.search(r'(?is)<div[^>]+class="[^"]*\busertext-body\b[^"]*"[^>]*>(.*?)</div>', block)
        time_match = re.search(r'(?is)<time[^>]+datetime="([^"]+)"', block)
        author_match = re.search(r'(?is)<a[^>]+class="[^"]*\bauthor\b[^"]*"[^>]*>(.*?)</a>', block)
        if not title_match and not body_match:
            continue
        record_date = parse_any_date(time_match.group(1) if time_match else "") or date.today()
        if not is_recent(record_date, cutoff):
            continue
        title = strip_tags(title_match.group(2)) if title_match else ""
        body_text = strip_tags(body_match.group(1)) if body_match else ""
        text = f"{title}. {body_text}".strip(". ").strip()
        url = urllib.parse.urljoin("https://old.reddit.com", html.unescape(title_match.group(1))) if title_match else ""
        records.append(
            CollectedRecord(
                source="Reddit",
                record_id=f"reddit-html-{len(records) + 1}",
                date=record_date,
                text=text[:4000],
                url=url,
                author=strip_tags(author_match.group(1)) if author_match else "",
            )
        )
        if len(records) >= max_records:
            break
    return records


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def collect_community(link: dict[str, Any], cutoff: date, max_records: int, sleep: float) -> tuple[list[CollectedRecord], str]:
    base_url = str(link.get("url") or "")
    records: list[CollectedRecord] = []
    seen: set[str] = set()

    for page in range(1, 11):
        url = base_url if page == 1 else f"{base_url}/page/{page}"
        try:
            text = http_get(url, accept="text/html").decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if page == 1:
                raise
            break
        blocks = re.findall(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text)
        for href, label_html in blocks:
            label = strip_tags(label_html)
            if not label or len(label) < 18:
                continue
            if not any(term in label.lower() for term in ["spotify", "recommend", "discover", "playlist", "music", "song", "radio", "daily mix", "release radar"]):
                continue
            absolute = urllib.parse.urljoin(base_url, href)
            key = absolute.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            page_text = ""
            record_date = date.today()
            try:
                detail = http_get(absolute, accept="text/html").decode("utf-8", errors="replace")
                page_text = extract_community_detail_text(detail) or label
                record_date = extract_html_date(detail) or record_date
            except urllib.error.URLError:
                page_text = label
            if not is_recent(record_date, cutoff):
                continue
            records.append(
                CollectedRecord(
                    source="Spotify Community",
                    record_id=f"community-{len(records) + 1}",
                    date=record_date,
                    text=page_text[:4000],
                    url=absolute,
                )
            )
            if len(records) >= max_records:
                return records, "ok"
            time.sleep(sleep)
        time.sleep(sleep)
    status = "ok" if records else "no matching public forum records found"
    return records[:max_records], status


def extract_html_date(text: str) -> date | None:
    patterns = [
        r'datetime="([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"dateCreated"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            parsed = parse_any_date(match.group(1))
            if parsed:
                return parsed
    return None


def extract_community_detail_text(text: str) -> str:
    meta = re.search(r'<meta[^>]+(?:name|property)="(?:description|og:description)"[^>]+content="([^"]+)"', text, re.I)
    if meta:
        return strip_tags(meta.group(1))
    article = re.search(r'(?is)<article[^>]*>(.*?)</article>', text)
    if article:
        return strip_tags(article.group(1))
    return ""


def collect_x(link: dict[str, Any], cutoff: date, max_records: int) -> tuple[list[CollectedRecord], str]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return [], "skipped: X requires API access or an authenticated export; set X_BEARER_TOKEN for API collection"
    query = urllib.parse.quote('(Spotify OR "Spotify recommendations" OR "Spotify discover") lang:en -is:retweet')
    start_time = cutoff.isoformat() + "T00:00:00Z"
    url = (
        "https://api.twitter.com/2/tweets/search/recent"
        f"?query={query}&max_results=100&tweet.fields=created_at,author_id,public_metrics&start_time={start_time}"
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    records = []
    for item in payload.get("data", [])[:max_records]:
        record_date = parse_any_date(item.get("created_at")) or date.today()
        records.append(
            CollectedRecord(
                source="X",
                record_id=str(item.get("id")),
                date=record_date,
                text=str(item.get("text") or ""),
                url=f"https://x.com/i/web/status/{item.get('id')}",
                author=str(item.get("author_id") or ""),
            )
        )
    return records, "ok"


def collect_link(link: dict[str, Any], cutoff: date, max_records: int, sleep: float) -> tuple[list[CollectedRecord], str]:
    source = str(link.get("source") or "").lower()
    try:
        if source == "play store":
            return collect_play_store(link, cutoff, max_records)
        if source == "app store":
            return collect_app_store(link, cutoff, max_records, sleep)
        if source == "reddit":
            return collect_reddit(link, cutoff, max_records, sleep)
        if source == "spotify community":
            return collect_community(link, cutoff, max_records, sleep)
        if source == "x":
            return collect_x(link, cutoff, max_records)
        return [], "skipped: unsupported source"
    except urllib.error.HTTPError as exc:
        return [], f"failed: HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return [], f"failed: network error {exc.reason}"
    except Exception as exc:
        return [], f"failed: {type(exc).__name__}: {exc}"


def dedupe(records: Iterable[CollectedRecord]) -> list[CollectedRecord]:
    seen: set[str] = set()
    unique = []
    for record in records:
        key = re.sub(r"\W+", " ", record.text.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def write_csv(records: list[CollectedRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source", "id", "date", "text", "rating", "url", "author"])
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "source": record.source,
                    "id": record.record_id,
                    "date": record.date.isoformat(),
                    "text": record.text,
                    "rating": record.rating,
                    "url": record.url,
                    "author": record.author,
                }
            )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    links = json.loads(Path(args.links).read_text(encoding="utf-8"))
    cutoff = cutoff_date(args.months)
    all_records: list[CollectedRecord] = []
    statuses = []

    for link in links:
        source = link.get("source", "Unknown")
        records, status = collect_link(link, cutoff, args.max_per_source, args.sleep)
        all_records.extend(records)
        statuses.append(
            {
                "source": source,
                "url": link.get("url"),
                "records_collected": len(records),
                "status": status,
            }
        )
        print(f"{source}: {len(records)} records ({status})")

    unique = dedupe(all_records)
    write_csv(unique, Path(args.output))
    status_path = Path(args.status)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({"cutoff": cutoff.isoformat(), "sources": statuses}, indent=2), encoding="utf-8")
    print(f"Wrote {len(unique)} unique records to {args.output}")
    print(f"Wrote source status to {args.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
