from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from src.collect_sources import collect_link, cutoff_date, dedupe, write_csv
from src.review_discovery_engine import analyze, clean_records, load_csv


ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "outputs"
UPLOAD_DIR = ROOT / "data" / "uploads"


st.set_page_config(
    page_title="AI-Powered Review Discovery Engine",
    page_icon="🔎",
    layout="wide",
)


def link_config_from_url(url: str) -> dict:
    parsed = __import__("urllib.parse").parse.urlparse(url.strip())
    host = parsed.netloc.lower()
    if "play.google.com" in host:
        query = __import__("urllib.parse").parse.parse_qs(parsed.query)
        return {
            "source": "Play Store",
            "url": url,
            "app_id": query.get("id", [""])[0] or "com.spotify.music",
            "country": "us",
            "language": "en",
        }
    if "apps.apple.com" in host:
        match = re.search(r"id(\d+)", parsed.path)
        return {"source": "App Store", "url": url, "app_id": match.group(1) if match else "", "country": "us"}
    if "reddit.com" in host:
        return {"source": "Reddit", "url": url}
    if "community.spotify.com" in host:
        return {"source": "Spotify Community", "url": url}
    if host in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
        return {"source": "X", "url": url}
    return {"source": "Unsupported", "url": url}


def compact_summary(summary: dict) -> dict:
    sentiment = summary.get("sentiment_distribution", [])
    sentiment_counts = {item.get("label"): item.get("count", 0) for item in sentiment}
    sentiment_label = max(sentiment_counts, key=sentiment_counts.get).title() if sentiment_counts else "Mixed"
    return {
        "sources": summary.get("data_sources_used", []),
        "stats": summary.get("cleaning_stats", {}),
        "sentiment": {"label": sentiment_label, "distribution": sentiment},
        "themes": summary.get("theme_distribution", [])[:8],
        "intents": summary.get("intent_distribution", [])[:8],
        "segments": summary.get("user_segments", [])[:8],
        "findings": summary.get("key_findings", [])[:6],
        "opportunities": opportunity_rows(summary),
        "mvp_ideas": [
            "Familiar-to-Fresh Slider for radio, Daily Mix, and playlist recommendations.",
            "New for You playlist that excludes recently played or saved tracks.",
            "Reason-Based Feedback for too familiar, wrong mood, wrong genre, or too mainstream.",
        ],
    }


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
    ]
    for row in rows:
        row["score"] = row["frequency"] * row["pain"] * row["impact"]
    return sorted(rows, key=lambda item: item["score"], reverse=True)


def run_analysis_from_csv(csv_path: Path) -> dict:
    records = load_csv(csv_path)
    cleaned, stats = clean_records(records, months=12, max_per_source=500, today=date.today())
    summary = analyze(cleaned, stats)
    st.session_state.latest_records = summary.get("records", [])
    st.session_state.latest_summary = summary
    return compact_summary(summary)


def collect_and_analyze_links(raw_links: str) -> tuple[dict | None, list[dict]]:
    links = [line.strip() for line in re.split(r"[\n,]+", raw_links) if line.strip()]
    statuses = []
    collected = []
    cutoff = cutoff_date(12)
    for url in links[:10]:
        config = link_config_from_url(url)
        if config["source"] == "Unsupported":
            statuses.append({"source": "Unsupported", "url": url, "records_collected": 0, "status": "skipped"})
            continue
        records, status = collect_link(config, cutoff, max_records=200, sleep=0.25)
        collected.extend(records)
        statuses.append({"source": config["source"], "url": url, "records_collected": len(records), "status": status})

    unique = dedupe(collected)
    if not unique:
        return None, statuses

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = UPLOAD_DIR / "streamlit_scraped_link_reviews.csv"
    write_csv(unique, csv_path)
    return run_analysis_from_csv(csv_path), statuses


def answer_question(question: str) -> tuple[str, list[dict]]:
    records = st.session_state.get("latest_records", [])
    summary = st.session_state.get("latest_summary", {})
    if not records:
        return "Upload a CSV or scrape links first. Answers are based only on the latest user-provided dataset.", []

    terms = [
        term
        for term in re.findall(r"[a-zA-Z][a-zA-Z']+", question.lower())
        if len(term) > 3 and term not in {"what", "why", "which", "users", "review", "reviews", "about", "does", "from", "with", "that"}
    ]
    scored = []
    for record in records:
        text = record.get("text", "")
        score = sum(1 for term in terms if term in text.lower())
        score += sum(1 for theme in record.get("themes", []) if any(term in theme.lower() for term in terms))
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    evidence = [record for _, record in scored[:3]] or records[:3]

    themes = summary.get("theme_distribution", [])[:3]
    intents = summary.get("intent_distribution", [])[:3]
    parts = []
    if themes:
        parts.append("Top themes: " + ", ".join(f"{item['label']} ({item['count']})" for item in themes) + ".")
    if intents:
        parts.append("Main intents: " + ", ".join(item["label"] for item in intents) + ".")
    if scored:
        parts.append(f"Found {len(scored)} matching reviews for this question.")
    return " ".join(parts), evidence


