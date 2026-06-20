![Logo Placeholder](https://codeberg.org/sharestream/sharestream/raw/branch/main/static/sharestream.svg)

# Sharestream

**Sharestream** is a lightweight, secure proxy for sharing videos from your self‑hosted media library. It generates clean, optionally expiring, optionally password‑protected links to individual videos or whole collections, and produces first‑class link previews on platforms like Lemmy, Mastodon, Bluesky, and Discord.

Built with FastAPI, Sharestream sits in front of your media server and exposes only what you choose to share — your library credentials never leave the server, and only the items you explicitly publish are reachable.

> **Backend support:** Sharestream currently integrates with [Stash](https://stashapp.cc). **Jellyfin support is planned for the near term**, and the codebase has been refactored toward a backend‑neutral design to make additional sources straightforward.


## Highlights

### Clean, adaptive interface
- **Dark theme**: an easy‑on‑the‑eyes color scheme with subtle, tasteful animations
- **Glassmorphism accents**: frosted‑glass surfaces and soft depth
- **Adaptive video player**: auto‑sizes to fit the viewport at the video's true aspect ratio (landscape *and* portrait), with metadata flowing beside it when there's room and stacking below when there isn't
- **Animated previews**: card thumbnails animate on hover, and collection cards cycle through animated previews of the videos inside them
- **Mobile‑friendly**: responsive layout that works across devices

### Short, clean URLs
Shares and static site pages live at memorable top‑level paths:
- Individual video: `https://yourdomain/{slug}` (random token or a custom slug)
- Collection: `https://yourdomain/{tag}`
- A video within a collection: `https://yourdomain/{tag}/{id}`
- Static page: `https://yourdomain/{slug}` (from `data/pages/{slug}.md` — e.g. `data/pages/community.md` → `/community`)

Legacy `/share/...` and `/tag/.../video/...` links keep working, so previously shared links continue to function. Old `/pages/{slug}` URLs permanently redirect to the top‑level path.

Custom share IDs and static page slugs share the same namespace — the admin panel rejects a share slug that collides with an existing page file (and vice versa: don't add a page file whose name matches an existing share).

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

## Key Features

| Feature | Description |
|:--------|:------------|
| **Adaptive Playback** | Video.js player with a custom skin, looping, and 30‑second skip controls |
| **Rich Link Previews** | Open Graph / Twitter‑player tags so posts embed cleanly on social platforms |
| **Smart Thumbnails** | Animated WebP where supported (Lemmy, browsers), static JPEG for clients that need it (Reddit/Embed.ly), animated GIF for clients that mishandle WebP (Matrix's `matrix-media-repo`) — same URL, negotiated per request |
| **Embed Policy** | Choose whether links embed the short *preview* clip or the *full* video — globally, or per‑share/per‑collection, including a *dynamic* size/length rule |
| **Admin Dashboard** | Full create / edit / delete management for both individual and collection shares |
| **Secure Sharing** | Optional per‑share passwords and expiring links |
| **Resolution Control** | Choose streaming quality: LOW, MEDIUM, or HIGH |
| **Access Safety Net** | Optional `limit_to_tag` so public tag shares only surface explicitly‑approved videos |
| **View Tracking** | Anonymous view counting; play counts are aggregated per video across all share contexts |
| **Static Pages** | Markdown files in `data/pages/` rendered as HTML at top‑level `/{slug}` URLs |
| **Markdown Descriptions** | Video descriptions from Stash render as formatted Markdown on the player page |
| **Contact / Takedown Form** | Built‑in DMCA‑style request form that emails you |

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

stash:
  server_ip: "127.0.0.1"
  port: 9999
  api_key: "your_stash_api_key"
  # limit_to_tag: 10844   # Optional: browsable pages (home + /gallery/tag) only surface videos that ALSO carry this tag; a share's own pages stay limited only while featured on home with no password

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
- **Static pages**: add Markdown files to `data/pages/` (created automatically on first run). A file `terms.md` is served at `/terms`. Use lowercase filenames; the first `# Heading` becomes the page title. Legacy `/pages/terms` redirects to `/terms`.

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
3. Configure resolution, password, embed mode, default sort, "Feature on Home?", and "Gallery mode?" (masonry layout for this collection's page)
4. **Reorder** collections any time by dragging their rows in the Shared Collections list

### Managing shares
- **Full editing** of existing video *and* collection shares (name, expiry, resolution, password, gallery flag, embed mode, default sort and masonry "Gallery mode?" for collections) — no need to delete and recreate
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
  - Studio/source, tags (the `limit_to_tag` tag is hidden), **Markdown‑formatted description**, external URLs, duration, resolution
- Password protection via a styled modal

### Gallery
- Hover a card to play an animated preview
- Collection cards cycle through animated previews of their contents
- Play count and duration overlays on each video card
- Lazy‑loaded thumbnails, sort controls, responsive grid

## Social Embeds

Sharestream emits Open Graph / Twitter‑player metadata tuned per platform:
- **Lemmy / browsers**: animated WebP thumbnail
- **Reddit / Embed.ly**: static JPEG thumbnail (they don't render WebP) — served from the *same* URL via content negotiation
- **Matrix (`matrix-media-repo`)**: animated GIF, transcoded from the WebP (downscaled to 400px wide, frame rate halved, kept under 3 MB), since the Matrix media repo stores the WebP as a still — same URL, negotiated per request. Applies to both per‑video thumbnails and collection (montage) thumbnails
- **Mastodon**: a dedicated bare `/embed/{id}` player page (no site chrome) is advertised as the `twitter:player`, so the card embeds just the video
- **og:video**: the short preview clip or the full video, per your `embed` policy

All media (previews, full video, thumbnails, HLS segments) is **proxied from the source on demand and never stored on disk** — only tiny `.m3u8` playlists and `.jpg` thumbnails are cached, in a private (non‑public) directory.

## Security & Privacy

- **JWT Authentication**: secure admin access
- **Capability URLs**: shares are reached via unguessable tokens (or your chosen custom slug), never by raw source IDs
- **`limit_to_tag`**: when set, anything reachable by *browsing* the site (the home gallery and `/gallery/tag/{name}`) only ever surfaces videos that also carry your approved tag — across the gallery, streams, previews, and thumbnails. A tag share's own pages stay limited only while it's featured on home with no password; password‑protected or non‑featured (capability‑URL) shares deliberately reach the full tag (see [`limit_to_tag`, featured shares, and password protection](#limit_to_tag-featured-shares-and-password-protection))
- **Password Protection**: optional per‑share passwords, enforced on both pages and media via a signed unlock cookie
- **Auto‑Expiration**: links expire on schedule
- **Credential Protection**: your source server's API key stays server‑side
- **Anonymous Tracking**: no personal data collected

### `limit_to_tag`, featured shares, and password protection

When `limit_to_tag` is set, Sharestream treats that tag as a **public‑content boundary** for everything reachable by *browsing the site*, and applies it in two layers:

- **Browsable/aggregation pages are always limited.** The home gallery and the `/gallery/tag/{name}` view only ever surface videos carrying *both* the relevant tag *and* `limit_to_tag`, no matter which shares they draw from. Non‑curated videos never appear while navigating the site.
- **A tag share's *own* pages and media are limited only when the share is *featured on the home gallery* (`show_in_gallery`) and has *no password*.** That single, broadly‑advertised, public surface stays curated. Every other tag share is treated as a deliberate, capability‑URL share and reaches the tag's **full** contents:
  - **Password‑protected tag shares bypass the filter** — a vetted, password‑gated recipient sees the complete tag.
  - **Non‑featured public tag shares bypass the filter** — they aren't advertised on the home gallery and are reachable only by their unguessable share link, so that link grants the full tag.

The mental model: `limit_to_tag` is your curation boundary for anything a stranger could stumble onto by browsing; a password *or* an unlisted (non‑featured) share link is a deliberate way to hand someone the full tag.

> **Notes**
> - Individual (single‑video) shares are unaffected by `limit_to_tag` either way — they point at a specific video you selected in the admin panel and are gated only by their capability URL and optional password.
> - Toggling a tag share to *featured on home* (with no password) re‑applies the filter to its own pages immediately, which can hide videos that were previously visible through its link; un‑featuring it (or adding a password) re‑exposes the full tag.

## Customization

### Branding options
1. **Logo**: SVG (preferred) or high‑DPI PNGs; the banner spans the page content width
2. **Favicon**: drop‑in override or generated default
3. **Fonts**: drop‑in `.woff2` per slot, or use the bundled web‑font defaults
4. **Visitor notice**: optional frosted‑glass acknowledgement / age gate
5. **Disclaimer, motto, social links**: all configurable
6. **Gallery**: curate your public homepage per share; set the site‑wide default sort in config and override per collection in the admin panel; choose a cropped grid or a native‑aspect‑ratio masonry layout (per collection, with a site‑wide default, plus an opt‑in for the homepage grid)
7. **Static pages**: Markdown files in `data/pages/` for terms, community info, or any other standalone content — served at `/{slug}` with the same site chrome as the rest of the site


## Troubleshooting

**Login issues?**
Clear your browser cache and localStorage, then try again.

**Videos not playing?**
Verify your Stash API key and that the Stash server is reachable.

**How do I share collections?**
Tag your videos in Stash, then use the collection sharing feature in the admin panel.

**Retagged an item in Stash but the collection hasn't updated?**
Tag→video membership is cached (default 15 minutes; set `cache.tag_membership_ttl_minutes`). Click **Clear Cache** in the admin panel to apply changes immediately.

**Missing thumbnails / stale previews on social?**
Sharestream caches small assets on first access; social platforms also cache preview cards aggressively — post a fresh URL (or use the platform's card debugger) and purge your CDN if you use one.

**A change to CSS/logo/fonts/thumbnails isn't showing?**
Those are served live (no restart needed) — hard‑refresh and purge your CDN. Only changes to the application code require restarting the service.

## Roadmap

- **Jellyfin backend** (planned, near term)
- A formal backend abstraction so additional media sources can be added cleanly
- Automated tests and CI
- Optional shared state (e.g. Redis) for multi‑worker deployments

## Recent Updates

- **Masonry gallery layout**: collections (and optionally the homepage "All Videos" grid) can arrange cards in balanced columns at each video's native aspect ratio instead of cropping to one shape. Per‑collection "Gallery mode?" toggle in the admin panel; `gallery.home_masonry` and `gallery.masonry_default` in config control the homepage grid and the new‑share toggle default
- **Animated GIF thumbnails for Matrix**: the negotiated thumbnail endpoints (per‑video *and* collection) now serve an animated GIF (transcoded from the WebP, 400px wide, half frame rate, under 3 MB) to `matrix-media-repo`, which otherwise renders the WebP as a still
- **Static Markdown pages** at top‑level `/{slug}` from `data/pages/{slug}.md`, with legacy `/pages/{slug}` redirects
- **Markdown video descriptions** on the player page (headings, lists, links, code blocks, etc.)
- **Aggregate play counts** per video across all share contexts, used consistently on cards, galleries, and the player page
- **Duration badges** on gallery cards
- **Configurable default sort** (`sharestream.default_sort` in config; per‑collection override in the admin panel)
- **Fully async upstream I/O**: all source calls now use `httpx.AsyncClient` (a shared, pooled, non‑blocking client) instead of synchronous `requests`, so concurrent viewers' requests no longer serialize on the event loop
- **Tag‑membership caching**: media requests cache the tag→video‑ID set (TTL via `cache.tag_membership_ttl_minutes`, default 15 min) instead of re‑querying the source on every hit, with a **Clear Cache** button in the admin panel
- **Private cached artifacts**: cached thumbnails and playlists moved out of the public static directory and served through gated routes
- **Signed unlock cookies**: passwords are no longer kept in the URL, and media routes are gated by the same unlock as pages
- **Short URLs** (`/{slug}`, `/{tag}`, `/{tag}/{id}`) with custom slugs for both video and collection shares
- **Adaptive, portrait‑aware video player** with looping and themed skip controls
- **Full editing** of existing shares, plus **drag‑to‑reorder** collections
- **Smart social embeds**: animated WebP / JPEG content negotiation, Mastodon `/embed` player, configurable preview‑vs‑full policy
- **Date‑based default sorting** (release date, falling back to date added)
- **Drop‑in branding**: SVG logo, generated/overridable favicon, per‑slot custom fonts
- **`limit_to_tag`** safety enforced on every browsable surface (home + `/gallery/tag`); a tag share's own pages stay limited only while featured on home with no password (password‑protected or non‑featured shares reach the full tag)

## Contributing

Contributions are welcome. Please open an issue to discuss substantial changes before submitting a pull request, and feel free to file bug reports and feature requests.

## License

Sharestream is licensed under the **GNU Affero General Public License v3.0 or later (AGPL‑3.0‑or‑later)**. See the [`LICENSE`](LICENSE) file for the full text.

Because Sharestream is typically run as a network service, the AGPL is a deliberate choice: if you modify Sharestream and make it available to users over a network, you must also make the corresponding source of your modified version available to those users.

Bundled third‑party assets (e.g. Video.js, web fonts, and social icons) remain under their respective upstream licenses.

## Legal Notice

Sharestream is a tool for proxying and sharing media you control. You are solely responsible for ensuring that any content you share complies with all applicable laws and with the terms of any platforms where you post links. The optional disclaimer and visitor‑notice features are provided for convenience and do not constitute legal advice.
