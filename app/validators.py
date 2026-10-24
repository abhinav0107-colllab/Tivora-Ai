import ipaddress
import re
import socket
from urllib.parse import urlparse

_MOBILE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(raw: str | None) -> str | None:
    """Returns +91XXXXXXXXXX for a valid Indian mobile number, else None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return "+91" + digits if _MOBILE.match(digits) else None


def resolve_host(host: str) -> list[str]:
    return list({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})


def assert_public_https_url(url: str) -> str:
    """Blocks webhook targets that point at private networks (SSRF protection)."""
    p = urlparse(url or "")
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Use a full https:// address.")
    try:
        addrs = resolve_host(p.hostname)
    except OSError:
        raise ValueError("That address could not be found.")
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("That address points to a private network and can’t be used.")
    return url


def redact_error(msg: str, limit: int = 200) -> str:
    msg = re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?…", str(msg))
    return msg[:limit]
