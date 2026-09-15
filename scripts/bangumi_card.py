#!/usr/bin/env python3
"""Generate a self-contained Bangumi statistics card for a GitHub profile."""

from __future__ import annotations

import argparse
import base64
import html
import os
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_BASE = "https://api.bgm.tv"
DEFAULT_USER = "925751"
ANIME_TYPE = 2
PAGE_SIZE = 50
RECENT_TYPES = {2, 3, 4}  # Completed, Watching, On Hold
RECENT_LIMIT = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024
USER_AGENT = (
    "SiIverAsh-Bangumi-Profile-Card/1.0 "
    "(+https://github.com/SiIverAsh/SiIverAsh)"
)


class BangumiError(RuntimeError):
    """Raised when required Bangumi data cannot be fetched or parsed."""


def create_session() -> requests.Session:
    """Return a requests session with retries suitable for the public API."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
    )

    token = os.getenv("BANGUMI_ACCESS_TOKEN", "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def get_json(
    session: requests.Session,
    path: str,
    *,
    timeout: float,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch and validate a JSON object from the Bangumi API."""
    try:
        response = session.get(
            f"{API_BASE}{path}", params=params, timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise BangumiError(f"Bangumi API request failed for {path}: {error}") from error
    except ValueError as error:
        raise BangumiError(f"Bangumi API returned invalid JSON for {path}") from error

    if not isinstance(payload, dict):
        raise BangumiError(f"Bangumi API returned an unexpected payload for {path}")
    return payload


def get_user(
    session: requests.Session, user: str, *, timeout: float = 15
) -> dict[str, Any]:
    """Fetch a public Bangumi user profile."""
    return get_json(session, f"/v0/users/{user}", timeout=timeout)


def get_collections(
    session: requests.Session, user: str, *, timeout: float = 15
) -> list[dict[str, Any]]:
    """Fetch every public anime collection entry for a Bangumi user."""
    collections: list[dict[str, Any]] = []
    offset = 0

    while True:
        payload = get_json(
            session,
            f"/v0/users/{user}/collections",
            timeout=timeout,
            params={
                "subject_type": ANIME_TYPE,
                "limit": PAGE_SIZE,
                "offset": offset,
            },
        )
        page = payload.get("data", [])
        if not isinstance(page, list):
            raise BangumiError("Bangumi collections response has no valid data list")

        anime_page = [
            item
            for item in page
            if isinstance(item, dict) and item.get("subject_type") == ANIME_TYPE
        ]
        collections.extend(anime_page)

        try:
            total = int(payload.get("total", len(collections)))
        except (TypeError, ValueError):
            total = len(collections)

        received = len(page)
        offset += received
        if received == 0 or received < PAGE_SIZE or offset >= total:
            break

    return collections


def calculate_stats(collections: list[dict[str, Any]]) -> dict[str, int | float]:
    """Calculate collection counts and the user's own rating average."""
    type_counts = Counter(
        item.get("type") for item in collections if isinstance(item.get("type"), int)
    )
    ratings = [
        float(item["rate"])
        for item in collections
        if isinstance(item.get("rate"), (int, float))
        and not isinstance(item.get("rate"), bool)
        and item["rate"] > 0
    ]

    return {
        "watching": type_counts[3],
        "completed": type_counts[2],
        "plan": type_counts[1],
        "on_hold": type_counts[4],
        "dropped": type_counts[5],
        "rated": len(ratings),
        "average_score": sum(ratings) / len(ratings) if ratings else 0.0,
    }


def parse_updated_at(value: Any) -> datetime:
    """Parse an API timestamp; malformed/missing values sort last."""
    if not isinstance(value, str) or not value.strip():
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def get_recent_watching(
    collections: list[dict[str, Any]], limit: int = RECENT_LIMIT
) -> list[dict[str, Any]]:
    """Return recently updated Watching, Completed, or On Hold anime."""
    candidates = [item for item in collections if item.get("type") in RECENT_TYPES]
    return sorted(
        candidates,
        key=lambda item: parse_updated_at(item.get("updated_at")),
        reverse=True,
    )[:limit]


def download_image(
    session: requests.Session, url: str, *, timeout: float = 15
) -> tuple[bytes, str] | None:
    """Download one image without allowing a cover failure to abort the card."""
    if not url or not url.startswith(("https://", "http://")):
        return None

    try:
        response = session.get(url, timeout=timeout, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise ValueError(f"unexpected content type {content_type or 'unknown'}")

        declared_size = int(response.headers.get("Content-Length", "0") or 0)
        if declared_size > MAX_IMAGE_BYTES:
            raise ValueError("image is larger than 8 MiB")

        chunks: list[bytes] = []
        downloaded = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            downloaded += len(chunk)
            if downloaded > MAX_IMAGE_BYTES:
                raise ValueError("image is larger than 8 MiB")
            chunks.append(chunk)
        if not chunks:
            raise ValueError("empty image response")
        return b"".join(chunks), content_type
    except (requests.RequestException, ValueError) as error:
        print(f"Warning: unable to download cover {url}: {error}", file=sys.stderr)
        return None


def image_to_base64(image: bytes, content_type: str) -> str:
    """Convert image bytes into an SVG-safe data URI."""
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def cover_url(item: dict[str, Any]) -> str:
    subject = item.get("subject")
    if not isinstance(subject, dict):
        return ""
    images = subject.get("images")
    if not isinstance(images, dict):
        return ""
    # The 200 px "small" cover is ample for the 112 px card slot and keeps the
    # generated SVG compact enough for a file that is committed every day.
    for size in ("small", "grid", "medium", "common", "large"):
        value = images.get(size)
        if isinstance(value, str) and value:
            return value
    return ""


def item_title(item: dict[str, Any]) -> str:
    subject = item.get("subject")
    if not isinstance(subject, dict):
        return "Untitled"
    for key in ("name_cn", "name"):
        value = subject.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Untitled"


def display_width(text: str) -> int:
    """Approximate rendered width in units where a CJK glyph occupies two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WFA" else 1 for char in text)


def truncate_title(title: str, max_width: int = 18) -> str:
    """Truncate a title without splitting Unicode characters."""
    if display_width(title) <= max_width:
        return title
    result: list[str] = []
    used = 0
    for char in title:
        char_width = 2 if unicodedata.east_asian_width(char) in "WFA" else 1
        if used + char_width > max_width - 1:
            break
        result.append(char)
        used += char_width
    return "".join(result).rstrip() + "…"


def render_svg(
    stats: dict[str, int | float],
    recent: list[dict[str, Any]],
    covers: list[str | None],
    *,
    updated_date: str,
) -> str:
    """Render the complete, self-contained 800x500 SVG document."""
    stat_items = (
        ("Watching", str(stats["watching"])),
        ("Completed", str(stats["completed"])),
        ("Plan to Watch", str(stats["plan"])),
        ("On Hold", str(stats["on_hold"])),
        ("Average Score", f'{float(stats["average_score"]):.2f}'),
        ("Rated", str(stats["rated"])),
    )
    stat_icons = (
        '<polygon points="3,2 14,8 3,14"/>',
        '<path d="M2 8.5l4 4L14.5 3"/>',
        '<path d="M3 1.5h10v13l-5-3-5 3z"/>',
        '<path d="M4 2v12M12 2v12"/>',
        '<path d="M8 1.5l2 4 4.5.7-3.2 3.1.7 4.5L8 11.7l-4 2.1.7-4.5-3.2-3.1L6 5.5z"/>',
        '<path d="M2 3h8l4 4-7 7-5-5z"/><circle cx="6" cy="6.5" r="1"/>',
    )

    stat_parts: list[str] = []
    for index, (label, value) in enumerate(stat_items):
        column = index % 3
        row = index // 3
        x = 48 + column * 250
        y = 91 + row * 41
        stat_parts.append(
            f'<g transform="translate({x} {y})">'
            f'<g class="stat-icon">{stat_icons[index]}</g>'
            f'<text class="stat-label" x="26" y="14">{html.escape(label)}</text>'
            f'<text class="stat-value" x="205" y="15" text-anchor="end">{html.escape(value)}</text>'
            '</g>'
        )

    recent_parts: list[str] = []
    clip_parts: list[str] = []
    column_width = 120
    cover_width = 84
    cover_height = 112
    for index in range(RECENT_LIMIT):
        center_x = 100 + index * column_width
        x = center_x - cover_width // 2
        y = 203
        clip_id = f"cover-clip-{index}"
        clip_parts.append(
            f'<clipPath id="{clip_id}"><rect x="{x}" y="{y}" '
            f'width="{cover_width}" height="{cover_height}" rx="5"/></clipPath>'
        )

        if index < len(recent):
            item = recent[index]
            data_uri = covers[index] if index < len(covers) else None
            title = html.escape(truncate_title(item_title(item), max_width=14))
            if data_uri:
                cover_markup = (
                    f'<image href="{data_uri}" x="{x}" y="{y}" '
                    f'width="{cover_width}" height="{cover_height}" '
                    f'preserveAspectRatio="xMidYMid slice" clip-path="url(#{clip_id})"/>'
                )
            else:
                cover_markup = (
                    f'<rect class="placeholder" x="{x}" y="{y}" '
                    f'width="{cover_width}" height="{cover_height}" rx="5"/>'
                    f'<text class="placeholder-icon" x="{center_x}" y="270" '
                    'text-anchor="middle">✿</text>'
                )
            recent_parts.append(
                cover_markup
                + f'<rect class="cover-border" x="{x}" y="{y}" '
                f'width="{cover_width}" height="{cover_height}" rx="5"/>'
                + f'<text class="anime-title" x="{center_x}" y="339" '
                f'text-anchor="middle">{title}</text>'
            )
        else:
            recent_parts.append(
                f'<rect class="placeholder empty" x="{x}" y="{y}" '
                f'width="{cover_width}" height="{cover_height}" rx="5"/>'
                f'<text class="placeholder-icon" x="{center_x}" y="270" '
                'text-anchor="middle">✿</text>'
                f'<text class="anime-title muted" x="{center_x}" y="339" '
                'text-anchor="middle">No entry</text>'
            )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400" viewBox="0 0 800 400" role="img" aria-labelledby="title desc">
  <title id="title">Bangumi Stats</title>
  <desc id="desc">Bangumi anime collection statistics and six recently updated titles.</desc>
  <defs>
    {''.join(clip_parts)}
  </defs>
  <style>
    .frame-line {{ stroke: #30363d; }}
    .title, .stat-value, .anime-title {{ fill: #f0f6fc; }}
    .section-title {{ fill: #f09199; }}
    .stat-label, .updated, .muted {{ fill: #8b949e; }}
    .stat-icon, .section-icon {{ fill: none; stroke: #8b949e; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }}
    .placeholder {{ fill: #21262d; }}
    .placeholder.empty {{ opacity: .62; }}
    .placeholder-icon {{ fill: #f09199; font: 32px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .cover-border {{ fill: none; stroke: #30363d; }}
    .title {{ font: 700 25px -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }}
    .section-title {{ font: 500 17px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .stat-label {{ font: 500 16px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .stat-value {{ font: 700 22px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .anime-title {{ font: 500 13px -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }}
    .updated {{ font: 11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    @media (prefers-color-scheme: light) {{
      .frame-line {{ stroke: #d0d7de; }}
      .title, .stat-value, .anime-title {{ fill: #1f2328; }}
      .stat-label, .updated, .muted {{ fill: #656d76; }}
      .stat-icon, .section-icon {{ stroke: #656d76; }}
      .placeholder {{ fill: #eaeef2; }}
      .cover-border {{ stroke: #d0d7de; }}
    }}
  </style>
  <line class="frame-line" x1="1.5" y1="0" x2="1.5" y2="400"/>
  <line class="frame-line" x1="2" y1="67.5" x2="800" y2="67.5"/>
  <text class="title" x="40" y="47">🌸 Bangumi Stats</text>
  {''.join(stat_parts)}
  <g class="section-icon" transform="translate(43 170)">
    <path d="M8 14S2 10.5 2 6a3.5 3.5 0 016-2.2A3.5 3.5 0 0114 6c0 4.5-6 8-6 8z"/>
  </g>
  <text class="section-title" x="68" y="184">Recent Watching</text>
  {''.join(recent_parts)}
  <text class="updated" x="758" y="382" text-anchor="end">Updated {html.escape(updated_date)}</text>
</svg>
'''


def generate_card(user: str, output: Path, *, timeout: float = 15) -> None:
    """Fetch live data, embed recent covers, and write the SVG atomically."""
    session = create_session()
    profile = get_user(session, user, timeout=timeout)
    collections = get_collections(session, user, timeout=timeout)
    stats = calculate_stats(collections)
    recent = get_recent_watching(collections)

    covers: list[str | None] = []
    for item in recent:
        downloaded = download_image(session, cover_url(item), timeout=timeout)
        covers.append(image_to_base64(*downloaded) if downloaded else None)

    svg = render_svg(
        stats,
        recent,
        covers,
        updated_date=datetime.now(timezone.utc).date().isoformat(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(svg, encoding="utf-8", newline="\n")
    temporary.replace(output)

    nickname = profile.get("nickname") or profile.get("username") or user
    print(
        f"Generated {output} for {nickname}: "
        f"{len(collections)} anime, {len(recent)} recent titles"
    )


def parse_args() -> argparse.Namespace:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user",
        default=os.getenv("BANGUMI_USER", DEFAULT_USER),
        help=f"Bangumi username or UID (default: {DEFAULT_USER})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repository_root / "bangumi.svg",
        help="Output SVG path (default: repository root/bangumi.svg)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        help="Per-request timeout in seconds (default: 15)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        generate_card(args.user, args.output, timeout=args.timeout)
    except (BangumiError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
