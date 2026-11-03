from zpl2pdf.channel_presets import CHANNEL_CHOICES, CHANNEL_PRESETS, channel_label


def test_channel_choices_has_all_three_channels_plus_outro_last():
    assert CHANNEL_CHOICES == ["mercado_livre", "shopee", "magalu", "outro"]


def test_channel_label_known_and_unknown():
    assert channel_label("mercado_livre") == "Mercado Livre"
    assert channel_label("shopee") == "Shopee"
    assert channel_label("magalu") == "Magalu"
    assert channel_label("outro") == "Outro"
    assert channel_label("nao-existe") == "Outro"


def test_common_fields_across_all_channels():
    for preset in CHANNEL_PRESETS.values():
        assert preset.sheet_width_mm == 100.0
        assert preset.sheet_height_mm == 150.0
        assert preset.orientation == "portrait"
        assert preset.dpi == 203
        assert preset.multi_mode == "paginate"
        assert preset.labels_per_page == 3


def test_mercado_livre_preset():
    p = CHANNEL_PRESETS["mercado_livre"]
    assert (p.margin_top_mm, p.margin_bottom_mm, p.margin_left_mm, p.margin_right_mm) == (10.0, 10.0, 0.0, 0.0)
    assert (p.gap_x_mm, p.gap_y_mm) == (0.0, 5.0)
    assert (p.fallback_width_mm, p.fallback_height_mm) == (60.0, 40.0)
    assert p.split_grid is False


def test_shopee_preset():
    p = CHANNEL_PRESETS["shopee"]
    assert (p.margin_top_mm, p.margin_bottom_mm, p.margin_left_mm, p.margin_right_mm) == (0.0, 0.0, 0.0, 0.0)
    assert (p.gap_x_mm, p.gap_y_mm) == (0.0, 0.0)
    assert (p.fallback_width_mm, p.fallback_height_mm) == (100.0, 150.0)
    assert p.split_grid is True


def test_magalu_preset_matches_mercado_livre():
    ml = CHANNEL_PRESETS["mercado_livre"]
    mg = CHANNEL_PRESETS["magalu"]
    assert (mg.margin_top_mm, mg.margin_bottom_mm, mg.margin_left_mm, mg.margin_right_mm) == (
        ml.margin_top_mm, ml.margin_bottom_mm, ml.margin_left_mm, ml.margin_right_mm,
    )
    assert (mg.gap_x_mm, mg.gap_y_mm) == (ml.gap_x_mm, ml.gap_y_mm)
    assert (mg.fallback_width_mm, mg.fallback_height_mm) == (ml.fallback_width_mm, ml.fallback_height_mm)
    assert mg.split_grid is False
