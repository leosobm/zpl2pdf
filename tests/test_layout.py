import pytest

from zpl2pdf.config import ConfigError, SheetConfig, resolve_sheet_size
from zpl2pdf.layout import (
    LOW_SCALE_WARNING_THRESHOLD,
    LayoutEngine,
    calculate_grid,
    compute_cell_size_mm,
    compute_draw_size_mm,
    resolve_label_size_mm,
)
from zpl2pdf.parser import ZplLabel
from zpl2pdf.renderer import RenderedLabel, dpi_to_dpmm


def make_sheet(**overrides) -> SheetConfig:
    defaults = dict(
        width_mm=210.0,
        height_mm=297.0,
        orientation="portrait",
        margin_top_mm=10.0,
        margin_bottom_mm=10.0,
        margin_left_mm=10.0,
        margin_right_mm=10.0,
        gap_x_mm=2.0,
        gap_y_mm=2.0,
    )
    defaults.update(overrides)
    return SheetConfig(**defaults)


def _square_sheet():
    return make_sheet(
        width_mm=100.0, height_mm=100.0,
        margin_top_mm=0, margin_bottom_mm=0, margin_left_mm=0, margin_right_mm=0,
        gap_x_mm=0, gap_y_mm=0,
    )


def _label(index: int, raw: str = "^XA^XZ") -> ZplLabel:
    return ZplLabel(raw=raw, index=index, pw_dots=None, ll_dots=None, jm_mode=None)


def _rendered(index: int, width_mm: float = 30.0, height_mm: float = 20.0) -> RenderedLabel:
    return RenderedLabel(png_bytes=b"", width_mm=width_mm, height_mm=height_mm, label_index=index)


def test_resolve_sheet_size_preset_a4():
    assert resolve_sheet_size("A4") == (210.0, 297.0)


def test_resolve_sheet_size_custom():
    assert resolve_sheet_size("100x150") == (100.0, 150.0)


def test_resolve_sheet_size_invalid_raises():
    with pytest.raises(ConfigError):
        resolve_sheet_size("nao-existe")


def test_sheet_config_landscape_swaps_dimensions():
    sheet = SheetConfig(width_mm=210.0, height_mm=297.0, orientation="landscape")
    assert sheet.width_mm == 297.0
    assert sheet.height_mm == 210.0


def test_dpi_to_dpmm_known_presets():
    assert dpi_to_dpmm(203) == 8
    assert dpi_to_dpmm(300) == 12


def test_resolve_label_size_mm_from_zpl():
    label = ZplLabel(raw="^XA^PW812^LL609^XZ", index=0, pw_dots=812, ll_dots=609, jm_mode=None)
    width_mm, height_mm = resolve_label_size_mm(label, dpmm=8, fallback_width_mm=None, fallback_height_mm=None)
    assert width_mm == pytest.approx(101.5)
    assert height_mm == pytest.approx(76.125)


def test_resolve_label_size_mm_fallback_used():
    label = _label(0)
    width_mm, height_mm = resolve_label_size_mm(
        label, dpmm=8, fallback_width_mm=100.0, fallback_height_mm=50.0
    )
    assert (width_mm, height_mm) == (100.0, 50.0)


def test_resolve_label_size_mm_missing_fallback_raises():
    label = _label(0)
    with pytest.raises(ConfigError):
        resolve_label_size_mm(label, dpmm=8, fallback_width_mm=None, fallback_height_mm=None)


# ---------------------------------------------------------------------------
# compute_cell_size_mm
# ---------------------------------------------------------------------------


def test_compute_cell_size_mm_no_margin_no_gap():
    sheet = _square_sheet()
    cell_w, cell_h = compute_cell_size_mm(sheet, cols=4, rows=2)
    assert cell_w == pytest.approx(25.0)
    assert cell_h == pytest.approx(50.0)


