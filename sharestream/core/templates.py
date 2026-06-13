"""Jinja2 environment and template rendering helpers.

A single global ``Environment`` (HTML auto-escaping on) loads templates from the
``static`` directory, mirroring the original monolith. Use :func:`render` to
render a template by filename.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from jinja2 import Environment, FileSystemLoader, select_autoescape

from sharestream.services.markdown import render_markdown

JINJA_ENV = Environment(
    loader=FileSystemLoader("static"),
    autoescape=select_autoescape(["html", "xml"]),
)
JINJA_ENV.filters['urlencode'] = quote_plus
JINJA_ENV.filters['markdown'] = render_markdown

# Backwards-compatible accessor: TEMPLATES("file.html") -> Template
TEMPLATES = JINJA_ENV.get_template


def render(template_name: str, **context) -> str:
    """Render ``template_name`` from the static template dir with ``context``."""
    return JINJA_ENV.get_template(template_name).render(**context)
