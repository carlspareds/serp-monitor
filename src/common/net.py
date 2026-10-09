"""Shared anti-SSRF network boundary (A3 review, H1): every outbound fetch
of a URL the app does not control (processing.evidence.capture_url) goes
through `safe_urlopen` instead of calling urllib directly -- those URLs come
from third-party data (SERP results, a form, an HTTP redirect), so an
attacker controlling one of them could try to point the fetch at an
internal resource of the dashboard's own network (169.254.169.254 of a
metadata endpoint, a localhost service, a VPN peer...).

`safe_urlopen` validates:
  1. http(s) scheme only (no file://, no gopher://, nothing else).
  2. the host resolves (socket.getaddrinfo) to addresses that are NOT
     private/loopback/link-local/reserved/multicast, NOR in the
     100.64.0.0/10 range (CGNAT/"shared address space" from RFC 6598 --
     the actual range Tailscale uses, which ipaddress.IPv4Address.
     is_private does NOT cover on its own).
  3. the same on EVERY redirect hop -- a server can respond 200 from a
     public host and then redirect to an internal host; validating only
     the initial URL is not enough (_ValidatingRedirectHandler re-validates
     `newurl` on every 30x before following it).

`allow_private=True` is an EXPLICIT OPT-IN for a caller that legitimately
needs to reach a host on the LAN or a VPN, where the default block is
exactly what gets in the way (no caller in this repo passes it today). What NEVER relaxes is link-local, reserved
and multicast: 169.254.169.254 is the cloud metadata endpoint, the classic
SSRF target, and no legitimate integration needs to talk to it.

ALWAYS best-effort (same spirit as the rest of the repo): any failure --
validation (SSRFBlocked), network, HTTP -- is swallowed here and translated
to (None, b"") or (status_code, b"") for real HTTPErrors (the caller
already knows how to distinguish "blocked/down" from "real HTTP error")."""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

_ALLOWED_SCHEMES = ("http", "https")
# 100.64.0.0/10 (RFC 6598, "shared address space" -- the actual range
# Tailscale/CGNAT uses). Different from the RFC 1918 ranges that
# ipaddress.is_private already covers (10/8, 172.16/12, 192.168/16) -- has
# to be checked separately.
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


class SSRFBlocked(Exception):
    """The requested URL/host points to (or resolved to) a disallowed destination."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                   allow_private: bool = False) -> bool:
    if allow_private:
        # LAN/loopback/tailnet allowed; cloud metadata is NOT (see module docstring).
        return ip.is_link_local or ip.is_reserved or ip.is_multicast
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_RANGE


def _validate_host(hostname: str, allow_private: bool = False) -> None:
    if not hostname:
        raise SSRFBlocked("empty hostname")
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFBlocked(f"DNS resolution failed for '{hostname}': {exc}") from exc
    if not infos:
        raise SSRFBlocked(f"no addresses resolved for '{hostname}'")
    for info in infos:
        raw_addr = info[4][0].split("%")[0]  # discard the IPv6 link-local zone id (fe80::1%eth0)
        ip = ipaddress.ip_address(raw_addr)
        if _is_blocked_ip(ip, allow_private=allow_private):
            raise SSRFBlocked(f"'{hostname}' resolves to a blocked address: {raw_addr}")


def _validate_url(url: str, allow_private: bool = False) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SSRFBlocked(f"scheme not allowed: {parsed.scheme!r}")
    _validate_host(parsed.hostname or "", allow_private=allow_private)


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler that re-validates EVERY hop (H1) before following
    it -- a normal urllib.request.urlopen follows redirects without ever
    looking again at the destination scheme/host."""

    def __init__(self, *args, allow_private: bool = False, **kwargs):
        self.allow_private = allow_private
        super().__init__(*args, **kwargs)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(url: str, *, timeout: int = 15, max_bytes: int = 512 * 1024,
                  headers: dict | None = None,
                  allow_private: bool = False) -> tuple[int | None, bytes]:
    """(status, body[:max_bytes]) -- see module docstring. A real HTTPError
    (404, 403, 999...) DOES propagate its code -- the caller needs it to
    distinguish "down" from "blocked by the site" (e.g. entity.
    profiles._BLOCKED_STATUSES). Any other failure (SSRFBlocked, DNS,
    timeout, connection refused) degrades to (None, b"") -- never raises."""
    try:
        _validate_url(url, allow_private=allow_private)
        opener = urllib.request.build_opener(
            _ValidatingRedirectHandler(allow_private=allow_private))
        req = urllib.request.Request(url, headers=headers or {})
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(max_bytes)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception as exc:  # noqa: BLE001 -- best-effort, see module docstring
        print(f"[common.net] fetch blocked/failed for '{url}': {exc}")
        return None, b""
