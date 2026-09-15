"""Interface web (Streamlit) para o zpl2pdf.

Reutiliza a mesma orquestração de `zpl2pdf/webapi.py` (compartilhada com a
API serverless do Vercel em `api/index.py`) — nenhuma lógica de
parsing/renderização/layout/grade é duplicada aqui, só a apresentação e a
leitura dos widgets da sidebar.

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

from pathlib import Path
from typing import Optional

import streamlit as st

from zpl2pdf.config import ConfigError
from zpl2pdf.parser import ZplParseError, parse_zpl_content
from zpl2pdf.renderer import RenderError
from zpl2pdf.webapi import RenderParams, analyze, render_output_pdf, render_preview

CACHE_DIR = "cache"

st.set_page_config(page_title="zpl2pdf", page_icon="🏷️", layout="wide")


def reset_outputs() -> None:
    for key in ("pdf_bytes", "pdf_summary", "preview_image"):
        st.session_state.pop(key, None)


def make_progress_callback():
    """Cria uma barra de progresso do Streamlit e devolve um callback
    `(i, total)` para passar a `render_preview`/`render_output_pdf`.

    O rate limiting + retry com backoff do `LabelaryRenderer` pode deixar a
    renderização bem mais lenta em arquivos com muitas etiquetas distintas
    (não cacheadas ainda), então a barra evita a impressão de que o processo
    travou.
    """
    progress_bar = st.progress(0.0, text="Renderizando etiqueta 0...")

    def on_progress(i: int, total: int) -> None:
        progress_bar.progress((i - 1) / total, text=f"Renderizando etiqueta {i} de {total}...")

    return progress_bar, on_progress


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
    sheet_size = f"{sheet_width_mm}x{sheet_height_mm}"
else:
    sheet_size = sheet_size_choice

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

content: Optional[str] = None
num_labels: Optional[int] = None
if uploaded_file is not None:
    try:
        content = uploaded_file.getvalue().decode("utf-8", errors="replace")
        num_labels = len(parse_zpl_content(content))
        if num_labels == 1:
            st.success(f"✅ Etiqueta única detectada em **{uploaded_file.name}**.")
        else:
            st.success(f"✅ **{num_labels}** etiquetas distintas detectadas em **{uploaded_file.name}**.")
    except ZplParseError as exc:
        st.error(f"Erro ao interpretar o ZPL: {exc}")

# ---------------------------------------------------------------------------
# Quantidade/distribuição (os widgets aqui alimentam RenderParams abaixo).
# ---------------------------------------------------------------------------
multi_mode: Optional[str] = None
labels_per_page = 1
pages = 1

if num_labels == 1:
    # ------------------------------------------------------------
    # Tipo 1: etiqueta única repetida.
    # ------------------------------------------------------------
    st.subheader("Quantidade")
    qc1, qc2 = st.columns(2)
    labels_per_page = qc1.number_input("Etiquetas por página", min_value=1, value=12, step=1)
    pages = qc2.number_input("Número de páginas", min_value=1, value=1, step=1)
elif num_labels is not None and num_labels > 1:
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
        st.caption(f"Grade dimensionada para as {num_labels} etiquetas distintas, em 1 página.")
    else:
        labels_per_page = st.number_input(
            "Etiquetas por página", min_value=1, max_value=num_labels, value=min(6, num_labels), step=1
        )

params: Optional[RenderParams] = None
if content is not None and num_labels is not None:
    params = RenderParams(
        sheet_size=sheet_size,
        orientation=orientation,
        margin_top_mm=margin_top,
        margin_bottom_mm=margin_bottom,
        margin_left_mm=margin_left,
        margin_right_mm=margin_right,
        gap_x_mm=gap_x,
        gap_y_mm=gap_y,
        dpi=int(dpi),
        fallback_width_mm=fallback_width_mm,
        fallback_height_mm=fallback_height_mm,
        stretch=bool(stretch),
        labels_per_page=int(labels_per_page),
        pages=int(pages),
        multi_mode=multi_mode,
    )

# ---------------------------------------------------------------------------
# Calcula a grade/escala (sem gastar chamadas ao Labelary).
# ---------------------------------------------------------------------------
analysis = None
if params is not None:
    try:
        analysis = analyze(content, params)
        scale_pct = analysis.scale * 100
        if multi_mode == "paginate":
            st.caption(f"📄 Serão geradas **{analysis.computed_pages}** página(s) para cobrir as {num_labels} etiquetas.")
        st.info(
            f"📐 Grade calculada: **{analysis.cols} colunas x {analysis.rows} linhas** = "
            f"{analysis.cols * analysis.rows} células · "
            f"Célula: {analysis.cell_width_mm:.1f}x{analysis.cell_height_mm:.1f}mm · "
            f"Pior escala: **{scale_pct:.0f}%** ({'esticada' if stretch else 'proporcional, centralizada'})"
        )
        if analysis.low_scale_warning:
            st.warning(
                f"⚠️ Ao menos uma etiqueta está sendo reduzida para **{scale_pct:.0f}%** do "
                "tamanho original nessa grade. Em escalas abaixo de 50%, o código de barras "
                "pode ficar difícil de ler fisicamente."
            )
    except ConfigError as exc:
        st.warning(f"⚠️ {exc}")

grid_ok = analysis is not None

# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------
col_preview, col_generate = st.columns(2)
preview_clicked = col_preview.button("🔍 Prévia da 1ª página", disabled=not grid_ok)
generate_clicked = col_generate.button("📄 Gerar PDF", disabled=not grid_ok, type="primary")

if preview_clicked and params is not None and content is not None:
    progress_bar = None
    try:
        if num_labels and num_labels > 1:
            progress_bar, on_progress = make_progress_callback()
            png_bytes = render_preview(content, params, cache_dir=CACHE_DIR, on_progress=on_progress)
        else:
            with st.spinner("Renderizando etiqueta de referência..."):
                png_bytes = render_preview(content, params, cache_dir=CACHE_DIR, on_progress=None)
        st.session_state["preview_image"] = png_bytes
    except RenderError as exc:
        st.error(
            f"Falha ao renderizar via Labelary: {exc}\n\n"
            "A prévia não foi gerada. Aguarde alguns instantes e tente novamente "
            "— isso costuma acontecer quando o limite de requisições/segundo "
            "do plano gratuito do Labelary é excedido."
        )
    finally:
        if progress_bar is not None:
            progress_bar.empty()

if "preview_image" in st.session_state:
    st.subheader("Prévia da 1ª página")
    st.image(st.session_state["preview_image"], use_container_width=False)

if generate_clicked and params is not None and content is not None and analysis is not None:
    progress_bar = None
    try:
        output_name = Path(uploaded_file.name).stem + ".pdf"
        tmp_path = Path(CACHE_DIR) / f"_tmp_{output_name}"

        if num_labels and num_labels > 1:
            progress_bar, on_progress = make_progress_callback()
            total_pages = render_output_pdf(
                content, params, tmp_path, cache_dir=CACHE_DIR, on_progress=on_progress
            )
        else:
            with st.spinner("Renderizando etiqueta de referência e montando o PDF..."):
                total_pages = render_output_pdf(
                    content, params, tmp_path, cache_dir=CACHE_DIR, on_progress=None
                )

        pdf_bytes = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)

        st.session_state["pdf_bytes"] = pdf_bytes
        st.session_state["pdf_summary"] = {
            "mode": analysis.mode,
            "pages": total_pages,
            "total_labels": analysis.total_labels_output,
            "cols": analysis.cols,
            "rows": analysis.rows,
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
    finally:
        if progress_bar is not None:
            progress_bar.empty()

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
