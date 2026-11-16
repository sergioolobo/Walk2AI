"""Build the live waitlist site from page.html.

    python landing/build.py            build landing/site/ (refuses while the privacy
                                       details are missing)
    python landing/build.py --preview  build anyway, with placeholders and no sending

page.html is the single source: it is also published as-is for previews. The build
wraps it in a full HTML document, serves the fonts from the site itself (so visitors'
browsers don't contact Google), fills in the privacy details, and tells the page where
to send sign-ups. Standard library only.
"""

import html
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "page.html"
CONFIG = HERE / "config.json"
# Your own copy of config.json, never committed (the repository is public).
LOCAL_CONFIG = HERE / "config.local.json"
FONTS = HERE / "fonts"
OUT = HERE / "site" / "index.html"

FONT_FACES = """<style>
@font-face { font-family: "Bricolage Grotesque"; font-style: normal; font-weight: 500 800;
  font-display: swap; src: url("fonts/bricolage-grotesque-latin.woff2") format("woff2"); }
@font-face { font-family: "Figtree"; font-style: normal; font-weight: 400 700;
  font-display: swap; src: url("fonts/figtree-latin.woff2") format("woff2"); }
</style>"""

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'%3E"
    "%3Crect x='42' y='58' width='116' height='120' rx='58' fill='%237DD3AE'/%3E"
    "%3Ccircle cx='100' cy='26' r='16' fill='%23FFC93C'/%3E"
    "%3Ccircle cx='80' cy='102' r='11' fill='%2313241B'/%3E"
    "%3Ccircle cx='120' cy='102' r='11' fill='%2313241B'/%3E%3C/svg%3E"
)

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def fail(message: str) -> None:
    print(f"build: {message}", file=sys.stderr)
    sys.exit(1)


def sending_config(cfg: dict[str, object], preview: bool) -> dict[str, object]:
    if preview:
        return {"mode": "preview"}
    mode = cfg.get("mode")
    if mode == "netlify":
        return {"mode": "netlify"}
    if mode == "post":
        url = str(cfg.get("post_url") or "")
        if not url.startswith("https://"):
            fail('mode "post" needs "post_url" (https://...) in config.json')
        return {"mode": "post", "url": url}
    if mode == "google-form":
        url = str(cfg.get("google_form_url") or "")
        fields = cfg.get("google_form_fields") or {}
        if not url.startswith("https://docs.google.com/forms/") or not url.endswith("/formResponse"):
            fail('mode "google-form" needs "google_form_url" ending in /formResponse')
        if not isinstance(fields, dict) or not str(fields.get("email", "")).startswith("entry."):
            fail('mode "google-form" needs google_form_fields.email like "entry.123456789"')
        return {
            "mode": "google-form",
            "url": url,
            "fields": {k: v for k, v in fields.items() if str(v).startswith("entry.")},
        }
    fail('"mode" must be "netlify", "post" or "google-form"')
    raise AssertionError  # unreachable


def fill_placeholders(page: str, cfg: dict[str, object], preview: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = str(cfg.get(key) or "").strip()
        if not value:
            if preview:
                return match.group(0)
            fail(f'"{key}" is empty in config.json: the privacy notice needs it before going live')
        if key == "contact_email":
            if not EMAIL.match(value):
                fail('"contact_email" does not look like an email address')
            safe = html.escape(value)
            return f'<a href="mailto:{safe}">{safe}</a>'
        return html.escape(value)

    return re.sub(r'<span class="todo" data-fill="(\w+)">[^<]*</span>', replace, page)


def build(preview: bool) -> None:
    source = LOCAL_CONFIG if LOCAL_CONFIG.exists() else CONFIG
    cfg = json.loads(source.read_text(encoding="utf-8"))
    page = SOURCE.read_text(encoding="utf-8")

    page, swapped = re.subn(r"<link [^>]*data-webfont[^>]*>", FONT_FACES, page)
    if swapped != 1:
        fail("page.html must contain exactly one <link ... data-webfont> for the fonts")
    page = fill_placeholders(page, cfg, preview)

    settings = json.dumps(sending_config(cfg, preview))
    page = page.replace(
        "<script>\n(function () {",
        f"<script>window.WAITLIST_CONFIG = {settings};</script>\n<script>\n(function () {{",
        1,
    )
    if "window.WAITLIST_CONFIG = " not in page:
        fail("could not find the page script to configure")

    document = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f'<link rel="icon" href="{FAVICON}">\n'
        '<meta property="og:title" content="Wobo">\n'
        '<meta property="og:description" content="An AI companion that runs on your steps.">\n'
        f"{page}\n</html>\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(document, encoding="utf-8")
    shutil.copytree(FONTS, OUT.parent / "fonts", dirs_exist_ok=True)
    label = "preview (sends nothing)" if preview else f"live, sending with {cfg.get('mode')}"
    print(f"build: wrote {OUT.relative_to(HERE.parent)} from {source.name} — {label}")


if __name__ == "__main__":
    build(preview="--preview" in sys.argv[1:])
