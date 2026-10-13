"""allow_private (B14/B21): explicit opt-in for LAN, loopback, and tailnet.
Link-local (169.254/16, cloud metadata), reserved, and multicast remain
blocked ALWAYS."""
import ipaddress

import pytest

from common import net as _net


@pytest.mark.parametrize("ip,blocked_default,blocked_private", [
    ("8.8.8.8", False, False),
    ("192.168.1.10", True, False),
    ("10.0.0.5", True, False),
    ("127.0.0.1", True, False),
    ("100.101.102.103", True, False),   # Tailscale / CGNAT
    ("169.254.169.254", True, True),    # cloud metadata: never
    ("224.0.0.1", True, True),
    ("240.0.0.1", True, True),
])
def test_blocked_ip_matrix(ip, blocked_default, blocked_private):
    addr = ipaddress.ip_address(ip)
    assert _net._is_blocked_ip(addr) is blocked_default
    assert _net._is_blocked_ip(addr, allow_private=True) is blocked_private


def test_validate_host_respects_flag(monkeypatch):
    monkeypatch.setattr(_net.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, ("192.168.1.10", 0))])
    with pytest.raises(_net.SSRFBlocked):
        _net._validate_host("nas.local")
    _net._validate_host("nas.local", allow_private=True)


def test_safe_urlopen_passes_flag_to_redirect_handler(monkeypatch):
    seen = {}

    class FakeHandler(_net._ValidatingRedirectHandler):
        def __init__(self, *a, **k):
            seen["allow_private"] = k.get("allow_private")
            super().__init__(*a, **k)

    monkeypatch.setattr(_net, "_ValidatingRedirectHandler", FakeHandler)
    monkeypatch.setattr(_net, "_validate_url", lambda url, allow_private=False: None)
    monkeypatch.setattr(_net.urllib.request, "build_opener",
                        lambda *h: type("O", (), {"open": lambda self, *a, **k: (_ for _ in ()).throw(OSError("no net"))})())
    assert _net.safe_urlopen("https://x.example/", allow_private=True) == (None, b"")
    assert seen["allow_private"] is True
