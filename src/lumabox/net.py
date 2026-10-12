import http.client
import ipaddress
import socket
import ssl

from .errors import LumaBoxError


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not getattr(ip, "is_site_local", False) and not (
        isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("192.0.0.0/24")
    )


def resolve_public(host: str) -> tuple[str, ...]:
    try:
        results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(item[4][0] for item in results))
        if not addresses or not all(is_public(ip) for ip in addresses):
            raise LumaBoxError("UNSAFE_TARGET", "DNS returned a non-public destination")
        return addresses
    except (OSError, ValueError):
        raise LumaBoxError("UNSAFE_TARGET", "Public DNS resolution failed") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        raw = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
