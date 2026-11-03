"""Predefinições de configuração por canal de venda/envio.

Cada marketplace costuma exportar etiquetas com um padrão bem definido de
tamanho de folha, margens, espaçamento e quantidade por página — selecionar
o canal pré-preenche esses campos de uma vez, em vez de o usuário precisar
redigitar os mesmos valores toda vez que troca de arquivo. Selecionar
"outro" (ou não passar `--channel` na CLI) não aplica nenhuma
predefinição — o comportamento é idêntico ao de antes dessa funcionalidade
existir, com o usuário preenchendo tudo manualmente.

Usado por `app.py` (Streamlit) e `zpl2pdf/cli.py`. A API web/Vercel
(`api/index.py`) não usa este módulo: a mesma tabela é mantida em paralelo,
em JavaScript puro, em `public/app.js` (`CHANNEL_PRESETS`) — ver o
comentário lá — para a seleção de canal funcionar inteiramente no
navegador, sem round-trip ao servidor. Ao alterar um valor aqui, replique a
mudança lá também.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelPreset:
    """Espelha os campos relevantes de `webapi.RenderParams` (mais os
    campos de folha/quantidade que `RenderParams` não guarda diretamente,
    já que eles alimentam outros parâmetros antes de chegar lá)."""

    label: str
    sheet_width_mm: float
    sheet_height_mm: float
    orientation: str
    dpi: int
    margin_top_mm: float
    margin_bottom_mm: float
    margin_left_mm: float
    margin_right_mm: float
    gap_x_mm: float
    gap_y_mm: float
    fallback_width_mm: float
    fallback_height_mm: float
    split_grid: bool
    multi_mode: str
    labels_per_page: int


# Comuns aos três canais: folha 100x150mm, retrato, 203dpi, distribuir em
# várias páginas (ver corpo de cada preset abaixo para os valores que
# variam por canal).
CHANNEL_PRESETS: dict[str, ChannelPreset] = {
    "mercado_livre": ChannelPreset(
        label="Mercado Livre",
        sheet_width_mm=100.0,
        sheet_height_mm=150.0,
        orientation="portrait",
        dpi=203,
        margin_top_mm=10.0,
        margin_bottom_mm=10.0,
        margin_left_mm=0.0,
        margin_right_mm=0.0,
        gap_x_mm=0.0,
        gap_y_mm=5.0,
        fallback_width_mm=60.0,
        fallback_height_mm=40.0,
        split_grid=False,
        multi_mode="paginate",
        labels_per_page=3,
    ),
    "shopee": ChannelPreset(
        label="Shopee",
        sheet_width_mm=100.0,
        sheet_height_mm=150.0,
        orientation="portrait",
        dpi=203,
        margin_top_mm=0.0,
        margin_bottom_mm=0.0,
        margin_left_mm=0.0,
        margin_right_mm=0.0,
        gap_x_mm=0.0,
        gap_y_mm=0.0,
        fallback_width_mm=100.0,
        fallback_height_mm=150.0,
        split_grid=True,
        multi_mode="paginate",
        labels_per_page=3,
    ),
    "magalu": ChannelPreset(
        label="Magalu",
        sheet_width_mm=100.0,
        sheet_height_mm=150.0,
        orientation="portrait",
        dpi=203,
        margin_top_mm=10.0,
        margin_bottom_mm=10.0,
        margin_left_mm=0.0,
        margin_right_mm=0.0,
        gap_x_mm=0.0,
        gap_y_mm=5.0,
        fallback_width_mm=60.0,
        fallback_height_mm=40.0,
        split_grid=False,
        multi_mode="paginate",
        labels_per_page=3,
    ),
}

# Ordem de exibição nos seletores (Streamlit/CLI) — "outro" sempre por
# último, como a opção neutra ("não aplica nenhuma predefinição").
CHANNEL_CHOICES: list[str] = [*CHANNEL_PRESETS.keys(), "outro"]


def channel_label(key: str) -> str:
    """Rótulo legível de um canal — "Outro" para `key == "outro"` ou
    qualquer chave desconhecida (nunca levanta, para servir direto em
    `format_func` de widgets)."""
    preset = CHANNEL_PRESETS.get(key)
    return preset.label if preset is not None else "Outro"
