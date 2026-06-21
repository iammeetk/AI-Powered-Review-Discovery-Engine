from __future__ import annotations

import cgi
import json
import mimetypes
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.collect_sources import collect_link, cutoff_date, dedupe, write_csv
from src.review_discovery_engine import (
    analyze,
    clean_records,
    load_csv,
    write_cleaned_csv,
    write_report,
    write_summary_json,
)


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
OUTPUT_DIR = ROOT / "outputs"
UPLOAD_DIR = ROOT / "data" / "uploads"
LAST_UPLOAD: dict = {}


def link_config_from_url(url: str) -> dict:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if "play.google.com" in host:
        query = parse_qs(parsed.query)
        return {
            "source": "Play Store",
            "url": url,
            "app_id": query.get("id", [""])[0] or "com.spotify.music",
            "country": "us",
            "language": "en",
        }
    if "apps.apple.com" in host:
        match = re.search(r"id(\d+)", parsed.path)
        return {
            "source": "App Store",
            "url": url,
            "app_id": match.group(1) if match else "",
            "country": "us",
        }
    if "reddit.com" in host:
        return {"source": "Reddit", "url": url}
    if "community.spotify.com" in host:
        return {"source": "Spotify Community", "url": url}
    if host in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
        return {"source": "X", "url": url}
    return {"source": "Web", "url": url}


def opportunity_rows(summary: dict) -> list[dict]:
    theme_counts = {item["label"]: item["count"] for item in summary.get("theme_distribution", [])}
    total = max(summary.get("cleaning_stats", {}).get("retained", 0), 1)

    rows = [
        {
            "insight": "Playlist/radio discovery feels stale",
            "evidence": f"{theme_counts.get('Search and browse friction', 0)} search/browse signals and {theme_counts.get('Repetitive recommendations', 0)} repetition signals.",
            "frequency": 5 if theme_counts.get("Search and browse friction", 0) / total >= 0.5 else 4,
            "pain": 5,
            "impact": 4,
            "recommendation": "Protect explicit play intent and improve playlist/radio freshness.",
        },
        {
            "insight": "Familiar content dominates discovery",
            "evidence": f"{theme_counts.get('Repetitive recommendations', 0)} records mention repeated or stale recommendations.",
            "frequency": 4,
            "pain": 5,
            "impact": 5,
            "recommendation": "Add a familiar-to-fresh control for recommendations and generated playlists.",
        },
        {
            "insight": "New music discovery is not distinct enough",
            "evidence": f"{theme_counts.get('Low novelty in discovery surfaces', 0)} records mention low novelty or recycled music.",
            "frequency": 3,
            "pain": 4,
            "impact": 5,
            "recommendation": "Create a New for You surface constrained to new-to-user tracks.",
        },
        {
            "insight": "Context is weakly understood",
            "evidence": f"{theme_counts.get('Weak context or mood understanding', 0)} records mention mood, style, or context mismatch.",
            "frequency": 3,
            "pain": 4,
            "impact": 4,
            "recommendation": "Let users start discovery from a session context without retraining their whole profile.",
        },
        {
            "insight": "Recommendation feedback lacks trust",
            "evidence": f"{theme_counts.get('Insufficient recommendation controls', 0)} records mention controls such as hide, dislike, or reset.",
            "frequency": 2,
            "pain": 5,
            "impact": 4,
            "recommendation": "Use reason-based feedback such as too familiar, wrong mood, or too mainstream.",
        },
    ]
    for row in rows:
        row["score"] = row["frequency"] * row["pain"] * row["impact"]
    return sorted(rows, key=lambda item: item["score"], reverse=True)


