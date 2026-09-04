"""Dataclasses de configuração e resolução de tamanho de folha (presets)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Tamanhos de folha em mm (largura x altura, orientação retrato).
SHEET_PRESETS: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "LETTER": (215.9, 279.4),
    "CARTA": (215.9, 279.4),
}


class ConfigError(Exception):
    """Erro de configuração (parâmetros inconsistentes ou inválidos)."""


@dataclass
class SheetConfig:
    """Configuração da folha/página do PDF final."""

    width_mm: float
    height_mm: float
    orientation: str = "portrait"  # "portrait" ou "landscape"
    margin_top_mm: float = 10.0
    margin_bottom_mm: float = 10.0
    margin_left_mm: float = 10.0
    margin_right_mm: float = 10.0
    gap_x_mm: float = 2.0
    gap_y_mm: float = 2.0

    def __post_init__(self) -> None:
        if self.orientation not in ("portrait", "landscape"):
            raise ConfigError(
                f"orientation inválida: {self.orientation!r}. Use 'portrait' ou 'landscape'."
            )
        if self.orientation == "landscape":
            self.width_mm, self.height_mm = (
                max(self.width_mm, self.height_mm),
                min(self.width_mm, self.height_mm),
            )
        else:
            self.width_mm, self.height_mm = (
                min(self.width_mm, self.height_mm),
                max(self.width_mm, self.height_mm),
            )

        for name in (
            "margin_top_mm",
            "margin_bottom_mm",
            "margin_left_mm",
            "margin_right_mm",
            "gap_x_mm",
            "gap_y_mm",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ConfigError(f"{name} não pode ser negativo (recebido {value}).")

    @property
    def usable_width_mm(self) -> float:
        return self.width_mm - self.margin_left_mm - self.margin_right_mm

    @property
    def usable_height_mm(self) -> float:
        return self.height_mm - self.margin_top_mm - self.margin_bottom_mm


def resolve_sheet_size(sheet_size: str) -> tuple[float, float]:
    """Resolve um preset (ex: 'A4', 'Letter') ou tamanho custom ('210x297') em mm."""
    key = sheet_size.strip().upper()
    if key in SHEET_PRESETS:
        return SHEET_PRESETS[key]

    if "x" in sheet_size.lower():
        parts = sheet_size.lower().split("x")
        if len(parts) == 2:
            try:
                width = float(parts[0].strip())
                height = float(parts[1].strip())
                if width <= 0 or height <= 0:
                    raise ValueError
                return width, height
            except ValueError:
                pass

    raise ConfigError(
        f"sheet-size inválido: {sheet_size!r}. Use um preset ({', '.join(sorted(SHEET_PRESETS))}) "
        "ou um tamanho custom no formato 'LARGURAxALTURA' em mm (ex: '210x297')."
    )


@dataclass
class PrintConfig:
    """Configuração de resolução de impressão e tamanho de etiqueta (fallback)."""

    dpi: int = 203
    fallback_label_width_mm: Optional[float] = None
    fallback_label_height_mm: Optional[float] = None

    def __post_init__(self) -> None:
        if self.dpi <= 0:
            raise ConfigError(f"dpi deve ser positivo (recebido {self.dpi}).")
