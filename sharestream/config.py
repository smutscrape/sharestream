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
SITE_NAME = config.get('site_name', 'Sharestream')  # Add site_name with fallback
SITE_MOTTO = config.get('site_motto', '')  # Add site_motto with empty default
SOCIAL_LINKS = config.get('social_links', [])  # Add social_links with empty list default
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