def compact_summary(summary: dict) -> dict:
    sentiment = summary.get("sentiment_distribution", [])
    sentiment_counts = {item.get("label"): item.get("count", 0) for item in sentiment}
    sentiment_label = "Mixed"
    if sentiment_counts:
        sentiment_label = max(sentiment_counts, key=sentiment_counts.get).title()
    return {
        "sources": summary.get("data_sources_used", []),
        "stats": summary.get("cleaning_stats", {}),
        "sentiment": {
            "label": sentiment_label,
            "distribution": sentiment,
        },
        "themes": summary.get("theme_distribution", [])[:8],
        "intents": summary.get("intent_distribution", [])[:8],
        "segments": summary.get("user_segments", [])[:8],
        "findings": summary.get("key_findings", [])[:6],
        "opportunities": opportunity_rows(summary),
        "problem_statement": (
            "Users who rely on playlists, radio, and recommendations struggle to discover fresh music "
            "because discovery surfaces overuse listening history and sometimes override explicit intent, "
            "resulting in lower trust, repetitive listening, and weaker perceived value of personalization."
        ),
        "mvp_ideas": [
            "Familiar-to-Fresh Slider for radio, Daily Mix, and playlist recommendations.",
            "New for You playlist that excludes recently played or saved tracks.",
            "Reason-Based Feedback for too familiar, wrong mood, wrong genre, or too mainstream.",
        ],
    }


def answer_from_uploaded_reviews(question: str) -> dict:
    if not LAST_UPLOAD:
        return {
            "answer": "Upload a CSV or scrape links first. This Q&A only answers from the latest user-provided dataset.",
            "evidence": [],
        }

    summary = LAST_UPLOAD["summary"]
    records = LAST_UPLOAD["records"]
    question_terms = [
        term
        for term in re.findall(r"[a-zA-Z][a-zA-Z']+", question.lower())
        if len(term) > 3 and term not in {"what", "why", "which", "users", "review", "reviews", "about", "does", "from", "with", "that"}
    ]

    scored = []
    for record in records:
        text = record.get("text", "")
        haystack = text.lower()
        score = sum(1 for term in question_terms if term in haystack)
        score += sum(1 for theme in record.get("themes", []) if any(term in theme.lower() for term in question_terms))
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda item: item[0], reverse=True)

    themes = summary.get("theme_distribution", [])[:3]
    intents = summary.get("intent_distribution", [])[:3]
    segments = summary.get("user_segments", [])[:3]
    evidence = [
        {
            "source": record.get("source", "Uploaded CSV"),
            "text": record.get("text", "")[:320],
        }
        for _, record in scored[:3]
    ]

    if not evidence:
        evidence = [
            {
                "source": record.get("source", "Uploaded CSV"),
                "text": record.get("text", "")[:320],
            }
            for record in records[:3]
        ]

    answer_parts = []
    if themes:
        answer_parts.append("The strongest uploaded-review themes are " + ", ".join(f"{item['label']} ({item['count']})" for item in themes) + ".")
    if intents:
        answer_parts.append("The main intents are " + ", ".join(item["label"] for item in intents) + ".")
    if segments:
        answer_parts.append("The clearest segments are " + ", ".join(item["label"] for item in segments) + ".")
    if question_terms and scored:
        answer_parts.append(f"For your question, I found {len(scored)} uploaded reviews matching the key terms: {', '.join(question_terms[:6])}.")
    else:
        answer_parts.append("I did not find exact keyword matches, so this answer uses the uploaded file's overall analysis.")

    return {
        "answer": " ".join(answer_parts),
        "evidence": evidence,
    }