def show_analysis(data: dict) -> None:
    st.subheader("Analysis Summary")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Records", data.get("stats", {}).get("retained", 0))
    col2.metric("Top Theme Mentions", data.get("themes", [{}])[0].get("count", 0) if data.get("themes") else 0)
    col3.metric("Sentiment", data.get("sentiment", {}).get("label", "Mixed"))
    col4.metric("Best Score", data.get("opportunities", [{}])[0].get("score", 0) if data.get("opportunities") else 0)

    left, right = st.columns([2, 1])
    with left:
        st.markdown("### Top Findings")
        for finding in data.get("findings", []):
            st.markdown(f"- {finding}")
    with right:
        st.markdown("### User Segments")
        st.dataframe(pd.DataFrame(data.get("segments", [])), hide_index=True, use_container_width=True)

    st.markdown("### Theme Extraction")
    st.dataframe(pd.DataFrame(data.get("themes", [])), hide_index=True, use_container_width=True)

    st.markdown("### Opportunity Prioritization")
    st.dataframe(pd.DataFrame(data.get("opportunities", [])), hide_index=True, use_container_width=True)

    st.markdown("### MVP Ideas")
    for idea in data.get("mvp_ideas", []):
        st.markdown(f"- {idea}")


st.title("AI-Powered Review Discovery Engine")
st.caption("AI-powered system that analyzes user feedback at scale")

with st.expander("Questions this system helps answer", expanded=True):
    st.markdown(
        """
        - Why do users struggle to discover new music?
        - What are the most common frustrations with recommendations?
        - What listening behaviors are users trying to achieve?
        - What causes users to repeatedly listen to the same content?
        - Which user segments experience different discovery challenges?
        - What unmet needs emerge consistently across reviews?
        """
    )

tab_current, tab_upload, tab_links, tab_ask = st.tabs(["Current analysis", "Upload CSV", "Paste links", "Ask reviews"])

with tab_current:
    if st.button("Load current analysis"):
        summary_path = OUTPUT_DIR / "analysis_summary.json"
        if summary_path.exists():
            st.session_state.current_analysis = compact_summary(json.loads(summary_path.read_text(encoding="utf-8")))
        else:
            st.error("No existing analysis_summary.json found.")
    if "current_analysis" in st.session_state:
        show_analysis(st.session_state.current_analysis)

with tab_upload:
    uploaded = st.file_uploader("Upload review CSV", type=["csv"])
    if uploaded and st.button("Analyze uploaded CSV"):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = UPLOAD_DIR / uploaded.name
        path.write_bytes(uploaded.getvalue())
        with st.spinner("Investigating uploaded reviews..."):
            st.session_state.user_analysis = run_analysis_from_csv(path)
        st.success("Uploaded CSV analyzed.")
    if "user_analysis" in st.session_state:
        show_analysis(st.session_state.user_analysis)

with tab_links:
    links = st.text_area(
        "Paste one review/discussion link per line",
        placeholder="https://play.google.com/store/apps/details?id=com.spotify.music\nhttps://www.reddit.com/r/spotify/",
        height=140,
    )
    if st.button("Scrape and analyze links"):
        with st.spinner("Scraping links and analyzing collected reviews..."):
            analysis, statuses = collect_and_analyze_links(links)
        st.session_state.source_statuses = statuses
        if analysis:
            st.session_state.user_analysis = analysis
            st.success("Links scraped and analyzed.")
        else:
            st.error("No records could be collected from the pasted links.")
    if "source_statuses" in st.session_state:
        st.markdown("### Source Collection Status")
        st.dataframe(pd.DataFrame(st.session_state.source_statuses), hide_index=True, use_container_width=True)
    if "user_analysis" in st.session_state:
        show_analysis(st.session_state.user_analysis)

with tab_ask:
    st.markdown("### Ask your reviews Anything")
    st.caption("Answers use only the latest uploaded CSV or scraped-link dataset.")
    question = st.text_input("Question", placeholder="What causes users to repeatedly listen to the same content?")
    if st.button("Ask"):
        answer, evidence = answer_question(question)
        st.write(answer)
        if evidence:
            st.markdown("#### Evidence")
            for item in evidence:
                st.info(f"{item.get('source', 'Review')}: {item.get('text', '')[:400]}")
