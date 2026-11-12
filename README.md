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

## Images

Each Reddit section's top story gets a picture, chosen in this order:

1. The image the poster attached (a photo or screenshot on Reddit)
2. The preview image of the site the post links to
3. An AI illustration drawn from the headline, captioned "AI illustration based on the headline"

AI illustrations need an OpenAI API key. Create one at https://platform.openai.com/api-keys, then in your GitHub repository go to Settings → Secrets and variables → Actions → New repository secret, name it `OPENAI_API_KEY` and paste the key. Without the key, stories that have no photo simply run without one.

The `images` block in `config.json` controls this: `ai_max_per_day` caps the drawings (default 4), `ai_quality` can be `low`, `medium` or `high`, and `"enabled": false` turns images off. At medium quality each drawing costs a few cents. Drawings are saved in `data/img/` and deleted with their edition after 90 days.

## Fonts

The page asks for Engravers' Old English BT, ITC Cheltenham and ITC Franklin Gothic first, so anyone who has them installed sees them. Everyone else gets close free versions from Google Fonts: UnifrakturMaguntia for the nameplate, Domine for headlines and Libre Franklin for labels and menus. Body text is Georgia, with the matching free Gelasio on devices that lack it.

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

## Research Papers

This section lists papers published in the past 7 days by the researchers in the `people` list of the `research-papers` entry in `config.json`, using OpenAlex, a free open index of scholarly papers. If none of them published anything that week, the section is left out of that day's edition. Each paper is labeled with which of your researchers is on it.

Clicking a paper's headline opens a reader panel with a plain-language summary, the authors' abstract and a button to the full paper. (Cmd- or Ctrl-click a headline to go straight to the paper instead.) Summaries are written by AI from the abstract only and are labeled as AI-written.

Summaries use whichever key the repository has. With an `OPENAI_API_KEY` secret (the same key the illustrations use) they're written by OpenAI's GPT-6 Luna. With an `ANTHROPIC_API_KEY` secret they're written by Claude Haiku instead, which takes priority when both keys exist. Each paper is summarized once and saved in `data/summaries.json`, so a week of papers costs well under a cent. With neither key, the panel shows the abstract alone. To change models or pick a provider, edit the `summaries` block in `config.json` (`"provider"` can be `auto`, `openai` or `anthropic`; `"enabled": false` turns summaries off).

How people are matched: each name (and each spelling in `variants`) is searched in OpenAlex among authors affiliated with Stanford (`"institution": "I97018004"`). A profile is kept only if the first and last names match, any middle initial you gave matches, and the profile's research topics fall in `fields` (Psychology or Neuroscience by default). That stops a same-named engineer or physician from slipping in. Matches are saved in `data/openalex_authors.json` and refreshed weekly; open that file to see exactly which profiles were used and which were rejected.

If someone is matched wrongly or not at all (new students often have few papers in OpenAlex yet), find their profile on openalex.org and pin it by hand:

```json
{ "name": "Leslie Remache", "variants": ["Leslie Remache"], "openalex_ids": ["A5012345678"] }
```

Change `"days"` to look further back. OpenAlex works without a key and this section uses well under its free daily allowance. If you see rate-limit messages in the log, create a free account at openalex.org, copy the key from Settings → API key, and save it as a repository secret named `OPENALEX_API_KEY`.

## Sports sections

Each team card shows the record, last result and next game from ESPN, the newest headlines from Google News, and (for teams with a `subreddit`) the most-upvoted fan posts of the day. A "Final scores" box on the front page lists any game finished in the last 36 hours.

To add a team, put an entry in a sports section's `teams` list:

```json
{ "name": "Warriors", "espn": "basketball/nba/gs", "news": "\"Golden State Warriors\"", "subreddit": "warriors" }
```

- `espn` is `sport/league/team`. Pro teams use the abbreviation in their ESPN page address (`/_/name/sf/`); college teams use the number (`/_/id/24/`). Leagues include `baseball/mlb`, `football/nfl`, `basketball/nba`, `hockey/nhl`, `football/college-football`, `basketball/mens-college-basketball` and `basketball/womens-college-basketball`.
- `news` is a Google News search. Quote exact phrases; `news_days` on the section sets how far back to look.
- ESPN's results feed is public but unofficial, so it could change without notice. If it does, the cards say scores were unavailable and the headlines still print.

## Previewing on your own computer

```
python3 fetch.py            # collects a real edition into data/
python3 -m http.server      # then open http://localhost:8000
```
