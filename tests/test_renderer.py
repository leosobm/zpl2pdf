from unittest.mock import patch

import pytest

from zpl2pdf.parser import ZplLabel
from zpl2pdf.renderer import LabelaryRenderer, RateLimiter, RenderError


class NoOpRateLimiter(RateLimiter):
    """Rate limiter que nunca espera — usado nos testes para isolar o backoff."""

    def wait(self) -> None:
        pass


def make_label(raw: str = "^XA^FO0,0^A0N,30,30^FDHello^FS^XZ", index: int = 0) -> ZplLabel:
    return ZplLabel(raw=raw, index=index, pw_dots=None, ll_dots=None, jm_mode=None)


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def no_real_sleep():
    """Evita esperar de verdade (backoff + rate limiter) durante os testes."""
    with patch("zpl2pdf.renderer.time.sleep") as mock_sleep:
        yield mock_sleep


def test_429_is_retried_with_exponential_backoff_and_eventually_succeeds(no_real_sleep):
    responses = [
        FakeResponse(429),
        FakeResponse(429),
        FakeResponse(200, content=b"PNGDATA"),
    ]
    renderer = LabelaryRenderer(
        cache=None,
        max_retries=5,
        retry_backoff_seconds=1.0,
        rate_limiter=NoOpRateLimiter(),
    )

    with patch("zpl2pdf.renderer.requests.post", side_effect=responses) as mock_post:
        result = renderer.render(make_label(), dpmm=8, width_mm=30.0, height_mm=20.0)

    assert result.png_bytes == b"PNGDATA"
    assert mock_post.call_count == 3

    # Backoff exponencial: 1ª espera ~1s (1.0 * 2**0), 2ª espera ~2s (1.0 * 2**1).
    sleep_calls = [call.args[0] for call in no_real_sleep.call_args_list]
    assert sleep_calls == [1.0, 2.0]


def test_429_respects_retry_after_header(no_real_sleep):
    responses = [
        FakeResponse(429, headers={"Retry-After": "5"}),
        FakeResponse(200, content=b"PNGDATA"),
    ]
    renderer = LabelaryRenderer(
        cache=None,
        max_retries=3,
        retry_backoff_seconds=1.0,
        rate_limiter=NoOpRateLimiter(),
    )

    with patch("zpl2pdf.renderer.requests.post", side_effect=responses):
        renderer.render(make_label(), dpmm=8, width_mm=30.0, height_mm=20.0)

    sleep_calls = [call.args[0] for call in no_real_sleep.call_args_list]
    # Retry-After (5s) > backoff exponencial (1s) -> deve prevalecer.
    assert sleep_calls == [5.0]


def test_persistent_429_raises_render_error_naming_the_label(no_real_sleep):
    renderer = LabelaryRenderer(
        cache=None,
        max_retries=3,
        retry_backoff_seconds=1.0,
        rate_limiter=NoOpRateLimiter(),
    )

    with patch("zpl2pdf.renderer.requests.post", return_value=FakeResponse(429)) as mock_post:
        with pytest.raises(RenderError) as exc_info:
            renderer.render(make_label(index=4), dpmm=8, width_mm=30.0, height_mm=20.0)

    assert mock_post.call_count == 3
    message = str(exc_info.value)
    assert "etiqueta 5" in message
    assert "3 tentativas" in message


def test_rate_limiter_spaces_calls_at_configured_rate():
    limiter = RateLimiter(max_calls_per_second=2.0)
    sleep_durations = []

    real_monotonic = __import__("time").monotonic
    fake_now = [0.0]

    def fake_monotonic():
        return fake_now[0]

    def fake_sleep(seconds: float):
        sleep_durations.append(seconds)
        fake_now[0] += seconds

    with patch("zpl2pdf.renderer.time.monotonic", side_effect=fake_monotonic), \
         patch("zpl2pdf.renderer.time.sleep", side_effect=fake_sleep):
        limiter.wait()
        limiter.wait()
        limiter.wait()

    # 2 chamadas/s -> intervalo mínimo de 0.5s entre chamadas consecutivas.
    assert sleep_durations == [0.5, 0.5]
