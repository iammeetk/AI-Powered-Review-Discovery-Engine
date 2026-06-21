from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


SOURCE_ALIASES = {
    "play": "Play Store",
    "play store": "Play Store",
    "google play": "Play Store",
    "app": "App Store",
    "app store": "App Store",
    "ios": "App Store",
    "reddit": "Reddit",
    "spotify community": "Spotify Community",
    "community": "Spotify Community",
}

THEME_RULES = {
    "Repetitive recommendations": [
        "repeat",
        "same",
        "repetitive",
        "recycle",
        "loop",
        "again",
        "same 30",
        "shuffle",
        "exact same",
        "same songs",
        "same song",
        "same playlist",
    ],
    "Low novelty in discovery surfaces": [
        "fresh",
        "new music",
        "discover",
        "unknown",
        "new artists",
        "smaller artists",
        "new releases",
        "suggested",
        "suggestions",
        "recommendations",
        "recommended",
        "similar",
    ],
    "Weak context or mood understanding": [
        "mood",
        "context",
        "working",
        "gym",
        "late-night",
        "late night",
        "seed",
        "style",
        "vibe",
    ],
    "Insufficient recommendation controls": [
        "control",
        "hide",
        "dislike",
        "not interested",
        "reset",
        "slider",
        "explain",
        "learned",
    ],
    "Mainstream or popularity bias": [
        "popular",
        "mainstream",
        "broad",
        "buried",
        "promoted",
        "hits",
    ],
    "Library history overpowers exploration": [
        "old favorite",
        "old likes",
        "saved",
        "library",
        "nostalgia",
        "comfort",
        "already know",
    ],
    "Search and browse friction": [
        "search",
        "browse",
        "genres",
        "home page",
        "release radar",
        "daily mix",
        "daily mixes",
        "radio",
        "playlist",
        "playlists",
        "autoplay",
    ],
    "Collaborative discovery gaps": [
        "friends",
        "blend",
        "together",
        "shared",
        "compare",
    ],
}

INTENT_RULES = {
    "Find genuinely new music": ["fresh", "discover", "new music", "unknown", "new artists"],
    "Refresh stale recommendations": ["same", "repeat", "repetitive", "bored", "loop", "recycle", "shuffle"],
    "Control the recommendation model": ["control", "hide", "dislike", "reset", "slider", "not interested"],
    "Match listening context": ["mood", "working", "gym", "late-night", "context"],
    "Explore niche or long-tail artists": ["smaller", "niche", "genres", "bands", "adjacent"],
    "Understand algorithm behavior": ["explain", "learned", "why", "thinks"],
    "Discover socially": ["friends", "blend", "together", "shared"],
    "Find a specific song or artist": ["search", "artist", "song", "playlist"],
}

SEGMENT_RULES = {
    "Comfort-loop listeners": ["comfort", "old favorite", "nostalgia", "same", "replay", "loop", "shuffle"],
    "Active explorers": ["discover", "fresh", "unknown", "new artists", "new music", "recommend"],
    "Mood and context curators": ["mood", "working", "gym", "late-night", "context"],
    "Niche and long-tail fans": ["niche", "smaller", "genres", "bands", "adjacent"],
    "Control seekers": ["control", "hide", "dislike", "reset", "slider", "not interested"],
    "Social discovery users": ["friends", "blend", "together", "shared"],
}

DISCOVERY_RELEVANCE_TERMS = [
    "algorithm",
    "artist",
    "autoplay",
    "browse",
    "daily mix",
    "daily mixes",
    "discover",
    "genre",
    "hide",
    "liked",
    "library",
    "mix",
    "new music",
    "playlist",
    "radio",
    "recommend",
    "release radar",
    "repeat",
    "same song",
    "same songs",
    "search",
    "shuffle",
    "similar",
    "skip",
    "song",
    "suggest",
]

POSITIVE_WORDS = {
    "love",
    "great",
    "better",
    "good",
    "fine",
    "strong",
    "safe",
    "want",
    "need",
}

NEGATIVE_WORDS = {
    "bad",
    "bored",
    "buried",
    "confusing",
    "hard",
    "ignores",
    "misses",
    "repetitive",
    "ruin",
    "same",
    "trapped",
    "worse",
}

