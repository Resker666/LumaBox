import socket
from unittest.mock import Mock

import pytest
from lumabox.errors import LumaBoxError
from lumabox.net import PinnedHTTPSConnection, resolve_public


def dns_result(ip):
    return (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))


@pytest.mark.parametrize("ips", [[], ["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::ffff:192.168.1.1"], ["fec0::1"], ["192.0.0.8"]])
def test_rejects_any_non_public_answer(monkeypatch, ips):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [dns_result(ip) for ip in ips])
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        resolve_public("mixed.test")


def test_public_answers_are_deduplicated(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [dns_result("93.184.216.34")] * 2)
    assert resolve_public("public.test") == ("93.184.216.34",)


def test_tls_connects_to_pinned_ip_with_original_hostname(monkeypatch):
    dial = Mock(return_value=Mock())
    monkeypatch.setattr(socket, "create_connection", dial)
    conn = PinnedHTTPSConnection("p3.douyinpic.com", "93.184.216.34", 15)
    wrapped = Mock()
    conn._context.wrap_socket = Mock(return_value=wrapped)
    conn.connect()
    assert conn.sock is wrapped
    assert dial.call_args.args[0] == ("93.184.216.34", 443)
    assert conn._context.wrap_socket.call_args.kwargs["server_hostname"] == "p3.douyinpic.com"
