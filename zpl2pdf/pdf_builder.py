"""Monta o PDF final a partir dos placements calculados pelo LayoutEngine."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from .layout import LabelPlacement
from .config import SheetConfig

logger = logging.getLogger(__name__)


def build_pdf(
    placements: list[LabelPlacement],
    sheet: SheetConfig,
    output_path: str | Path,
) -> int:
    """Desenha todas as etiquetas nas posições calculadas e salva o PDF.

    Retorna o número total de páginas geradas.
    """
    if not placements:
        raise ValueError("Nenhuma etiqueta para desenhar (lista de placements vazia).")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    page_width_pt = sheet.width_mm * mm
    page_height_pt = sheet.height_mm * mm

    c = canvas.Canvas(str(output_path), pagesize=(page_width_pt, page_height_pt))

    total_pages = max(p.page_index for p in placements) + 1
    current_page = -1
    image_cache: dict[int, ImageReader] = {}

    for placement in placements:
        if placement.page_index != current_page:
            if current_page >= 0:
                c.showPage()
            current_page = placement.page_index
            logger.info("Gerando página %d/%d...", current_page + 1, total_pages)

        png_bytes = placement.rendered_label.png_bytes
        image = image_cache.get(id(png_bytes))
        if image is None:
            image = ImageReader(io.BytesIO(png_bytes))
            image_cache[id(png_bytes)] = image

        c.drawImage(
            image,
            placement.x_mm * mm,
            placement.y_mm * mm,
            width=placement.width_mm * mm,
            height=placement.height_mm * mm,
            preserveAspectRatio=False,
            anchor="sw",
        )
        logger.debug(
            "  Etiqueta desenhada na página %d (x=%.1fmm, y=%.1fmm, %.1fx%.1fmm).",
            current_page + 1,
            placement.x_mm,
            placement.y_mm,
            placement.width_mm,
            placement.height_mm,
        )

    c.showPage()
    c.save()
    return total_pages