def test_compute_cell_size_mm_accounts_for_margin_and_gap():
    # usable: 190x277mm; 6 cols com 5 gaps de 2mm entre elas
    sheet = make_sheet()
    cell_w, cell_h = compute_cell_size_mm(sheet, cols=6, rows=4)
    assert cell_w == pytest.approx((190.0 - 5 * 2.0) / 6)
    assert cell_h == pytest.approx((277.0 - 3 * 2.0) / 4)


# ---------------------------------------------------------------------------
# compute_draw_size_mm
# ---------------------------------------------------------------------------


def test_compute_draw_size_mm_preserves_aspect_ratio():
    # célula 50x50, etiqueta 30x20 (aspect 1.5) -> limitada pela largura
    draw_w, draw_h = compute_draw_size_mm(50.0, 50.0, 30.0, 20.0, stretch=False)
    expected_scale = 50.0 / 30.0
    assert draw_w == pytest.approx(30.0 * expected_scale)
    assert draw_h == pytest.approx(20.0 * expected_scale)
    assert draw_w / draw_h == pytest.approx(30.0 / 20.0)


def test_compute_draw_size_mm_stretch_fills_cell():
    draw_w, draw_h = compute_draw_size_mm(50.0, 30.0, 30.0, 20.0, stretch=True)
    assert (draw_w, draw_h) == (50.0, 30.0)


# ---------------------------------------------------------------------------
# calculate_grid
# ---------------------------------------------------------------------------


def test_calculate_grid_perfect_square_count():
    sheet = make_sheet()
    cols, rows = calculate_grid(16, sheet, label_width_mm=30.0, label_height_mm=20.0)
    assert (cols, rows) == (4, 4)


def test_calculate_grid_prefers_balanced_rectangle_over_elongated():
    sheet = make_sheet()
    cols, rows = calculate_grid(24, sheet, label_width_mm=30.0, label_height_mm=20.0)
    # o par de divisores mais equilibrado de 24 é (4,6)/(6,4); não deve
    # degenerar em algo como 1x24, 2x12, 3x8, 8x3, 12x2 ou 24x1.
    assert {cols, rows} == {4, 6}
    assert abs(cols - rows) == 2


def test_calculate_grid_never_raises_for_large_quantity():
    # antes isso "não cabia" e levantava erro; agora deve sempre calcular
    # uma grade (a etiqueta será redimensionada para caber).
    sheet = make_sheet()
    cols, rows = calculate_grid(500, sheet, label_width_mm=30.0, label_height_mm=20.0)
    assert cols * rows == 500


def test_calculate_grid_one_label():
    sheet = make_sheet()
    cols, rows = calculate_grid(1, sheet, label_width_mm=30.0, label_height_mm=20.0)
    assert (cols, rows) == (1, 1)


def test_calculate_grid_prime_count_has_no_alternative():
    sheet = make_sheet()
    cols, rows = calculate_grid(7, sheet, label_width_mm=30.0, label_height_mm=20.0)
    assert {cols, rows} == {1, 7}


def test_calculate_grid_invalid_count_raises():
    sheet = make_sheet()
    with pytest.raises(ConfigError):
        calculate_grid(0, sheet, label_width_mm=30.0, label_height_mm=20.0)


def test_calculate_grid_invalid_label_size_raises():
    sheet = make_sheet()
    with pytest.raises(ConfigError):
        calculate_grid(10, sheet, label_width_mm=0.0, label_height_mm=20.0)


# ---------------------------------------------------------------------------
# LayoutEngine — geometria da grade
# ---------------------------------------------------------------------------


def test_layout_engine_fit_scale_for_matches_limiting_dimension():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=2, rows=2)  # células 50x50
    assert engine.fit_scale_for(30.0, 20.0) == pytest.approx(min(50.0 / 30.0, 50.0 / 20.0))


def test_layout_engine_low_scale_triggers_warning_threshold():
    # grade com muitas colunas -> células estreitas -> escala bem menor que 1
    sheet = make_sheet()  # A4, usable 190x277
    engine = LayoutEngine(sheet, cols=40, rows=5)
    assert engine.fit_scale_for(30.0, 20.0) < LOW_SCALE_WARNING_THRESHOLD


