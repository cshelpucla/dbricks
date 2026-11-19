"""Thumbnail URLs for Wikimedia Commons files (people's portraits in taxonomy_people.image)."""
import hashlib
from urllib.parse import quote

THUMB_WIDTH = 120   # Commons serves only its standard thumbnail widths (60, 120, 250, ...); any other gets HTTP 400


def thumb_url(name: str | None) -> str | None:
    """'Taylor Swift 2023.png' -> https://upload.wikimedia.org/wikipedia/commons/thumb/<h>/<hh>/<file>/120px-<file>,
    where h, hh are the first one and two hex digits of the md5 of the file name (Commons' storage layout)."""
    if not name:
        return None
    name = name.replace(" ", "_")
    h = hashlib.md5(name.encode()).hexdigest()
    q = quote(name)
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/{h[0]}/{h[:2]}/{q}/{THUMB_WIDTH}px-{q}"