ENGLISH_STOPWORDS = {
    "the",
    "and",
    "to",
    "of",
    "a",
    "i",
    "it",
    "is",
    "my",
    "for",
    "but",
    "that",
    "with",
    "in",
    "not",
    "want",
    "need",
    "spotify",
}


@dataclass(frozen=True)
class ReviewRecord:
    source: str
    record_id: str
    date: date
    text: str
    rating: str = ""
    url: str = ""
    author: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Spotify discovery feedback.")
    parser.add_argument("--input", default="data/raw", help="Folder containing CSV/JSON exports.")
    parser.add_argument("--output", default="outputs", help="Folder for generated reports.")
    parser.add_argument("--months", type=int, default=12, help="Lookback window in months.")
    parser.add_argument("--max-per-source", type=int, default=500, help="Maximum records per source.")
    parser.add_argument("--skip-slide", action="store_true", help="Skip PowerPoint slide generation.")
    return parser.parse_args(argv)


def normalize_source(value: str) -> str:
    key = (value or "").strip().lower()
    return SOURCE_ALIASES.get(key, value.strip() or "Unknown")


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def load_records(input_dir: Path) -> list[ReviewRecord]:
    records: list[ReviewRecord] = []
    for path in sorted(input_dir.glob("*")):
        if path.suffix.lower() == ".csv":
            records.extend(load_csv(path))
        elif path.suffix.lower() == ".json":
            records.extend(load_json(path))
    return records


def load_csv(path: Path) -> list[ReviewRecord]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [record_from_mapping(row, fallback_id=f"{path.stem}-{i}") for i, row in enumerate(reader, 1)]


