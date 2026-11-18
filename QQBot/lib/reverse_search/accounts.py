"""社交账号识别与聚合（纯函数，可离线测试）。

从候选的 author_url / page_url 里认出平台与账号 handle，
按 (平台, handle) 聚合证据；作者主页优先于单条帖子页。
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .engines import Candidate

# 域名 → 平台标签
_PLATFORMS = (
    (("x.com", "twitter.com", "t.co"), "Twitter/X"),
    (("instagram.com",), "Instagram"),
    (("weibo.com", "weibo.cn"), "微博"),
    (("space.bilibili.com", "bilibili.com", "b23.tv"), "B站"),
    (("pixiv.net",), "pixiv"),
    (("artstation.com",), "ArtStation"),
    (("lofter.com",), "LOFTER"),
    (("xiaohongshu.com", "xhslink.com"), "小红书"),
    (("facebook.com", "fb.com"), "Facebook"),
    (("douyin.com", "tiktok.com"), "抖音/TikTok"),
    (("youtube.com", "youtu.be"), "YouTube"),
    (("deviantart.com",), "DeviantArt"),
    (("tumblr.com",), "Tumblr"),
    (("bsky.app",), "Bluesky"),
)

# 这些平台按「画师/创作者」标注，不当 coser 本人
_ARTIST_LABELS = {"pixiv": "画师（非 coser 本人）",
                  "ArtStation": "画师", "DeviantArt": "画师"}

# 各平台路径里不属于 handle 的首段
_NON_HANDLE = {
    "Twitter/X": {"home", "search", "i", "intent", "hashtag", "share", "explore", "settings"},
    "Instagram": {"p", "reel", "reels", "stories", "explore", "tv", "accounts"},
    "微博": {"p", "detail", "status", "u", "n", "at"},
    "B站": {"video", "read", "bangumi", "v", "cheese"},
    "pixiv": {"artworks", "tags", "search", "ranking", "novel", "discovery"},
    "小红书": {"explore", "discovery", "search_result", "board"},
    "YouTube": {"watch", "shorts", "results", "playlist", "feed"},
    "抖音/TikTok": {"video", "discover", "search", "fyp"},
    "Facebook": {"watch", "photo", "story.php", "groups", "pages", "permalink.php",
                 "marketplace", "login.php", "settings", "search", "help", "events", "gaming"},
    "Tumblr": {"tagged", "search", "dashboard"},
    "Bluesky": {"search", "notifications", "settings"},
    # /view/<id> 是旧版作品页——曾把 "view" 当 handle、两条噪声命中聚成
    # 「证据 2」假采纳（2026-11-17 真实运行，sim 27.79）
    "DeviantArt": {"view", "art", "gallery", "favorites", "favourites", "tag",
                   "browse", "daily", "topic", "shop", "poll", "status",
                   "journal", "search", "users", "settings", "login", "welcome"},
}

_POST_PATTERNS = (
    ("Twitter/X", re.compile(r"/status/\d+")),
    ("Instagram", re.compile(r"^/(p|reel|reels|tv)/")),
    ("Facebook", re.compile(r"^/(photo(?:s|\.php)?|story\.php|permalink\.php|reel|watch|groups|share)(?:/|$)|/(photos|posts|videos|reels)(?:/|$)", re.I)),
    ("ArtStation", re.compile(r"^/artwork/")),
    ("LOFTER", re.compile(r"^/post/")),
    ("微博", re.compile(r"^/(detail|status)/")),
    ("B站", re.compile(r"^/(video|read)/")),
    ("pixiv", re.compile(r"^/(?:[a-z]{2}/)?artworks/")),
    ("小红书", re.compile(r"^/(explore|discovery)/")),
    ("YouTube", re.compile(r"^/(watch|shorts)")),
    ("抖音/TikTok", re.compile(r"/(?:video|photo)/")),
    ("Tumblr", re.compile(r"^/post/")),
    ("Bluesky", re.compile(r"/post/")),
    ("DeviantArt", re.compile(r"^/(view|art)/|/art/")),
)

_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_.@])@([A-Za-z0-9_.]{2,30})(?![A-Za-z0-9_.])")
_TRACKING_KEYS = {"fbclid", "gclid", "igsh", "ref_src", "ref_url", "spm"}
_PROFILE_PATHS = {
    "Twitter/X": r"/[A-Za-z0-9_]{1,15}",
    "Instagram": r"/[A-Za-z0-9_.]{2,30}",
    "微博": r"/(?:(?:u|n)/)?[\w-]+",
    "pixiv": r"/(?:[a-z]{2}/)?users/[0-9]+",
    "ArtStation": r"/[\w-]+",
    "小红书": r"/user/profile/[A-Za-z0-9]+",
    "Facebook": r"/[A-Za-z0-9.]+",
    "抖音/TikTok": r"/(?:@[\w.-]+|user/[\w-]+)",
    "YouTube": r"/(?:@[\w.-]+|(?:channel|user|c)/[\w-]+)",
    "DeviantArt": r"/[\w-]+",
    "Tumblr": r"/[\w-]+",
    "Bluesky": r"/profile/[\w.:-]+",
}
_PROFILE_HOSTS = {
    "Twitter/X": {"x.com", "twitter.com", "m.twitter.com", "mobile.twitter.com", "m.x.com"},
    "Instagram": {"instagram.com", "m.instagram.com"},
    "Facebook": {"facebook.com", "m.facebook.com", "mbasic.facebook.com", "fb.com"},
    "Bluesky": {"bsky.app"},
}
_ID_PATHS = {
    "Twitter/X": r"/status/([0-9]+)(?:/|$)",
    "Instagram": r"^/(?:p|reels?|tv)/([A-Za-z0-9_-]+)(?:/|$)",
    "Facebook": r"/(?:posts|photos|videos)/(?:[^/]+/)*([0-9]+|pfbid[0-9A-Za-z]+)$",
    "pixiv": r"^/(?:[a-z]{2}/)?artworks/([0-9]+)$",
}



def _host_path(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path or ""


def platform_of(url: str) -> str:
    host, _ = _host_path(url)
    if not host:
        return ""
    for domains, label in _PLATFORMS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return label
    return ""


def is_post_page(url: str) -> bool:
    plat = platform_of(url)
    if not plat:
        return False
    _, path = _host_path(url)
    return any(p == plat and rx.search(path) for p, rx in _POST_PATTERNS)


def is_author_page(url: str) -> bool:
    """Recognize known profile URLs by positive path rules."""
    plat = platform_of(url)
    host, path = _host_path(url)
    path = path.rstrip("/")
    if not handle_of(url) or is_post_page(url):
        return False
    if plat in _PROFILE_HOSTS and host not in _PROFILE_HOSTS[plat]:
        return False
    if plat == "Facebook" and path == "/profile.php":
        return True
    if plat == "B站":
        return host == "space.bilibili.com" and bool(re.fullmatch(r"/[0-9]+", path))
    if plat == "DeviantArt" and host != "deviantart.com":
        return not path and host.split(".")[0] not in {"api", "help", "support", "m"}
    return bool(plat in _PROFILE_PATHS and re.fullmatch(_PROFILE_PATHS[plat], path))


def canonical_url(url: str) -> str:
    """Validate an outward-facing link and remove known tracking parameters."""
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or parsed.username is not None
                or not re.fullmatch(r"[a-z0-9.:-]+", host)):
            return ""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith(
                    (".local", ".localhost", ".internal", ".lan", ".home.arpa")):
                return ""
        else:
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                return ""
        domain = "[" + host + "]" if ":" in host else host
        if port is not None and (parsed.scheme, port) not in {("https", 443), ("http", 80)}:
            domain += f":{port}"
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                           if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_KEYS])
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme, domain, parsed.path or "/", query, ""))


def page_key(url: str) -> str:
    """Identify a page across tracking URLs and known platform aliases."""
    clean = canonical_url(url)
    if not clean:
        return ""
    parts = urlsplit(clean)
    platform = platform_of(clean)
    path = parts.path.rstrip("/")
    post_pattern = _ID_PATHS.get(platform)
    match = re.search(post_pattern, path, re.I) if post_pattern else None
    if match:
        return f"{platform}:post:{match[1]}"
    if platform == "Facebook":
        query = dict(parse_qsl(parts.query))
        post = query.get("fbid") or query.get("story_fbid")
        if post:
            return "Facebook:post:" + post
    if is_author_page(clean):
        return f"{platform}:profile:{handle_of(clean).lower()}"
    return urlunsplit((parts.scheme, parts.netloc.removeprefix("www."),
                       path or "/", parts.query, ""))


def handle_of(url: str) -> str:
    """从 URL 提取账号 handle；提不出返回空串。"""
    plat = platform_of(url)
    if not plat:
        return ""
    host, path = _host_path(url)
    segs = [s for s in path.split("/") if s]
    if plat == "Facebook" and path == "/profile.php":
        identifier = dict(parse_qsl(urlsplit(url).query)).get("id", "")
        return identifier if identifier.isdigit() else ""
    if plat == "B站":
        if host.startswith("space."):
            return segs[0] if segs else ""
        return ""
    if plat == "pixiv":
        if len(segs) >= 2 and segs[-2] == "users" and segs[-1].isdigit():
            return segs[-1]
        return ""
    if plat == "Bluesky":
        return segs[1] if len(segs) > 1 and segs[0] == "profile" else ""
    if plat == "小红书" and len(segs) == 3 and segs[:2] == ["user", "profile"]:
        return segs[2]
    if plat in {"YouTube", "抖音/TikTok"} and len(segs) == 2 and segs[0] in {"channel", "user", "c"}:
        return segs[1]
    if plat == "微博":
        if segs and segs[0] in ("u", "n") and len(segs) > 1:
            return segs[1]
        if segs and segs[0] not in _NON_HANDLE[plat]:
            return segs[0]
        return ""
    if plat == "DeviantArt" and host != "deviantart.com":
        # 作者主页常用 <user>.deviantart.com 子域
        sub = host.split(".")[0]
        return "" if sub in _NON_HANDLE[plat] else sub
    if not segs:
        return ""
    first = segs[0]
    if first in _NON_HANDLE.get(plat, set()):
        return ""
    return first.lstrip("@")


def handles_in_text(text: str) -> list[str]:
    """标题/描述文本里的 @handle。"""
    return _HANDLE_RE.findall(text or "")


@dataclass
class Account:
    """聚合后的一个账号身份。"""
    platform: str
    handle: str = ""
    name: str = ""
    url: str = ""
    is_author_page: bool = False
    has_author_url: bool = False
    label: str = ""
    evidence_count: int = 0
    best_similarity: float = 0.0
    engines: set = field(default_factory=set)
    rejection_reason: str = ""

    @property
    def key(self) -> tuple:
        return self.platform, (self.handle or self.name).lower()

    @property
    def display(self) -> str:
        return self.name or self.handle or self.url


def _identity_key(plat: str, handle: str, name: str) -> tuple:
    return plat, (handle or name).lower()


def aggregate_accounts(candidates: list[Candidate]) -> list[Account]:
    """把候选里的社交账号线索按 (平台, handle/名字) 聚合。

    - author_url / page_url 中认出的社交链接都算证据
    - 同一账号：作者主页 URL 优先于帖子页
    - 只有 author 名字没有链接时，平台记为「未知」
    """
    merged: dict[tuple, Account] = {}
    evidence: dict[tuple, set[str]] = {}

    def touch(key, **kw) -> Account:
        acc = merged.get(key)
        if acc is None:
            acc = Account(platform=key[0], handle=kw.get("handle", ""),
                          name=kw.get("name", ""))
            acc.label = _ARTIST_LABELS.get(acc.platform, "")
            merged[key] = acc
        return acc

    for cand in candidates:
        seen_keys: set[tuple] = set()
        for url, is_author in ((cand.author_url, True), (cand.page_url, False)):
            url = canonical_url(url)
            if not url:
                continue
            plat = platform_of(url)
            if not plat:
                continue
            handle = handle_of(url)
            profile = is_author_page(url)
            name = cand.author if is_author and profile else ""
            if not handle or not (profile or is_post_page(url)):
                continue
            key = _identity_key(plat, handle, name)
            acc = touch(key, handle=handle, name=name)
            if not acc.url or (not acc.is_author_page and profile):
                acc.url = url
                acc.is_author_page = profile
            if is_author and profile:
                acc.has_author_url = True
            if name and not acc.name:
                acc.name = name
            if handle and not acc.handle:
                acc.handle = handle
            seen_keys.add(key)
        # 无链接但有作者名（如 SauceNAO 某些索引）
        if cand.author and not seen_keys:
            key = _identity_key("未知", "", cand.author)
            acc = touch(key, name=cand.author)
            seen_keys.add(key)
        source = page_key(cand.page_url) or page_key(cand.author_url)
        for key in seen_keys:
            acc = merged[key]
            if source:
                pages = evidence.setdefault(key, set())
                pages.add(source)
                acc.evidence_count = len(pages)
            acc.best_similarity = max(acc.best_similarity, cand.similarity or 0)
            acc.engines.add(cand.engine)
    return list(merged.values())
