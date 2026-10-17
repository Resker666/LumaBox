import pytest


@pytest.mark.parametrize("error,detail,want", [
    ("RESOURCE_TIMEOUT", {}, True),
    ("DOWNLOAD_FAILED", {"network_reason": "CONNECTION_RESET", "stage": "media_transfer"}, True),
    ("DOWNLOAD_FAILED", {"network_reason": "CONNECTION_TIMEOUT", "stage": "page_navigation"}, True),
    ("UNSAFE_TARGET", {"network_reason": "DNS_TIMEOUT", "stage": "dns_resolution"}, True),
    ("DOWNLOAD_FAILED", {}, False),
    ("DOWNLOAD_FAILED", {"network_reason": "CONNECTION_RESET", "stage": "publication"}, False),
    ("RESOURCE_TIMEOUT", {"network_reason": "DNS_NON_PUBLIC"}, False),
    ("RESOURCE_TIMEOUT", {"network_reason": "TLS_ERROR"}, False),
    ("RESOURCE_TIMEOUT", {"network_reason": "CONNECTION_REFUSED"}, False),
    ("RESOURCE_TIMEOUT", {"network_reason": "CONNECTION_UNREACHABLE"}, False),
])
def test_retry_only_recognized_transient_failure(error, detail, want):
    from lumabox.web.retry_policy import should_retry
    assert should_retry(error, detail) is want


@pytest.mark.parametrize("error", ["AUTH_REQUIRED", "CHALLENGE_REQUIRED", "BLOCKED", "RATE_LIMITED",
    "NOT_FOUND", "SIZE_LIMIT", "STORAGE_FULL", "UNSUPPORTED_MEDIA", "PARSE_CHANGED", "VERIFY_FAILED",
    "DEPENDENCY_MISSING", "INTERRUPTED", "TIMEOUT", "RESULT_PARSE_FAILED"])
def test_terminal_errors_override_transient_diagnostics(error):
    from lumabox.web.retry_policy import should_retry
    assert not should_retry(error, {"network_reason": "CONNECTION_TIMEOUT", "stage": "media_transfer"})
