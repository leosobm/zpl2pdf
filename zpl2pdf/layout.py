"""Motor de layout: calcula uma grade (cols x rows) que comporta EXATAMENTE
uma quantidade de células na folha, e redimensiona etiquetas renderizadas
para caber em cada célula.

Diferente de uma abordagem que rejeita quantidades que não cabem no
tamanho real da etiqueta, aqui a quantidade pedida é sempre respeitada: a
etiqueta (já renderizada a partir do ZPL) é redimensionada — por padrão
mantendo a proporção original e centralizada na célula, ou esticada para
preencher 100% da célula se `stretch=True`.

`LayoutEngine` cuida só da geometria da grade (tamanho de célula, posições);
como cada célula é preenchida depende do cenário:

- `place_single`: repete UMA etiqueta renderizada em toda a grade, ao longo
  de N páginas idênticas (arquivo com uma única etiqueta de referência).
- `place_sequence`: distribui uma LISTA de etiquetas renderizadas
  distintas, uma por célula, em sequência — a mesma função cobre tanto
  "caber tudo em 1 página" (grade dimensionada para len(lista)) quanto
  "paginar" (grade dimensionada para labels_per_page; o restante
  transborda automaticamente para páginas seguintes, com a última página
  podendo ficar parcialmente preenchida, usando o mesmo tamanho de célula).

Todas as posições são calculadas em milímetros reais, com origem no canto
inferior esquerdo da folha, no mesmo sistema de coordenadas do reportlab.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import ConfigError, SheetConfig
from .parser import ZplLabel
from .renderer import RenderedLabel

# Piso defensivo para o tamanho de uma célula (mm), só para nunca passar
# largura/altura zero ou negativa ao reportlab em configurações patológicas
# (ex: gaps somados maiores que a área útil da folha).
MIN_CELL_MM = 1.0

# Abaixo deste fator de escala, o código de barras pode ficar difícil de
# ler fisicamente — usado para gerar um aviso não bloqueante na UI.
LOW_SCALE_WARNING_THRESHOLD = 0.5


def resolve_label_size_mm(
    label: ZplLabel,
    dpmm: int,
    fallback_width_mm: Optional[float],
    fallback_height_mm: Optional[float],
) -> tuple[float, float]:
    """Determina o tamanho físico ORIGINAL (mm) de uma etiqueta.

    Usa ^PW/^LL (convertidos via dpmm) quando presentes no ZPL; caso
    contrário, usa o tamanho de fallback informado pelo usuário. Esse é o
    tamanho "nativo" da etiqueta, usado tanto para pedir a imagem ao
    renderer quanto como referência de proporção ao calcular a grade.
    """
    if label.has_explicit_size():
        assert label.pw_dots is not None and label.ll_dots is not None
        return label.pw_dots / dpmm, label.ll_dots / dpmm

    if fallback_width_mm is None or fallback_height_mm is None:
        raise ConfigError(
            f"Etiqueta {label.index + 1} não define ^PW/^LL e nenhum tamanho de "
            "fallback foi informado. Use --label-width/--label-height (ou o "
            "equivalente no config) para definir o tamanho manualmente."
        )
    return fallback_width_mm, fallback_height_mm


def compute_cell_size_mm(sheet: SheetConfig, cols: int, rows: int) -> tuple[float, float]:
    """Tamanho (mm) de cada célula da grade: área útil da folha menos os
    gaps entre células, dividida por cols/rows. Pode retornar valores não
    positivos se a grade pedida for maior do que a área útil comporta
    fisicamente — quem desenha decide como lidar com isso (ver MIN_CELL_MM)."""
    total_gap_x = max(cols - 1, 0) * sheet.gap_x_mm
    total_gap_y = max(rows - 1, 0) * sheet.gap_y_mm
    cell_width_mm = (sheet.usable_width_mm - total_gap_x) / cols
    cell_height_mm = (sheet.usable_height_mm - total_gap_y) / rows
    return cell_width_mm, cell_height_mm


def compute_draw_size_mm(
    cell_width_mm: float,
    cell_height_mm: float,
    label_width_mm: float,
    label_height_mm: float,
    stretch: bool,
) -> tuple[float, float]:
    """Tamanho final (mm) de uma etiqueta desenhada dentro de uma célula.

    Sem `stretch`, preserva a proporção original (`fit_scale_for` aplicado
    uniformemente aos dois eixos); com `stretch`, ocupa 100% da célula.
    """
    if stretch:
        return cell_width_mm, cell_height_mm
    scale = min(cell_width_mm / label_width_mm, cell_height_mm / label_height_mm)
    return label_width_mm * scale, label_height_mm * scale


def calculate_grid(
    labels_per_page: int,
    sheet: SheetConfig,
    label_width_mm: float,
    label_height_mm: float,
) -> tuple[int, int]:
    """Escolhe (cols, rows) com cols * rows == labels_per_page (a quantidade
    pedida é sempre respeitada exatamente, uma célula por etiqueta).

    Critério em duas etapas:

    1. Entre todos os pares de divisores de `labels_per_page`, mantém só os
       que minimizam |cols - rows| — ou seja, a grade mais próxima possível
       de um retângulo equilibrado (quadrado), evitando arranjos degenerados
       como 1 coluna x N linhas quando uma divisão mais balanceada existe.
    2. Quando restam duas orientações empatadas nesse critério (ex: 4x6 e
       6x4), escolhe a que resulta na maior escala possível para encaixar a
       etiqueta preservando sua proporção original — nesse desempate, a
       proporção da etiqueta (e da folha) decide a orientação da grade.

    `label_width_mm`/`label_height_mm` servem apenas de referência para essa
    escolha de orientação (em arquivos com várias etiquetas distintas, use o
    tamanho da primeira etiqueta como referência); cada etiqueta é depois
    redimensionada individualmente para a célula com seu próprio tamanho.

    Não levanta erro por a quantidade "não caber" fisicamente: a etiqueta é
    sempre redimensionada depois para caber na célula calculada.
    """
    if labels_per_page < 1:
        raise ConfigError(f"labels_per_page deve ser >= 1 (recebido {labels_per_page}).")
    if label_width_mm <= 0 or label_height_mm <= 0:
        raise ConfigError(
            f"Tamanho de etiqueta inválido: {label_width_mm}x{label_height_mm}mm."
        )

    divisor_pairs = [
        (cols, labels_per_page // cols)
        for cols in range(1, labels_per_page + 1)
        if labels_per_page % cols == 0
    ]
    min_diff = min(abs(cols - rows) for cols, rows in divisor_pairs)
    candidates = [(cols, rows) for cols, rows in divisor_pairs if abs(cols - rows) == min_diff]

    best: Optional[tuple[float, int, int]] = None
    for cols, rows in candidates:
        cell_width_mm, cell_height_mm = compute_cell_size_mm(sheet, cols, rows)
        scale = min(cell_width_mm / label_width_mm, cell_height_mm / label_height_mm)
        if best is None or scale > best[0]:
            best = (scale, cols, rows)

    assert best is not None, "sempre há ao menos um par de divisores de labels_per_page"
    return best[1], best[2]


@dataclass(frozen=True)
class CellPosition:
    """Posição e tamanho (mm, origem inferior-esquerda) de uma célula da grade."""

    col: int
    row: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float


@dataclass(frozen=True)
class LabelPlacement:
    """Onde, em qual página, e em que tamanho uma etiqueta renderizada deve
    ser desenhada (já redimensionada para a célula)."""

    page_index: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    rendered_label: RenderedLabel


class LayoutEngine:
    """Geometria de uma grade (cols x rows) fixa na folha: tamanho de célula
    e posições. Não depende do tamanho de nenhuma etiqueta específica — quem
    preenche a grade (`place_single`/`place_sequence`) decide, célula a
    célula, como redimensionar a etiqueta renderizada que cai ali."""

    def __init__(self, sheet: SheetConfig, cols: int, rows: int, stretch: bool = False):
        if cols < 1 or rows < 1:
            raise ConfigError(f"cols e rows devem ser >= 1 (recebido cols={cols}, rows={rows}).")

        self.sheet = sheet
        self.cols = cols
        self.rows = rows
        self.stretch = stretch

        raw_cell_width_mm, raw_cell_height_mm = compute_cell_size_mm(sheet, cols, rows)
        self.cell_width_mm = max(raw_cell_width_mm, MIN_CELL_MM)
        self.cell_height_mm = max(raw_cell_height_mm, MIN_CELL_MM)

    @property
    def grid_capacity(self) -> int:
        return self.cols * self.rows

    def fit_scale_for(self, label_width_mm: float, label_height_mm: float) -> float:
        """Escala uniforme (preservando proporção) que uma etiqueta desse
        tamanho recebe nesta grade — também serve, mesmo em modo `stretch`,
        como indicador de "pior caso" de legibilidade."""
        return min(self.cell_width_mm / label_width_mm, self.cell_height_mm / label_height_mm)

    def cell_positions(self) -> list[CellPosition]:
        """Posição e tamanho (mm, canto inferior-esquerdo) de cada célula da grade."""
        sheet = self.sheet
        positions: list[CellPosition] = []
        for row in range(self.rows):
            top_y = sheet.height_mm - sheet.margin_top_mm - row * (
                self.cell_height_mm + sheet.gap_y_mm
            )
            bottom_y = top_y - self.cell_height_mm
            for col in range(self.cols):
                x = sheet.margin_left_mm + col * (self.cell_width_mm + sheet.gap_x_mm)
                positions.append(
                    CellPosition(
                        col=col, row=row, x_mm=x, y_mm=bottom_y,
                        width_mm=self.cell_width_mm, height_mm=self.cell_height_mm,
                    )
                )
        return positions

    def _place_in_cell(self, cell: CellPosition, rendered_label: RenderedLabel, page_index: int) -> LabelPlacement:
        draw_width_mm, draw_height_mm = compute_draw_size_mm(
            cell.width_mm, cell.height_mm,
            rendered_label.width_mm, rendered_label.height_mm,
            self.stretch,
        )
        offset_x_mm = (cell.width_mm - draw_width_mm) / 2
        offset_y_mm = (cell.height_mm - draw_height_mm) / 2
        return LabelPlacement(
            page_index=page_index,
            x_mm=cell.x_mm + offset_x_mm,
            y_mm=cell.y_mm + offset_y_mm,
            width_mm=draw_width_mm,
            height_mm=draw_height_mm,
            rendered_label=rendered_label,
        )

    def place_single(self, rendered_label: RenderedLabel, pages: int) -> list[LabelPlacement]:
        """Repete `rendered_label` em toda a grade, por `pages` páginas
        idênticas (arquivo com uma única etiqueta de referência)."""
        if pages < 1:
            raise ConfigError(f"pages deve ser >= 1 (recebido {pages}).")

        positions = self.cell_positions()
        placements: list[LabelPlacement] = []
        for page in range(pages):
            for cell in positions:
                placements.append(self._place_in_cell(cell, rendered_label, page))
        return placements

    def place_sequence(self, rendered_labels: list[RenderedLabel]) -> list[LabelPlacement]:
        """Distribui uma lista de etiquetas renderizadas distintas pelas
        células da grade, em sequência, transbordando para novas páginas
        conforme necessário. O tamanho de célula usado é sempre o da grade
        (fixado por `cols`/`rows` na construção) — inclusive na última
        página, mesmo que ela não preencha todas as células (o restante
        fica em branco, sem recalcular um tamanho maior para elas)."""
        positions = self.cell_positions()
        per_page = len(positions)
        if per_page == 0:
            raise ConfigError("A grade resultou em 0 células por página (cols/rows inválidos).")

        placements: list[LabelPlacement] = []
        for i, rendered_label in enumerate(rendered_labels):
            page_index = i // per_page
            cell = positions[i % per_page]
            placements.append(self._place_in_cell(cell, rendered_label, page_index))
        return placements
