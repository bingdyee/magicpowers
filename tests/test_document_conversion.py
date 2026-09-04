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
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    chapter = """<!doctype html>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter One</h1><p>Hello from an EPUB.</p>
</body></html>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


def test_epub_is_converted_with_markitdown(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    _write_epub(source)

    conversion, output = convert_document(source)

    assert conversion.backend == "markitdown"
    assert conversion.title == "Test Book"
    assert "# Chapter One" in conversion.markdown
    assert "Hello from an EPUB." in output.read_text(encoding="utf-8")


def test_output_is_not_overwritten_by_default(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    output = tmp_path / "note.md"
    source.write_text("new content", encoding="utf-8")
    output.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        convert_document(source, output)

    assert output.read_text(encoding="utf-8") == "keep me"


def test_toolset_stays_inside_workspace(tmp_path: Path) -> None:
    toolset = DocumentConversionToolset(root=tmp_path)
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")

    result = json.loads(toolset.convert_document("note.txt"))

    assert result["output"] == "note.md"
    assert result["backend"] == "markitdown"
    assert toolset._tool.name == "convert_document"
    assert toolset.convert_document("../outside.pdf").startswith("Error converting document: path escapes")


def test_click_cli_exposes_ebook_convert_command(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    output = tmp_path / "note.md"
    source.write_text("hello from Click", encoding="utf-8")

    result = CliRunner().invoke(cli, ["ebook-convert", str(source), "--output", str(output)])

    assert result.exit_code == 0
    assert json.loads(result.output)["backend"] == "markitdown"
    assert "hello from Click" in output.read_text(encoding="utf-8")
