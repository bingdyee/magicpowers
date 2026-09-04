from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from click.testing import CliRunner

from any_harness.cli import cli
from any_harness.tools.ebook_convert import (
    DocumentConversionToolset,
    convert_document,
)


TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)


def _write_epub(path: Path) -> None:
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title>
    <dc:creator>Magicpowers</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="book-id">test-book</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="images/cover.png" media-type="image/png"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    chapter = """<!doctype html>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter One</h1><p>Hello from an EPUB.</p><img src="images/cover.png" alt="Cover"/>
</body></html>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)
        archive.writestr("OEBPS/images/cover.png", TINY_PNG)


def test_epub_is_converted_with_markitdown(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    _write_epub(source)

    conversion, output = convert_document(source)

    assert conversion.backend == "markitdown"
    assert conversion.title == "Test Book"
    assert "# Chapter One" in conversion.markdown
    markdown = output.read_text(encoding="utf-8")
    assert "Hello from an EPUB." in markdown
    assert "book_assets/image-001-cover.png" in markdown
    assert (tmp_path / "book_assets" / "image-001-cover.png").read_bytes() == TINY_PNG
    assert len(conversion.images) == 1


def test_output_is_not_overwritten_by_default(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    output = tmp_path / "note.md"
    source.write_text("new content", encoding="utf-8")
    output.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        convert_document(source, output)

    assert output.read_text(encoding="utf-8") == "keep me"


def test_embedded_data_image_is_written_to_assets_directory(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    encoded = __import__("base64").b64encode(TINY_PNG).decode()
    source.write_text(f'<h1>Page</h1><img alt="Dot" src="data:image/png;base64,{encoded}">', encoding="utf-8")

    conversion, output = convert_document(source)

    markdown = output.read_text(encoding="utf-8")
    assert "page_assets/image-001-image.png" in markdown
    assert (tmp_path / "page_assets" / "image-001-image.png").read_bytes() == TINY_PNG
    assert len(conversion.images) == 1


def test_toolset_stays_inside_workspace(tmp_path: Path) -> None:
    toolset = DocumentConversionToolset(root=tmp_path)
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")

    result = json.loads(toolset.convert_document("note.txt"))

    assert result["output"] == "note.md"
    assert result["backend"] == "markitdown"
    assert result["images"] == 0
    assert toolset._tool.name == "convert_document"
    assert toolset.convert_document("../outside.pdf").startswith("Error converting document: path escapes")


def test_click_cli_exposes_ebook_convert_command(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    output = tmp_path / "note.md"
    source.write_text("hello from Click", encoding="utf-8")

    result = CliRunner().invoke(cli, ["ebook-convert", str(source), "--output", str(output)])

    assert result.exit_code == 0
    assert json.loads(result.output)["backend"] == "markitdown"
    assert json.loads(result.output)["images"] == 0
    assert "hello from Click" in output.read_text(encoding="utf-8")
