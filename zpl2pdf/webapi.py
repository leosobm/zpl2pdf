"""Orquestração compartilhada entre a UI Streamlit (`app.py`) e a API
serverless do Vercel (`api/index.py`): a partir do conteúdo bruto de um
arquivo ZPL e dos parâmetros de folha/impressão/distribuição, calcula a
grade (sem gastar chamadas à API) e renderiza a prévia da 1ª página ou o PDF
final. Não depende de nenhum framework web (Streamlit, Flask, ...) — só dos
outros módulos do pacote (`parser`, `config`, `layout`, `renderer`,
`pdf_builder`).

Detecta automaticamente o tipo de arquivo:

- **Uma única etiqueta**: repetida `labels_per_page` vezes por página, ao
  longo de `pages` páginas idênticas.
- **Várias etiquetas distintas**: exige `multi_mode` — `fit-one-page`
  (grade dimensionada para o total de etiquetas do arquivo, em 1 página) ou
  `paginate` (distribui em páginas de `labels_per_page` etiquetas cada; o
  número de páginas é calculado automaticamente).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from .config import ConfigError, SheetConfig, resolve_sheet_size
from .layout import (
    LOW_SCALE_WARNING_THRESHOLD,
    LabelPlacement,
    LayoutEngine,
    calculate_grid,
    resolve_label_size_mm,
)
from .parser import ZplLabel, parse_zpl_content
from .pdf_builder import build_pdf
from .renderer import LabelCache, LabelaryRenderer, RenderedLabel, dpi_to_dpmm

MULTI_MODE_CHOICES = ("fit-one-page", "paginate")

# Callback opcional chamado antes de renderizar cada etiqueta: (índice 1-based, total).
ProgressCallback = Callable[[int, int], None]


@dataclass
class RenderParams:
    """Espelha os parâmetros configuráveis pelo usuário (sidebar do Streamlit
    ou formulário web): tamanho/orientação da folha, margens, gap, dpi,
    fallback de tamanho de etiqueta, modo de redimensionamento e a
    quantidade/distribuição de etiquetas."""

    sheet_size: str = "A4"
    orientation: str = "portrait"
    margin_top_mm: float = 10.0
    margin_bottom_mm: float = 10.0
    margin_left_mm: float = 10.0
    margin_right_mm: float = 10.0
    gap_x_mm: float = 2.0
    gap_y_mm: float = 2.0
    dpi: int = 203
    fallback_width_mm: Optional[float] = None
    fallback_height_mm: Optional[float] = None
    stretch: bool = False
    labels_per_page: int = 1
    pages: int = 1
    multi_mode: Optional[str] = None

    def build_sheet(self) -> SheetConfig:
        width_mm, height_mm = resolve_sheet_size(self.sheet_size)
        return SheetConfig(
            width_mm=width_mm,
            height_mm=height_mm,
            orientation=self.orientation,
            margin_top_mm=self.margin_top_mm,
            margin_bottom_mm=self.margin_bottom_mm,
            margin_left_mm=self.margin_left_mm,
            margin_right_mm=self.margin_right_mm,
            gap_x_mm=self.gap_x_mm,
            gap_y_mm=self.gap_y_mm,
        )


@dataclass(frozen=True)
class AnalysisResult:
    """Resultado do cálculo de grade/escala — não renderiza nenhuma imagem,
    então não consome chamadas à API do Labelary."""

    mode: str  # "single" | "fit-one-page" | "paginate"
    total_labels_in_file: int
    total_labels_output: int
    computed_pages: int
    cols: int
    rows: int
    cell_width_mm: float
    cell_height_mm: float
    scale: float
    low_scale_warning: bool


def count_labels(content: str) -> int:
    """Conta quantas etiquetas distintas o arquivo tem, sem calcular grade
    nem chamar o Labelary — usado pela UI para decidir se mostra os campos
    de "etiqueta única" ou os de "múltiplas etiquetas" antes de conhecer o
    resto dos parâmetros (que dependem dessa escolha)."""
    return len(parse_zpl_content(content))


def _labels_and_sizes(
    content: str, params: RenderParams
) -> tuple[list[ZplLabel], list[tuple[float, float]], int]:
    labels = parse_zpl_content(content)
    dpmm = dpi_to_dpmm(params.dpi)
    resolved_sizes = [
        resolve_label_size_mm(label, dpmm, params.fallback_width_mm, params.fallback_height_mm)
        for label in labels
    ]
    return labels, resolved_sizes, dpmm


def _grid_count_and_output(
    labels: list[ZplLabel], params: RenderParams
) -> tuple[str, int, int, int]:
    """Retorna (mode, grid_count, total_labels_output, computed_pages)."""
    if len(labels) == 1:
        return "single", params.labels_per_page, params.labels_per_page * params.pages, params.pages

    if params.multi_mode not in MULTI_MODE_CHOICES:
        raise ConfigError(
            f"O arquivo contém {len(labels)} etiquetas diferentes. Informe multi_mode "
            "'fit-one-page' (cabe tudo em 1 página) ou 'paginate' (distribui em páginas "
            "de labels_per_page etiquetas cada)."
        )
    if params.multi_mode == "fit-one-page":
        return "fit-one-page", len(labels), len(labels), 1

    grid_count = params.labels_per_page
    computed_pages = math.ceil(len(labels) / grid_count)
    return "paginate", grid_count, len(labels), computed_pages


def _prepare(
    content: str, params: RenderParams, cache_dir: Optional[str]
) -> tuple[
    list[ZplLabel],
    list[tuple[float, float]],
    int,
    str,
    int,
    SheetConfig,
    LayoutEngine,
    LabelaryRenderer,
]:
    labels, resolved_sizes, dpmm = _labels_and_sizes(content, params)
    mode, grid_count, _total, _pages = _grid_count_and_output(labels, params)

    sheet = params.build_sheet()
    ref_width_mm, ref_height_mm = resolved_sizes[0]
    cols, rows = calculate_grid(grid_count, sheet, ref_width_mm, ref_height_mm)
    engine = LayoutEngine(sheet, cols, rows, stretch=params.stretch)

    cache = LabelCache(cache_dir) if cache_dir else None
    renderer = LabelaryRenderer(cache=cache)

    return labels, resolved_sizes, dpmm, mode, grid_count, sheet, engine, renderer


def analyze(content: str, params: RenderParams) -> AnalysisResult:
    """Calcula grade/escala a partir do ZPL e dos parâmetros, sem chamar o
    Labelary — seguro para rodar a cada mudança de parâmetro na UI."""
    labels, resolved_sizes, _dpmm = _labels_and_sizes(content, params)
    mode, grid_count, total_labels_output, computed_pages = _grid_count_and_output(labels, params)

    sheet = params.build_sheet()
    ref_width_mm, ref_height_mm = resolved_sizes[0]
    cols, rows = calculate_grid(grid_count, sheet, ref_width_mm, ref_height_mm)
    engine = LayoutEngine(sheet, cols, rows, stretch=params.stretch)
    worst_scale = min(engine.fit_scale_for(w, h) for w, h in resolved_sizes)

    return AnalysisResult(
        mode=mode,
        total_labels_in_file=len(labels),
        total_labels_output=total_labels_output,
        computed_pages=computed_pages,
        cols=cols,
        rows=rows,
        cell_width_mm=engine.cell_width_mm,
        cell_height_mm=engine.cell_height_mm,
        scale=worst_scale,
        low_scale_warning=worst_scale < LOW_SCALE_WARNING_THRESHOLD,
    )


def render_all(
    labels: list[ZplLabel],
    resolved_sizes: list[tuple[float, float]],
    dpmm: int,
    renderer: LabelaryRenderer,
    on_progress: Optional[ProgressCallback] = None,
) -> list[RenderedLabel]:
    """Renderiza uma lista de etiquetas, uma a uma (a ordem importa para o
    rate limiting do `renderer`). Chama `on_progress(i, total)` (1-based)
    antes de cada uma, se informado — usado pela UI para uma barra de
    progresso; sem efeito na renderização em si."""
    total = len(labels)
    rendered = []
    for i, (label, (w, h)) in enumerate(zip(labels, resolved_sizes), start=1):
        if on_progress is not None:
            on_progress(i, total)
        rendered.append(renderer.render(label, dpmm, w, h))
    return rendered


def build_preview_image(
    sheet: SheetConfig, placements: list[LabelPlacement], target_width_px: int = 700
) -> Image.Image:
    """Monta uma imagem raster da 1ª página, só para conferência visual do layout."""
    scale = target_width_px / sheet.width_mm
    height_px = max(1, round(sheet.height_mm * scale))
    canvas = Image.new("RGB", (target_width_px, height_px), "white")

    for placement in placements:
        if placement.page_index != 0:
            continue
        label_img = Image.open(io.BytesIO(placement.rendered_label.png_bytes)).convert("RGB")
        w_px = max(1, round(placement.width_mm * scale))
        h_px = max(1, round(placement.height_mm * scale))
        label_img = label_img.resize((w_px, h_px))
        x_px = round(placement.x_mm * scale)
        y_px = height_px - round((placement.y_mm + placement.height_mm) * scale)
        canvas.paste(label_img, (x_px, y_px))

    return canvas


def render_preview(
    content: str,
    params: RenderParams,
    cache_dir: Optional[str] = "cache",
    on_progress: Optional[ProgressCallback] = None,
) -> bytes:
    """Renderiza só o necessário para a 1ª página (no modo `paginate`, só o
    subconjunto que cabe nela) e devolve um PNG (bytes) da folha inteira."""
    labels, resolved_sizes, dpmm, mode, grid_count, sheet, engine, renderer = _prepare(
        content, params, cache_dir
    )

    if mode == "single":
        rendered = renderer.render(labels[0], dpmm, *resolved_sizes[0])
        placements = engine.place_single(rendered, pages=1)
    elif mode == "fit-one-page":
        rendered_labels = render_all(labels, resolved_sizes, dpmm, renderer, on_progress)
        placements = engine.place_sequence(rendered_labels)
    else:  # paginate: só a 1ª página, sem gastar chamadas de API nas restantes.
        subset = labels[:grid_count]
        subset_sizes = resolved_sizes[:grid_count]
        rendered_labels = render_all(subset, subset_sizes, dpmm, renderer, on_progress)
        placements = engine.place_sequence(rendered_labels)

    image = build_preview_image(sheet, placements)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_output_pdf(
    content: str,
    params: RenderParams,
    output_path: str | Path,
    cache_dir: Optional[str] = "cache",
    on_progress: Optional[ProgressCallback] = None,
) -> int:
    """Renderiza todas as etiquetas necessárias e monta o PDF final em
    `output_path`. Retorna o número total de páginas geradas."""
    labels, resolved_sizes, dpmm, mode, _grid_count, sheet, engine, renderer = _prepare(
        content, params, cache_dir
    )

    if mode == "single":
        rendered = renderer.render(labels[0], dpmm, *resolved_sizes[0])
        placements = engine.place_single(rendered, params.pages)
    else:
        rendered_labels = render_all(labels, resolved_sizes, dpmm, renderer, on_progress)
        placements = engine.place_sequence(rendered_labels)

    return build_pdf(placements, sheet, output_path)
