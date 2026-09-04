"""Interface web (Streamlit) para o zpl2pdf.

Reutiliza os mesmos módulos da CLI (parser, renderer, layout, pdf_builder) —
nenhuma lógica de parsing/renderização/layout é duplicada aqui, apenas a
orquestração e a apresentação.

Detecta automaticamente o tipo de arquivo enviado:

- **Uma única etiqueta**: informe quantas cópias por página e quantas
  páginas — a grade é dimensionada para essa quantidade e a etiqueta é
  redimensionada para caber em cada célula.
- **Várias etiquetas distintas**: escolha entre "caber tudo em uma página"
  (grade dimensionada para o total de etiquetas do arquivo) ou "distribuir
  em várias páginas" (grade dimensionada para `labels_per_page`; o número
  de páginas é calculado automaticamente).

Rodar com: streamlit run app.py
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Optional

import streamlit as st
from PIL import Image

from zpl2pdf.config import ConfigError, SheetConfig, resolve_sheet_size
from zpl2pdf.layout import LOW_SCALE_WARNING_THRESHOLD, LayoutEngine, calculate_grid, resolve_label_size_mm
from zpl2pdf.parser import ZplLabel, ZplParseError, parse_zpl_content
from zpl2pdf.pdf_builder import build_pdf
from zpl2pdf.renderer import LabelCache, LabelaryRenderer, RenderError, dpi_to_dpmm

CACHE_DIR = "cache"

st.set_page_config(page_title="zpl2pdf", page_icon="🏷️", layout="wide")


def build_preview_image(sheet: SheetConfig, placements, target_width_px: int = 700) -> Image.Image:
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


def reset_outputs() -> None:
    for key in ("pdf_bytes", "pdf_summary", "preview_image"):
        st.session_state.pop(key, None)


st.title("🏷️ zpl2pdf — Conversor de ZPL para PDF")
st.caption(
    "Envie um arquivo .zpl. Com uma etiqueta, ela é repetida na grade. Com "
    "várias etiquetas distintas, escolha como distribuí-las nas páginas."
)

# ---------------------------------------------------------------------------
# Sidebar: parâmetros de configuração
# ---------------------------------------------------------------------------
st.sidebar.header("Configuração")

sheet_size_choice = st.sidebar.selectbox("Tamanho da folha", ["A4", "Letter", "Custom"])
if sheet_size_choice == "Custom":
    c1, c2 = st.sidebar.columns(2)
    sheet_width_mm = c1.number_input("Largura folha (mm)", min_value=10.0, value=210.0, step=1.0)
    sheet_height_mm = c2.number_input("Altura folha (mm)", min_value=10.0, value=297.0, step=1.0)
else:
    sheet_width_mm, sheet_height_mm = resolve_sheet_size(sheet_size_choice)

orientation = st.sidebar.selectbox(
    "Orientação", ["portrait", "landscape"], format_func=lambda v: "Retrato" if v == "portrait" else "Paisagem"
)

st.sidebar.subheader("Margens (mm)")
mc1, mc2 = st.sidebar.columns(2)
margin_top = mc1.number_input("Superior", min_value=0.0, value=10.0, step=1.0)
margin_bottom = mc2.number_input("Inferior", min_value=0.0, value=10.0, step=1.0)
mc3, mc4 = st.sidebar.columns(2)
margin_left = mc3.number_input("Esquerda", min_value=0.0, value=10.0, step=1.0)
margin_right = mc4.number_input("Direita", min_value=0.0, value=10.0, step=1.0)

st.sidebar.subheader("Espaçamento entre etiquetas (mm)")
sc1, sc2 = st.sidebar.columns(2)
gap_x = sc1.number_input("Horizontal", min_value=0.0, value=2.0, step=0.5)
gap_y = sc2.number_input("Vertical", min_value=0.0, value=2.0, step=0.5)

st.sidebar.subheader("Impressão")
dpi = st.sidebar.selectbox("DPI", [203, 300], index=0)

st.sidebar.subheader("Fallback de tamanho da etiqueta (mm)")
st.sidebar.caption("Usado somente se o ZPL não definir ^PW/^LL. Deixe em 0 para não definir.")
fc1, fc2 = st.sidebar.columns(2)
fallback_width_input = fc1.number_input("Largura", min_value=0.0, value=0.0, step=1.0)
fallback_height_input = fc2.number_input("Altura", min_value=0.0, value=0.0, step=1.0)
fallback_width_mm: Optional[float] = fallback_width_input if fallback_width_input > 0 else None
fallback_height_mm: Optional[float] = fallback_height_input if fallback_height_input > 0 else None

st.sidebar.subheader("Redimensionamento")
stretch = st.sidebar.checkbox(
    "Esticar para preencher célula",
    value=False,
    help="Se marcado, ignora a proporção original e estica a etiqueta para ocupar 100% "
    "da célula (pode distorcer o código de barras). Se desmarcado (padrão), a proporção "
    "original é mantida e a etiqueta é centralizada na célula.",
)

# ---------------------------------------------------------------------------
# Área principal: upload
# ---------------------------------------------------------------------------
uploaded_file = st.file_uploader("Arquivo ZPL (.zpl)", type=["zpl", "txt"])

file_id = f"{uploaded_file.name}-{uploaded_file.size}" if uploaded_file else None
if st.session_state.get("_last_file_id") != file_id:
    reset_outputs()
    st.session_state["_last_file_id"] = file_id

labels: Optional[list[ZplLabel]] = None
if uploaded_file is not None:
    try:
        content = uploaded_file.getvalue().decode("utf-8", errors="replace")
        labels = parse_zpl_content(content)
        if len(labels) == 1:
            st.success(f"✅ Etiqueta única detectada em **{uploaded_file.name}**.")
        else:
            st.success(f"✅ **{len(labels)}** etiquetas distintas detectadas em **{uploaded_file.name}**.")
    except ZplParseError as exc:
        st.error(f"Erro ao interpretar o ZPL: {exc}")

# ---------------------------------------------------------------------------
# Monta a folha.
# ---------------------------------------------------------------------------
sheet: Optional[SheetConfig] = None
if labels:
    try:
        sheet = SheetConfig(
            width_mm=sheet_width_mm,
            height_mm=sheet_height_mm,
            orientation=orientation,
            margin_top_mm=margin_top,
            margin_bottom_mm=margin_bottom,
            margin_left_mm=margin_left,
            margin_right_mm=margin_right,
            gap_x_mm=gap_x,
            gap_y_mm=gap_y,
        )
    except ConfigError as exc:
        st.warning(f"⚠️ {exc}")

dpmm = dpi_to_dpmm(int(dpi))

# ---------------------------------------------------------------------------
# Resolve o tamanho de TODAS as etiquetas (sem renderizar — não gasta API).
# ---------------------------------------------------------------------------
resolved_sizes: Optional[list[tuple[float, float]]] = None
if labels and sheet is not None:
    try:
        resolved_sizes = [
            resolve_label_size_mm(label, dpmm, fallback_width_mm, fallback_height_mm)
            for label in labels
        ]
    except ConfigError as exc:
        st.warning(f"⚠️ {exc}")

engine: Optional[LayoutEngine] = None
cols = rows = 0
multi_mode: Optional[str] = None
labels_per_page = 1
pages = 1
computed_pages = 1

if labels and sheet is not None and resolved_sizes is not None:
    ref_width_mm, ref_height_mm = resolved_sizes[0]

    if len(labels) == 1:
        # ------------------------------------------------------------
        # Tipo 1: etiqueta única repetida.
        # ------------------------------------------------------------
        st.subheader("Quantidade")
        qc1, qc2 = st.columns(2)
        labels_per_page = qc1.number_input("Etiquetas por página", min_value=1, value=12, step=1)
        pages = qc2.number_input("Número de páginas", min_value=1, value=1, step=1)
        grid_count = int(labels_per_page)
    else:
        # ------------------------------------------------------------
        # Tipo 2: múltiplas etiquetas distintas — escolher o modo.
        # ------------------------------------------------------------
        st.subheader("Como distribuir as etiquetas")
        mode_label = st.radio(
            "Modo",
            options=["Caber tudo em 1 página", "Distribuir em várias páginas"],
            horizontal=True,
            label_visibility="collapsed",
        )
        multi_mode = "fit-one-page" if mode_label == "Caber tudo em 1 página" else "paginate"

        if multi_mode == "fit-one-page":
            grid_count = len(labels)
            st.caption(f"Grade dimensionada para as {len(labels)} etiquetas distintas, em 1 página.")
        else:
            labels_per_page = st.number_input(
                "Etiquetas por página", min_value=1, max_value=len(labels), value=min(6, len(labels)), step=1
            )
            grid_count = int(labels_per_page)
            computed_pages = math.ceil(len(labels) / grid_count)
            st.caption(f"📄 Serão geradas **{computed_pages}** página(s) para cobrir as {len(labels)} etiquetas.")

    try:
        cols, rows = calculate_grid(grid_count, sheet, ref_width_mm, ref_height_mm)
        engine = LayoutEngine(sheet, cols, rows, stretch=bool(stretch))
        # pior escala entre TODAS as etiquetas do arquivo (o tamanho de célula é o
        # mesmo em todas as páginas, então qualquer etiqueta pode ser o pior caso).
        worst_scale = min(engine.fit_scale_for(w, h) for w, h in resolved_sizes)
        scale_pct = worst_scale * 100
        st.info(
            f"📐 Grade calculada: **{cols} colunas x {rows} linhas** = {cols * rows} células · "
            f"Célula: {engine.cell_width_mm:.1f}x{engine.cell_height_mm:.1f}mm · "
            f"Pior escala: **{scale_pct:.0f}%** ({'esticada' if stretch else 'proporcional, centralizada'})"
        )
        if worst_scale < LOW_SCALE_WARNING_THRESHOLD:
            st.warning(
                f"⚠️ Ao menos uma etiqueta está sendo reduzida para **{scale_pct:.0f}%** do "
                "tamanho original nessa grade. Em escalas abaixo de 50%, o código de barras "
                "pode ficar difícil de ler fisicamente."
            )
    except ConfigError as exc:
        st.warning(f"⚠️ {exc}")
        engine = None

grid_ok = labels is not None and engine is not None

# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------
col_preview, col_generate = st.columns(2)
preview_clicked = col_preview.button("🔍 Prévia da 1ª página", disabled=not grid_ok)
generate_clicked = col_generate.button("📄 Gerar PDF", disabled=not grid_ok, type="primary")

renderer = LabelaryRenderer(cache=LabelCache(CACHE_DIR))


def _render_all(labels_to_render: list[ZplLabel], resolved: list[tuple[float, float]]) -> list:
    """Renderiza uma lista de etiquetas, mostrando uma barra de progresso.

    O rate limiting + retry com backoff do `LabelaryRenderer` pode deixar a
    renderização bem mais lenta em arquivos com muitas etiquetas distintas
    (não cacheadas ainda), então a barra evita a impressão de que o processo
    travou.
    """
    total = len(labels_to_render)
    progress_bar = st.progress(0.0, text=f"Renderizando etiqueta 0 de {total}...")
    rendered = []
    try:
        for i, (label, (w, h)) in enumerate(zip(labels_to_render, resolved), start=1):
            progress_bar.progress((i - 1) / total, text=f"Renderizando etiqueta {i} de {total}...")
            rendered.append(renderer.render(label, dpmm, w, h))
            progress_bar.progress(i / total, text=f"Renderizando etiqueta {i} de {total}...")
    finally:
        progress_bar.empty()
    return rendered


if preview_clicked and engine is not None and labels is not None and sheet is not None:
    try:
        if len(labels) == 1:
            with st.spinner("Renderizando etiqueta de referência..."):
                rendered = renderer.render(labels[0], dpmm, *resolved_sizes[0])
            placements = engine.place_single(rendered, pages=1)
        elif multi_mode == "fit-one-page":
            rendered_labels = _render_all(labels, resolved_sizes)
            placements = engine.place_sequence(rendered_labels)
        else:
            preview_subset = labels[: int(labels_per_page)]
            rendered_labels = _render_all(preview_subset, resolved_sizes[: int(labels_per_page)])
            placements = engine.place_sequence(rendered_labels)
        st.session_state["preview_image"] = build_preview_image(sheet, placements)
    except RenderError as exc:
        st.error(
            f"Falha ao renderizar via Labelary: {exc}\n\n"
            "A prévia não foi gerada. Aguarde alguns instantes e tente novamente "
            "— isso costuma acontecer quando o limite de requisições/segundo "
            "do plano gratuito do Labelary é excedido."
        )

if "preview_image" in st.session_state:
    st.subheader("Prévia da 1ª página")
    st.image(st.session_state["preview_image"], use_container_width=False)

if generate_clicked and engine is not None and labels is not None and sheet is not None:
    try:
        if len(labels) == 1:
            with st.spinner("Renderizando etiqueta de referência..."):
                rendered_single = renderer.render(labels[0], dpmm, *resolved_sizes[0])
        else:
            rendered_labels = _render_all(labels, resolved_sizes)

        with st.spinner("Montando o PDF..."):
            if len(labels) == 1:
                placements = engine.place_single(rendered_single, int(pages))
                total_labels = int(labels_per_page) * int(pages)
            else:
                placements = engine.place_sequence(rendered_labels)
                total_labels = len(labels)

            output_name = Path(uploaded_file.name).stem + ".pdf"
            tmp_path = Path(CACHE_DIR) / f"_tmp_{output_name}"
            total_pages = build_pdf(placements, sheet, tmp_path)
            pdf_bytes = tmp_path.read_bytes()
            tmp_path.unlink(missing_ok=True)

        st.session_state["pdf_bytes"] = pdf_bytes
        st.session_state["pdf_summary"] = {
            "mode": "single" if len(labels) == 1 else multi_mode,
            "pages": total_pages,
            "total_labels": total_labels,
            "cols": cols,
            "rows": rows,
            "filename": output_name,
        }
    except RenderError as exc:
        st.error(
            f"Falha ao renderizar via Labelary: {exc}\n\n"
            "Nenhum PDF foi gerado (para evitar um arquivo incompleto). "
            "Aguarde alguns instantes e tente novamente — isso costuma "
            "acontecer quando o limite de requisições/segundo do plano "
            "gratuito do Labelary é excedido."
        )
    except (ConfigError, ValueError) as exc:
        st.error(f"Erro ao gerar o PDF: {exc}")

if "pdf_bytes" in st.session_state:
    summary = st.session_state["pdf_summary"]
    mode_desc = {
        "single": "etiqueta única repetida",
        "fit-one-page": "múltiplas etiquetas, 1 página",
        "paginate": "múltiplas etiquetas, paginado",
    }.get(summary["mode"], summary["mode"])
    st.success(
        f"🎉 PDF gerado ({mode_desc}): grade {summary['cols']}x{summary['rows']} · "
        f"**{summary['pages']}** página(s) · **{summary['total_labels']}** etiquetas no total."
    )
    st.download_button(
        "⬇️ Baixar PDF",
        data=st.session_state["pdf_bytes"],
        file_name=summary["filename"],
        mime="application/pdf",
    )
