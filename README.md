# Bluesky AI Vault

Simple service: **AI chat on top** of Bluesky fetch → vault → schedule / post-now (Zernio).

## Quick start

```bash
cd ai-vault
pip install -r requirements.txt

# Optional but recommended for full natural-language control
export XAI_API_KEY=your_xai_key

# Already has your Neon + Zernio keys from the original app
python app.py
```

Open http://localhost:10000

## What you can say

- “Login with myhandle.bsky.social and xxxx-xxxx-xxxx-xxxx”
- “Fetch 20 posts from @dailymotivator”
- “Save them all to the vault”
- “Schedule 10 posts over the next week, every 3 hours”
- “Post the latest vault item now as a story”
- “What’s the status?” / “List vault” / “List scheduled”

## Environment variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Neon Postgres (already set in code as fallback) |
| `ZERNIO_API_KEY` | Zernio key (already set as fallback) |
| `XAI_API_KEY` | xAI / Grok key — enables real AI tool calling |
| `PORT` | Default 10000 |

Without `XAI_API_KEY` the chat still works with a simple keyword fallback (status, list vault, etc.).
"# smartinstagram" 
