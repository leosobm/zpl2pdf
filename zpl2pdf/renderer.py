"""Renderização de etiquetas ZPL em imagens PNG.

Define a interface `LabelRenderer` (para permitir trocar por um renderizador
offline no futuro) e a implementação padrão `LabelaryRenderer`, que usa a API
pública do Labelary (http://api.labelary.com).
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from .parser import ZplLabel

logger = logging.getLogger(__name__)

# dpmm suportados pela API pública do Labelary (ver documentação da API).
_SUPPORTED_DPMM = (6, 8, 12, 24)

# dpi comumente usados em impressoras térmicas -> dpmm nativo correspondente.
_DPI_TO_DPMM = {
    152: 6,
    203: 8,
    300: 12,
    600: 24,
}


class RenderError(Exception):
    """Erro ao renderizar uma etiqueta ZPL em imagem."""


def dpi_to_dpmm(dpi: int) -> int:
    """Converte dpi (dots per inch) para o dpmm suportado mais próximo pelo Labelary.

    1 polegada = 25.4 mm, então dpmm "ideal" = dpi / 25.4. Como a API do
    Labelary só aceita um conjunto discreto de valores (6, 8, 12, 24), o dpi
    informado é mapeado para o valor exato conhecido quando possível, ou
    arredondado para o dpmm suportado mais próximo, com aviso no console.
    """
    if dpi in _DPI_TO_DPMM:
        return _DPI_TO_DPMM[dpi]

    ideal_dpmm = dpi / 25.4
    closest = min(_SUPPORTED_DPMM, key=lambda d: abs(d - ideal_dpmm))
    logger.warning(
        "dpi=%s não corresponde a um preset exato do Labelary "
        "(dpmm ideal=%.2f). Usando dpmm=%s (o mais próximo suportado).",
        dpi,
        ideal_dpmm,
        closest,
    )
    return closest


@dataclass(frozen=True)
class RenderedLabel:
    """Resultado da renderização de uma etiqueta: imagem PNG + tamanho real em mm."""

    png_bytes: bytes
    width_mm: float
    height_mm: float
    label_index: int


class LabelRenderer(ABC):
    """Interface para renderizadores de etiqueta ZPL -> imagem PNG."""

    @abstractmethod
    def render(
        self,
        label: ZplLabel,
        dpmm: int,
        width_mm: float,
        height_mm: float,
    ) -> RenderedLabel:
        """Renderiza uma etiqueta ZPL em PNG, no tamanho físico dado (mm)."""
        raise NotImplementedError


class LabelCache:
    """Cache local de imagens renderizadas, indexado por hash do conteúdo ZPL."""

    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(zpl_raw: str, dpmm: int, width_mm: float, height_mm: float) -> str:
        payload = f"{zpl_raw}|{dpmm}|{width_mm:.3f}|{height_mm:.3f}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get(
        self, zpl_raw: str, dpmm: int, width_mm: float, height_mm: float
    ) -> Optional[bytes]:
        path = self.cache_dir / f"{self._key(zpl_raw, dpmm, width_mm, height_mm)}.png"
        if path.exists():
            return path.read_bytes()
        return None

    def put(
        self,
        zpl_raw: str,
        dpmm: int,
        width_mm: float,
        height_mm: float,
        png_bytes: bytes,
    ) -> None:
        path = self.cache_dir / f"{self._key(zpl_raw, dpmm, width_mm, height_mm)}.png"
        path.write_bytes(png_bytes)


class LabelaryRenderer(LabelRenderer):
    """Renderiza etiquetas via a API pública do Labelary.

    http://api.labelary.com/v1/printers/{dpmm}dpmm/labels/{width}x{height}/{index}/
    """

    BASE_URL = "http://api.labelary.com/v1/printers"

    def __init__(
        self,
        cache: Optional[LabelCache] = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.5,
    ):
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    def render(
        self,
        label: ZplLabel,
        dpmm: int,
        width_mm: float,
        height_mm: float,
    ) -> RenderedLabel:
        if self.cache is not None:
            cached = self.cache.get(label.raw, dpmm, width_mm, height_mm)
            if cached is not None:
                logger.info("Etiqueta %d: usando cache local.", label.index + 1)
                return RenderedLabel(
                    png_bytes=cached,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    label_index=label.index,
                )

        width_in = width_mm / 25.4
        height_in = height_mm / 25.4
        url = f"{self.BASE_URL}/{dpmm}dpmm/labels/{width_in:.2f}x{height_in:.2f}/0/"

        png_bytes = self._request_with_retry(url, label.raw, label.index)

        if self.cache is not None:
            self.cache.put(label.raw, dpmm, width_mm, height_mm, png_bytes)

        return RenderedLabel(
            png_bytes=png_bytes,
            width_mm=width_mm,
            height_mm=height_mm,
            label_index=label.index,
        )

    def _request_with_retry(self, url: str, zpl_raw: str, label_index: int) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    data=zpl_raw.encode("utf-8"),
                    headers={
                        "Accept": "image/png",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=self.timeout_seconds,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Etiqueta %d: falha de rede na tentativa %d/%d (%s).",
                    label_index + 1,
                    attempt,
                    self.max_retries,
                    exc,
                )
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = RenderError(
                        f"Labelary retornou HTTP {response.status_code} "
                        f"para etiqueta {label_index + 1}: {response.text[:200]}"
                    )
                    logger.warning(
                        "Etiqueta %d: HTTP %d na tentativa %d/%d.",
                        label_index + 1,
                        response.status_code,
                        attempt,
                        self.max_retries,
                    )
                else:
                    raise RenderError(
                        f"Falha ao renderizar etiqueta {label_index + 1} via Labelary "
                        f"(HTTP {response.status_code}): {response.text[:300]}"
                    )

            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_seconds * attempt)

        raise RenderError(
            f"Não foi possível renderizar a etiqueta {label_index + 1} após "
            f"{self.max_retries} tentativas. Verifique sua conexão com a "
            f"internet ou a disponibilidade da API do Labelary "
            f"(http://api.labelary.com). Último erro: {last_error}"
        )
