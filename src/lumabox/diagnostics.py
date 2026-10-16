"""Bounded network reasons; raw exception strings and signed URLs stay private."""
import errno
import re
import ssl

NETWORK_REASONS = frozenset({"DNS_NON_PUBLIC", "DNS_LOOKUP_FAILED", "DNS_TIMEOUT",
    "CONNECTION_REFUSED", "CONNECTION_RESET", "CONNECTION_TIMEOUT", "CONNECTION_UNREACHABLE",
    "TLS_ERROR", "CONNECTION_FAILED"})
STAGES = frozenset({"dns_resolution", "page_navigation", "browser_start", "browser_load",
    "media_extraction", "media_transfer", "media_verification", "publication", "input_policy", "target_identity"})
MEDIA_REASONS = {
    "Target work region could not be confirmed": "TARGET_UNCONFIRMED",
    "The browser switched to a different work": "WORK_CHANGED",
    "Complete media count could not be confirmed": "MEDIA_COUNT_UNCONFIRMED",
    "The complete ordered work was not available": "MEDIA_ORDER_UNCONFIRMED",
    "A normal HTTPS media resource is not available": "MEDIA_URL_UNAVAILABLE",
    "The slide's motion resource is not available": "MOTION_URL_UNAVAILABLE",
    "Gallery images are still showing the site's default placeholder": "IMAGES_LOADING",
}


def network_reason(error):
    if isinstance(error, ssl.SSLError):
        return "TLS_ERROR"
    if isinstance(error, TimeoutError):
        return "CONNECTION_TIMEOUT"
    if isinstance(error, ConnectionRefusedError):
        return "CONNECTION_REFUSED"
    if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "CONNECTION_RESET"
    if isinstance(error, OSError) and error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065):
        return "CONNECTION_UNREACHABLE"
    if isinstance(error, ConnectionError):
        return "CONNECTION_FAILED"
    return None


def public_diagnostics(diagnostics):
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    detail = {key: diagnostics[key] for key in ("limit_bytes", "actual_bytes", "at_least_bytes")
              if type(diagnostics.get(key)) is int and 0 <= diagnostics[key] < 2**63}
    reason = diagnostics.get("network_reason")
    # Also understand reports produced before structured network reasons existed.
    if not isinstance(reason, str) or reason not in NETWORK_REASONS:
        reason = {"DNS returned a non-public destination": "DNS_NON_PUBLIC",
                  "Public DNS resolution failed": "DNS_LOOKUP_FAILED"}.get(diagnostics.get("reason") if isinstance(diagnostics.get("reason"), str) else None)
    if reason:
        detail["network_reason"] = reason
        stage, host = diagnostics.get("stage"), diagnostics.get("host")
        if isinstance(stage, str) and stage in STAGES:
            detail["stage"] = stage
        if isinstance(host, str) and re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
            detail["host"] = host.lower()
    message = diagnostics.get("reason")
    media_reason = MEDIA_REASONS.get(message) if isinstance(message, str) else None
    if media_reason and diagnostics.get("stage") in {"media_extraction", "target_identity"}:
        detail.update(media_reason=media_reason, stage=diagnostics["stage"])
    return detail
