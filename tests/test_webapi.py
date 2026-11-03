import io
import math

import pytest
from PIL import Image

from zpl2pdf.config import ConfigError
from zpl2pdf.renderer import LabelaryRenderer, RenderedLabel
from zpl2pdf.webapi import (
    RenderParams,
    analyze,
    count_labels,
    render_output_pdf,
    total_physical_quantity,
)


def _label_block(name: str, pq: int | None = None) -> str:
    pq_cmd = f"^PQ{pq},0,1,Y" if pq is not None else ""
    return f"^XA^PW406^LL203^FO10,10^FD{name}^FS{pq_cmd}^XZ"


def test_count_labels_counts_distinct_blocks():
    content = _label_block("um") + _label_block("dois") + _label_block("tres")
    assert count_labels(content) == 3


def test_total_physical_quantity_defaults_to_one_per_label():
    content = _label_block("um") + _label_block("dois")
    assert total_physical_quantity(content) == 2


def test_total_physical_quantity_sums_pq_per_label():
    content = _label_block("a", pq=10) + _label_block("b", pq=3) + _label_block("c", pq=12)
    assert total_physical_quantity(content) == 25


def test_analyze_requires_multi_mode_for_multiple_labels():
    content = _label_block("a") + _label_block("b")
    params = RenderParams(multi_mode=None)
    with pytest.raises(ConfigError):
        analyze(content, params)


def test_analyze_fit_one_page_uses_total_physical_quantity_not_distinct_count():
    # 3 etiquetas distintas, mas 10+3+12=25 etiquetas físicas via ^PQ
    content = _label_block("a", pq=10) + _label_block("b", pq=3) + _label_block("c", pq=12)
    params = RenderParams(multi_mode="fit-one-page")
    result = analyze(content, params)

    assert result.total_labels_in_file == 3
    assert result.total_labels_output == 25
    assert result.cols * result.rows == 25
    assert result.computed_pages == 1


def test_analyze_fit_one_page_without_pq_matches_distinct_count():
    content = _label_block("a") + _label_block("b") + _label_block("c")
    params = RenderParams(multi_mode="fit-one-page")
    result = analyze(content, params)

    assert result.total_labels_output == 3
    assert result.cols * result.rows == 3


def test_analyze_paginate_computes_pages_from_physical_quantity():
    # 58 etiquetas físicas, 6 por página -> ceil(58/6) = 10 páginas
    content = "".join(
        _label_block(name, pq=qty)
        for name, qty in [
            ("a", 10), ("b", 3), ("c", 3), ("d", 12), ("e", 1),
            ("f", 7), ("g", 13), ("h", 1), ("i", 4), ("j", 4),
        ]
    )
    params = RenderParams(multi_mode="paginate", labels_per_page=6)
    result = analyze(content, params)

    assert result.total_labels_in_file == 10
    assert result.total_labels_output == 58
    assert result.computed_pages == math.ceil(58 / 6)
    assert result.cols * result.rows == 6


def test_analyze_single_label_ignores_pq_and_uses_labels_per_page():
    # etiqueta única: ^PQ não deve influenciar a grade (fluxo controlado
    # explicitamente por labels_per_page/pages).
    content = _label_block("unica", pq=99)
    params = RenderParams(labels_per_page=4, pages=2)
    result = analyze(content, params)

    assert result.mode == "single"
    assert result.total_labels_output == 8
    assert result.cols * result.rows == 4


# ---------------------------------------------------------------------------
# split_grid: exportações (ex: picking da Shopee) onde uma única etiqueta do
# arquivo já é uma "folha" com várias etiquetas físicas desenhadas dentro da
# mesma imagem — sem nenhum comando ZPL indicando a quantidade real (só
# existe nos pixels, numa grade só conhecida depois de renderizar+detectar).
# O total antes de renderizar é uma estimativa por baixo (sem a divisão); o
# valor exato sai de render_output_pdf.
# ---------------------------------------------------------------------------


def test_analyze_with_split_grid_marks_total_as_estimated():
    content = _label_block("a") + _label_block("b")
    params = RenderParams(multi_mode="fit-one-page", split_grid=True)
    result = analyze(content, params)

    # analyze() nunca chama o Labelary, então não sabe quantas sub-etiquetas
    # cada bloco vai gerar — o total mostrado é só a contagem de blocos (sem
    # a divisão), marcado como estimativa.
    assert result.total_labels_output == 2
    assert result.is_output_estimated is True


def test_analyze_without_split_grid_is_not_estimated():
    content = _label_block("a") + _label_block("b")
    params = RenderParams(multi_mode="fit-one-page")
    result = analyze(content, params)

    assert result.is_output_estimated is False


def _make_grid_png(
    cols: int,
    rows: int,
    filled_cells: set[tuple[int, int]],
    cell_px: int = 40,
    gutter_px: int = 20,
) -> tuple[bytes, float, float]:
    """Mesma lógica de `test_renderer._make_grid_png`: quadrados de
    conteúdo cercados por um corredor branco real (necessário para a
    divisão automática encontrar os limites entre sub-etiquetas)."""
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
    return buffer.getvalue(), float(width_px), float(height_px)


def test_render_output_pdf_discards_blank_split_cells(monkeypatch, tmp_path):
    """Reproduz o caso real (Shopee): cada etiqueta do arquivo é, na
    verdade, uma folha 2x2 só parcialmente preenchida (3 das 4 células têm
    conteúdo) — com 2 blocos no arquivo e 1 por página, o PDF final deve ter
    exatamente 6 páginas (3+3 reais), não 8 (o teto de uma grade cheia 2x2)."""
    content = _label_block("folha1") + _label_block("folha2")
    png_bytes, width_mm, height_mm = _make_grid_png(
        cols=2, rows=2, filled_cells={(0, 0), (0, 1), (1, 0)}
    )

    def fake_render(self, label, dpmm, width_mm, height_mm):
        return RenderedLabel(png_bytes=png_bytes, width_mm=width_mm, height_mm=height_mm, label_index=label.index)

    monkeypatch.setattr(LabelaryRenderer, "render", fake_render)

    params = RenderParams(
        multi_mode="paginate",
        labels_per_page=1,
        fallback_width_mm=width_mm,
        fallback_height_mm=height_mm,
        split_grid=True,
    )
    output_path = tmp_path / "out.pdf"
    total_pages = render_output_pdf(content, params, output_path, cache_dir=None)

    assert total_pages == 6
