"""Poster registry — platform → concrete poster class."""
from .facebook import FacebookPoster
from .instagram import InstagramPoster
from .threads import ThreadsPoster
from .tiktok import TikTokPoster
from .youtube import YouTubePoster

registry: dict[str, type] = {
    "tiktok": TikTokPoster,
    "youtube": YouTubePoster,
    "facebook": FacebookPoster,
    "instagram": InstagramPoster,
    "threads": ThreadsPoster,
}


def get(platform: str):
    return registry.get(platform)
