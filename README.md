# Magicpowers

Magicpowers is a complete AI novel/comic creation methodology for your content creation agents, built on top of a set of composable skills and some initial instructions that make sure your agent uses them.

## Document to Markdown

Documents can be normalized to Markdown with Microsoft
[MarkItDown](https://github.com/microsoft/markitdown):

```bash
uv run magicpowers ebook-convert book.epub
uv run magicpowers ebook-convert report.pdf -o notes/report.md
uv run magicpowers ebook-convert manuscript.docx --overwrite
```

PDF, DOCX, EPUB, HTML, CSV, JSON, plain text, and other supported inputs are
passed directly to MarkItDown.

The reusable Python API and Google ADK toolset are also available:

```python
from any_harness.tools import DocumentConversionToolset, convert_document

conversion, output_path = convert_document("book.epub")
toolset = DocumentConversionToolset(root="./workspace")
```

The converter rejects accidental overwrites unless `overwrite=True` (or
`--overwrite`) is supplied.
