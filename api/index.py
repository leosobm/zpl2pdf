"""API serverless (Vercel) para o zpl2pdf.

Um único app Flask (padrão comum de "Flask on Vercel") expõe a mesma
orquestração de `zpl2pdf/webapi.py` usada pela UI Streamlit (`app.py`) —
nenhuma lógica de parsing/renderização/layout/grade é duplicada aqui, só a
camada HTTP. `vercel.json` reescreve todo tráfego de `/api/*` para este
arquivo; o Flask cuida do roteamento interno das rotas abaixo.

Rotas:
- POST /api/analyze  -> JSON com grade/escala calculada (sem chamar o Labelary).
- POST /api/preview  -> PNG (image/png) com a prévia da 1ª página.
- POST /api/generate -> PDF (application/pdf) com o resultado final.

Cada rota recebe o arquivo ZPL como multipart (`file`) e os parâmetros de
layout/impressão como campos de formulário (ver `_params_from_request`).

Limitações do modelo serverless (ver seção "Deploy no Vercel" do README):
- Sem progresso em tempo real por etiqueta (a função só responde ao final).
- Timeout de função: arquivos com muitas etiquetas distintas ainda não
  cacheadas podem demorar mais do que o limite do plano Vercel, por causa do
  rate limiting de ~2 req/s ao Labelary.
- Cache por hash em `/tmp`, válido só durante o tempo de vida do container
  (não persiste de forma garantida entre requisições).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, send_from_directory

from zpl2pdf.config import ConfigError
from zpl2pdf.parser import ZplParseError
from zpl2pdf.renderer import RenderError
from zpl2pdf.webapi import RenderParams, analyze, count_labels, render_output_pdf, render_preview

app = Flask(__name__)

CACHE_DIR = os.path.join(tempfile.gettempdir(), "zpl2pdf_cache")
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


def _float_or_none(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _params_from_request() -> RenderParams:
    form = request.form
    return RenderParams(
        sheet_size=form.get("sheet_size", "A4"),
        orientation=form.get("orientation", "portrait"),
        margin_top_mm=float(form.get("margin_top_mm", 10.0)),
        margin_bottom_mm=float(form.get("margin_bottom_mm", 10.0)),
        margin_left_mm=float(form.get("margin_left_mm", 10.0)),
        margin_right_mm=float(form.get("margin_right_mm", 10.0)),
        gap_x_mm=float(form.get("gap_x_mm", 2.0)),
        gap_y_mm=float(form.get("gap_y_mm", 2.0)),
        dpi=int(form.get("dpi", 203)),
        fallback_width_mm=_float_or_none(form.get("fallback_width_mm")),
        fallback_height_mm=_float_or_none(form.get("fallback_height_mm")),
        stretch=form.get("stretch", "false").lower() in ("1", "true", "on"),
        labels_per_page=int(form.get("labels_per_page", 1)),
        pages=int(form.get("pages", 1)),
        multi_mode=form.get("multi_mode") or None,
    )


def _content_from_request() -> str:
    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        raise ZplParseError("Nenhum arquivo .zpl enviado (campo 'file' ausente).")
    return uploaded.read().decode("utf-8", errors="replace")


@app.errorhandler(ZplParseError)
@app.errorhandler(ConfigError)
def _handle_client_error(exc: Exception) -> tuple[Response, int]:
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(RenderError)
def _handle_render_error(exc: Exception) -> tuple[Response, int]:
    return (
        jsonify(
            {
                "error": str(exc),
                "hint": (
                    "Falha ao renderizar via Labelary — aguarde alguns instantes e "
                    "tente novamente (costuma acontecer quando o limite de "
                    "requisições/segundo do plano gratuito é excedido)."
                ),
            }
        ),
        502,
    )


@app.route("/api/health", methods=["GET"])
def health() -> Response:
    return jsonify({"status": "ok"})


@app.route("/api/count", methods=["POST"])
def count_route() -> Response:
    """Conta quantas etiquetas distintas o arquivo tem, sem calcular grade
    nem exigir os demais parâmetros — chamado assim que o arquivo é
    selecionado, antes de saber se é modo etiqueta-única ou múltiplas."""
    content = _content_from_request()
    return jsonify({"total_labels_in_file": count_labels(content)})


@app.route("/api/analyze", methods=["POST"])
def analyze_route() -> Response:
    content = _content_from_request()
    params = _params_from_request()
    result = analyze(content, params)
    return jsonify(
        {
            "mode": result.mode,
            "total_labels_in_file": result.total_labels_in_file,
            "total_labels_output": result.total_labels_output,
            "computed_pages": result.computed_pages,
            "cols": result.cols,
            "rows": result.rows,
            "cell_width_mm": result.cell_width_mm,
            "cell_height_mm": result.cell_height_mm,
            "scale": result.scale,
            "low_scale_warning": result.low_scale_warning,
        }
    )


@app.route("/api/preview", methods=["POST"])
def preview_route() -> Response:
    content = _content_from_request()
    params = _params_from_request()
    png_bytes = render_preview(content, params, cache_dir=CACHE_DIR)
    return Response(png_bytes, mimetype="image/png")


@app.route("/api/generate", methods=["POST"])
def generate_route() -> Response:
    content = _content_from_request()
    params = _params_from_request()
    uploaded_name = request.files["file"].filename
    output_name = Path(uploaded_name).stem + ".pdf"

    tmp_path = Path(tempfile.gettempdir()) / f"zpl2pdf_{uuid.uuid4().hex}.pdf"
    try:
        render_output_pdf(content, params, tmp_path, cache_dir=CACHE_DIR)
        pdf_bytes = tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
    )


# Serve o frontend estático só para desenvolvimento local com `flask run` —
# no Vercel, `public/` é servido diretamente pela plataforma e essas rotas
# nunca são atingidas (só `/api/*` é reescrito para esta função).
@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def static_files(path: str) -> Response:
    return send_from_directory(PUBLIC_DIR, path)


# Execução local (fora do Vercel): `python -m api.index` ou `flask --app api.index run`.
if __name__ == "__main__":
    app.run(debug=True, port=5000)
