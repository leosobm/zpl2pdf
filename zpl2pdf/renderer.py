"""Renderização de etiquetas ZPL em imagens PNG.

Define a interface `LabelRenderer` (para permitir trocar por um renderizador
offline no futuro) e a implementação padrão `LabelaryRenderer`, que usa a API
pública do Labelary (http://api.labelary.com).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from PIL import Image

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

# O Labelary simula uma impressora real: se o ZPL enviado trouxer um ^PQ com
# quantidade > 1, ele tenta devolver uma cópia renderizada por unidade — e
# recusa (HTTP 413 "Maximum label count exceeded") quantidades grandes (ex:
# exportações de picking com ^PQ56, ^PQ72...). Como só precisamos de UMA
# imagem por etiqueta distinta (as cópias físicas são resolvidas depois, sem
# nova chamada à API — ver `webapi._expand_rendered`), a quantidade
# original é neutralizada antes de enviar/cachear.
_PQ_QUANTITY_PATTERN = re.compile(r"(\^PQ)\d+", re.IGNORECASE)


def _render_single_copy(zpl_raw: str) -> str:
    """Força `^PQ` (se presente) a pedir 1 única cópia ao Labelary."""
    return _PQ_QUANTITY_PATTERN.sub(r"\g<1>1", zpl_raw)


class RenderError(Exception):
    """Erro ao renderizar uma etiqueta ZPL em imagem."""


# O plano gratuito do Labelary aceita no máximo 3 requisições/segundo; usamos
# uma margem de segurança abaixo disso por padrão.
_DEFAULT_MAX_CALLS_PER_SECOND = 2.0


class RateLimiter:
    """Espaça chamadas para respeitar um limite de requisições/segundo.

    Implementado como um "token bucket" de capacidade 1: cada `wait()`
    bloqueia até que ao menos `1 / max_calls_per_second` segundos tenham se
    passado desde a chamada anterior liberada. Thread-safe, então funciona
    tanto para chamadas sequenciais quanto para chamadas feitas em paralelo
    a partir de múltiplas threads.
    """

    def __init__(self, max_calls_per_second: float = _DEFAULT_MAX_CALLS_PER_SECOND):
        if max_calls_per_second <= 0:
            raise ValueError("max_calls_per_second deve ser > 0.")
        self._min_interval = 1.0 / max_calls_per_second
        self._lock = threading.Lock()
        self._next_allowed_time = 0.0

    def wait(self) -> None:
        """Bloqueia (se necessário) até que a próxima chamada seja permitida."""
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed_time - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed_time = max(now, self._next_allowed_time) + self._min_interval


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


# Abaixo deste limiar de "branco" (0-255, escala de cinza) um pixel conta como
# fundo vazio; acima desta fração de pixels em branco, a célula inteira é
# descartada por não ter conteúdo desenhado.
_BLANK_WHITE_THRESHOLD = 250
_BLANK_CELL_RATIO = 0.995


def _is_blank_cell(cell: Image.Image) -> bool:
    """True se a célula não tem conteúdo visual (quase só fundo branco).

    Usado para descartar posições vazias de uma grade parcialmente
    preenchida (ex: a última folha de um lote, com menos etiquetas do que
    cabem na grade — ver `split_rendered_label`)."""
    histogram = cell.convert("L").histogram()
    total_pixels = sum(histogram)
    if total_pixels == 0:
        return True
    white_pixels = sum(histogram[_BLANK_WHITE_THRESHOLD:])
    return (white_pixels / total_pixels) >= _BLANK_CELL_RATIO


def split_rendered_label(
    rendered: RenderedLabel, cols: int, rows: int
) -> list[RenderedLabel]:
    """Recorta uma etiqueta renderizada em uma grade `cols x rows` de
    sub-etiquetas, descartando células em branco.

    Alguns exportadores (ex: a exportação de etiquetas de picking da
    Shopee) agrupam várias etiquetas físicas distintas numa única imagem
    `~DG`/`^GF` — uma "folha" pré-montada pela origem, sem nenhum comando
    ZPL indicando a quantidade (ela só existe nos pixels da imagem). Esta
    função assume uma grade fixa e uniforme dentro da imagem renderizada
    (`cols` colunas x `rows` linhas, todas do mesmo tamanho) — comum nesse
    tipo de exportação — e descarta células vazias (tipicamente a última
    "folha" de um lote, só parcialmente preenchida).

    Com `cols <= 1 and rows <= 1` (padrão), devolve `[rendered]` sem
    modificar nada — comportamento idêntico a antes desta funcionalidade."""
    if cols <= 1 and rows <= 1:
        return [rendered]
    if cols < 1 or rows < 1:
        raise RenderError(f"split_grid_cols/rows devem ser >= 1 (recebido {cols}x{rows}).")

    image = Image.open(io.BytesIO(rendered.png_bytes))
    width_px, height_px = image.size
    cell_width_px = width_px // cols
    cell_height_px = height_px // rows
    cell_width_mm = rendered.width_mm / cols
    cell_height_mm = rendered.height_mm / rows

    pieces: list[RenderedLabel] = []
    for row in range(rows):
        for col in range(cols):
            box = (
                col * cell_width_px,
                row * cell_height_px,
                (col + 1) * cell_width_px,
                (row + 1) * cell_height_px,
            )
            cell = image.crop(box)
            if _is_blank_cell(cell):
                continue
            buffer = io.BytesIO()
            cell.save(buffer, format="PNG")
            pieces.append(
                RenderedLabel(
                    png_bytes=buffer.getvalue(),
                    width_mm=cell_width_mm,
                    height_mm=cell_height_mm,
                    label_index=rendered.label_index,
                )
            )

    if not pieces:
        raise RenderError(
            f"Etiqueta {rendered.label_index + 1}: a grade {cols}x{rows} não encontrou "
            "nenhuma célula com conteúdo — verifique se cols/rows correspondem à grade "
            "real da imagem."
        )
    return pieces


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

    O plano gratuito do Labelary aceita no máximo 3 requisições/segundo, então
    as chamadas são espaçadas por um `RateLimiter` (default: 2/s, com margem
    de segurança) e um HTTP 429 é automaticamente reenviado com backoff
    exponencial (até `max_retries` tentativas) antes de desistir.
    """

    BASE_URL = "http://api.labelary.com/v1/printers"

    # Teto do backoff exponencial, para não esperar tempo demais numa única
    # tentativa mesmo com max_retries alto.
    _MAX_BACKOFF_SECONDS = 30.0

    def __init__(
        self,
        cache: Optional[LabelCache] = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 5,
        retry_backoff_seconds: float = 1.0,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.rate_limiter = rate_limiter or RateLimiter()

    def render(
        self,
        label: ZplLabel,
        dpmm: int,
        width_mm: float,
        height_mm: float,
    ) -> RenderedLabel:
        # A quantidade física (`^PQ`) é resolvida depois, sem nova chamada à
        # API — o Labelary só precisa gerar 1 cópia, independente do ^PQ
        # original do arquivo (ver `_render_single_copy`).
        zpl_for_labelary = _render_single_copy(label.raw)

        if self.cache is not None:
            cached = self.cache.get(zpl_for_labelary, dpmm, width_mm, height_mm)
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

        png_bytes = self._request_with_retry(url, zpl_for_labelary, label.index)

        if self.cache is not None:
            self.cache.put(zpl_for_labelary, dpmm, width_mm, height_mm, png_bytes)

        return RenderedLabel(
            png_bytes=png_bytes,
            width_mm=width_mm,
            height_mm=height_mm,
            label_index=label.index,
        )

    def _backoff_seconds(self, attempt: int, retry_after_header: Optional[str]) -> float:
        """Backoff exponencial (1x, 2x, 4x, 8x, ... * retry_backoff_seconds), com teto.

        Se o servidor informar `Retry-After`, respeitamos esse valor quando
        for maior do que o backoff calculado.
        """
        exponential = min(
            self.retry_backoff_seconds * (2 ** (attempt - 1)),
            self._MAX_BACKOFF_SECONDS,
        )
        if retry_after_header:
            try:
                return max(exponential, float(retry_after_header))
            except ValueError:
                pass
        return exponential

    def _request_with_retry(self, url: str, zpl_raw: str, label_index: int) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            # Espaça as chamadas para respeitar o limite de requisições/segundo
            # do plano gratuito do Labelary (aplica-se também às retentativas).
            self.rate_limiter.wait()
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
                backoff = self._backoff_seconds(attempt, None)
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code == 429:
                    last_error = RenderError(
                        f"Labelary retornou HTTP 429 (limite de requisições excedido) "
                        f"para etiqueta {label_index + 1}."
                    )
                    backoff = self._backoff_seconds(attempt, response.headers.get("Retry-After"))
                    logger.warning(
                        "Etiqueta %d: HTTP 429 (rate limit) na tentativa %d/%d — "
                        "aguardando %.1fs antes de tentar novamente.",
                        label_index + 1,
                        attempt,
                        self.max_retries,
                        backoff,
                    )
                elif response.status_code in (500, 502, 503, 504):
                    last_error = RenderError(
                        f"Labelary retornou HTTP {response.status_code} "
                        f"para etiqueta {label_index + 1}: {response.text[:200]}"
                    )
                    backoff = self._backoff_seconds(attempt, None)
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
                time.sleep(backoff)

        raise RenderError(
            f"Não foi possível renderizar a etiqueta {label_index + 1} após "
            f"{self.max_retries} tentativas — desistindo para não gerar um PDF "
            f"incompleto. Verifique sua conexão com a internet ou a "
            f"disponibilidade da API do Labelary (http://api.labelary.com) e "
            f"tente novamente mais tarde. Último erro: {last_error}"
        )