def test_layout_engine_invalid_cols_rows_raises():
    sheet = make_sheet()
    with pytest.raises(ConfigError):
        LayoutEngine(sheet, cols=0, rows=1)


def test_layout_engine_cell_positions_top_left_first():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=2, rows=2)
    positions = engine.cell_positions()
    first = positions[0]
    assert first.col == 0 and first.row == 0
    assert first.x_mm == pytest.approx(0.0)
    assert first.y_mm == pytest.approx(50.0)
    last = positions[-1]
    assert last.col == 1 and last.row == 1
    assert last.x_mm == pytest.approx(50.0)
    assert last.y_mm == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# LayoutEngine.place_single — Tipo 1: etiqueta única repetida
# ---------------------------------------------------------------------------


def test_place_single_preserves_aspect_ratio_and_centers():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=2, rows=2, stretch=False)
    rendered = _rendered(0, 30.0, 20.0)
    placements = engine.place_single(rendered, pages=1)

    first = placements[0]
    expected_scale = 50.0 / 30.0
    assert first.width_mm == pytest.approx(30.0 * expected_scale)
    assert first.height_mm == pytest.approx(20.0 * expected_scale)
    cell = engine.cell_positions()[0]
    expected_offset_x = (cell.width_mm - first.width_mm) / 2
    expected_offset_y = (cell.height_mm - first.height_mm) / 2
    assert first.x_mm == pytest.approx(cell.x_mm + expected_offset_x)
    assert first.y_mm == pytest.approx(cell.y_mm + expected_offset_y)


def test_place_single_stretch_fills_entire_cell():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=2, rows=2, stretch=True)
    rendered = _rendered(0, 30.0, 20.0)
    placements = engine.place_single(rendered, pages=1)

    first = placements[0]
    assert first.width_mm == pytest.approx(50.0)
    assert first.height_mm == pytest.approx(50.0)
    cell = engine.cell_positions()[0]
    assert first.x_mm == pytest.approx(cell.x_mm)
    assert first.y_mm == pytest.approx(cell.y_mm)


def test_place_single_repeats_same_label_across_pages():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=2, rows=2)
    rendered = _rendered(0, 30.0, 20.0)

    placements = engine.place_single(rendered, pages=3)
    assert len(placements) == 12
    pages = {p.page_index for p in placements}
    assert pages == {0, 1, 2}
    assert all(sum(1 for p in placements if p.page_index == pg) == 4 for pg in pages)
    assert all(p.rendered_label is rendered for p in placements)


def test_place_single_invalid_pages_raises():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=1, rows=1)
    with pytest.raises(ConfigError):
        engine.place_single(_rendered(0), pages=0)


def test_place_single_no_overlap_within_a_page():
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=3, rows=3, stretch=True)
    rendered = _rendered(0, 30.0, 20.0)
    placements = engine.place_single(rendered, pages=1)

    boxes = [(p.x_mm, p.y_mm, p.x_mm + p.width_mm, p.y_mm + p.height_mm) for p in placements]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ax0, ay0, ax1, ay1 = boxes[i]
            bx0, by0, bx1, by1 = boxes[j]
            overlap_x = ax0 < bx1 - 1e-9 and bx0 < ax1 - 1e-9
            overlap_y = ay0 < by1 - 1e-9 and by0 < ay1 - 1e-9
            assert not (overlap_x and overlap_y)


# ---------------------------------------------------------------------------
# LayoutEngine.place_sequence — Tipo 2: múltiplas etiquetas distintas
# (Modo A "caber em 1 página" e Modo B "paginar" usam o mesmo método —
# só muda o tamanho de grade escolhido por quem chama).
# ---------------------------------------------------------------------------


