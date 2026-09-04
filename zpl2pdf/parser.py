"""Parser de arquivos ZPL: separa etiquetas individuais e extrai metadados.

Cada etiqueta ZPL é delimitada pelos comandos ^XA (start format) e ^XZ
(end format). Este módulo também extrai, quando presentes, os comandos:

- ^PW<dots>  -> largura da etiqueta em dots
- ^LL<dots>  -> comprimento (altura) da etiqueta em dots
- ^JM<A|B>   -> modo de dots-per-millimeter (A = resolução cheia, B = metade)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ZplParseError(Exception):
    """Erro ao localizar/interpretar etiquetas em um arquivo ZPL."""


_LABEL_PATTERN = re.compile(r"\^XA.*?\^XZ", re.DOTALL | re.IGNORECASE)
_PW_PATTERN = re.compile(r"\^PW(\d+)", re.IGNORECASE)
_LL_PATTERN = re.compile(r"\^LL(\d+)", re.IGNORECASE)
_JM_PATTERN = re.compile(r"\^JM([AB])", re.IGNORECASE)


@dataclass(frozen=True)
class ZplLabel:
    """Representa uma única etiqueta (bloco ^XA...^XZ) dentro de um arquivo ZPL."""

    raw: str
    index: int
    pw_dots: Optional[int]
    ll_dots: Optional[int]
    jm_mode: Optional[str]

    def has_explicit_size(self) -> bool:
        """True se a etiqueta define sua própria largura e altura via ZPL."""
        return self.pw_dots is not None and self.ll_dots is not None


def _extract_int(pattern: re.Pattern, text: str) -> Optional[int]:
    match = pattern.search(text)
    if match is None:
        return None
    return int(match.group(1))


def _extract_str(pattern: re.Pattern, text: str) -> Optional[str]:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(1).upper()


def split_labels(content: str) -> list[str]:
    """Separa o conteúdo bruto de um arquivo ZPL em blocos ^XA...^XZ individuais.

    Levanta ZplParseError se nenhum bloco válido for encontrado.
    """
    blocks = _LABEL_PATTERN.findall(content)
    if not blocks:
        raise ZplParseError(
            "Nenhum bloco ^XA...^XZ encontrado no arquivo. "
            "Verifique se o conteúdo é um ZPL válido."
        )

    open_count = len(re.findall(r"\^XA", content, re.IGNORECASE))
    close_count = len(re.findall(r"\^XZ", content, re.IGNORECASE))
    if open_count != close_count:
        raise ZplParseError(
            f"Número de ^XA ({open_count}) diferente de ^XZ ({close_count}). "
            "O arquivo ZPL parece estar truncado ou malformado."
        )
    if open_count != len(blocks):
        raise ZplParseError(
            f"Foram encontrados {open_count} comandos ^XA mas apenas {len(blocks)} "
            "blocos ^XA...^XZ puderam ser isolados. Verifique blocos aninhados "
            "ou malformados no arquivo."
        )

    return blocks


def parse_zpl_content(content: str) -> list[ZplLabel]:
    """Faz o parse de uma string com uma ou mais etiquetas ZPL."""
    blocks = split_labels(content)
    labels: list[ZplLabel] = []
    for i, block in enumerate(blocks):
        labels.append(
            ZplLabel(
                raw=block.strip(),
                index=i,
                pw_dots=_extract_int(_PW_PATTERN, block),
                ll_dots=_extract_int(_LL_PATTERN, block),
                jm_mode=_extract_str(_JM_PATTERN, block),
            )
        )
    return labels


def parse_zpl_file(path: str | Path) -> list[ZplLabel]:
    """Lê um arquivo .zpl do disco e retorna a lista de etiquetas encontradas."""
    file_path = Path(path)
    if not file_path.exists():
        raise ZplParseError(f"Arquivo ZPL não encontrado: {file_path}")
    if not file_path.is_file():
        raise ZplParseError(f"Caminho não é um arquivo: {file_path}")

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ZplParseError(f"Falha ao ler o arquivo {file_path}: {exc}") from exc

    return parse_zpl_content(content)
