import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time

from .errors import LumaBoxError


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_reserved and not ip.is_multicast and not getattr(ip, "is_site_local", False) and not (
        isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("192.0.0.0/24")
    )


def bounded_call(function, timeout: float, cancel=None):
    """Bound non-interruptible lookup/header work; abandoned calls perform no I/O writes."""
    result = queue.Queue(maxsize=1)
    deadline = time.monotonic() + timeout

    def call():
        try:
            result.put((True, function()))
        except Exception as exc:
            result.put((False, exc))

    threading.Thread(target=call, daemon=True).start()
    while True:
        if cancel is not None and cancel.is_set():
            raise LumaBoxError("INTERRUPTED", "Operation cancelled")
        left = deadline - time.monotonic()
        if left <= 0:
            raise LumaBoxError("RESOURCE_TIMEOUT", "Operation exceeded its time limit")
        try:
            success, value = result.get(timeout=min(0.05, left))
        except queue.Empty:
            continue
        if success:
            return value
        raise value


def resolve_public(host: str, timeout: float = 15.0, cancel=None) -> tuple[str, ...]:
    try:
        results = bounded_call(lambda: socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout, cancel)
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
            # Publish the TLS socket before its handshake so the download
            # watchdog can interrupt a handshake at the absolute deadline.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock.do_handshake()
        except BaseException:
            if self.sock is not None:
                self.sock.close()
            raw.close()
            raise