def test_place_sequence_modo_a_fits_all_in_one_page():
    # 6 etiquetas distintas, grade dimensionada para exatamente 6 -> 1 página
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=3, rows=2)  # 6 células
    rendered_labels = [_rendered(i) for i in range(6)]

    placements = engine.place_sequence(rendered_labels)
    assert len(placements) == 6
    assert {p.page_index for p in placements} == {0}
    # cada etiqueta aparece exatamente uma vez, na ordem
    assert [p.rendered_label.label_index for p in placements] == list(range(6))


def test_place_sequence_modo_b_paginates_with_ceil():
    # 7 etiquetas, grade dimensionada para 3 (labels_per_page) -> ceil(7/3)=3 páginas
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=3, rows=1)  # 3 células/página
    rendered_labels = [_rendered(i) for i in range(7)]

    placements = engine.place_sequence(rendered_labels)
    pages = {p.page_index for p in placements}
    assert pages == {0, 1, 2}
    assert sum(1 for p in placements if p.page_index == 0) == 3
    assert sum(1 for p in placements if p.page_index == 1) == 3
    assert sum(1 for p in placements if p.page_index == 2) == 1  # última página parcial


def test_place_sequence_partial_last_page_uses_same_cell_size_as_full_pages():
    # a etiqueta sobrando na última página deve ocupar uma célula do MESMO
    # tamanho das páginas cheias — não recalculada para uma grade menor.
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=3, rows=2, stretch=True)  # 6 células/página -> 50/3 x 50/2
    rendered_labels = [_rendered(i, 10.0, 10.0) for i in range(7)]  # 7ª sobra sozinha na pág. 2

    placements = engine.place_sequence(rendered_labels)
    full_page_sizes = {(p.width_mm, p.height_mm) for p in placements if p.page_index == 0}
    partial_page = [p for p in placements if p.page_index == 1]

    assert len(partial_page) == 1
    assert len(full_page_sizes) == 1  # todas as células da página cheia têm o mesmo tamanho
    # a etiqueta da página parcial usa exatamente o mesmo tamanho de célula
    assert (partial_page[0].width_mm, partial_page[0].height_mm) == next(iter(full_page_sizes))
    # e fica na primeira posição da grade (canto superior-esquerdo), não centralizada
    # num espaço menor recalculado
    first_cell = engine.cell_positions()[0]
    assert partial_page[0].x_mm == pytest.approx(first_cell.x_mm)
    assert partial_page[0].y_mm == pytest.approx(first_cell.y_mm)


def test_place_sequence_unused_cells_on_partial_page_are_simply_absent():
    # 7 etiquetas / grade de 6 -> página 2 tem só 1 placement (não 6 com espaços
    # "vazios" representados); as demais posições simplesmente não recebem placement.
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=3, rows=2)
    rendered_labels = [_rendered(i) for i in range(7)]
    placements = engine.place_sequence(rendered_labels)
    assert sum(1 for p in placements if p.page_index == 1) == 1


def test_place_sequence_handles_labels_with_different_native_sizes():
    # cada etiqueta é redimensionada com base no PRÓPRIO aspect ratio original,
    # não no tamanho/proporção da primeira etiqueta do lote.
    sheet = _square_sheet()
    engine = LayoutEngine(sheet, cols=2, rows=1, stretch=False)  # células 50x100
    wide = _rendered(0, 40.0, 10.0)   # aspect 4:1 -> limitada pela largura
    tall = _rendered(1, 10.0, 40.0)   # aspect 1:4 -> limitada pela altura

    placements = engine.place_sequence([wide, tall])
    wide_placement, tall_placement = placements

    # wide: scale = min(50/40, 100/10) = 1.25 -> 50x12.5
    assert wide_placement.width_mm == pytest.approx(50.0)
    assert wide_placement.height_mm == pytest.approx(12.5)
    # tall: scale = min(50/10, 100/40) = 2.5 -> 25x100
    assert tall_placement.width_mm == pytest.approx(25.0)
    assert tall_placement.height_mm == pytest.approx(100.0)