class WorkflowHandler(BaseHTTPRequestHandler):
    server_version = "ReviewWorkflow/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.serve_file(WEB_ROOT / "app.html", "text/html; charset=utf-8")
            return
        if path == "/api/current":
            self.handle_current()
            return
        if path.startswith("/outputs/"):
            self.serve_file(ROOT / path.lstrip("/"), None)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/analyze":
            self.handle_upload()
            return
        if path == "/api/collect":
            self.handle_collect_links()
            return
        if path == "/api/ask":
            self.handle_ask()
            return
        self.send_error(404)

    def handle_current(self) -> None:
        summary_path = OUTPUT_DIR / "analysis_summary.json"
        if not summary_path.exists():
            self.write_json({"error": "No existing analysis found. Upload a CSV first."}, status=404)
            return
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.write_json(compact_summary(summary))

    def handle_upload(self) -> None:
        ctype, pdict = cgi.parse_header(self.headers.get("content-type", ""))
        if ctype != "multipart/form-data":
            self.write_json({"error": "Upload must be multipart/form-data."}, status=400)
            return
        pdict["boundary"] = bytes(pdict["boundary"], "utf-8")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        field = form["file"] if "file" in form else None
        if field is None or not getattr(field, "filename", ""):
            self.write_json({"error": "CSV file is required."}, status=400)
            return

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path(field.filename).name or "upload.csv"
        upload_path = UPLOAD_DIR / safe_name
        upload_path.write_bytes(field.file.read())

        try:
            records = load_csv(upload_path)
            cleaned, stats = clean_records(records, months=12, max_per_source=500, today=__import__("datetime").date.today())
            summary = analyze(cleaned, stats)
            global LAST_UPLOAD
            LAST_UPLOAD = {"summary": summary, "records": summary.get("records", [])}
            run_dir = OUTPUT_DIR / "web_run"
            run_dir.mkdir(parents=True, exist_ok=True)
            write_cleaned_csv(cleaned, run_dir / "cleaned_reviews.csv")
            write_summary_json(summary, run_dir / "analysis_summary.json")
            write_report(summary, run_dir / "pm_report.md")
            self.write_json(compact_summary(summary))
        except Exception as exc:
            self.write_json({"error": f"Analysis failed: {exc}"}, status=500)

    def handle_collect_links(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            raw_links = str(payload.get("links") or "")
            links = [line.strip() for line in re.split(r"[\n,]+", raw_links) if line.strip()]
            if not links:
                self.write_json({"error": "Paste at least one link."}, status=400)
                return

            cutoff = cutoff_date(12)
            statuses = []
            collected = []
            for url in links[:10]:
                config = link_config_from_url(url)
                if config["source"] == "Web":
                    statuses.append(
                        {
                            "source": "Unsupported",
                            "url": url,
                            "records_collected": 0,
                            "status": "skipped: supported sources are Play Store, App Store, Reddit, Spotify Community, and X API",
                        }
                    )
                    continue
                records, status = collect_link(config, cutoff, max_records=200, sleep=0.25)
                collected.extend(records)
                statuses.append(
                    {
                        "source": config["source"],
                        "url": url,
                        "records_collected": len(records),
                        "status": status,
                    }
                )

            unique = dedupe(collected)
            if not unique:
                self.write_json(
                    {
                        "error": "No records could be collected from the pasted links.",
                        "source_status": statuses,
                    },
                    status=422,
                )
                return

            run_dir = OUTPUT_DIR / "link_run"
            raw_dir = UPLOAD_DIR
            run_dir.mkdir(parents=True, exist_ok=True)
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / "scraped_link_reviews.csv"
            write_csv(unique, raw_path)

            records = load_csv(raw_path)
            cleaned, stats = clean_records(records, months=12, max_per_source=500, today=__import__("datetime").date.today())
            summary = analyze(cleaned, stats)
            global LAST_UPLOAD
            LAST_UPLOAD = {"summary": summary, "records": summary.get("records", [])}
            write_cleaned_csv(cleaned, run_dir / "cleaned_reviews.csv")
            write_summary_json(summary, run_dir / "analysis_summary.json")
            write_report(summary, run_dir / "pm_report.md")
            response = compact_summary(summary)
            response["source_status"] = statuses
            self.write_json(response)
        except Exception as exc:
            self.write_json({"error": f"Link collection failed: {exc}"}, status=500)

    def handle_ask(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            question = str(payload.get("question") or "").strip()
            if not question:
                self.write_json({"error": "Question is required."}, status=400)
                return
            self.write_json(answer_from_uploaded_reviews(question))
        except Exception as exc:
            self.write_json({"error": f"Question answering failed: {exc}"}, status=500)

    def serve_file(self, path: Path, content_type: str | None) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        content_type = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def write_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    port = int(argv[0]) if argv else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), WorkflowHandler)
    print(f"Review Analysis Workflow running at http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
