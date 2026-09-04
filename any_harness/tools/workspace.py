"""Google ADK toolset for scoped local workspace operations."""

from __future__ import annotations
import os
import re
import json
import asyncio
import subprocess
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Any, override
from functools import wraps
from fnmatch import fnmatch

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_configs import ToolArgsConfig

from ._paths import resolve_working_path


DEFAULT_EXCLUDE_PATTERNS = [
    ".context",
    ".conductor",
    ".claude",
    ".codex",
    ".cursor",
    ".venv",
    ".venvs",
    "venv",
    ".env*",
    "*.env",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".nox",
    ".ipynb_checkpoints",
    "dist",
    "build",
    "*.egg-info",
    "node_modules",
    ".next",
    ".turbo",
    ".nuxt",
    ".svelte-kit",
    ".docusaurus",
    ".parcel-cache",
    ".nyc_output",
    "*.tsbuildinfo",
    ".serverless",
    ".gradle",
    ".kotlin",
    "*.class",
    ".dart_tool",
    ".flutter-plugins",
    ".flutter-plugins-dependencies",
    ".build",
    "xcuserdata",
    "*.xcuserstate",
    ".bundle",
    "*.gem",
    ".yardoc",
    "_build",
    ".elixir_ls",
    ".vs",
    ".terraform",
    "*.tfstate",
    "*.tfstate.*",
    ".terragrunt-cache",
    ".DS_Store",
]

TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".rst",
    ".log",
    ".toml",
    ".cfg",
    ".ini",
    ".env",
    ".editorconfig",
    ".py",
    ".pyi",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".less",
    ".vue",
    ".svelte",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".rs",
    ".go",
    ".zig",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".groovy",
    ".gradle",
    ".cs",
    ".fs",
    ".csproj",
    ".fsproj",
    ".rb",
    ".php",
    ".pl",
    ".pm",
    ".ex",
    ".exs",
    ".erl",
    ".hs",
    ".ml",
    ".mli",
    ".swift",
    ".m",
    ".r",
    ".lua",
    ".dart",
    ".jl",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".sql",
    ".graphql",
    ".gql",
    ".tf",
    ".hcl",
    ".dockerfile",
    ".proto",
    ".avsc",
    ".thrift",
    ".makefile",
    ".cmake",
    ".bazel",
    ".bzl",
}

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_WINDOWS_RESERVED_NAMES_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", re.IGNORECASE)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _format_size(size: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _extract_snippet(content: str, query: str, context_chars: int = 200) -> str:
    index = content.lower().find(query.lower())
    if index == -1:
        return ""
    start = max(0, index - context_chars)
    end = min(len(content), index + len(query) + context_chars)
    snippet = content[start:end]
    return ("..." if start else "") + snippet + ("..." if end < len(content) else "")


def _format_with_line_numbers(text: str, start_line: int = 1) -> str:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(f"{number:6d}\t{line}" for number, line in enumerate(lines, start=start_line))


def _contains_control_chars(text: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in text)


