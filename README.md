# The Daily Build

A personal morning newspaper. Every day at about 6 a.m. Pacific, a GitHub Action collects the most-upvoted posts from the subreddits in `config.json`, files them in `data/`, and republishes the page on GitHub Pages. Past editions stay in the archive for 90 days.

## Setup (about 10 minutes)

1. **Make a repository.** On GitHub, create a new public repository (free Pages needs public, or a paid plan for private) and upload everything in this folder, including the hidden `.github` folder.
2. **Turn on Pages.** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
3. **Set your user agent.** In `config.json`, replace `YOUR_REDDIT_USERNAME` with your Reddit username. Reddit asks every automated reader to identify itself.
4. **Print the first edition.** Actions tab → "Print today's edition" → Run workflow. When it finishes, your paper is at `https://<your-username>.github.io/<repo-name>/`.

From then on it prints itself every morning.

## If Reddit blocks the collector

Reddit sometimes refuses unsigned requests from cloud servers like GitHub's. When that happens the collector falls back to Reddit's RSS feeds, which still work but carry no upvote counts (that section will say "scores unavailable today"). To get reliable scores, register a free Reddit app:

1. Go to https://www.reddit.com/prefs/apps → "create another app" → type **script**, any name, redirect URI `http://localhost`.
2. In your GitHub repository: Settings → Secrets and variables → Actions → add `REDDIT_CLIENT_ID` (the string under the app name) and `REDDIT_CLIENT_SECRET`.

Reddit has been tightening API access, so if app creation asks you to apply for approval, the RSS fallback keeps the paper running in the meantime.

## Changing what's covered

Edit the `sections` list in `config.json`:

- `subreddit`: the subreddit name without `r/`
- `period`: `day`, `week` or `month`. Small communities (like r/ClaudeWorkflows) are set to `week` so the section isn't empty most mornings.
- `count`: how many stories the section shows
- `min_score`: leave out posts below this many upvotes

Any news site's RSS feed can be a section too:

```json
{ "id": "hn", "title": "Hacker News", "type": "rss", "url": "https://hnrss.org/frontpage?points=150", "count": 6 }
```

## Previewing on your own computer

```
python3 fetch.py            # collects a real edition into data/
python3 -m http.server      # then open http://localhost:8000
```