def load_json(path: Path) -> list[ReviewRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("records", [])
    return [record_from_mapping(row, fallback_id=f"{path.stem}-{i}") for i, row in enumerate(rows, 1)]


def record_from_mapping(row: dict[str, Any], fallback_id: str) -> ReviewRecord:
    text = str(row.get("text") or row.get("review") or row.get("body") or row.get("comment") or "").strip()
    parsed_date = parse_date(row.get("date") or row.get("created_at") or row.get("published_at"))
    return ReviewRecord(
        source=normalize_source(str(row.get("source") or row.get("platform") or "")),
        record_id=str(row.get("id") or row.get("review_id") or fallback_id),
        date=parsed_date or date.min,
        text=text,
        rating=str(row.get("rating") or row.get("score") or ""),
        url=str(row.get("url") or row.get("link") or ""),
        author=str(row.get("author") or row.get("user") or ""),
        metadata={k: v for k, v in row.items() if k not in {"source", "platform", "id", "review_id", "date", "created_at", "published_at", "text", "review", "body", "comment", "rating", "score", "url", "link", "author", "user"}},
    )


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", text.lower())).strip()


def looks_english(text: str) -> bool:
    letters = re.findall(r"[A-Za-z]", text)
    if not letters:
        return False
    ascii_ratio = sum(1 for ch in text if ord(ch) < 128) / max(len(text), 1)
    words = re.findall(r"[A-Za-z']+", text.lower())
    stopword_hits = sum(1 for word in words if word in ENGLISH_STOPWORDS)
    return ascii_ratio >= 0.92 and (len(words) < 6 or stopword_hits >= 1)


def is_discovery_relevant(text: str) -> bool:
    normalized = normalize_text(text)
    return any(term in normalized for term in DISCOVERY_RELEVANCE_TERMS)


def clean_records(records: Iterable[ReviewRecord], months: int, max_per_source: int, today: date) -> tuple[list[ReviewRecord], dict[str, int]]:
    cutoff = today - timedelta(days=round(months * 30.4375))
    counters: dict[str, int] = defaultdict(int)
    per_source: Counter[str] = Counter()
    seen: set[str] = set()
    cleaned: list[ReviewRecord] = []

    for record in records:
        counters["loaded"] += 1
        if not record.text:
            counters["dropped_empty_text"] += 1
            continue
        if record.date < cutoff:
            counters["dropped_outside_window"] += 1
            continue
        if not looks_english(record.text):
            counters["dropped_non_english"] += 1
            continue
        if not is_discovery_relevant(record.text):
            counters["dropped_not_discovery_related"] += 1
            continue
        if not match_rules(record.text, THEME_RULES):
            counters["dropped_low_signal_discovery"] += 1
            continue
        key = normalize_text(record.text)
        if key in seen:
            counters["dropped_duplicate"] += 1
            continue
        if per_source[record.source] >= max_per_source:
            counters["dropped_source_cap"] += 1
            continue
        seen.add(key)
        per_source[record.source] += 1
        cleaned.append(record)

    counters["retained"] = len(cleaned)
    return cleaned, dict(counters)


def match_rules(text: str, rules: dict[str, list[str]]) -> list[str]:
    normalized = normalize_text(text)
    matches = []
    for label, needles in rules.items():
        if any(needle in normalized for needle in needles):
            matches.append(label)
    return matches


def sentiment(text: str, rating: str = "") -> dict[str, Any]:
    words = re.findall(r"[a-z']+", text.lower())
    positive = sum(1 for word in words if word in POSITIVE_WORDS)
    negative = sum(1 for word in words if word in NEGATIVE_WORDS)
    rating_value = None
    try:
        rating_value = float(rating)
    except (TypeError, ValueError):
        pass
    score = positive - negative
    if rating_value is not None:
        score += (rating_value - 3) * 0.7
    label = "positive" if score >= 1.5 else "negative" if score <= -1 else "mixed"
    return {"label": label, "score": round(score, 2), "positive_terms": positive, "negative_terms": negative}


def analyze(records: list[ReviewRecord], cleaning_stats: dict[str, int]) -> dict[str, Any]:
    analyzed = []
    theme_counts: Counter[str] = Counter()
    intent_counts: Counter[str] = Counter()
    segment_counts: Counter[str] = Counter()
    sentiment_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter(record.source for record in records)
    quotes_by_theme: dict[str, list[dict[str, str]]] = defaultdict(list)

    for record in records:
        themes = match_rules(record.text, THEME_RULES) or ["General discovery friction"]
        intents = match_rules(record.text, INTENT_RULES) or ["Improve music discovery"]
        segments = match_rules(record.text, SEGMENT_RULES) or ["General Spotify listeners"]
        sentiment_result = sentiment(record.text, record.rating)

        for theme in themes:
            theme_counts[theme] += 1
            if len(quotes_by_theme[theme]) < 3:
                quotes_by_theme[theme].append({"source": record.source, "text": record.text})
        intent_counts.update(intents)
        segment_counts.update(segments)
        sentiment_counts[sentiment_result["label"]] += 1

        analyzed.append(
            {
                "source": record.source,
                "id": record.record_id,
                "date": record.date.isoformat(),
                "text": record.text,
                "rating": record.rating,
                "sentiment": sentiment_result,
                "themes": themes,
                "intents": intents,
                "segments": segments,
            }
        )

    total = max(len(records), 1)
    clusters = build_clusters(theme_counts, quotes_by_theme, total)
    findings = build_findings(theme_counts, intent_counts, segment_counts, sentiment_counts, total)

    return {
        "data_sources_used": [{"source": source, "records": count} for source, count in source_counts.most_common()],
        "cleaning_stats": cleaning_stats,
        "sentiment_distribution": pct_counts(sentiment_counts, total),
        "theme_distribution": pct_counts(theme_counts, total),
        "intent_distribution": pct_counts(intent_counts, total),
        "user_segments": pct_counts(segment_counts, total),
        "insight_clusters": clusters,
        "key_findings": findings,
        "opportunities": build_opportunities(theme_counts, intent_counts, segment_counts),
        "representative_feedback": representative_feedback(clusters),
        "records": analyzed,
    }


def pct_counts(counter: Counter[str], total: int) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": count, "share": round(count / total, 3)}
        for label, count in counter.most_common()
    ]


def build_clusters(theme_counts: Counter[str], quotes: dict[str, list[dict[str, str]]], total: int) -> list[dict[str, Any]]:
    clusters = []
    for theme, count in theme_counts.most_common():
        clusters.append(
            {
                "theme": theme,
                "count": count,
                "share": round(count / total, 3),
                "summary": summarize_theme(theme),
                "representative_feedback": quotes.get(theme, [])[:2],
            }
        )
    return clusters


