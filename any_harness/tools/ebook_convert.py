"""Convert workspace documents and ebooks to Markdown with MarkItDown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
from typing import Any, override
import unicodedata

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


def _markitdown_convert(source: Path) -> MarkdownConversion:
    try:
        result = MarkItDown(enable_plugins=False).convert_local(source)
    except Exception as exc:
        raise DocumentConversionError(f"MarkItDown could not convert {source.name}: {exc}") from exc
    return MarkdownConversion(
        source=source,
        markdown=result.markdown,
        title=result.title,
        backend="markitdown",
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
) -> Path:
    """Atomically write a conversion result to a UTF-8 Markdown file."""
    output_path = Path(output).expanduser().resolve() if output is not None else conversion.source.with_suffix(".md")
    if output_path == conversion.source:
        raise DocumentConversionError("Output path must be different from the source path")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = conversion.markdown.rstrip() + "\n"
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
) -> tuple[MarkdownConversion, Path]:
    """Convert a document and write its Markdown output."""
    conversion = convert_to_markdown(source)
    return conversion, write_markdown(conversion, output, overwrite=overwrite)


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
        ) -> str:
            return await self.aconvert_document(source_path, output_path, overwrite)

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
    ) -> str:
        """Convert a workspace document or ebook to a Markdown file.

        The input is passed directly to MarkItDown. Supported formats include
        PDF, DOCX, EPUB, HTML, CSV, JSON, and plain text.

        Args:
            source_path: Source path relative to the workspace root.
            output_path: Optional output path relative to the workspace root.
                Defaults to the source filename with a .md extension.
            overwrite: Replace an existing output file when true.
        """
        try:
            source = self._resolve_workspace_path(source_path)
            output = self._resolve_workspace_path(output_path) if output_path else source.with_suffix(".md")
            conversion, written_path = convert_document(
                source,
                output,
                overwrite=overwrite,
            )
            return json.dumps(
                {
                    "source": source.relative_to(self.root).as_posix(),
                    "output": written_path.relative_to(self.root).as_posix(),
                    "title": conversion.title,
                    "characters": len(conversion.markdown),
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
    ) -> str:
        """Async variant of ``convert_document``."""
        return await asyncio.to_thread(self.convert_document, source_path, output_path, overwrite)

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        return [self._tool] if self._is_tool_selected(self._tool, readonly_context) else []
