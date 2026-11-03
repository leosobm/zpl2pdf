import io
from unittest.mock import patch

import pytest
from PIL import Image

from zpl2pdf.parser import ZplLabel
from zpl2pdf.renderer import (
    LabelaryRenderer,
    RateLimiter,
    RenderedLabel,
    RenderError,
    split_rendered_label,
)


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


def test_render_normalizes_pq_quantity_to_1_before_calling_labelary(no_real_sleep):
    """O Labelary simula uma impressora real: se o ^PQ do bloco pedir, digamos,
    56 cópias, ele tenta devolver 56 imagens numa só resposta e recusa (HTTP 413)
    acima do próprio teto dele. Só precisamos de 1 imagem por etiqueta distinta
    (as cópias físicas são resolvidas depois, sem nova chamada à API), então o
    ^PQ original nunca deve chegar como está ao corpo da requisição."""
    label = make_label(raw="^XA^FO0,0^FDHello^FS^PQ56,0,1,Y^XZ")
    renderer = LabelaryRenderer(cache=None, rate_limiter=NoOpRateLimiter())

    with patch(
        "zpl2pdf.renderer.requests.post", return_value=FakeResponse(200, content=b"PNGDATA")
    ) as mock_post:
        renderer.render(label, dpmm=8, width_mm=30.0, height_mm=20.0)

    sent_body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "^PQ56" not in sent_body
    assert "^PQ1,0,1,Y" in sent_body


def _make_grid_png(
    cols: int,
    rows: int,
    filled_cells: set[tuple[int, int]],
    cell_px: int = 40,
    gutter_px: int = 20,
) -> bytes:
    """Desenha um PNG `cols x rows` onde cada `(row, col)` em `filled_cells`
    recebe um quadrado preto (conteúdo) cercado por um corredor branco real
    de `gutter_px` — simula a "folha" composta de uma exportação tipo
    Shopee, onde a divisão automática depende de encontrar esses
    corredores em branco entre etiquetas vizinhas (sem gutter, duas células
    preenchidas lado a lado virariam uma única banda de conteúdo)."""
    width_px = cols * (cell_px + gutter_px) + gutter_px
    height_px = rows * (cell_px + gutter_px) + gutter_px
    image = Image.new("RGB", (width_px, height_px), "white")
    for row, col in filled_cells:
        x0 = gutter_px + col * (cell_px + gutter_px)
        y0 = gutter_px + row * (cell_px + gutter_px)
        for y in range(y0, y0 + cell_px):
            for x in range(x0, x0 + cell_px):
                image.putpixel((x, y), (0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    # width_mm/height_mm = width_px/height_px -> dpmm=1 nos testes, para que
    # o vão mínimo de 1.0mm usado para unir faixas internamente vire ~1px
    # (bem menor que o gutter_px real desenhado acima).
    return buffer.getvalue(), float(width_px), float(height_px)


def test_split_rendered_label_is_noop_when_disabled():
    rendered = RenderedLabel(png_bytes=b"whatever", width_mm=100.0, height_mm=150.0, label_index=0)
    pieces = split_rendered_label(rendered, enabled=False)
    assert pieces == [rendered]


def test_split_rendered_label_discards_blank_cells_and_keeps_filled_ones():
    # Grade 2x2, só as células (0,0) e (1,1) (diagonal) têm conteúdo.
    png_bytes, width_mm, height_mm = _make_grid_png(cols=2, rows=2, filled_cells={(0, 0), (1, 1)})
    rendered = RenderedLabel(png_bytes=png_bytes, width_mm=width_mm, height_mm=height_mm, label_index=3)

    pieces = split_rendered_label(rendered, enabled=True)

    assert len(pieces) == 2
    for piece in pieces:
        assert 0 < piece.width_mm < width_mm
        assert 0 < piece.height_mm < height_mm
        assert piece.label_index == 3


def test_split_rendered_label_all_blank_raises_render_error():
    png_bytes, width_mm, height_mm = _make_grid_png(cols=2, rows=2, filled_cells=set())
    rendered = RenderedLabel(png_bytes=png_bytes, width_mm=width_mm, height_mm=height_mm, label_index=0)

    with pytest.raises(RenderError):
        split_rendered_label(rendered, enabled=True)


def test_split_rendered_label_full_grid_keeps_all_cells():
    filled = {(row, col) for row in range(5) for col in range(2)}
    png_bytes, width_mm, height_mm = _make_grid_png(cols=2, rows=5, filled_cells=filled)
    rendered = RenderedLabel(png_bytes=png_bytes, width_mm=width_mm, height_mm=height_mm, label_index=0)

    pieces = split_rendered_label(rendered, enabled=True)

    assert len(pieces) == 10


def test_split_rendered_label_adapts_to_partial_last_row():
    # Reproduz o caso real (Shopee): a última "folha" de um lote só preenche
    # parte da grade (aqui, 1 das 2 colunas na última linha) — a grade real
    # tem 5 linhas, mas só 9 células com conteúdo (não 10).
    filled = {(row, col) for row in range(4) for col in range(2)} | {(4, 0)}
    png_bytes, width_mm, height_mm = _make_grid_png(cols=2, rows=5, filled_cells=filled)
    rendered = RenderedLabel(png_bytes=png_bytes, width_mm=width_mm, height_mm=height_mm, label_index=0)

    pieces = split_rendered_label(rendered, enabled=True)

    assert len(pieces) == 9


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
