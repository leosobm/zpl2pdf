from zpl2pdf.cli import build_arg_parser, merge_config


def _parse(extra_args: list[str]):
    parser = build_arg_parser()
    return parser.parse_args(["--input", "in.zpl", "--output", "out.pdf", *extra_args])


def test_merge_config_without_channel_uses_plain_defaults():
    args = _parse([])
    merged = merge_config(args, file_config={})

    assert merged["sheet_size"] == "A4"
    assert merged["margin_top"] == 10.0
    assert merged["split_grid"] is False
    assert merged["multi_mode"] is None


def test_merge_config_applies_shopee_channel_preset():
    args = _parse(["--channel", "shopee"])
    merged = merge_config(args, file_config={})

    assert merged["sheet_size"] == "100x150"
    assert merged["orientation"] == "portrait"
    assert merged["dpi"] == 203
    assert (merged["margin_top"], merged["margin_bottom"], merged["margin_left"], merged["margin_right"]) == (
        0.0, 0.0, 0.0, 0.0,
    )
    assert (merged["gap_x"], merged["gap_y"]) == (0.0, 0.0)
    assert (merged["label_width"], merged["label_height"]) == (100.0, 150.0)
    assert merged["multi_mode"] == "paginate"
    assert merged["labels_per_page"] == 3
    assert merged["split_grid"] is True


def test_merge_config_applies_mercado_livre_channel_preset():
    args = _parse(["--channel", "mercado_livre"])
    merged = merge_config(args, file_config={})

    assert (merged["margin_top"], merged["margin_bottom"], merged["margin_left"], merged["margin_right"]) == (
        10.0, 10.0, 0.0, 0.0,
    )
    assert (merged["gap_x"], merged["gap_y"]) == (0.0, 5.0)
    assert (merged["label_width"], merged["label_height"]) == (60.0, 40.0)
    assert merged["labels_per_page"] == 3
    assert merged["split_grid"] is False


def test_merge_config_outro_channel_applies_no_preset():
    args = _parse(["--channel", "outro"])
    merged = merge_config(args, file_config={})

    assert merged["sheet_size"] == "A4"
    assert merged["margin_top"] == 10.0
    assert merged["split_grid"] is False


def test_explicit_cli_flag_overrides_channel_preset():
    # --channel shopee define dpi=203, mas --dpi 300 explícito deve vencer.
    args = _parse(["--channel", "shopee", "--dpi", "300"])
    merged = merge_config(args, file_config={})

    assert merged["dpi"] == 300
    # O resto do preset da Shopee continua valendo.
    assert merged["split_grid"] is True


def test_file_config_split_grid_false_overrides_channel_preset_true():
    args = _parse(["--channel", "shopee"])
    merged = merge_config(args, file_config={"split_grid": False})

    assert merged["split_grid"] is False


def test_cli_split_grid_flag_always_wins_true():
    args = _parse(["--channel", "mercado_livre", "--split-grid"])
    merged = merge_config(args, file_config={})

    # mercado_livre não ativa split_grid por padrão, mas a flag explícita liga.
    assert merged["split_grid"] is True


def test_channel_from_config_file_when_no_cli_flag():
    args = _parse([])
    merged = merge_config(args, file_config={"channel": "magalu"})

    assert merged["labels_per_page"] == 3
    assert (merged["margin_top"], merged["margin_bottom"]) == (10.0, 10.0)
