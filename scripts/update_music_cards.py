"""Cache the NetEase music widgets used by the profile README.

The README points at the generated files instead of the live endpoint so a
temporary upstream or Vercel failure cannot make the profile cards disappear.
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://netease-music-widget.vercel.app/api"
USER_ID = os.environ.get("NETEASE_USER_ID", "623550269")
RETRIES = 5
TIMEOUT_SECONDS = 30
ROOT = Path(__file__).resolve().parent.parent


def validate_svg(data: bytes, content_type: str) -> None:
    preview = data[:2048].lower()
    if len(data) < 200 or b"<svg" not in preview:
        raise ValueError("response is not a valid SVG")
    if content_type and not any(
        expected in content_type.lower()
        for expected in ("image/svg+xml", "text/xml", "application/xml")
    ):
        raise ValueError(f"unexpected content type: {content_type}")


def customize_title(data: bytes, title: str) -> bytes:
    """Use the period as the heading and drop the duplicate right label."""
    text = data.decode("utf-8")
    text, metadata_count = re.subn(
        r'(<title id="card-title">).*?(</title>)',
        rf"\g<1>{title}\g<2>",
        text,
        count=1,
    )
    text, heading_count = re.subn(
        r'(<text x="24" y="14"[^>]*>\s*).*?(\s*</text>)',
        rf"\g<1>{title}\g<2>",
        text,
        count=1,
        flags=re.DOTALL,
    )
    text, label_count = re.subn(
        r'\s*<text x="420" y="14"[^>]*>.*?</text>',
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    if (metadata_count, heading_count, label_count) != (1, 1, 1):
        raise ValueError("upstream SVG title layout changed")
    return text.encode("utf-8")


def download_card(period: str) -> bytes:
    query = urlencode(
        {
            "id": USER_ID,
            "type": period,
            "count": 8,
            "theme": "dark",
            "show_rank": "true",
        }
    )
    request = Request(
        f"{API_URL}?{query}",
        headers={
            "Accept": "image/svg+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "github-profile-music-card-updater/1.0",
        },
    )

    for attempt in range(1, RETRIES + 1):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = response.read()
                validate_svg(data, response.headers.get("Content-Type", ""))
                title = "Last 7 Days" if period == "week" else "All Time"
                return customize_title(data, title)
        except (HTTPError, URLError, HTTPException, OSError, ValueError) as error:
            if attempt == RETRIES:
                raise RuntimeError(
                    f"failed to download {period!r} card after {RETRIES} attempts"
                ) from error
            delay = min(2 ** (attempt - 1), 16) + random.random()
            print(
                f"Attempt {attempt}/{RETRIES} for {period!r} failed: {error}; "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise AssertionError("retry loop ended unexpectedly")


def main() -> None:
    # Fetch everything before writing anything. A partial upstream failure keeps
    # the last known-good pair intact.
    cards = {
        "music.week.svg": download_card("week"),
        "music.all.svg": download_card("all"),
    }

    for filename, data in cards.items():
        destination = ROOT / filename
        temporary = destination.with_suffix(".svg.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        print(f"Updated {destination.name} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
