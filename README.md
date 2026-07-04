# Drix Scout

Pre-meeting sales intelligence from free feeds. Fill in whatever you know about
a prospect — company, person, industry, solution, region, competitors, or just
a lens like "Breaches" — and get a ranked brief in four importance tiers.

Built on the WorldMonitor feed-curation technique: parameterized Google News
RSS queries plus institutional press feeds (Federal Reserve, SEC). No paid
APIs, no API keys, no database. One file, Python standard library only.

## Run locally

```
python main.py
```

Opens your browser at http://localhost:8787. On Windows, double-click
`Drix Scout.bat` instead.

## Deploy to Railway

1. Push this folder to a GitHub repository.
2. In Railway: **New Project → Deploy from GitHub repo** → pick the repo.
   Nixpacks auto-detects Python (via `requirements.txt`) and starts it with
   the `Procfile` (`web: python main.py`).
3. **Settings → Networking → Generate Domain.** No PORT configuration needed —
   the app reads Railway's `PORT` env var and binds `0.0.0.0` automatically.
4. Recommended: **Variables → add `DRIX_KEY`** with any secret value. Without
   it the app is open to the whole internet. With it, visit
   `https://your-app.up.railway.app/?key=YOUR_VALUE` once per browser — a
   cookie keeps you signed in for 30 days.

## Notes

- The app stores nothing. Every brief is fetched live and held in memory only.
- Use **Print / Save as PDF** in the app to keep a brief.
- Google News RSS is free but unofficial; if a cloud IP ever gets rate-limited,
  results thin out temporarily rather than erroring.
- `samples/` holds the original prototype script and an example one-page brief
  (North Dallas Bank & Trust).
# DRiX-Research
