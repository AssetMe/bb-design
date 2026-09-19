"""URL normalization and scope rules.

Normalization is deliberately conservative: the goal is to collapse URLs that
are *demonstrably* the same resource without destroying URLs where the query
string selects content.  The original URL is always preserved separately by the
caller; nothing here is lossy for the archive itself.
"""
from __future__ import annotations

import ipaddress
import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urldefrag,
    urljoin,
    urlsplit,
    urlunsplit,
)

DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}

#: Parameters that are purely analytics/attribution.  These never select
#: content, so removing them is safe and greatly reduces duplicate fetches.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_social",
        "utm_social-type", "utm_brand", "utm_creative_format",
        "utm_marketing_tactic",
        "gclid", "gclsrc", "dclid", "gbraid", "wbraid",
        "fbclid", "msclkid", "twclid", "igshid", "ttclid", "li_fat_id",
        "yclid", "ysclid", "_openstat",
        "mc_cid", "mc_eid",
        "_ga", "_gl", "_hsenc", "_hsmi", "hsa_acc", "hsa_cam", "hsa_grp",
        "ref_src", "ref_url",
        "vero_conv", "vero_id", "wickedid", "oly_anon_id", "oly_enc_id",
        "s_kwcid", "elqTrackId", "elqTrack", "assetType", "pk_campaign",
        "pk_kwd", "piwik_campaign", "piwik_kwd", "mtm_campaign", "mtm_source",
        "mtm_medium", "mtm_keyword", "mtm_content", "mtm_cid", "mtm_group",
        "mtm_placement",
    }
)

#: Unreserved characters per RFC 3986 - percent-encoding these is redundant.
_UNRESERVED_SAFE = "-._~"
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_SESSION_PATH_RE = re.compile(r";jsessionid=[^/?#]*", re.I)


class UrlError(ValueError):
    """Raised when a string cannot be interpreted as an archivable URL."""


def _normalize_percent_encoding(value: str, safe: str) -> str:
    """Decode redundantly-encoded unreserved chars, uppercase remaining hex."""
    # Decode everything we can, then re-encode with a stable safe set.  This
    # collapses %2D -> '-' and normalises %2f -> %2F in one pass.
    decoded = unquote(value, errors="strict") if "%" in value else value
    try:
        decoded.encode("utf-8")
    except UnicodeEncodeError:  # pragma: no cover - defensive
        return value
    return quote(decoded, safe=safe)


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    path = _SESSION_PATH_RE.sub("", path)
    # Resolve . and .. without letting them escape the root.
    had_trailing_slash = path.endswith("/")
    collapsed = posixpath.normpath(path)
    if collapsed == ".":
        collapsed = "/"
    while collapsed.startswith("//"):
        collapsed = collapsed[1:]
    if not collapsed.startswith("/"):
        collapsed = "/" + collapsed
    if had_trailing_slash and not collapsed.endswith("/"):
        collapsed += "/"
    return _normalize_percent_encoding(collapsed, safe="/:@!$&'()*+,;=" + _UNRESERVED_SAFE)


def _normalize_query(query: str, drop_params: Iterable[str], sort_params: bool) -> str:
    if not query:
        return ""
    drop = {p.lower() for p in drop_params}
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in drop]
    if not kept:
        return ""
    # Deduplicate exact repeats (?a=1&a=1) but keep genuine multi-values (?a=1&a=2).
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for pair in kept:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    if sort_params:
        unique.sort()
    safe = "!$'()*+,:@/?" + _UNRESERVED_SAFE
    return "&".join(
        f"{quote(k, safe=safe)}={quote(v, safe=safe)}" if v != "" else quote(k, safe=safe)
        for k, v in unique
    )


def normalize(
    url: str,
    base: str | None = None,
    *,
    drop_params: Iterable[str] = TRACKING_PARAMS,
    sort_params: bool = True,
    strip_index: bool = False,
) -> str:
    """Return a canonicalised form of ``url``, resolved against ``base``.

    Raises :class:`UrlError` for non-archivable URLs (mailto:, javascript:,
    data:, empty fragments-only links, ...).
    """
    if url is None:
        raise UrlError("url is None")
    raw = url.strip()
    if not raw:
        raise UrlError("empty url")
    # Strip control characters / stray whitespace that HTML authors leave in.
    raw = "".join(ch for ch in raw if ord(ch) > 0x20 or ch == " ").strip()
    raw = raw.replace(" ", "%20")
    if base:
        raw = urljoin(base, raw)
    raw, _frag = urldefrag(raw)
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlError(f"unsupported scheme: {scheme or '(relative)'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlError("missing host")
    try:  # normalise IPv6 literals / IPv4 shorthand
        ip = ipaddress.ip_address(host)
        host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            pass
    netloc = host
    if parts.port is not None and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    path = _normalize_path(parts.path)
    if strip_index:
        for index in ("index.html", "index.htm", "index.php", "default.html"):
            if path.endswith("/" + index):
                path = path[: -len(index)]
                break
    query = _normalize_query(parts.query, drop_params, sort_params)
    return urlunsplit((scheme, netloc, path, query, ""))


def try_normalize(url: str, base: str | None = None, **kwargs) -> str | None:
    """:func:`normalize` that returns ``None`` instead of raising."""
    try:
        return normalize(url, base, **kwargs)
    except (UrlError, ValueError, UnicodeError):
        return None


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def registrable_suffix(host: str) -> str:
    """Best-effort eTLD+1 without a public-suffix dependency.

    Handles the common ``co.uk``-style two-part suffixes explicitly; anything
    else falls back to the last two labels.  Only used for the optional
    ``include_subdomains`` scope rule, never for security decisions.
    """
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    two_part = {
        "co", "com", "net", "org", "gov", "edu", "ac", "mil", "or", "ne", "go",
    }
    if labels[-2] in two_part and len(labels[-1]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass(slots=True)
class Scope:
    """Decides which URLs belong to the archive."""

    allowed_hosts: set[str] = field(default_factory=set)
    include_subdomains: bool = True
    allow_patterns: Sequence[str] = ()
    deny_patterns: Sequence[str] = ()
    _allow_re: list[re.Pattern[str]] = field(default_factory=list, init=False, repr=False)
    _deny_re: list[re.Pattern[str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.allowed_hosts = {h.lower().rstrip(".") for h in self.allowed_hosts if h}
        self._allow_re = [re.compile(p) for p in self.allow_patterns]
        self._deny_re = [re.compile(p) for p in self.deny_patterns]

    @classmethod
    def from_roots(cls, roots: Iterable[str], **kwargs) -> "Scope":
        hosts: set[str] = set()
        for root in roots:
            host = host_of(root)
            if not host:
                continue
            hosts.add(host)
            # Treat www and apex as the same site - virtually always true.
            hosts.add(host[4:] if host.startswith("www.") else "www." + host)
        return cls(allowed_hosts=hosts, **kwargs)

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if not host:
            return False
        if host in self.allowed_hosts:
            return True
        if self.include_subdomains:
            bases = {registrable_suffix(h) for h in self.allowed_hosts}
            if registrable_suffix(host) in bases:
                return True
            return any(host.endswith("." + h) for h in self.allowed_hosts)
        return False

    def allows(self, url: str) -> bool:
        if not self.host_allowed(host_of(url)):
            return False
        if any(rx.search(url) for rx in self._deny_re):
            return False
        if self._allow_re and not any(rx.search(url) for rx in self._allow_re):
            return False
        return True
