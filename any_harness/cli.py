"""Command-line interface for Magicpowers."""

from __future__ import annotations

import json
from pathlib import Path

import click

from .tools.ebook_convert import DocumentConversionError, convert_document


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Magicpowers command-line tools."""


@cli.command("ebook-convert")
@click.argument(
    "source",
    type=click.Path(path_type=Path, exists=True, file_okay=True, dir_okay=False, readable=True),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path, file_okay=True, dir_okay=False, writable=True),
    help="Output Markdown path. Defaults to the source filename with a .md extension.",
)
@click.option(
    "--assets-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True, writable=True),
    help="Image output directory. Defaults to <output-stem>_assets.",
)
@click.option("--overwrite", is_flag=True, help="Replace an existing output file.")
def ebook_convert(source: Path, output: Path | None, assets_dir: Path | None, overwrite: bool) -> None:
    """Convert SOURCE to Markdown using MarkItDown.

    Supported inputs include PDF, DOCX, EPUB, HTML, CSV, JSON, and plain text.
    """
    try:
        conversion, output_path = convert_document(source, output, overwrite=overwrite, assets_dir=assets_dir)
    except (DocumentConversionError, FileNotFoundError, FileExistsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        json.dumps(
            {
                "source": str(conversion.source),
                "output": str(output_path),
                "title": conversion.title,
                "characters": len(conversion.markdown),
                "images": len(conversion.images),
                "backend": conversion.backend,
            },
            ensure_ascii=False,
        )
    )
