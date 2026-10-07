# newsfeed backend — worldwide outlet directory and news API

There is no server to run. GitHub runs three scripts on a schedule and publishes the results as
static JSON files on GitHub Pages, which the Android app downloads.

```
Wikidata ──► build_directory.py ──► data/directory.json ─┐
                                                          ├──► fetch_posts.py ──► site/api/*.json ──► GitHub Pages
outlet websites ──► discover_feeds.py ──► data/feeds.json ┘
```

## What each script does

| Script | When | What it produces |
| --- | --- | --- |
| `scripts/build_directory.py` | monthly (daily workflow, skips when fresh) | `data/directory.json` — every newspaper, daily newspaper, news agency, news website and online newspaper in Wikidata that has a country and a website, plus everything in `data/manual_outlets.json` |
| `scripts/discover_feeds.py` | every run, 1,500 outlets at a time | `data/feeds.json` — the public RSS/Atom feed for each outlet, found from the site's `<link rel="alternate">` tag or the usual paths, honouring robots.txt |
| `scripts/fetch_posts.py` | every 30 minutes | `site/api/index.json`, `site/api/outlets.json`, `site/api/outlets/<CC>.json`, `site/api/posts/<CC>.json` |
| `scripts/verify_feeds.py` | on demand | checks a list of candidate outlets and reports which feeds actually work |

## Published API

| URL | Contents |
| --- | --- |
| `/api/index.json` | `{"generated","outlets","live","posts","countries","countries_with_posts"}` |
| `/api/outlets.json` | array of `{"id","name","country","lang","site","live"}` — `live` means a working feed was found |
| `/api/outlets/<CC>.json` | the same, for one country only (smaller download) |
| `/api/posts/<CC>.json` | newest first, max 800: `{"o","t","s","l","ts","y","i"}` — outlet id, headline, snippet (≤220 chars), link, epoch ms, `article`/`video`/`short`, optional image |
| `/privacy.html` | the privacy policy used by the Play listing and the app |

Every one of the 193 UN countries gets a posts file, even when empty, so the app never hits a 404
for a country the user selected.

## Setup (already done for this repository)

1. Public repository `newsfeed-backend`.
2. **Settings → Pages → Source: GitHub Actions.**
3. Optional: set a repository variable `CONTACT_EMAIL` (Settings → Secrets and variables → Actions →
   Variables) so feed owners can contact you. The scripts fall back to the repository owner's
   GitHub address.
4. **Actions → Update outlet list and feeds → Run workflow** to rebuild the directory and find more
   feeds. **Actions → Refresh news posts → Run workflow** to publish the API.

## Honest limits

- Wikidata coverage is uneven: some countries have hundreds of outlets, others few. Add missing
  outlets to `data/manual_outlets.json` (copy an existing line; `feed` and `yt` are optional).
- Not every outlet publishes a feed, so some stay `"live": false`. Sites that block automated
  access are skipped rather than fought with.
- Feed discovery is capped per run; it converges over several days. The directory workflow runs
  twice a day for that reason.
- Only a headline, a short snippet, a link and a timestamp are stored. Full articles are never
  republished — readers are sent to the outlet's own page. Respect each outlet's terms.
- Scheduled workflows pause after 60 days without repository activity; the daily data commit
  normally keeps the repository active.
- GitHub Pages has soft size and bandwidth limits. If the directory or traffic grows, split
  `outlets.json` per country (already published) or move to Cloudflare R2/Pages or Firebase
  Hosting plus Cloud Run.
