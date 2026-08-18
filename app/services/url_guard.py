"""Rejects monitor URLs that resolve onto the private network (SSRF guard).

Used on the MCP surface only. The REST API deliberately still accepts any URL
— see ``docs/study-guide/15-what-was-not-built.md`` — but an MCP tool takes its
arguments from a language model, which can be steered by text the model reads
elsewhere. A monitor aimed at ``169.254.169.254`` turns DevPulse into a probe
for cloud metadata, and the recorded status code leaks the answer.

This is a mitigation, not a cure. It resolves the hostname once, at creation
time; ``app/services/checker.py`` resolves again on every scheduled check, so a
hostname whose DNS record changes afterwards (rebinding) still gets through.
Closing that needs pinning the resolved address at check time, which is a
change to the checker and a separate decision.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import structlog

logger = structlog.stdlib.get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """Raised when a URL is malformed or resolves to a non-public address."""


def _is_public(address: str) -> bool:
    """False for loopback, private, link-local, reserved and multicast IPs."""
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def assert_public_url(url: str) -> None:
    """Raise ``UnsafeUrlError`` unless every address ``url`` resolves to is public.

    Checks *all* addresses rather than the first: a hostname with both a public
    and a loopback record would otherwise pass here and be checked against the
    loopback one later. Resolution goes through the event loop's resolver so a
    slow DNS server stalls this request rather than the whole process — the
    scheduler shares this event loop.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"URL scheme must be http or https, got {parts.scheme!r}"
        )
    hostname = parts.hostname
    if not hostname:
        raise UnsafeUrlError("URL has no hostname")

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {hostname!r}: {exc}") from exc

    addresses = {info[4][0] for info in infos}
    blocked = sorted(addr for addr in addresses if not _is_public(addr))
    if blocked:
        logger.warning("unsafe_url_rejected", url=url, addresses=blocked)
        raise UnsafeUrlError(
            f"Refusing to monitor {hostname!r}: it resolves to a private or "
            f"reserved address ({', '.join(blocked)}). Monitors may only "
            "target publicly routable hosts."
        )
