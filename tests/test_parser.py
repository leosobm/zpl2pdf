from pathlib import Path

import pytest

from zpl2pdf.parser import ZplParseError, parse_zpl_content, parse_zpl_file, split_labels


def test_split_labels_single():
    content = "^XA^FO50,50^A0N,50,50^FDOla^FS^XZ"
    blocks = split_labels(content)
    assert len(blocks) == 1
    assert blocks[0].startswith("^XA")
    assert blocks[0].endswith("^XZ")


def test_split_labels_multiple():
    content = """
    ^XA^PW406^LL203^FO10,10^FDEtiqueta 1^FS^XZ
    ^XA^PW406^LL203^FO10,10^FDEtiqueta 2^FS^XZ
    ^XA^PW406^LL203^FO10,10^FDEtiqueta 3^FS^XZ
    """
    blocks = split_labels(content)
    assert len(blocks) == 3


def test_split_labels_no_blocks_raises():
    with pytest.raises(ZplParseError):
        split_labels("isso nao e zpl valido")


def test_split_labels_unbalanced_raises():
    content = "^XA^FDetiqueta^FS^XZ^XA^FDoutra sem fechamento^FS"
    with pytest.raises(ZplParseError):
        split_labels(content)


def test_parse_zpl_content_extracts_pw_ll():
    content = "^XA^PW812^LL609^FO0,0^FDteste^FS^XZ"
    labels = parse_zpl_content(content)
    assert len(labels) == 1
    label = labels[0]
    assert label.pw_dots == 812
    assert label.ll_dots == 609
    assert label.has_explicit_size() is True


def test_parse_zpl_content_missing_pw_ll():
    content = "^XA^FO0,0^FDteste^FS^XZ"
    labels = parse_zpl_content(content)
    assert labels[0].pw_dots is None
    assert labels[0].ll_dots is None
    assert labels[0].has_explicit_size() is False


def test_parse_zpl_content_extracts_jm_mode():
    content = "^XA^JMA^PW406^LL203^FDteste^FS^XZ"
    labels = parse_zpl_content(content)
    assert labels[0].jm_mode == "A"


def test_parse_zpl_content_indexes_labels_in_order():
    content = "^XA^FDum^FS^XZ^XA^FDdois^FS^XZ"
    labels = parse_zpl_content(content)
    assert [l.index for l in labels] == [0, 1]
    assert "um" in labels[0].raw
    assert "dois" in labels[1].raw


def test_split_labels_bundles_preceding_dg_and_drops_image_delete_blocks():
    """Padrão comum em exportações do Zebra Setup Utilities: um comando ~DG
    (fora de ^XA...^XZ) armazena uma imagem, um bloco ^XA a recupera via ^XG
    e imprime, e um bloco ^XA...^ID...^XZ seguinte só a apaga da memória
    (sem desenhar nada). Sem agrupar o ~DG com o bloco de impressão, o
    Labelary recebe o ^XG sem a imagem correspondente e falha com HTTP 404
    "ZPL generated no labels"."""
    content = (
        "~DGR:LOGO.GRF,100,10,:Z64:FAKEDATA1:1234"
        "^XA^MMT,Y^PON^MNY^FO0,0^XGR:LOGO.GRF,1,1^FS^PQ1,0,0,N^XZ"
        "^XA^IDR:LOGO.GRF^FS^XZ"
        "~DGR:LOGO.GRF,100,10,:Z64:FAKEDATA2:5678"
        "^XA^MMT,Y^PON^MNY^FO0,0^XGR:LOGO.GRF,1,1^FS^PQ1,0,0,N^XZ"
        "^XA^IDR:LOGO.GRF^FS^XZ"
    )
    blocks = split_labels(content)

    assert len(blocks) == 2
    for block in blocks:
        assert block.startswith("~DGR:LOGO.GRF")
        assert "^XG" in block
        assert "^ID" not in block
    assert "FAKEDATA1" in blocks[0]
    assert "FAKEDATA2" in blocks[1]


def test_split_labels_all_image_delete_only_raises():
    content = "^XA^IDR:LOGO.GRF^FS^XZ^XA^IDR:LOGO.GRF^FS^XZ"
    with pytest.raises(ZplParseError):
        split_labels(content)


def test_parse_zpl_file_not_found(tmp_path: Path):
    missing = tmp_path / "nao_existe.zpl"
    with pytest.raises(ZplParseError):
        parse_zpl_file(missing)


def test_parse_zpl_file_reads_real_file(tmp_path: Path):
    zpl_file = tmp_path / "teste.zpl"
    zpl_file.write_text("^XA^PW406^LL203^FDola^FS^XZ", encoding="utf-8")
    labels = parse_zpl_file(zpl_file)
    assert len(labels) == 1
    assert labels[0].pw_dots == 406
