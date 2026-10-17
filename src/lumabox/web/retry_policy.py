"""One conservative retry, never an access-policy or validation workaround."""
RETRY_DELAY_SECONDS = 10
MAX_ATTEMPTS = 2


def should_retry(error: str, diagnostics: dict) -> bool:
    if error not in {"DOWNLOAD_FAILED", "RESOURCE_TIMEOUT", "UNSAFE_TARGET"}:
        return False
    detail = diagnostics if isinstance(diagnostics, dict) else {}
    reason, stage = detail.get("network_reason"), detail.get("stage")
    if reason and reason not in {"CONNECTION_RESET", "CONNECTION_TIMEOUT", "DNS_TIMEOUT"}:
        return False
    if reason == "DNS_TIMEOUT":
        return stage == "dns_resolution"
    if reason in {"CONNECTION_RESET", "CONNECTION_TIMEOUT"}:
        return error != "UNSAFE_TARGET" and stage in {"page_navigation", "browser_load", "media_transfer"}
    return error == "RESOURCE_TIMEOUT" and stage in {None, "page_navigation", "browser_load", "media_extraction", "media_transfer"}
