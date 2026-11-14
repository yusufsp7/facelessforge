"""Platform registry.

Each platform declares the URLs and JS checks the login-poll loop and posters
need. The login_check_js snippets follow AutoClip's approach: look for a
logged-in marker element (EN + ID variants) and/or absence of a login form.
"""


class PlatformSpec:
    def __init__(self, *, id: str, name: str, login_url: str, upload_url: str,
                 login_success_hosts: tuple, login_check_js: str,
                 account_name_js: str = "", probe_url: str = "",
                 schedule_supported: bool = False):
        self.id = id
        self.name = name
        self.login_url = login_url
        self.upload_url = upload_url
        self.login_success_hosts = login_success_hosts
        self.login_check_js = login_check_js
        self.account_name_js = account_name_js
        self.probe_url = probe_url or login_url
        self.schedule_supported = schedule_supported


_PLATFORMS: dict[str, PlatformSpec] = {
    "tiktok": PlatformSpec(
        id="tiktok",
        name="TikTok",
        login_url="https://www.tiktok.com/login/phone-or-email/email",
        upload_url="https://www.tiktokstudio.com/upload?enter_from=page&lang=en",
        login_success_hosts=("tiktok.com",),
        login_check_js=(
            "!!document.querySelector('div[data-e2e=\"profile-icon\"]') || "
            "!!document.querySelector('a[data-e2e=\"nav-profile\"]') || "
            "!!document.querySelector('[data-e2e=\"channel-menu-item\"]')"
        ),
        account_name_js=(
            "() => { const el = document.querySelector("
            "'div[data-e2e=\"profile-icon\"] span, a[data-e2e=\"nav-profile\"] span');"
            " return el ? el.textContent.trim() : ''; }"
        ),
        probe_url="https://www.tiktok.com/",
        schedule_supported=True,
    ),
    "youtube": PlatformSpec(
        id="youtube",
        name="YouTube",
        login_url="https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fwww.youtube.com%2F",
        upload_url="https://studio.youtube.com/",
        login_success_hosts=("youtube.com", "accounts.google.com"),
        login_check_js=(
            "!!document.querySelector('img#img.avatar, button#avatar-btn, "
            "yt-img-shadow#avatar img, ytd-masthead [aria-label*=\"Account\"]')"
        ),
        account_name_js=(
            "() => { const el = document.querySelector("
            "'yt-masthead-account-picker [aria-label], ytd-masthead [aria-label*=\"Account\"]');"
            " return el ? (el.getAttribute('aria-label') || '').trim() : ''; }"
        ),
        probe_url="https://www.youtube.com/",
        schedule_supported=False,  # native schedule belum dipakai — worker upload tepat waktu
    ),
    "facebook": PlatformSpec(
        id="facebook",
        name="Facebook",
        login_url="https://www.facebook.com/login",
        upload_url="https://www.facebook.com/reels/create",
        login_success_hosts=("facebook.com",),
        login_check_js=(
            "(!!document.querySelector('div[role=\"navigation\"][aria-label=\"Facebook\"]') || "
            "!!document.querySelector('div[role=\"navigation\"] a[aria-label]')) && "
            "(!document.querySelector('input[name=\"email\"], #loginform, form[action*=\"login\"]'))"
        ),
        account_name_js=(
            "() => { const el = document.querySelector("
            "'div[role=\"navigation\"] a[aria-label] span') || "
            "document.querySelector('a[href=\"/me/\"] span');"
            " return el ? el.textContent.trim() : ''; }"
        ),
        probe_url="https://www.facebook.com/",
        schedule_supported=True,
    ),
    "instagram": PlatformSpec(
        id="instagram",
        name="Instagram",
        login_url="https://www.instagram.com/accounts/login/",
        upload_url="https://www.instagram.com/",
        login_success_hosts=("instagram.com",),
        login_check_js=(
            "!!document.querySelector('svg[aria-label*=\"Beranda\"], svg[aria-label*=\"Home\"], "
            "a[href=\"/explore/\"]') && "
            "(!document.querySelector('input[name=\"username\"], form[action*=\"/accounts/login/\"]'))"
        ),
        account_name_js=(
            "() => { const el = document.querySelector('a[href^=\"/direct/\"] img[alt], "
            "img[alt*=\"Foto profil\"]'); return el ? (el.getAttribute('alt') || '').trim() : ''; }"
        ),
        probe_url="https://www.instagram.com/",
        schedule_supported=False,
    ),
    "threads": PlatformSpec(
        id="threads",
        name="Threads",
        login_url="https://www.threads.net/login",
        upload_url="https://www.threads.net/",
        login_success_hosts=("threads.net", "threads.com"),
        login_check_js=(
            "(!document.querySelector('input[name=\"username\"]')) && "
            "(!!document.querySelector('a[href^=\"/inbox\"], [aria-label*=\"Balasan\"], "
            "[aria-label*=\"Replies\"], div[data-pressable-container=\"true\"]'))"
        ),
        account_name_js="() => ''",
        probe_url="https://www.threads.net/",
        schedule_supported=False,
    ),
}

# Extra platforms are added here later; UI reads this registry.


def get(platform: str) -> PlatformSpec:
    return _PLATFORMS[platform]


def all() -> dict[str, PlatformSpec]:
    return dict(_PLATFORMS)


def public_list() -> list[dict]:
    return [{
        "id": p.id, "name": p.name, "login_url": p.login_url,
        "upload_url": p.upload_url, "schedule_supported": p.schedule_supported,
    } for p in _PLATFORMS.values()]
