from dataclasses import FrozenInstanceError, replace

import pytest

from app.domain.retrieval import RenderRequest, RetrievalRequest


def test_render_allowance_is_separate_from_raw_retrieval():
    request = RenderRequest("https://example.com")
    assert request.timeout_seconds == 60
    assert request.max_requests == 75
    with pytest.raises(FrozenInstanceError):
        request.__setattr__("url", "https://other.example")
    with pytest.raises(ValueError, match="timeout"):
        RetrievalRequest("https://example.com", timeout_seconds=31)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_requests": True},
        {"max_requests": 0},
        {"max_requests": 76},
        {"max_response_bytes": 64 * 1024 * 1024 + 1},
        {"max_response_bytes": True},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"timeout_seconds": float("nan")},
        {"scope_origin": ""},
        {"scope_path_prefix": "relative"},
        {"url": ""},
    ],
)
def test_invalid_render_allowances_fail_at_the_domain_boundary(changes):
    with pytest.raises(ValueError):
        replace(RenderRequest("https://example.com"), **changes)