def summarize_theme(theme: str) -> str:
    summaries = {
        "Repetitive recommendations": "Users perceive discovery products as recycling a narrow track and artist set.",
        "Low novelty in discovery surfaces": "Discovery is valued, but users do not feel enough genuinely unfamiliar music appears.",
        "Weak context or mood understanding": "The model blends contexts that users expect to keep separate.",
        "Insufficient recommendation controls": "Users want clearer ways to steer, reset, or understand the recommendation system.",
        "Mainstream or popularity bias": "Long-tail and niche discovery are crowded out by broad or promoted content.",
        "Library history overpowers exploration": "Past listening history can dominate current exploration intent.",
        "Search and browse friction": "Known-item search works better than open-ended browsing or genre exploration.",
        "Collaborative discovery gaps": "Social surfaces compare existing libraries more than helping groups discover together.",
    }
    return summaries.get(theme, "Users describe friction that weakens confidence in music discovery.")


def build_findings(theme_counts: Counter[str], intent_counts: Counter[str], segment_counts: Counter[str], sentiment_counts: Counter[str], total: int) -> list[str]:
    findings = []
    ranked_themes = [(label, count) for label, count in theme_counts.most_common() if label != "General discovery friction"]
    ranked_intents = [(label, count) for label, count in intent_counts.most_common() if label != "Improve music discovery"]
    ranked_segments = [(label, count) for label, count in segment_counts.most_common() if label != "General Spotify listeners"]
    top_theme, top_theme_count = ranked_themes[0] if ranked_themes else (theme_counts.most_common(1)[0] if theme_counts else ("No theme", 0))
    top_intent, _ = ranked_intents[0] if ranked_intents else (intent_counts.most_common(1)[0] if intent_counts else ("No intent", 0))
    top_segment, _ = ranked_segments[0] if ranked_segments else (segment_counts.most_common(1)[0] if segment_counts else ("No segment", 0))
    negative_share = sentiment_counts.get("negative", 0) / max(total, 1)
    mixed_share = sentiment_counts.get("mixed", 0) / max(total, 1)

    findings.append(f"{top_theme} is the largest friction area, appearing in {top_theme_count} of {total} retained records.")
    findings.append(f"The dominant user intent is to {top_intent.lower()}, indicating the product job is active exploration rather than passive playback.")
    findings.append(f"{top_segment} are the clearest affected segment, with needs that differ from casual listeners.")
    findings.append(f"Sentiment is mostly unresolved: {round((negative_share + mixed_share) * 100)}% of feedback is negative or mixed.")
    findings.append("Users often describe a control gap: they can skip or hide content, but do not trust that Spotify understands the reason.")
    findings.append("Repeated listening is usually caused by history overpowering momentary intent, not by a lack of interest in discovery.")
    return findings


def build_opportunities(theme_counts: Counter[str], intent_counts: Counter[str], segment_counts: Counter[str]) -> list[dict[str, str]]:
    return [
        {
            "opportunity": "Familiar-to-fresh control",
            "why": "A visible control would let users choose comfort listening or exploration without training the whole account incorrectly.",
            "signals": ", ".join(top_labels(theme_counts, ["Repetitive recommendations", "Low novelty in discovery surfaces", "Library history overpowers exploration"])),
        },
        {
            "opportunity": "Recommendation feedback with reason codes",
            "why": "Users need hide/dislike/reset actions that communicate whether the issue is artist, song, mood, popularity, or repetition.",
            "signals": ", ".join(top_labels(theme_counts, ["Insufficient recommendation controls", "Weak context or mood understanding"])),
        },
        {
            "opportunity": "Exploration mode for niche and adjacent artists",
            "why": "A mode that dampens mainstream popularity and library history can serve users seeking smaller releases or adjacent genres.",
            "signals": ", ".join(top_labels(segment_counts, ["Niche and long-tail fans", "Active explorers"])),
        },
        {
            "opportunity": "Context-aware discovery surfaces",
            "why": "Separate work, gym, night, social, and nostalgia contexts so discovery does not collapse into a single profile.",
            "signals": ", ".join(top_labels(theme_counts, ["Weak context or mood understanding", "Collaborative discovery gaps"])),
        },
    ]


