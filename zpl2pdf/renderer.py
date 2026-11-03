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


# Distância (em mm) usada para decidir se um espaço em branco entre duas
# faixas de conteúdo é só a quebra de linha natural DENTRO de uma mesma
# etiqueta (ex: o espaço entre o QR code e a última linha de texto) ou um
# corredor de verdade ENTRE duas etiquetas distintas da grade. Calibrado
# contra o arquivo real de picking da Shopee usado para validar este
# recurso: quebras de linha internas medem ~0,75-0,9mm, o corredor entre
# colunas mede ~1,6mm e o corredor entre linhas mede ~3,4mm — 1,0mm fica
# folgado o bastante acima do primeiro caso e abaixo dos outros dois.
_SPLIT_MERGE_GAP_MM = 1.0
# Margem (em mm) adicionada ao redor de cada célula detectada, para não
# cortar rente ao próprio conteúdo (anti-aliasing da borda do QR code etc).
_SPLIT_CELL_PADDING_MM = 0.75


def _axis_content_profile(image: Image.Image, collapse_width: bool) -> list[float]:
    """Intensidade média de cinza (0=preto, 255=branco) ao longo de um eixo,
    obtida via redimensionamento por box-filter do Pillow (sem numpy): cada
    pixel da imagem reduzida a 1px de largura (ou altura) é a média de toda
    a linha (ou coluna) correspondente da imagem original."""
    gray = image.convert("L")
    if collapse_width:
        small = gray.resize((1, gray.height), Image.Resampling.BOX)
    else:
        small = gray.resize((gray.width, 1), Image.Resampling.BOX)
    return [float(value) for value in small.getdata()]


def _content_bands(profile: list[float], merge_gap_px: int) -> list[tuple[int, int]]:
    """Encontra faixas contíguas (start, end) do perfil que têm conteúdo
    (abaixo do limiar de branco), unindo faixas separadas por um vão menor
    que `merge_gap_px` — isso absorve quebras de linha internas sem juntar
    etiquetas vizinhas de verdade."""
    raw_bands: list[tuple[int, int]] = []
    start: Optional[int] = None
    for index, value in enumerate(profile):
        is_content = value < _BLANK_WHITE_THRESHOLD
        if is_content and start is None:
            start = index
        elif not is_content and start is not None:
            raw_bands.append((start, index))
            start = None
    if start is not None:
        raw_bands.append((start, len(profile)))
    if not raw_bands:
        return []

    merged = [raw_bands[0]]
    for band_start, band_end in raw_bands[1:]:
        prev_start, prev_end = merged[-1]
        if band_start - prev_end < merge_gap_px:
            merged[-1] = (prev_start, band_end)
        else:
            merged.append((band_start, band_end))
    return merged


def split_rendered_label(rendered: RenderedLabel, enabled: bool) -> list[RenderedLabel]:
    """Recorta uma etiqueta renderizada em sub-etiquetas, detectando
    automaticamente a grade real a partir dos pixels — sem exigir que o
    usuário informe o número de colunas/linhas.

    Alguns exportadores (ex: a exportação de etiquetas de picking da
    Shopee) agrupam várias etiquetas físicas distintas numa única imagem
    `~DG`/`^GF` — uma "folha" pré-montada pela origem, sem nenhum comando
    ZPL indicando a quantidade (ela só existe nos pixels da imagem) e,
    frequentemente, sem uma grade uniforme (a última folha de um lote tem
    menos linhas preenchidas que as anteriores). Em vez de assumir um
    tamanho de célula fixo (o que corta etiquetas ao meio quando a grade
    real não bate exatamente com a divisão), a imagem é projetada em dois
    perfis de intensidade média — um por linha, um por coluna — e as faixas
    de conteúdo detectadas em cada eixo (separadas por corredores de
    verdade, não por quebras de linha internas) formam a grade via produto
    cartesiano; cada célula resultante ainda passa pelo mesmo filtro de
    célula-em-branco usado antes, para descartar posições vazias de uma
    grade parcialmente preenchida.

    Com `enabled=False` (padrão), devolve `[rendered]` sem modificar nada —
    comportamento idêntico a antes desta funcionalidade."""
    if not enabled:
        return [rendered]

    image = Image.open(io.BytesIO(rendered.png_bytes))
    width_px, height_px = image.size
    dpmm_x = width_px / rendered.width_mm
    dpmm_y = height_px / rendered.height_mm
    merge_gap_x_px = max(1, round(_SPLIT_MERGE_GAP_MM * dpmm_x))
    merge_gap_y_px = max(1, round(_SPLIT_MERGE_GAP_MM * dpmm_y))
    pad_x_px = round(_SPLIT_CELL_PADDING_MM * dpmm_x)
    pad_y_px = round(_SPLIT_CELL_PADDING_MM * dpmm_y)

    row_bands = _content_bands(
        _axis_content_profile(image, collapse_width=True), merge_gap_y_px
    )
    col_bands = _content_bands(
        _axis_content_profile(image, collapse_width=False), merge_gap_x_px
    )

    pieces: list[RenderedLabel] = []
    for y0, y1 in row_bands:
        for x0, x1 in col_bands:
            box = (
                max(0, x0 - pad_x_px),
                max(0, y0 - pad_y_px),
                min(width_px, x1 + pad_x_px),
                min(height_px, y1 + pad_y_px),
            )
            cell = image.crop(box)
            if _is_blank_cell(cell):
                continue
            buffer = io.BytesIO()
            cell.save(buffer, format="PNG")
            cell_width_px, cell_height_px = cell.size
            pieces.append(
                RenderedLabel(
                    png_bytes=buffer.getvalue(),
                    width_mm=cell_width_px / dpmm_x,
                    height_mm=cell_height_px / dpmm_y,
                    label_index=rendered.label_index,
                )
            )

    if not pieces:
        raise RenderError(
            f"Etiqueta {rendered.label_index + 1}: não foi encontrada nenhuma "
            "sub-etiqueta com conteúdo ao dividir a imagem automaticamente — "
            "ela pode não seguir o padrão de grade esperado (várias etiquetas "
            "lado a lado numa única imagem ~DG)."
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
