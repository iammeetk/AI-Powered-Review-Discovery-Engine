# AI-Powered Review Discovery Engine

AI-powered product insight workflow that analyzes user feedback at scale across app reviews, Reddit discussions, community forums, and uploaded CSV exports.

The project is prepared for Streamlit Cloud deployment with `app.py` as the app entry file.

## Features

- Load existing Spotify discovery analysis from `outputs/analysis_summary.json`.
- Upload review CSV files and analyze them in-session.
- Paste links from Play Store, Reddit, Spotify Community, App Store, or X/social sources.
- Scrape supported public sources and analyze collected reviews.
- Extract themes, intents, user segments, sentiment, and opportunity scores.
- Ask questions against the latest uploaded or scraped dataset only.

## Setup

```bash
pip install -r requirements.txt
```

Required CSV fields:

```text
source,id,date,text,rating,url,author
```

Only `source`, `date`, and `text` are required for analysis.

## Local Run

```bash
streamlit run app.py
```

If `streamlit` is not on your PATH, run:

```bash
python -m streamlit run app.py
```

## Deployment Steps

### GitHub

```bash
git init
git add .
git commit -m "Prepare Streamlit review discovery app"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

If the repository already exists locally:

```bash
git status
git add .
git commit -m "Prepare Streamlit deployment"
git push
```

### Streamlit Cloud

1. Go to [Streamlit Community Cloud](https://share.streamlit.io/).
2. Sign in with GitHub.
3. Select **New app**.
4. Choose your repository and branch.
5. Set the main file path to:

```text
app.py
```

6. Deploy.

## Folder Structure

```text
.
â”œâ”€â”€ app.py
â”œâ”€â”€ requirements.txt
â”œâ”€â”€ .streamlit/
â”‚   â””â”€â”€ config.toml
â”œâ”€â”€ src/
â”‚   â”œâ”€â”€ collect_sources.py
â”‚   â”œâ”€â”€ review_discovery_engine.py
â”‚   â””â”€â”€ workflow_server.py
â”œâ”€â”€ data/
â”‚   â”œâ”€â”€ raw/
â”‚   â””â”€â”€ uploads/
â”œâ”€â”€ outputs/
â”œâ”€â”€ tools/
â”œâ”€â”€ web/
â””â”€â”€ source_links.json
```

## Tech Stack

- Python
- Streamlit
- Pandas
- `google-play-scraper`
- Standard-library scraping utilities

## Troubleshooting

- **Streamlit command not found**: use `python -m streamlit run app.py`.
- **Play Store links do not scrape**: confirm `google-play-scraper` is installed from `requirements.txt`.
- **App Store returns no reviews**: Apple may require authenticated or API-based access for some review feeds.
- **Reddit returns blocked/rate-limited status**: try again later or use exported Reddit data as CSV.
- **Q&A says no uploaded dataset**: upload a CSV or scrape links first; the Q&A intentionally ignores the preloaded demo analysis.
- **Streamlit Cloud deployment fails**: check that `requirements.txt` is committed and the main file path is `app.py`.

## Notes

The analysis layer is deterministic and lightweight. It uses rule-based extraction for themes, sentiment, intents, segments, and opportunity scoring so it can run reliably on Streamlit Cloud without external model credentials.

## Local Virtual Environment And API Key

Create and use the local virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Add your local LLM API key in `.env`:

```text
GROQ_API_KEY=your_real_api_key_here
```

For Streamlit Cloud, add the same key under **App settings -> Secrets**:

```toml
GROQ_API_KEY = "your_real_api_key_here"
```

Do not commit `.env` or `.streamlit/secrets.toml`; both are ignored by Git.

