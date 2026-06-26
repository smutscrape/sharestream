![Logo Placeholder](https://codeberg.org/sharestream/sharestream/raw/branch/main/static/sharestream.svg)

# Sharestream

**Sharestream** is a lightweight, secure proxy for sharing videos from your self‑hosted media library. It generates clean, optionally expiring, optionally password‑protected links to individual videos or whole collections, and produces first‑class link previews on platforms like Lemmy, Mastodon, Bluesky, and Discord.

Built with FastAPI, Sharestream sits in front of your media server and exposes only what you choose to share — your library credentials never leave the server, and only the items you explicitly publish are reachable.

> **Backend support:** Sharestream currently integrates with [Stash](https://stashapp.cc). **Jellyfin support is planned for the near term**, and the codebase has been refactored toward a backend‑neutral design to make additional sources straightforward.

## Highlights

### Rich social embeds
Sharestream emits Open Graph / Twitter‑player metadata tuned per platform, so your links look great everywhere:
- **Animated WebP** thumbnails for Lemmy, Mastodon, Discord, and browsers
- **Static JPEG** fallback for Reddit and Embed.ly (which can't render WebP) — same URL, negotiated per request
- **Animated GIF** for Matrix's `matrix-media-repo` (which stores WebP as a still) — transcoded on the fly, downscaled to 400px wide, frame rate halved, kept under 3 MB
- **Dedicated embed player** for Mastodon's `twitter:player` iframe — bare video, no site chrome
- **Configurable embed policy**: choose whether `og:video` uses the short preview clip or the full video, globally or per‑share, with an optional *dynamic* mode that picks based on file size and duration

### Clean, adaptive interface
- **Dark theme**: an easy‑on‑the‑eyes color scheme with subtle, tasteful animations
- **Glassmorphism accents**: frosted‑glass surfaces and soft depth
- **Adaptive video player**: auto‑sizes to fit the viewport at the video's true aspect ratio (landscape *and* portrait), with metadata flowing beside it when there's room and stacking below when there isn't
- **Animated previews**: card thumbnails animate on hover, and collection cards cycle through animated previews of the videos inside them
- **Mobile‑friendly**: responsive layout that works across devices

### Short, clean URLs
Shares and static site pages live at memorable top‑level paths:
- Individual video: `https://yourdomain/{slug}` (random token or a custom slug)
- Collection gallery: `https://yourdomain/{gallery}`
- A video within a collection: `https://yourdomain/{gallery}/{sqid}`
- Global video page: `https://yourdomain/v/{sqid}`
- Static page: `https://yourdomain/{slug}` (from `data/pages/{slug}.md` — e.g. `data/pages/community.md` → `/community`)

Legacy `/share/...` and `/tag/.../video/...` links keep working, so previously shared links continue to function.

### Collection sharing
Share groups of videos with a single link:
- Group videos by tag into themed collections
- **Drag‑to‑reorder** collections in the admin panel — the order is reflected on the homepage
- Custom, human‑readable share IDs
- Per‑video playback within a collection, with the same rich metadata as individual shares

### Public gallery
- Optional curated homepage for featured content (opt‑in per share via "Feature on Home?")
- Sort by **Date** (default), Title (A→Z), Play Count, Rating, or Random — consistent across the homepage and collection galleries
- **Configurable default sort** in `config.yaml`; individual collection shares can override it in the admin panel
- **Aggregate play counts** per video (summed across every share context) shown on cards and used for "Play Count" sorting
- **Duration badges** on video cards (compact runtime in the bottom‑right corner)
- **Masonry layout** (optional): instead of cropping every card to one shape, arrange cards in balanced columns at each video's *native* aspect ratio. Toggle it per collection in the admin panel, default the toggle's state site‑wide, and/or enable it for the homepage "All Videos" grid — all via `config.yaml`
- Lazy‑loading thumbnails and hover‑to‑animate previews

### Reactive faceted search
Inside any collection gallery, a search bar lets viewers filter the grid in real time:
- **Text search** against video titles and descriptions
- **Entity chips** for performers, studios, and tags — add multiple to narrow results
- **Autocomplete** with prefix‑first ranking and tag‑alias support
- **Strict AND filtering**: selecting multiple performers or tags requires *all* of them to be present
- Results update the gallery grid directly, preserving masonry layout and hover previews

### Scene visibility model
Sharestream uses a config‑driven visibility system backed by Stash tags:
- **Public** scenes appear on the homepage, in search, and at `/v/{slug}`
- **Listed** scenes appear in search and at `/v/{slug}`, but not on the homepage
- **Unlisted** scenes are reachable only by their individual share slug or inside a password‑protected collection
- **Hidden** scenes are 404 everywhere

Password‑protected collection shares may include unlisted scenes; public (no‑password) collections and the `/tag/{tag}` gallery show only listed/public content.

### Filedrop: public upload page
An optional upload page lets visitors submit videos directly into your Stash library:
- Configurable file‑size limit and allowed extensions
- Optional password gate for the upload page itself
- Automatic Stash scanning, tagging, and metadata generation
- Optional user‑chosen tags from the public tag vocabulary
- Optional auto‑minted password‑protected share link on completion
- [Smutscrape](https://codeberg.org/smutscrape/smutscrape) integration for URL‑based scraping and ingestion

## Key Features

| Feature | Description |
|:--------|:------------|
| **Rich Social Embeds** | Animated WebP / JPEG / GIF content negotiation, Mastodon embed player, configurable preview‑vs‑full `og:video` policy |
| **Adaptive Playback** | Video.js player with a custom skin, looping, and 30‑second skip controls |
| **Smart Thumbnails** | Animated WebP where supported, static JPEG for Reddit/Embed.ly, animated GIF for Matrix — same URL, negotiated per request |
| **Admin Dashboard** | Full create / edit / delete management for both individual and collection shares |
| **Secure Sharing** | Optional per‑share passwords and expiring links |
| **Resolution Control** | Choose streaming quality: LOW, MEDIUM, or HIGH |
| **Scene Visibility** | Config‑driven public / listed / unlisted / hidden tiers via Stash tags |
| **Faceted Search** | Reactive in‑gallery filtering by text, performers, studios, and tags with autocomplete |
| **View Tracking** | Anonymous per‑scene play counting, aggregated across all entry paths |
| **Static Pages** | Markdown files in `data/pages/` rendered as HTML at top‑level `/{slug}` URLs |
| **Markdown Descriptions** | Video descriptions from Stash render as formatted Markdown on the player page |
| **Filedrop Uploads** | Optional public upload page with Stash ingestion, tagging, and auto‑share |
| **Contact / Takedown Form** | Built‑in DMCA‑style request form that emails you |

## Efficient GraphQL & unguessable URLs

Sharestream minimizes upstream load by requesting only the fields it needs from Stash — tag‑membership checks use an id‑only query, gallery renders batch scene metadata in one call, and the autocomplete cache fetches just tags, performers, and studios without pulling full scene payloads.

Video URLs use **[Sqids](https://sqids.org/)** (formerly Hashids) to encode Stash scene IDs into short, non‑sequential strings — so `/v/{sqid}` never exposes a raw sequential ID, and guessing adjacent scenes is infeasible. For production deployments, **randomize your Sqids alphabet** so the encoding is unique to your instance. Generate a shuffled base62 alphabet with:

```bash
python3 -c "import random; s=list('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'); random.shuffle(s); print(''.join(s))"
```

Set the output as `sharestream.slug_alphabet` in your `config.yaml`. Keep it stable across restarts — changing it invalidates every existing `/v/` link.

## Installation & Setup

### Prerequisites
* Python 3.10+ (the codebase uses modern type syntax; 3.12 recommended)
* Mamba/Conda or pip
* A running Stash instance with API access

### Quick Start

1. **Clone the repository:**
```bash
git clone https://codeberg.org/sharestream/sharestream
cd sharestream
```

2. **Set up your environment:**

Using mamba (recommended):
```bash
mamba create -n sharestream python=3.12 fastapi uvicorn httpx pyyaml sqlalchemy pydantic python-jose cryptography passlib bcrypt jinja2 python-multipart markdown bleach
mamba activate sharestream
```

Or with pip:
```bash
pip install -r requirements.txt
```

3. **Configure your instance** in `config.yaml` (see `example-config.yaml` for a fully‑commented template):
```yaml
site_name: "Your Site"
site_motto: "An optional tagline"

# Optional notice shown once to new visitors (remembered via cookie). Use it for
# an age gate, a disclaimer, or any "please acknowledge before entering" message.
content_warning: ""

# Optional icons shown in the footer
social_links:
  - name: "Mastodon"
    logo: "./static/social_logos/mastodon.svg"
    url: "https://mastodon.social/@you"
    rel: "me" # include this for Mastodon verification

sharestream:
  host: "0.0.0.0"
  port: 6969
  base_domain: "https://media.example.com"   # Your public domain (used in share links & previews)
  admin_username: "admin"
  admin_password: "your_secure_password"
  default_resolution: MEDIUM
  share_id_length: 5                          # length of auto‑generated share tokens
  default_sort: date                          # optional: date | title | hits | rating | random
  slug_alphabet: "..."                        # optional: shuffled base62 alphabet for unguessable /v/ URLs
  slug_min_length: 6                          # optional: minimum Sqids output length

stash:
  server_ip: "127.0.0.1"
  port: 9999
  api_key: "your_stash_api_key"
  # Scene visibility is governed by visibility_tags below. limit_to_tag is
  # DEPRECATED as an access-control mechanism and now only scopes curated
  # Gallery surfaces. Configure visibility_tags and remove limit_to_tag when ready.
  # limit_to_tag: 10844
  visibility_tags:
    public: 123    # Stash tag id for publicly visible scenes
    listed: 456    # Stash tag id for listed (searchable but not on homepage) scenes
    hidden: 789    # Stash tag id for hidden (404 everywhere) scenes

# Social‑embed (og:video) policy for link previews
embed:
  mode: dynamic            # preview | full | dynamic
  max_full_duration: 60    # seconds  (dynamic: embed full only if at/under this)
  max_full_size_mb: 50     # MB       (dynamic: ...and at/under this)

# Gallery layout policy (both default false)
gallery:
  home_masonry: false      # homepage "All Videos" grid uses the masonry layout
  masonry_default: false   # default state of the per‑collection "Gallery mode?" toggle for NEW shares

# Caching policy
cache:
  tag_membership_ttl_minutes: 15   # how long a tag's video-membership set is cached (default 15)

# SMTP settings for the contact / takedown form (optional)
contact_form:
  mailto: contact@yourdomain
  host: smtp.example.com
  port: 587
  user: you@yourdomain
  pass: your_smtp_password

# Optional disclaimer shown in the page footer. Not legal advice — adapt to your
# situation and the nature of the content you host.
disclaimer: "This content is shared for private use only. Unauthorized distribution is prohibited."
```

4. **Add your branding** (all optional — drop files into `static/localized/`):
- **Logo**: `logo.svg` is preferred (crisp, tiny, no flash). PNGs (`logo.png` + optional `logo@2x.png`/`logo@3x.png`) also work.
- **Favicon**: `favicon.ico`/`favicon.png` (a themed default is generated if you don't provide one).
- **Fonts**: drop a `.woff2` into `static/localized/fonts/` named `base_font.woff2`, `title_font.woff2`, `button_font.woff2`, `motto_font.woff2`, or `disclaimer_font.woff2` to override that slot. No CSS editing — and no 404s for slots you leave empty. Defaults are clean web fonts (Inter for body, Chakra Petch for titles/buttons).
- **Static pages**: add Markdown files to `data/pages/` (created automatically on first run). A file `terms.md` is served at `/terms`. Use lowercase filenames; the first `# Heading` becomes the page title.

5. **Run the server** (from the project root, so the relative `config.yaml`, `static/`, `data/`, and database paths resolve):
```bash
# ASGI entry point — host/port come from config.yaml
uvicorn sharestream.main:app --host 0.0.0.0 --port 6969

# or use the bundled runner (honors config host/port and a --debug flag)
python -m sharestream
```
The app is a modular package (currently `sharestream/`; `main.py` exposes `app = create_app()`). `core/`, `db/`, `schemas/`, `backends/`, `services/`, and `routers/` hold the rest.

## Admin Panel

Access the admin panel at `https://yourdomain/__admin`.

### Sharing a video
1. Enter a Stash video ID or use "Lookup" for auto‑fill
2. Set expiration (1–365 days)
3. Choose resolution and an optional password
4. Pick a **Share ID** (Random or Custom slug) and a **Social Embed** mode
5. Toggle "Feature on Home?" for gallery display
6. Copy your short share link

### Sharing a collection
1. Enter a tag name and click "Lookup" to verify it exists
2. Choose share ID type — **Random**, **Use Tag Name**, or **Custom**
3. Configure resolution, password, embed mode, default sort, "Feature on Home?", "Apply limit tag?", and "Gallery mode?" (masonry layout for this collection's page)
4. **Reorder** collections any time by dragging their rows in the Shared Collections list

### Managing shares
- **Full editing** of existing video *and* collection shares (name, expiry, resolution, password, gallery flag, embed mode, default sort, limit‑tag application, and masonry "Gallery mode?" for collections) — no need to delete and recreate
- Password handling on edit: leave blank to keep the existing one, or tick "Remove password"
- **Real‑time stats**: view counts and relative expiration times
- **Quick actions**: copy, edit, delete; bulk refresh; **Clear Cache** (drops the cached tag→video membership sets — use it right after retagging items in Stash so a collection reflects the change without waiting out the TTL)

## Viewing Experience

### Video player
- Custom‑skinned Video.js player with looping and 30‑second skip buttons
- Player box auto‑fits the viewport at the video's native aspect ratio (read from the source up front, so portrait videos render correctly on first paint)
- Rounded corners, frosted controls
- Full metadata, identical for individual and collection videos:
  - Statistics (aggregate views across all share contexts, rating, date)
  - Performers/contributors with website and social links
  - Studio/source, tags, **Markdown‑formatted description**, external URLs, duration, resolution
- Password protection via a styled modal

### Gallery
- Hover a card to play an animated preview
- Collection cards cycle through animated previews of their contents
- Play count and duration overlays on each video card
- Reactive faceted search bar with autocomplete for performers, studios, and tags
- Lazy‑loaded thumbnails, sort controls, responsive grid

## Security & Privacy

- **JWT Authentication**: secure admin access
- **Capability URLs**: shares are reached via unguessable tokens (or your chosen custom slug), never by raw source IDs. Global `/v/{sqid}` URLs use Sqids with an optional randomized alphabet so scene IDs are non‑sequential and unguessable.
- **Scene visibility model**: config‑driven public / listed / unlisted / hidden tiers via Stash tags. Public

> **Live instances:**
> - **SFW demo:** [demo.silent.surf](https://demo.silent.surf) — uptime not guaranteed, may be reset at any time
> - **NSFW production example:** [homeschool.porn](https://homeschool.porn) — kinky taboo porn captions, very not safe for work
