"""Interface de linha de comando: orquestra parser -> renderer -> layout -> pdf.

Detecta automaticamente o tipo de arquivo de entrada:

- **Uma única etiqueta** (`^XA...^XZ`): repete essa etiqueta de referência
  `labels_per_page` vezes por página, ao longo de `pages` páginas
  idênticas.
- **Várias etiquetas distintas**: exige `--multi-mode` para escolher entre
  `fit-one-page` (todas em uma única página, grade dimensionada para o
  total de etiquetas do arquivo) ou `paginate` (distribui sequencialmente
  em páginas de `labels_per_page` etiquetas cada; `pages` é calculado
  automaticamente como ceil(N / labels_per_page) e não deve ser informado).

Em ambos os casos, a quantidade de etiquetas por célula é sempre
respeitada: a etiqueta renderizada é redimensionada para caber (mantendo a
proporção original por padrão, ou esticada com `--stretch`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import ConfigError, PrintConfig, SheetConfig, resolve_sheet_size
from .layout import LOW_SCALE_WARNING_THRESHOLD, LayoutEngine, calculate_grid, resolve_label_size_mm
from .parser import ZplParseError, parse_zpl_file
from .pdf_builder import build_pdf
from .renderer import LabelCache, LabelaryRenderer, RenderError, dpi_to_dpmm

logger = logging.getLogger("zpl2pdf")

MULTI_MODE_CHOICES = ("fit-one-page", "paginate")

DEFAULTS: dict[str, Any] = {
    "sheet_size": "A4",
    "orientation": "portrait",
    "labels_per_page": 1,
    "pages": 1,
    "multi_mode": None,
    "margin_top": 10.0,
    "margin_bottom": 10.0,
    "margin_left": 10.0,
    "margin_right": 10.0,
    "gap_x": 2.0,
    "gap_y": 2.0,
    "dpi": 203,
    "label_width": None,
    "label_height": None,
    "cache_dir": "cache",
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zpl2pdf",
        description=(
            "Converte etiqueta(s) ZPL em um PDF pronto para impressão, "
            "redimensionadas para caber na grade calculada."
        ),
    )
    parser.add_argument("--input", required=True, help="Caminho do arquivo .zpl de entrada.")
    parser.add_argument("--output", required=True, help="Caminho do PDF de saída.")
    parser.add_argument("--config", help="Arquivo de configuração YAML ou JSON (opcional).")
    parser.add_argument(
        "--sheet-size", dest="sheet_size", help="Preset (A4, Letter) ou 'LARGURAxALTURA' em mm."
    )
    parser.add_argument("--orientation", choices=["portrait", "landscape"])
    parser.add_argument(
        "--labels-per-page", type=int, dest="labels_per_page",
        help="Etiqueta única: cópias por página. Múltiplas etiquetas + --multi-mode paginate: "
        "quantas etiquetas por página (o número de páginas é calculado automaticamente).",
    )
    parser.add_argument(
        "--pages", type=int,
        help="Quantas páginas gerar. Só se aplica a arquivo com etiqueta única "
        "(ignorado com múltiplas etiquetas, onde é sempre calculado).",
    )
    parser.add_argument(
        "--multi-mode", choices=list(MULTI_MODE_CHOICES), dest="multi_mode",
        help="Obrigatório quando o arquivo contém múltiplas etiquetas distintas: "
        "'fit-one-page' (todas em 1 página) ou 'paginate' (distribui usando --labels-per-page).",
    )
    parser.add_argument("--margin-top", type=float, dest="margin_top")
    parser.add_argument("--margin-bottom", type=float, dest="margin_bottom")
    parser.add_argument("--margin-left", type=float, dest="margin_left")
    parser.add_argument("--margin-right", type=float, dest="margin_right")
    parser.add_argument("--gap-x", type=float, dest="gap_x")
    parser.add_argument("--gap-y", type=float, dest="gap_y")
    parser.add_argument("--dpi", type=int)
    parser.add_argument("--label-width", type=float, dest="label_width")
    parser.add_argument("--label-height", type=float, dest="label_height")
    parser.add_argument("--cache-dir", dest="cache_dir")
    parser.add_argument("--no-cache", action="store_true", dest="no_cache")
    parser.add_argument(
        "--stretch", action="store_true", dest="stretch",
        help="Estica a etiqueta para preencher 100%% da célula (ignora a proporção original; pode distorcer).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def load_config_file(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"Arquivo de configuração não encontrado: {file_path}")
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text) or {}
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ConfigError(
            f"Extensão de config não suportada: {suffix}. Use .yaml, .yml ou .json."
        )
    if not isinstance(data, dict):
        raise ConfigError(f"Config em {file_path} deve ser um mapeamento (dict) no nível raiz.")
    return data


def merge_config(args: argparse.Namespace, file_config: dict[str, Any]) -> dict[str, Any]:
    """Prioridade: argumento explícito de CLI > arquivo de config > defaults."""
    merged = dict(DEFAULTS)
    merged.update(file_config)
    for key in DEFAULTS:
        cli_value = getattr(args, key, None)
        if cli_value is not None:
            merged[key] = cli_value
    if getattr(args, "no_cache", False):
        merged["cache_dir"] = None
    merged["stretch"] = bool(args.stretch) or bool(file_config.get("stretch", False))
    return merged


def _log_scale(engine: LayoutEngine, width_mm: float, height_mm: float, stretch: bool, label: str = "") -> None:
    scale = engine.fit_scale_for(width_mm, height_mm)
    logger.info(
        "Célula: %.1fx%.1fmm · modo %s · escala%s: %.0f%%.",
        engine.cell_width_mm, engine.cell_height_mm,
        "esticar" if stretch else "proporcional",
        f" ({label})" if label else "",
        scale * 100,
    )
    if scale < LOW_SCALE_WARNING_THRESHOLD:
        logger.warning(
            "%sReduzida para %.0f%% do tamanho original — o código de barras pode "
            "ficar difícil de ler fisicamente nessa escala.",
            f"[{label}] " if label else "",
            scale * 100,
        )


def run(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    try:
        file_config = load_config_file(args.config) if args.config else {}
        cfg = merge_config(args, file_config)

        sheet_width_mm, sheet_height_mm = resolve_sheet_size(str(cfg["sheet_size"]))
        sheet = SheetConfig(
            width_mm=sheet_width_mm,
            height_mm=sheet_height_mm,
            orientation=cfg["orientation"],
            margin_top_mm=float(cfg["margin_top"]),
            margin_bottom_mm=float(cfg["margin_bottom"]),
            margin_left_mm=float(cfg["margin_left"]),
            margin_right_mm=float(cfg["margin_right"]),
            gap_x_mm=float(cfg["gap_x"]),
            gap_y_mm=float(cfg["gap_y"]),
        )
        print_config = PrintConfig(
            dpi=int(cfg["dpi"]),
            fallback_label_width_mm=cfg["label_width"],
            fallback_label_height_mm=cfg["label_height"],
        )
        labels_per_page = int(cfg["labels_per_page"])
        stretch = bool(cfg["stretch"])

        labels = parse_zpl_file(args.input)
        dpmm = dpi_to_dpmm(print_config.dpi)

        cache = LabelCache(cfg["cache_dir"]) if cfg["cache_dir"] else None
        renderer = LabelaryRenderer(cache=cache)

        if len(labels) == 1:
            # Tipo 1: etiqueta única repetida.
            pages = int(cfg["pages"])
            reference_label = labels[0]
            width_mm, height_mm = resolve_label_size_mm(
                reference_label, dpmm,
                print_config.fallback_label_width_mm, print_config.fallback_label_height_mm,
            )
            logger.info("Etiqueta de referência: %.1fx%.1fmm.", width_mm, height_mm)

            cols, rows = calculate_grid(labels_per_page, sheet, width_mm, height_mm)
            logger.info(
                "Grade calculada: %d col x %d lin (%d célula(s)) para %d etiqueta(s)/página.",
                cols, rows, cols * rows, labels_per_page,
            )

            logger.info("Renderizando etiqueta de referência...")
            rendered = renderer.render(reference_label, dpmm, width_mm, height_mm)

            engine = LayoutEngine(sheet, cols, rows, stretch=stretch)
            _log_scale(engine, width_mm, height_mm, stretch)

            placements = engine.place_single(rendered, pages)
            total_labels = labels_per_page * pages

            total_pages = build_pdf(placements, sheet, args.output)

            print()
            print("Resumo:")
            print(f"  Grade por página: {cols} col x {rows} lin ({labels_per_page} etiquetas/página)")
            print(f"  Escala aplicada: {engine.fit_scale_for(width_mm, height_mm) * 100:.0f}% "
                  f"({'esticada' if stretch else 'proporcional, centralizada'})")
            print(f"  Páginas geradas: {total_pages}")
            print(f"  Total de etiquetas no PDF: {total_labels}")
            print(f"  PDF final: {Path(args.output).resolve()}")
            return 0

        # Tipo 2: múltiplas etiquetas distintas.
        multi_mode = cfg["multi_mode"]
        if multi_mode not in MULTI_MODE_CHOICES:
            raise ConfigError(
                f"O arquivo contém {len(labels)} etiquetas diferentes. Escolha o modo com "
                f"--multi-mode fit-one-page (cabe tudo em 1 página) ou "
                f"--multi-mode paginate (distribui em páginas de --labels-per-page etiquetas cada)."
            )

        logger.info("Arquivo contém %d etiquetas distintas.", len(labels))
        resolved_sizes: list[tuple[float, float]] = []
        rendered_labels = []
        for i, label in enumerate(labels):
            width_mm, height_mm = resolve_label_size_mm(
                label, dpmm,
                print_config.fallback_label_width_mm, print_config.fallback_label_height_mm,
            )
            resolved_sizes.append((width_mm, height_mm))
            logger.info("Renderizando etiqueta %d/%d (%.1fx%.1fmm)...", i + 1, len(labels), width_mm, height_mm)
            rendered_labels.append(renderer.render(label, dpmm, width_mm, height_mm))

        ref_width_mm, ref_height_mm = resolved_sizes[0]

        if multi_mode == "fit-one-page":
            grid_count = len(labels)
        else:
            grid_count = labels_per_page

        cols, rows = calculate_grid(grid_count, sheet, ref_width_mm, ref_height_mm)
        logger.info("Grade calculada: %d col x %d lin (%d célula(s)).", cols, rows, cols * rows)

        engine = LayoutEngine(sheet, cols, rows, stretch=stretch)
        worst_scale = min(engine.fit_scale_for(w, h) for w, h in resolved_sizes)
        logger.info(
            "Célula: %.1fx%.1fmm · modo %s · pior escala: %.0f%%.",
            engine.cell_width_mm, engine.cell_height_mm,
            "esticar" if stretch else "proporcional",
            worst_scale * 100,
        )
        if worst_scale < LOW_SCALE_WARNING_THRESHOLD:
            logger.warning(
                "Ao menos uma etiqueta foi reduzida para %.0f%% do tamanho original — o "
                "código de barras pode ficar difícil de ler fisicamente nessa escala.",
                worst_scale * 100,
            )

        placements = engine.place_sequence(rendered_labels)
        total_pages = build_pdf(placements, sheet, args.output)

        print()
        print("Resumo:")
        if multi_mode == "fit-one-page":
            print(f"  Modo: caber tudo em 1 página ({len(labels)} etiquetas distintas)")
        else:
            print(f"  Modo: paginar ({labels_per_page} etiquetas/página)")
        print(f"  Grade por página: {cols} col x {rows} lin")
        print(f"  Pior escala aplicada: {worst_scale * 100:.0f}% ({'esticada' if stretch else 'proporcional, centralizada'})")
        print(f"  Páginas geradas: {total_pages}")
        print(f"  Total de etiquetas no PDF: {len(labels)}")
        print(f"  PDF final: {Path(args.output).resolve()}")
        return 0

    except (ZplParseError, ConfigError, RenderError, ValueError) as exc:
        logger.error("Erro: %s", exc)
        return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
