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

No caso de múltiplas etiquetas, cada bloco pode trazer seu próprio comando
`^PQ<quantidade>` (Print Quantity — comum em exportações de picking/
expedição, onde cada etiqueta de produto tem uma quantidade diferente de
cópias). Essa quantidade (`ZplLabel.pq_quantity`, 1 se ausente) é sempre
respeitada: a grade e a paginação são dimensionadas pelo total de cópias
físicas (soma de todos os `^PQ`), não pelo número de designs distintos —
mas cada etiqueta ainda é renderizada uma única vez (cache por hash do
conteúdo) e reaproveitada nas cópias necessárias.

Alguns exportadores (ex: etiquetas de picking da Shopee) vão além: agrupam
várias etiquetas físicas distintas numa única imagem `~DG`/`^GF` — uma
"folha" já pré-montada pela origem, sem nenhum comando ZPL indicando a
quantidade real (ela só existe nos pixels da imagem, não no texto) e,
frequentemente, sem uma grade uniforme entre folhas (a última folha de um
lote normalmente tem menos linhas preenchidas que as anteriores).
`RenderParams.split_grid` (`False` por padrão = sem efeito) ativa a
detecção automática dessa grade a partir dos próprios pixels — sem exigir
que o usuário informe colunas/linhas — ver `renderer.split_rendered_label`.
Como a divisão só é possível depois de ter a imagem renderizada (e a
quantidade real de sub-etiquetas varia de folha para folha), o total
mostrado em `AnalysisResult` antes de renderizar NÃO inclui essa divisão —
é só a contagem de blocos/`^PQ` do arquivo, marcada como estimativa
(`is_output_estimated`); o total exato só sai depois de `render_output_pdf`.
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
from .renderer import LabelCache, LabelaryRenderer, RenderedLabel, dpi_to_dpmm, split_rendered_label

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
    split_grid: bool = False

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
    is_output_estimated: bool = False


def count_labels(content: str) -> int:
    """Conta quantas etiquetas distintas o arquivo tem, sem calcular grade
    nem chamar o Labelary — usado pela UI para decidir se mostra os campos
    de "etiqueta única" ou os de "múltiplas etiquetas" antes de conhecer o
    resto dos parâmetros (que dependem dessa escolha)."""
    return len(parse_zpl_content(content))


def _total_physical_quantity(labels: list[ZplLabel]) -> int:
    """Soma das quantidades ^PQ de cada etiqueta (1 por etiqueta sem ^PQ)."""
    return sum(label.pq_quantity for label in labels)


def total_physical_quantity(content: str) -> int:
    """Soma das quantidades ^PQ de todas as etiquetas do arquivo (1 por
    etiqueta sem ^PQ) — não chama o Labelary; usado pela UI para popular
    limites/defaults de "etiquetas por página" antes de conhecer o resto
    dos parâmetros de layout."""
    return _total_physical_quantity(parse_zpl_content(content))


def _expand_rendered(
    labels: list[ZplLabel], rendered: list[RenderedLabel], params: RenderParams
) -> list[RenderedLabel]:
    """Expande cada `RenderedLabel` em sua sequência final de placements:

    1. Divide a imagem automaticamente (detectando a grade real nos pixels
       — ver `renderer.split_rendered_label`), se `split_grid` estiver
       ativo; sem isso, é um no-op (1 imagem).
    2. Repete cada sub-etiqueta resultante `pq_quantity` vezes (comando
       `^PQ` da etiqueta original, 1 se ausente).
    """
    expanded: list[RenderedLabel] = []
    for label, item in zip(labels, rendered):
        pieces = split_rendered_label(item, params.split_grid)
        for piece in pieces:
            expanded.extend([piece] * label.pq_quantity)
    return expanded


