import math

import pytest

from zpl2pdf.config import ConfigError
from zpl2pdf.webapi import RenderParams, analyze, count_labels, total_physical_quantity


def _label_block(name: str, pq: int | None = None) -> str:
    pq_cmd = f"^PQ{pq},0,1,Y" if pq is not None else ""
    return f"^XA^PW406^LL203^FO10,10^FD{name}^FS{pq_cmd}^XZ"


def test_count_labels_counts_distinct_blocks():
    content = _label_block("um") + _label_block("dois") + _label_block("tres")
    assert count_labels(content) == 3


def test_total_physical_quantity_defaults_to_one_per_label():
    content = _label_block("um") + _label_block("dois")
    assert total_physical_quantity(content) == 2


def test_total_physical_quantity_sums_pq_per_label():
    content = _label_block("a", pq=10) + _label_block("b", pq=3) + _label_block("c", pq=12)
    assert total_physical_quantity(content) == 25


def test_analyze_requires_multi_mode_for_multiple_labels():
    content = _label_block("a") + _label_block("b")
    params = RenderParams(multi_mode=None)
    with pytest.raises(ConfigError):
        analyze(content, params)


def test_analyze_fit_one_page_uses_total_physical_quantity_not_distinct_count():
    # 3 etiquetas distintas, mas 10+3+12=25 etiquetas físicas via ^PQ
    content = _label_block("a", pq=10) + _label_block("b", pq=3) + _label_block("c", pq=12)
    params = RenderParams(multi_mode="fit-one-page")
    result = analyze(content, params)

    assert result.total_labels_in_file == 3
    assert result.total_labels_output == 25
    assert result.cols * result.rows == 25
    assert result.computed_pages == 1


def test_analyze_fit_one_page_without_pq_matches_distinct_count():
    content = _label_block("a") + _label_block("b") + _label_block("c")
    params = RenderParams(multi_mode="fit-one-page")
    result = analyze(content, params)

    assert result.total_labels_output == 3
    assert result.cols * result.rows == 3


def test_analyze_paginate_computes_pages_from_physical_quantity():
    # 58 etiquetas físicas, 6 por página -> ceil(58/6) = 10 páginas
    content = "".join(
        _label_block(name, pq=qty)
        for name, qty in [
            ("a", 10), ("b", 3), ("c", 3), ("d", 12), ("e", 1),
            ("f", 7), ("g", 13), ("h", 1), ("i", 4), ("j", 4),
        ]
    )
    params = RenderParams(multi_mode="paginate", labels_per_page=6)
    result = analyze(content, params)

    assert result.total_labels_in_file == 10
    assert result.total_labels_output == 58
    assert result.computed_pages == math.ceil(58 / 6)
    assert result.cols * result.rows == 6


def test_analyze_single_label_ignores_pq_and_uses_labels_per_page():
    # etiqueta única: ^PQ não deve influenciar a grade (fluxo controlado
    # explicitamente por labels_per_page/pages).
    content = _label_block("unica", pq=99)
    params = RenderParams(labels_per_page=4, pages=2)
    result = analyze(content, params)

    assert result.mode == "single"
    assert result.total_labels_output == 8
    assert result.cols * result.rows == 4
