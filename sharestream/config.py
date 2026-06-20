"""Application configuration.

All settings are loaded from ``config.yaml`` (resolved relative to the current
working directory, which is the project root — see the deployment notes in the
README). Config-derived values are exposed as module-level constants so the rest
of the codebase can ``from sharestream.config import BASE_DOMAIN`` etc.

Keep the keys here compatible with ``example-config.yaml``; the public config
surface must not change during refactors.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import yaml

from sharestream.services.footer import parse_footer_config

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Load configuration
# ------------------------------------------------------------------
try:
    with open("config.yaml", "r") as config_file:
        config = yaml.safe_load(config_file)
except Exception as e:  # pragma: no cover - fatal at startup
    logger.error(f"Failed to load config.yaml: {e}")
    raise

# Make sure LIMIT_TO_TAG is defined globally after config is loaded
LIMIT_TO_TAG = config['stash'].get('limit_to_tag', None)

SHARESTREAM_HOST = config['sharestream']['host']
SHARESTREAM_PORT = config['sharestream']['port']
BASE_DOMAIN = config['sharestream']['base_domain']
STASH_SERVER = f"http://{config['stash']['server_ip']}:{config['stash']['port']}"
STASH_API_KEY = config['stash']['api_key']
DISCLAIMER = config.get('disclaimer', '')
ADMIN_USERNAME = config['sharestream']['admin_username']
ADMIN_PASSWORD = config['sharestream']['admin_password']
DEFAULT_RESOLUTION = config['sharestream'].get('default_resolution', 'MEDIUM')
SHARE_ID_LENGTH = config['sharestream'].get('share_id_length', 8)
# Default gallery sort mode for the home page and tag-share pages (the values the
# sort dropdown offers). Individual tag shares can override it in the admin panel.
# Falls back to 'date' when unset/invalid.
VALID_SORTS = {'date', 'title', 'hits', 'rating', 'duration', 'random'}
DEFAULT_SORT = str(config['sharestream'].get('default_sort', 'date')).strip().lower()
if DEFAULT_SORT not in VALID_SORTS:
    DEFAULT_SORT = 'date'
SITE_NAME = config.get('site_name', 'Sharestream')  # Add site_name with fallback
SITE_MOTTO = config.get('site_motto', '')  # Add site_motto with empty default
# Operator-provided social-embed (Open Graph) thumbnail for the home page and
# static pages. A path to a raster image (jpg/png) served as-is by /og/site-thumbnail.
# Falls back to the favicon PNG, then the bundled default, when unset/missing.
SITE_THUMBNAIL = config.get('site_thumbnail') or ''
# Human-readable site description for og:description on the home page (and the
# fallback description for static pages with no body text).
SITE_DESCRIPTION = config.get('site_description', '') or ''
SOCIAL_LINKS = config.get('social_links', [])  # Add social_links with empty list default
FOOTER = parse_footer_config(config)
# Optional content warning / age-gate shown to first-time visitors on the home
# page. Empty/unset disables it.
CONTENT_WARNING = config.get('content_warning', '') or ''

# Social-embed (og:video) policy. `mode` is one of preview | full | dynamic.
# In dynamic mode the FULL video is embedded only when it is small on BOTH
# axes (duration <= max_full_duration AND size <= max_full_size_mb); otherwise
# the short Stash preview clip is used. Either threshold may be null to ignore.
EMBED_CONFIG = config.get('embed', {}) or {}
VALID_EMBED_MODES = {'preview', 'full', 'dynamic'}
EMBED_MODE = str(EMBED_CONFIG.get('mode', 'preview')).lower()
if EMBED_MODE not in VALID_EMBED_MODES:
    EMBED_MODE = 'preview'
EMBED_MAX_FULL_DURATION = EMBED_CONFIG.get('max_full_duration')  # seconds or None
EMBED_MAX_FULL_SIZE_MB = EMBED_CONFIG.get('max_full_size_mb')    # megabytes or None

# Gallery layout policy.
# - home_masonry: when true, the home page "All Videos" gallery (the individual
#   video cards below the featured collections) uses the masonry layout that
#   preserves each video's native aspect ratio instead of the cropped grid.
# - masonry_default: the default state of the admin "Gallery mode?" toggle when
#   creating a tag share. false = opt-in (toggle starts off); true = opt-out
#   (toggle starts on, so new shares are masonry unless unchecked). Existing
#   shares keep whatever was stored; this only sets the new-share default.
GALLERY_CONFIG = config.get('gallery', {}) or {}
GALLERY_HOME_MASONRY = bool(GALLERY_CONFIG.get('home_masonry', False))
GALLERY_MASONRY_DEFAULT = bool(GALLERY_CONFIG.get('masonry_default', False))

# Caching policy. Tag membership checks (does video X belong to shared tag Y?)
# are cached per tag for this many minutes to avoid re-querying Stash on every
# media request. Defaults to 15 minutes when unset/invalid.
CACHE_CONFIG = config.get('cache', {}) or {}
try:
    TAG_MEMBERSHIP_TTL_SECONDS = float(CACHE_CONFIG.get('tag_membership_ttl_minutes', 15)) * 60
    if TAG_MEMBERSHIP_TTL_SECONDS <= 0:
        raise ValueError
except (TypeError, ValueError):
    TAG_MEMBERSHIP_TTL_SECONDS = 15 * 60

# Generated collection (tag-share) social-embed thumbnails — the merged animated
# WebP / collage JPEG — are cached on disk and rebuilt when older than this.
# Defaults to 6 hours when unset/invalid.
try:
    COLLECTION_THUMBNAIL_TTL_SECONDS = float(CACHE_CONFIG.get('collection_thumbnail_ttl_minutes', 360)) * 60
    if COLLECTION_THUMBNAIL_TTL_SECONDS <= 0:
        raise ValueError
except (TypeError, ValueError):
    COLLECTION_THUMBNAIL_TTL_SECONDS = 360 * 60

# ------------------------------------------------------------------
# Filedrop: optional public upload page that ingests files into Stash.
# ------------------------------------------------------------------
# Stash has no byte-upload API, so uploads are SFTP'd into a folder that Stash
# scans, then scanned + (optionally) tagged. The whole feature is OFF unless
# `filedrop.enabled` is true, so deploying the code never opens an upload hole.
FILEDROP_CONFIG = config.get('filedrop', {}) or {}
FILEDROP_ENABLED = bool(FILEDROP_CONFIG.get('enabled', False))
# Optional shared password (plaintext here by design); empty/unset = open page.
FILEDROP_PASSWORD = str(FILEDROP_CONFIG.get('password', '') or '')
# Optional Stash tag id applied to every ingested scene; empty/None = no tag.
# Retained only as a back-compat input for FILEDROP_NEW_UPLOAD_TAGS below.
_filedrop_tag = FILEDROP_CONFIG.get('tag_id')
FILEDROP_TAG_ID = str(_filedrop_tag) if _filedrop_tag not in (None, '') else None
# List of Stash tag ids applied to every ingested scene. Supersedes the single
# `tag_id`; when `new_upload_tags` is absent we fall back to [tag_id] so existing
# configs keep working.
_new_upload_tags = FILEDROP_CONFIG.get('new_upload_tags')
if _new_upload_tags in (None, ''):
    FILEDROP_NEW_UPLOAD_TAGS = [FILEDROP_TAG_ID] if FILEDROP_TAG_ID else []
else:
    if not isinstance(_new_upload_tags, (list, tuple)):
        _new_upload_tags = [_new_upload_tags]
    FILEDROP_NEW_UPLOAD_TAGS = [str(t) for t in _new_upload_tags if t not in (None, '')]
# When an upload is NOT auto-viewable via a public tag, optionally mint a
# password-protected SharedVideo (random password) on completion.
FILEDROP_AUTO_SHARE = bool(FILEDROP_CONFIG.get('auto_share_uploads', False))
# When true, the upload UI shows a tag picker and the server accepts the
# uploader's own tag choices (restricted to the public-tag vocabulary).
FILEDROP_ALLOW_USER_TAGS = bool(FILEDROP_CONFIG.get('allow_user_tags', False))
# SSH/SFTP delivery to the box hosting Stash's library.
FILEDROP_SSH_HOST = str(FILEDROP_CONFIG.get('ssh_host', '') or '')
FILEDROP_SSH_USER = str(FILEDROP_CONFIG.get('ssh_user', '') or '')
FILEDROP_SSH_KEY = os.path.expanduser(str(FILEDROP_CONFIG.get('ssh_key', '~/.ssh/id_ed25519')))
FILEDROP_SSH_PORT = int(FILEDROP_CONFIG.get('ssh_port', 22) or 22)
# Where bytes are written on the host (host_dir) vs. how Stash sees that same
# folder (stash_scan_path) — they differ when Stash runs in a container with a
# bind mount (e.g. host /mnt/Media -> container /data).
FILEDROP_HOST_DIR = str(FILEDROP_CONFIG.get('host_dir', '') or '')
FILEDROP_STASH_SCAN_PATH = str(FILEDROP_CONFIG.get('stash_scan_path', '') or '')
try:
    FILEDROP_MAX_UPLOAD_MB = int(FILEDROP_CONFIG.get('max_upload_mb', 5000) or 5000)
    if FILEDROP_MAX_UPLOAD_MB <= 0:
        raise ValueError
except (TypeError, ValueError):
    FILEDROP_MAX_UPLOAD_MB = 5000
# Accepted upload extensions (lowercase, with dot).
FILEDROP_ALLOWED_EXTS = {
    '.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.wmv', '.flv', '.ts', '.mpg', '.mpeg', '.m2ts',
}

# SMTP settings for contact form
CONTACT_FORM_CONFIG = config.get('contact_form', {})
SMTP_MAILTO = CONTACT_FORM_CONFIG.get('mailto', '')
SMTP_HOST = CONTACT_FORM_CONFIG.get('host', '')
SMTP_PORT = CONTACT_FORM_CONFIG.get('port', 465)
SMTP_USER = CONTACT_FORM_CONFIG.get('user', '')
SMTP_PASS = CONTACT_FORM_CONFIG.get('pass', '')

# Directory for storing .m3u8 files
# Private cache for generated share artifacts (HLS playlists, screenshot JPEGs).
# Deliberately OUTSIDE the publicly-mounted ./static tree: everything here is
# served only through routes that enforce expiry / password / tag-membership
# checks. Putting it under /static would let anyone fetch a password-protected
# share's cached screenshot directly by URL, bypassing the gate.
SHARES_DIR = Path("data/shares")
SHARES_DIR.mkdir(parents=True, exist_ok=True)

# Static Markdown pages served at /{slug} (e.g. data/pages/terms.md -> /terms).
PAGES_DIR = Path("data/pages")
PAGES_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------
# JWT / cookie signing key.
# ------------------------------------------------------------------
# This signs both admin JWTs and the per-share password-unlock cookies, so it
# MUST be stable across restarts (otherwise every restart logs admins out and
# forces every viewer to re-enter share passwords) and identical across workers
# (a randomly-per-process key breaks auth the moment you run more than one
# uvicorn worker). Resolution order: explicit config value -> persisted file ->
# generate once and persist.
SECRET_KEY = (config['sharestream'].get('secret_key') or '').strip()
if not SECRET_KEY:
    _key_file = Path(".secret_key")
    try:
        if _key_file.exists():
            SECRET_KEY = _key_file.read_text().strip()
        if not SECRET_KEY:
            SECRET_KEY = secrets.token_urlsafe(32)
            _key_file.write_text(SECRET_KEY)
            try:
                os.chmod(_key_file, 0o600)
            except OSError:
                pass
            logger.info("Generated and persisted a new signing key to .secret_key")
    except OSError as e:
        # Couldn't read/write the file (e.g. read-only FS): fall back to an
        # ephemeral key and warn that sessions won't survive a restart.
        SECRET_KEY = SECRET_KEY or secrets.token_urlsafe(32)
        logger.warning(f"Could not persist signing key ({e}); admin sessions and "
                       f"share-unlock cookies will not survive a restart. Set "
                       f"sharestream.secret_key in config to fix.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# SQLite database setup
DATABASE_URL = "sqlite:///shared_videos.db"
