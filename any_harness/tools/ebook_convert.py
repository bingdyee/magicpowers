"""Convert workspace documents and ebooks to Markdown with MarkItDown."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re
import tempfile
from typing import Any, override
import unicodedata
from urllib.parse import quote, unquote, urlsplit
import zipfile

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_configs import ToolArgsConfig
from markitdown import MarkItDown

from ._paths import resolve_working_path


class DocumentConversionError(RuntimeError):
    """Raised when a document cannot be converted to Markdown."""


@dataclass(frozen=True, slots=True)
class MarkdownConversion:
    """The in-memory result of one document conversion."""

    source: Path
    markdown: str
    title: str | None
    backend: str
    images: tuple[ImageAsset, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageAsset:
    """An image referenced by converted Markdown and ready to be written."""

    reference: str
    filename: str
    data: bytes


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)")
_DATA_IMAGE_RE = re.compile(r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+)(?:;[^,]*)?;base64,(?P<data>[a-zA-Z0-9+/=\s]+)$")
_IMAGE_EXTENSIONS = frozenset({".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"})


def _safe_image_filename(index: int, original_name: str | None, mime_type: str | None = None) -> str:
    name = Path(original_name or "").name
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip(".-")
    if not name:
        extension = mimetypes.guess_extension(mime_type or "") or ".bin"
        if extension == ".jpe":
            extension = ".jpg"
        name = f"image{extension}"
    return f"image-{index:03d}-{name}"


def _markdown_image_targets(markdown: str) -> list[str]:
    targets: list[str] = []
    for match in _MARKDOWN_IMAGE_RE.finditer(markdown):
        target = match.group("target")
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if target not in targets:
            targets.append(target)
    return targets


def _extract_data_images(markdown: str) -> tuple[str, list[ImageAsset]]:
    assets: list[ImageAsset] = []
    for target in _markdown_image_targets(markdown):
        match = _DATA_IMAGE_RE.fullmatch(target)
        if match is None:
            continue
        try:
            data = base64.b64decode(re.sub(r"\s+", "", match.group("data")), validate=True)
        except ValueError as exc:
            raise DocumentConversionError("MarkItDown returned an invalid embedded image") from exc
        token = f"__MAGICPOWERS_IMAGE_{len(assets) + 1:03d}__"
        assets.append(
            ImageAsset(
                reference=token,
                filename=_safe_image_filename(len(assets) + 1, None, match.group("mime")),
                data=data,
            )
        )
        markdown = markdown.replace(target, token)
    return markdown, assets


def _find_archive_member(reference: str, members: list[str]) -> str | None:
    reference_path = unquote(urlsplit(reference).path)
    normalized = posixpath.normpath(reference_path).lstrip("/")
    if normalized.startswith("../"):
        normalized = normalized.removeprefix("../")
    matches = [member for member in members if member == normalized or member.endswith(f"/{normalized}")]
    if len(matches) == 1:
        return matches[0]
    basename = posixpath.basename(normalized)
    basename_matches = [member for member in members if posixpath.basename(member) == basename]
    return basename_matches[0] if len(basename_matches) == 1 else None


def _extract_epub_images(source: Path, markdown: str, start_index: int) -> list[ImageAsset]:
    assets: list[ImageAsset] = []
    with zipfile.ZipFile(source) as archive:
        members = [name for name in archive.namelist() if Path(name).suffix.casefold() in _IMAGE_EXTENSIONS]
        for target in _markdown_image_targets(markdown):
            if urlsplit(target).scheme or target.startswith("__MAGICPOWERS_IMAGE_"):
                continue
            member = _find_archive_member(target, members)
            if member is None:
                continue
            index = start_index + len(assets)
            assets.append(
                ImageAsset(
                    reference=target,
                    filename=_safe_image_filename(index, posixpath.basename(member)),
                    data=archive.read(member),
                )
            )
    return assets


def _extract_local_images(source: Path, markdown: str, start_index: int) -> list[ImageAsset]:
    assets: list[ImageAsset] = []
    for target in _markdown_image_targets(markdown):
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith("__MAGICPOWERS_IMAGE_"):
            continue
        candidate = (source.parent / unquote(parsed.path)).resolve()
        try:
            candidate.relative_to(source.parent)
        except ValueError:
            continue
        if not candidate.is_file() or candidate.suffix.casefold() not in _IMAGE_EXTENSIONS:
            continue
        index = start_index + len(assets)
        assets.append(
            ImageAsset(
                reference=target,
                filename=_safe_image_filename(index, candidate.name),
                data=candidate.read_bytes(),
            )
        )
    return assets


def _extract_pdf_images(source: Path, start_index: int) -> list[tuple[int, ImageAsset]]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        # MarkItDown can still extract PDF text without PDFium; image extraction
        # is best-effort when the optional rendering dependency is unavailable.
        return []

    assets: list[tuple[int, ImageAsset]] = []
    document = pdfium.PdfDocument(source)
    try:
        for page_number, page in enumerate(document, start=1):
            try:
                page_images = page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE])
                for page_image_number, image in enumerate(page_images, start=1):
                    if not isinstance(image, pdfium.PdfImage):
                        continue
                    bitmap = image.get_bitmap(render=True)
                    try:
                        buffer = BytesIO()
                        bitmap.to_pil().save(buffer, format="PNG")
                    finally:
                        bitmap.close()
                    index = start_index + len(assets)
                    token = f"__MAGICPOWERS_IMAGE_{index:03d}__"
                    assets.append(
                        (
                            page_number,
                            ImageAsset(
                                reference=token,
                                filename=f"page-{page_number:03d}-image-{page_image_number:03d}.png",
                                data=buffer.getvalue(),
                            ),
                        )
                    )
            finally:
                page.close()
    finally:
        document.close()
    return assets


def _collect_images(source: Path, markdown: str) -> tuple[str, tuple[ImageAsset, ...]]:
    markdown, assets = _extract_data_images(markdown)
    suffix = source.suffix.casefold()
    if suffix == ".epub":
        assets.extend(_extract_epub_images(source, markdown, len(assets) + 1))
    elif suffix == ".pdf":
        pdf_images = _extract_pdf_images(source, len(assets) + 1)
        if pdf_images:
            image_lines = ["## Extracted images"]
            for page_number, asset in pdf_images:
                image_lines.append(f"![Page {page_number} image]({asset.reference})")
                assets.append(asset)
            markdown = f"{markdown.rstrip()}\n\n" + "\n\n".join(image_lines)
    elif suffix in {".html", ".htm"}:
        assets.extend(_extract_local_images(source, markdown, len(assets) + 1))
    return markdown, tuple(assets)


def _markitdown_convert(source: Path) -> MarkdownConversion:
    try:
        result = MarkItDown(enable_plugins=False).convert_local(source, keep_data_uris=True)
    except Exception as exc:
        raise DocumentConversionError(f"MarkItDown could not convert {source.name}: {exc}") from exc
    markdown, images = _collect_images(source, result.markdown)
    return MarkdownConversion(
        source=source,
        markdown=markdown,
        title=result.title,
        backend="markitdown",
        images=images,
    )


def convert_to_markdown(
    source: str | Path,
) -> MarkdownConversion:
    """Convert a local file to Markdown without writing the result.

    The source is passed directly to MarkItDown, which supports PDF, DOCX,
    EPUB, HTML, CSV, JSON, plain text, and other formats.
    """
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    return _markitdown_convert(source_path)


def write_markdown(
    conversion: MarkdownConversion,
    output: str | Path | None = None,
    *,
    overwrite: bool = False,
    assets_dir: str | Path | None = None,
) -> Path:
    """Atomically write a conversion result to a UTF-8 Markdown file."""
    output_path = Path(output).expanduser().resolve() if output is not None else conversion.source.with_suffix(".md")
    if output_path == conversion.source:
        raise DocumentConversionError("Output path must be different from the source path")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image_paths: list[tuple[ImageAsset, Path]] = []
    if conversion.images:
        image_directory = (
            Path(assets_dir).expanduser().resolve()
            if assets_dir is not None
            else output_path.parent / f"{output_path.stem}_assets"
        )
        image_directory.mkdir(parents=True, exist_ok=True)
        image_paths = [(asset, image_directory / asset.filename) for asset in conversion.images]
        if not overwrite:
            existing_image = next((path for _, path in image_paths if path.exists()), None)
            if existing_image is not None:
                raise FileExistsError(f"Image file already exists: {existing_image}")

    markdown = conversion.markdown
    for asset, image_path in image_paths:
        relative_path = Path(os.path.relpath(image_path, output_path.parent)).as_posix()
        markdown = markdown.replace(asset.reference, quote(relative_path, safe="/@"))
    markdown = markdown.rstrip() + "\n"

    for asset, image_path in image_paths:
        image_path.write_bytes(asset.data)

    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        temporary_path.write_text(markdown, encoding="utf-8")
        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def convert_document(
    source: str | Path,
    output: str | Path | None = None,
    *,
    overwrite: bool = False,
    assets_dir: str | Path | None = None,
) -> tuple[MarkdownConversion, Path]:
    """Convert a document and write its Markdown output."""
    conversion = convert_to_markdown(source)
    return conversion, write_markdown(conversion, output, overwrite=overwrite, assets_dir=assets_dir)


class DocumentConversionToolset(BaseToolset):
    """Expose workspace-scoped document conversion as a Google ADK tool."""

    def __init__(
        self,
        root: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.root = Path(root or resolve_working_path("workspace")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

        async def call_convert_document(
            source_path: str,
            output_path: str | None = None,
            overwrite: bool = False,
            assets_dir: str | None = None,
        ) -> str:
            return await self.aconvert_document(source_path, output_path, overwrite=overwrite, assets_dir=assets_dir)

        call_convert_document.__name__ = "convert_document"
        call_convert_document.__doc__ = self.convert_document.__doc__
        self._tool = FunctionTool(call_convert_document)

    @override
    @classmethod
    def from_config(
        cls: type[DocumentConversionToolset], config: ToolArgsConfig, config_abs_path: str
    ) -> DocumentConversionToolset:
        del config_abs_path
        values = config.model_dump()
        root = values.pop("workspace_dir", None)
        return cls(root=root, **values)

    def _resolve_workspace_path(self, path: str) -> Path:
        if not path or not path.strip():
            raise ValueError("path cannot be empty")
        normalized = unicodedata.normalize("NFKC", path)
        if any(unicodedata.category(character) == "Cc" for character in normalized):
            raise ValueError("path contains control characters")
        windows_path = PureWindowsPath(normalized)
        if windows_path.drive or windows_path.is_absolute() or Path(normalized).is_absolute():
            raise ValueError("path must be relative to the workspace")
        resolved = (self.root / normalized.replace("\\", "/")).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("path escapes workspace root") from None
        return resolved

    def convert_document(
        self,
        source_path: str,
        output_path: str | None = None,
        overwrite: bool = False,
        assets_dir: str | None = None,
    ) -> str:
        """Convert a workspace document or ebook to a Markdown file.

        The input is passed directly to MarkItDown. Supported formats include
        PDF, DOCX, EPUB, HTML, CSV, JSON, and plain text.

        Args:
            source_path: Source path relative to the workspace root.
            output_path: Optional output path relative to the workspace root.
                Defaults to the source filename with a .md extension.
            assets_dir: Optional image directory relative to the workspace root.
                Defaults to <output-stem>_assets.
            overwrite: Replace an existing output file when true.
        """
        try:
            source = self._resolve_workspace_path(source_path)
            output = self._resolve_workspace_path(output_path) if output_path else source.with_suffix(".md")
            image_directory = self._resolve_workspace_path(assets_dir) if assets_dir else None
            conversion, written_path = convert_document(
                source,
                output,
                overwrite=overwrite,
                assets_dir=image_directory,
            )
            return json.dumps(
                {
                    "source": source.relative_to(self.root).as_posix(),
                    "output": written_path.relative_to(self.root).as_posix(),
                    "title": conversion.title,
                    "characters": len(conversion.markdown),
                    "images": len(conversion.images),
                    "backend": conversion.backend,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return f"Error converting document: {exc}"

    async def aconvert_document(
        self,
        source_path: str,
        output_path: str | None = None,
        overwrite: bool = False,
        assets_dir: str | None = None,
    ) -> str:
        """Async variant of ``convert_document``."""
        return await asyncio.to_thread(
            self.convert_document,
            source_path,
            output_path,
            overwrite,
            assets_dir,
        )

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        return [self._tool] if self._is_tool_selected(self._tool, readonly_context) else []