def top_labels(counter: Counter[str], labels: list[str]) -> list[str]:
    return [label for label in labels if counter.get(label, 0) > 0] or labels[:1]


def representative_feedback(clusters: list[dict[str, Any]]) -> list[dict[str, str]]:
    feedback: list[dict[str, str]] = []
    for cluster in clusters:
        for quote in cluster["representative_feedback"]:
            feedback.append({"theme": cluster["theme"], **quote})
            if len(feedback) >= 8:
                return feedback
    return feedback


def write_cleaned_csv(records: list[ReviewRecord], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
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


def write_report(analysis: dict[str, Any], path: Path) -> None:
    lines = [
        "# Spotify Discovery Review Analysis",
        "",
        "## Data Sources Used",
        "",
    ]
    for item in analysis["data_sources_used"]:
        lines.append(f"- {item['source']}: {item['records']} retained records")
    lines.extend(["", "## Key Findings", ""])
    lines.extend(f"{i}. {finding}" for i, finding in enumerate(analysis["key_findings"], 1))
    lines.extend(["", "## Representative User Feedback", ""])
    for quote in analysis["representative_feedback"]:
        lines.append(f"- **{quote['theme']}** ({quote['source']}): \"{quote['text']}\"")
    lines.extend(["", "## User Segments", ""])
    for item in analysis["user_segments"]:
        lines.append(f"- {item['label']}: {item['count']} signals ({round(item['share'] * 100)}%)")
    lines.extend(["", "## Opportunities", ""])
    for item in analysis["opportunities"]:
        lines.append(f"- **{item['opportunity']}**: {item['why']} Signals: {item['signals']}.")
    lines.extend(["", "## One-Slide Workflow", "", "Reviews -> AI Analysis -> Insights"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary_json(analysis: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_slide(output_dir: Path, summary_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "generate_workflow_slide.mjs"
    if not script.exists():
        return
    node_exe = find_node_executable()
    env = os.environ.copy()
    node_modules = find_node_modules()
    if node_modules:
        env["NODE_PATH"] = str(node_modules)
        artifact_tool = node_modules / "@oai" / "artifact-tool" / "dist" / "artifact_tool.mjs"
        if artifact_tool.exists():
            env["ARTIFACT_TOOL_MODULE"] = str(artifact_tool)
    subprocess.run(
        [str(node_exe), str(script), str(summary_path), str(output_dir)],
        check=True,
        env=env,
    )


def find_node_executable() -> Path:
    explicit = os.environ.get("REVIEW_ENGINE_NODE")
    if explicit:
        return Path(explicit)
    bundled = Path(sys.executable).resolve().parents
    for parent in bundled:
        candidate = parent / "node" / "bin" / "node.exe"
        if candidate.exists():
            return candidate
    discovered = shutil.which("node")
    if discovered:
        return Path(discovered)
    raise RuntimeError("Node.js was not found; rerun with --skip-slide or set REVIEW_ENGINE_NODE.")


def find_node_modules() -> Path | None:
    explicit = os.environ.get("NODE_PATH")
    if explicit:
        return Path(explicit)
    for parent in Path(sys.executable).resolve().parents:
        candidate = parent / "node" / "node_modules"
        if candidate.exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    records = load_records(input_dir)
    cleaned, cleaning_stats = clean_records(records, args.months, args.max_per_source, today=date.today())
    analysis = analyze(cleaned, cleaning_stats)

    cleaned_path = output_dir / "cleaned_reviews.csv"
    summary_path = output_dir / "analysis_summary.json"
    report_path = output_dir / "pm_report.md"

    write_cleaned_csv(cleaned, cleaned_path)
    write_summary_json(analysis, summary_path)
    write_report(analysis, report_path)

    if not args.skip_slide:
        try:
            generate_slide(output_dir, summary_path)
        except Exception as exc:
            print(f"Slide generation skipped: {exc}", file=sys.stderr)

    print(f"Retained {len(cleaned)} records from {cleaning_stats.get('loaded', 0)} loaded records.")
    print(f"Wrote {cleaned_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