def _prefix_covering_quantity(
    labels: list[ZplLabel],
    resolved_sizes: list[tuple[float, float]],
    target_count: int,
) -> tuple[list[ZplLabel], list[tuple[float, float]]]:
    """Menor prefixo de `labels` cuja soma de `pq_quantity` alcança
    `target_count` — usado para renderizar só o necessário para a 1ª página
    no modo `paginate`, mesmo quando etiquetas têm quantidades > 1."""
    selected_labels: list[ZplLabel] = []
    selected_sizes: list[tuple[float, float]] = []
    total = 0
    for label, size in zip(labels, resolved_sizes):
        if total >= target_count:
            break
        selected_labels.append(label)
        selected_sizes.append(size)
        total += label.pq_quantity
    return selected_labels, selected_sizes


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
    """Retorna (mode, grid_count, total_labels_output, computed_pages).

    `total_labels_output` é a quantidade FÍSICA de etiquetas (soma dos
    `^PQ` de cada uma, ou 1 por etiqueta sem `^PQ`) — não o número de
    designs distintos no arquivo, e NÃO inclui a divisão automática de
    imagens agrupadas (`params.split_grid`): essa divisão só é possível
    depois de renderizar (ver `AnalysisResult.is_output_estimated`)."""
    if len(labels) == 1:
        return "single", params.labels_per_page, params.labels_per_page * params.pages, params.pages

    if params.multi_mode not in MULTI_MODE_CHOICES:
        raise ConfigError(
            f"O arquivo contém {len(labels)} etiquetas diferentes. Informe multi_mode "
            "'fit-one-page' (cabe tudo em 1 página) ou 'paginate' (distribui em páginas "
            "de labels_per_page etiquetas cada)."
        )

    total_physical = _total_physical_quantity(labels)

    if params.multi_mode == "fit-one-page":
        return "fit-one-page", total_physical, total_physical, 1

    grid_count = params.labels_per_page
    computed_pages = math.ceil(total_physical / grid_count)
    return "paginate", grid_count, total_physical, computed_pages


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
    cache = LabelCache(cache_dir) if cache_dir else None
    renderer = LabelaryRenderer(cache=cache)

    ref_width_mm, ref_height_mm = resolved_sizes[0]
    if params.split_grid and mode != "single":
        # Renderiza a 1ª etiqueta já aqui para aprender o tamanho REAL de
        # uma sub-etiqueta (a grade da imagem só se conhece depois de
        # renderizar+dividir) — essa chamada fica em cache e é reaproveitada
        # pelo render completo logo depois, sem custo extra de API. Em
        # "caber tudo em 1 página", também usa a contagem de pedaços dessa
        # amostra como estimativa (empírica, não um chute) de quantas
        # sub-etiquetas cada bloco costuma gerar, para dimensionar a grade
        # com folga — divergências (última folha de um lote, parcial) só
        # fazem a página transbordar normalmente, sem perder etiquetas.
        sample_rendered = renderer.render(labels[0], dpmm, *resolved_sizes[0])
        sample_pieces = split_rendered_label(sample_rendered, enabled=True)
        ref_width_mm, ref_height_mm = sample_pieces[0].width_mm, sample_pieces[0].height_mm
        if mode == "fit-one-page":
            grid_count = _total_physical_quantity(labels) * len(sample_pieces)

    cols, rows = calculate_grid(grid_count, sheet, ref_width_mm, ref_height_mm)
    engine = LayoutEngine(sheet, cols, rows, stretch=params.stretch)

    return labels, resolved_sizes, dpmm, mode, grid_count, sheet, engine, renderer


def analyze(content: str, params: RenderParams) -> AnalysisResult:
    """Calcula grade/escala a partir do ZPL e dos parâmetros, sem chamar o
    Labelary — seguro para rodar a cada mudança de parâmetro na UI.

    Com `params.split_grid` ativo, a divisão de imagens agrupadas não entra
    nesse cálculo (só é possível depois de renderizar) — os totais mostrados
    são os valores ANTES de dividir, marcados como estimativa
    (`is_output_estimated=True`): o total final pode ser bem maior."""
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
        is_output_estimated=params.split_grid and mode != "single",
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
        placements = engine.place_sequence(_expand_rendered(labels, rendered_labels, params))
    elif params.split_grid:
        # paginate + split: cada bloco pode virar várias sub-etiquetas, só
        # descoberto ao renderizar+dividir — acumula incrementalmente até
        # cobrir a 1ª página (grid_count), sem gastar chamadas de API além
        # do necessário.
        expanded: list[RenderedLabel] = []
        rendered_count = 0
        for label, size in zip(labels, resolved_sizes):
            if len(expanded) >= grid_count:
                break
            rendered_count += 1
            if on_progress is not None:
                on_progress(rendered_count, len(labels))
            rendered_one = renderer.render(label, dpmm, *size)
            for piece in split_rendered_label(rendered_one, params.split_grid):
                expanded.extend([piece] * label.pq_quantity)
        placements = engine.place_sequence(expanded)
    else:  # paginate: só o necessário para cobrir a 1ª página (grid_count
        # etiquetas físicas), considerando ^PQ — sem gastar chamadas de API
        # nas etiquetas das páginas seguintes.
        subset, subset_sizes = _prefix_covering_quantity(labels, resolved_sizes, grid_count)
        rendered_labels = render_all(subset, subset_sizes, dpmm, renderer, on_progress)
        placements = engine.place_sequence(_expand_rendered(subset, rendered_labels, params))

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
        placements = engine.place_sequence(_expand_rendered(labels, rendered_labels, params))

    return build_pdf(placements, sheet, output_path)