class WorkspaceToolset(BaseToolset):
    """Local workspace tools implemented as a Google ADK toolset.

    File paths are restricted to ``root``. This is a path boundary, not a
    process sandbox: commands still run with the host process permissions.

    ``allowed`` and ``confirm`` contain short aliases. Allowed tools execute
    directly, confirmed tools use Google ADK's native tool confirmation flow,
    and tools in neither list are hidden from the model.
    """

    READ_TOOLS = ["read", "list", "search"]
    WRITE_TOOLS = ["write", "edit", "move", "delete", "shell"]
    ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
    _ALIASES = {
        "read": "read_file",
        "list": "list_files",
        "search": "search_content",
        "write": "write_file",
        "edit": "edit_file",
        "move": "move_file",
        "delete": "delete_file",
        "shell": "run_command",
    }

    def __init__(
        self,
        root: str | Path,
        allowed: list[str] | None = None,
        confirm: list[str] | None = None,
        require_read_before_write: bool = False,
        max_file_lines: int = 100_000,
        max_file_length: int = 10_000_000,
        exclude_patterns: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.root = Path(root or resolve_working_path("workspace")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.require_read_before_write = require_read_before_write
        self.max_file_lines = max_file_lines
        self.max_file_length = max_file_length
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS) if exclude_patterns is None else list(exclude_patterns)
        self._read_paths: set[Path] = set()
        allowed_aliases, confirm_aliases = self._resolve_partitions(allowed, confirm)
        self._tools = self._build_tools(allowed_aliases, confirm_aliases)

    @override
    @classmethod
    def from_config(cls: type[WorkspaceToolset], config: ToolArgsConfig, config_abs_path: str) -> WorkspaceToolset:
        config_values = config.model_dump()
        workspace_dir = config_values.pop("workspace_dir", None)
        return cls(root=workspace_dir, **config_values)

    @classmethod
    def _resolve_partitions(cls, allowed: list[str] | None, confirm: list[str] | None) -> tuple[list[str], list[str]]:
        for arg_name, arg_value in (("allowed", allowed), ("confirm", confirm)):
            if arg_value is not None and not isinstance(arg_value, list):
                raise TypeError(
                    f"`{arg_name}` must be a list of aliases, got {type(arg_value).__name__}: "
                    f"{arg_value!r}. Valid aliases: {cls.ALL_TOOLS}"
                )
        if allowed is None and confirm is None:
            return list(cls.READ_TOOLS), list(cls.WRITE_TOOLS)
        allowed = allowed or []
        confirm = confirm or []
        valid = set(cls.ALL_TOOLS)
        unknown_allowed = set(allowed) - valid
        if unknown_allowed:
            raise ValueError(
                f"Unknown alias(es) in `allowed`: {sorted(unknown_allowed)}. Valid aliases: {cls.ALL_TOOLS}"
            )
        unknown_confirm = set(confirm) - valid
        if unknown_confirm:
            raise ValueError(
                f"Unknown alias(es) in `confirm`: {sorted(unknown_confirm)}. Valid aliases: {cls.ALL_TOOLS}"
            )
        overlap = set(allowed) & set(confirm)
        if overlap:
            raise ValueError(
                f"Alias(es) appear in both `allowed` and `confirm`: {sorted(overlap)}. They must be mutually exclusive."
            )
        return list(allowed), list(confirm)

    def _build_tools(self, allowed: list[str], confirm: list[str]) -> list[FunctionTool]:
        tools = [self._function_tool(alias, require_confirmation=False) for alias in allowed]
        tools.extend(self._function_tool(alias, require_confirmation=True) for alias in confirm)
        return tools

    def _function_tool(self, alias: str, require_confirmation: bool) -> FunctionTool:
        method_name = self._ALIASES[alias]
        async_function = getattr(self, "a" + method_name)
        sync_function = getattr(self, method_name)

        @wraps(sync_function)
        async def run_in_thread(*args: Any, **kwargs: Any) -> str:
            return await async_function(*args, **kwargs)

        return FunctionTool(run_in_thread, require_confirmation=require_confirmation)

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        return [tool for tool in self._tools if self._is_tool_selected(tool, readonly_context)]

    @override
    async def process_llm_request(self, *, tool_context: Any, llm_request: Any) -> None:
        """Add safe editing guidance when the edit tool is exposed."""
        del tool_context
        if any(tool.name == "edit_file" for tool in self._tools):
            llm_request.append_instructions(
                [
                    "Current workspace directory: " + str(self.root),
                    "Work within this workspace directory and do not access files outside of it.",
                    "This folder is home. Treat it that way.",
                    "Write It Down - No Mental Notes!",
                    "Always read_file before editing. Use the exact substring from its line-numbered output as "
                    "edit_file's old_str. Do not guess file contents or pass line numbers to edit_file.",
                ]
            )

    def _resolve_path(self, path: str) -> Path:
        if not path or not path.strip():
            raise ValueError("path cannot be empty")
        normalized = unicodedata.normalize("NFKC", path)
        if _contains_control_chars(normalized):
            raise ValueError("path contains control characters")
        windows_path = PureWindowsPath(normalized)
        if windows_path.drive or windows_path.is_absolute():
            raise ValueError("path must be relative")
        clean_parts = []
        for part in normalized.replace("\\", "/").split("/"):
            if part in ("", ".", ".."):
                clean_parts.append(part)
                continue
            stripped = part.rstrip(". ")
            if not stripped or _WINDOWS_RESERVED_NAMES_RE.match(stripped):
                raise ValueError(f"invalid path segment: {part!r}")
            clean_parts.append(stripped)
        resolved = (self.root / "/".join(clean_parts)).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("path escapes workspace root") from None
        return resolved

    def _is_excluded(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return False
        return any(fnmatch(part, pattern) for part in relative.parts for pattern in self.exclude_patterns)

    def _check_read_before_write(self, path: Path, operation: str) -> str | None:
        if not self.require_read_before_write or not path.exists() or path in self._read_paths:
            return None
        return (
            f"Error: require_read_before_write is enabled and {path.name} hasn't been read this session. "
            f"Call read_file first to confirm contents before the {operation}."
        )

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """Read a file with 1-indexed line numbers.

        Args:
            path: File path relative to the workspace root.
            start_line: Optional first line to return, inclusive.
            end_line: Optional last line to return, inclusive.
            encoding: Text encoding, defaulting to UTF-8.
        """
        try:
            file_path = self._resolve_path(path)
        except ValueError:
            return "Error: path escapes workspace root"
        try:
            if not file_path.is_file():
                return f"Error: file not found: {path}"
            contents = file_path.read_text(encoding=encoding)
            self._read_paths.add(file_path)
            if start_line is None and end_line is None:
                if len(contents) > self.max_file_length:
                    return (
                        f"Error: file too long ({len(contents)} chars > {self.max_file_length}). "
                        "Use start_line/end_line to read a chunk, or use search_content to find specific text first."
                    )
                line_count = contents.count("\n") + 1
                if line_count > self.max_file_lines:
                    return (
                        f"Error: file too long ({line_count} lines > {self.max_file_lines}). "
                        "Use start_line/end_line to read a chunk, or use search_content to find specific text first."
                    )
                return _format_with_line_numbers(contents)
            lines = contents.split("\n")
            start = start_line if start_line is not None else 1
            end = end_line if end_line is not None else len(lines)
            chunk = "\n".join(lines[max(0, start - 1) : min(len(lines), end)])
            return _format_with_line_numbers(chunk, start_line=start)
        except Exception as exc:
            return f"Error reading file: {exc}"

    def list_files(
        self,
        directory: str = ".",
        pattern: str | None = None,
        recursive: bool = False,
        max_depth: int = 3,
    ) -> str:
        """List workspace files and directories as JSON.

        Args:
            directory: Directory relative to the workspace root.
            pattern: Optional glob pattern.
            recursive: Whether to walk subdirectories.
            max_depth: Maximum recursive depth.
        """
        try:
            base = self._resolve_path(directory)
        except ValueError:
            return "Error: directory escapes workspace root"
        try:
            if not base.is_dir():
                return f"Error: not a directory: {directory}"
            entries: list[Path] = []
            if recursive:
                base_depth = len(base.parts)
                for dirpath, dirnames, filenames in os.walk(base):
                    current = Path(dirpath)
                    relative_depth = len(current.parts) - base_depth
                    visible_dirs = [name for name in dirnames if not self._is_excluded(current / name)]
                    if relative_depth >= max_depth:
                        dirnames[:] = []
                    else:
                        dirnames[:] = visible_dirs
                    for name in filenames + visible_dirs:
                        candidate = current / name
                        try:
                            self._resolve_path(candidate.relative_to(self.root).as_posix())
                        except ValueError, OSError:
                            continue
                        if self._is_excluded(candidate) or (pattern and not fnmatch(name, pattern)):
                            continue
                        entries.append(candidate)
            elif pattern:
                for candidate in base.glob(pattern):
                    try:
                        self._resolve_path(candidate.relative_to(self.root).as_posix())
                    except ValueError, OSError:
                        continue
                    if not self._is_excluded(candidate):
                        entries.append(candidate)
            else:
                for candidate in base.iterdir():
                    try:
                        self._resolve_path(candidate.relative_to(self.root).as_posix())
                    except ValueError, OSError:
                        continue
                    if not self._is_excluded(candidate):
                        entries.append(candidate)
            files = []
            for candidate in sorted(entries):
                try:
                    is_directory = candidate.is_dir()
                    size = None if is_directory else _format_size(candidate.stat().st_size)
                except OSError:
                    continue
                files.append(
                    {
                        "path": candidate.relative_to(self.root).as_posix(),
                        "type": "dir" if is_directory else "file",
                        "size": size,
                    }
                )
            return json.dumps(
                {"directory": directory, "pattern": pattern, "recursive": recursive, "files": files}, indent=2
            )
        except Exception as exc:
            return f"Error listing files: {exc}"

    def search_content(self, query: str, directory: str = ".", limit: int = 10) -> str:
        """Search workspace text files case-insensitively.

        Args:
            query: Substring to search for.
            directory: Directory relative to the workspace root.
            limit: Maximum number of matching files.
        """
        if not query or not query.strip():
            return "Error: query cannot be empty"
        try:
            base = self._resolve_path(directory)
        except ValueError:
            return "Error: directory escapes workspace root"
        try:
            if not base.is_dir():
                return f"Error: not a directory: {directory}"
            matches = []
            for dirpath, dirnames, filenames in os.walk(base):
                current = Path(dirpath)
                dirnames[:] = [name for name in dirnames if not self._is_excluded(current / name)]
                for filename in filenames:
                    if len(matches) >= limit:
                        break
                    file_path = current / filename
                    try:
                        resolved = self._resolve_path(file_path.relative_to(self.root).as_posix())
                    except ValueError, OSError:
                        continue
                    if self._is_excluded(file_path) or file_path.suffix.lower() not in TEXT_EXTENSIONS:
                        continue
                    try:
                        if resolved.stat().st_size > 500 * 1024:
                            continue
                        content = resolved.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    if query.lower() in content.lower():
                        matches.append(
                            {
                                "file": file_path.relative_to(self.root).as_posix(),
                                "size": _format_size(resolved.stat().st_size),
                                "snippet": _extract_snippet(content, query),
                            }
                        )
                if len(matches) >= limit:
                    break
            return json.dumps({"query": query, "matches_found": len(matches), "files": matches}, indent=2)
        except Exception as exc:
            return f"Error searching content: {exc}"

    def write_file(self, path: str, content: str, overwrite: bool = True, encoding: str = "utf-8") -> str:
        """Atomically create or overwrite a workspace file.

        Args:
            path: File path relative to the workspace root.
            content: Text to write.
            overwrite: Whether an existing file may be replaced.
            encoding: Text encoding, defaulting to UTF-8.
        """
        try:
            file_path = self._resolve_path(path)
        except ValueError:
            return "Error: path escapes workspace root"
        try:
            if file_path.exists() and not overwrite:
                return f"Error: file exists and overwrite=False: {path}"
            if error := self._check_read_before_write(file_path, "write"):
                return error
            file_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = file_path.with_name(file_path.name + ".tmp")
            try:
                temporary.write_text(content, encoding=encoding)
                os.replace(temporary, file_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._read_paths.add(file_path)
            return f"Wrote {len(content)} chars to {path}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    def edit_file(
        self,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        encoding: str = "utf-8",
    ) -> str:
        """Atomically replace exact text in a workspace file.

        Args:
            path: File path relative to the workspace root.
            old_str: Exact substring to replace.
            new_str: Replacement substring.
            replace_all: Whether to replace every occurrence.
            encoding: Text encoding, defaulting to UTF-8.
        """
        if not old_str:
            return "Error: old_str cannot be empty"
        try:
            file_path = self._resolve_path(path)
        except ValueError:
            return "Error: path escapes workspace root"
        try:
            if not file_path.is_file():
                return f"Error: file not found: {path}"
            if error := self._check_read_before_write(file_path, "edit"):
                return error
            contents = file_path.read_text(encoding=encoding)
            count = contents.count(old_str)
            if count == 0:
                return f"Error: old_str not found in {path}"
            if count > 1 and not replace_all:
                return (
                    f"Error: old_str matches {count} times in {path}; "
                    "provide a more unique snippet or pass replace_all=True"
                )
            replacements = count if replace_all else 1
            new_contents = contents.replace(old_str, new_str) if replace_all else contents.replace(old_str, new_str, 1)
            temporary = file_path.with_name(file_path.name + ".tmp")
            try:
                temporary.write_text(new_contents, encoding=encoding)
                os.replace(temporary, file_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            suffix = "s" if replacements != 1 else ""
            return f"Edited {path}: replaced {replacements} occurrence{suffix}"
        except Exception as exc:
            return f"Error editing file: {exc}"

    def move_file(self, src: str, dst: str, overwrite: bool = False) -> str:
        """Move or rename a file inside the workspace.

        Args:
            src: Source file path relative to the workspace root.
            dst: Destination file path relative to the workspace root.
            overwrite: Whether an existing destination may be replaced.
        """
        try:
            source = self._resolve_path(src)
        except ValueError:
            return "Error: src escapes workspace root"
        try:
            destination = self._resolve_path(dst)
        except ValueError:
            return "Error: dst escapes workspace root"
        try:
            if not source.exists():
                return f"Error: src not found: {src}"
            if source.is_dir():
                return f"Error: src is a directory, not a file: {src}"
            if destination.exists() and not overwrite:
                return f"Error: dst exists and overwrite=False: {dst}"
            if error := self._check_read_before_write(source, "move"):
                return error
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination) if overwrite else source.rename(destination)
            if source in self._read_paths:
                self._read_paths.remove(source)
                self._read_paths.add(destination)
            return f"Moved {src} -> {dst}"
        except Exception as exc:
            return f"Error moving file: {exc}"

    def delete_file(self, path: str) -> str:
        """Delete a file inside the workspace.

        Args:
            path: File path relative to the workspace root.
        """
        try:
            file_path = self._resolve_path(path)
        except ValueError:
            return "Error: path escapes workspace root"
        try:
            if not file_path.exists():
                return f"Error: file not found: {path}"
            if file_path.is_dir():
                return f"Error: path is a directory, not a file: {path}"
            if error := self._check_read_before_write(file_path, "delete"):
                return error
            file_path.unlink()
            self._read_paths.discard(file_path)
            return f"Deleted {path}"
        except Exception as exc:
            return f"Error deleting file: {exc}"

    def run_command(self, args: list[str], tail: int = 100, timeout: int = 120) -> str:
        """Run a command without a shell in the workspace root.

        Args:
            args: Executable and arguments as separate strings.
            tail: Maximum trailing output lines to return.
            timeout: Maximum runtime in seconds.
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=self.root,
                timeout=timeout,
            )
            if result.returncode != 0:
                error = "\n".join(_strip_ansi(result.stderr).splitlines()[-tail:])
                return f"Error (exit {result.returncode}): {error}"
            return "\n".join(_strip_ansi(result.stdout).splitlines()[-tail:])
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout} seconds"
        except Exception as exc:
            return f"Error running command: {exc}"

    async def aread_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """Async variant of ``read_file``."""
        return await asyncio.to_thread(self.read_file, path, start_line, end_line, encoding)

    async def alist_files(
        self,
        directory: str = ".",
        pattern: str | None = None,
        recursive: bool = False,
        max_depth: int = 3,
    ) -> str:
        """Async variant of ``list_files``."""
        return await asyncio.to_thread(self.list_files, directory, pattern, recursive, max_depth)

    async def asearch_content(self, query: str, directory: str = ".", limit: int = 10) -> str:
        """Async variant of ``search_content``."""
        return await asyncio.to_thread(self.search_content, query, directory, limit)

    async def awrite_file(
        self,
        path: str,
        content: str,
        overwrite: bool = True,
        encoding: str = "utf-8",
    ) -> str:
        """Async variant of ``write_file``."""
        return await asyncio.to_thread(self.write_file, path, content, overwrite, encoding)

    async def aedit_file(
        self,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        encoding: str = "utf-8",
    ) -> str:
        """Async variant of ``edit_file``."""
        return await asyncio.to_thread(self.edit_file, path, old_str, new_str, replace_all, encoding)

    async def amove_file(self, src: str, dst: str, overwrite: bool = False) -> str:
        """Async variant of ``move_file``."""
        return await asyncio.to_thread(self.move_file, src, dst, overwrite)

    async def adelete_file(self, path: str) -> str:
        """Async variant of ``delete_file``."""
        return await asyncio.to_thread(self.delete_file, path)

    async def arun_command(self, args: list[str], tail: int = 100, timeout: int = 120) -> str:
        """Run a command asynchronously in the workspace root."""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.root,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                return f"Error: command timed out after {timeout} seconds"
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            if process.returncode != 0:
                error = "\n".join(_strip_ansi(stderr).splitlines()[-tail:])
                return f"Error (exit {process.returncode}): {error}"
            return "\n".join(_strip_ansi(stdout).splitlines()[-tail:])
        except Exception as exc:
            return f"Error running command: {exc}"
