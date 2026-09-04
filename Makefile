SHELL := /bin/sh
.DEFAULT_GOAL := help

UV ?= uv
DOCSIFY ?= docsify
UV_RUN := $(UV) run
DOCS_DIR ?= docs
PORT ?= 3000
PYTHON_SOURCES ?= any_harness tests
BOOK_OUTPUT ?= $(DOCS_DIR)/ebooks/$(notdir $(basename $(SOURCE))).md

OUTPUT_OPTION = $(if $(OUTPUT),--output "$(OUTPUT)")
ASSETS_OPTION = $(if $(ASSETS_DIR),--assets-dir "$(ASSETS_DIR)")
OVERWRITE_OPTION = $(if $(filter 1 true yes,$(OVERWRITE)),--overwrite)

.PHONY: help install sync lock build format format-check lint typecheck test check ci \
	ebook-convert book-add docs-serve clean

help: ## 显示可用目标和可覆盖变量
	@awk 'BEGIN {FS = ":.*## "; printf "Magicpowers\n\n用法: make <目标> [变量=值]\n\n目标:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\n变量:\n  SOURCE         待转换书籍路径\n  OUTPUT         Markdown 输出路径\n  ASSETS_DIR     图片资源目录\n  OVERWRITE      设为 1/true/yes 时允许覆盖\n  DOCS_DIR       Docsify 目录（默认 docs）\n  PORT           Docsify 服务端口（默认 3000）\n'

install: sync ## 安装项目及开发依赖

sync: ## 同步项目及开发依赖
	$(UV) sync --all-groups

lock: ## 刷新依赖锁文件
	$(UV) lock

build: ## 构建 Python 发布包
	$(UV) build

format: ## 格式化 Python 代码
	$(UV_RUN) ruff format $(PYTHON_SOURCES)
	$(UV_RUN) ruff check --fix $(PYTHON_SOURCES)

format-check: ## 检查代码格式但不修改文件
	$(UV_RUN) ruff format --check $(PYTHON_SOURCES)

lint: ## 运行 Ruff 静态检查
	$(UV_RUN) ruff check $(PYTHON_SOURCES)

typecheck: ## 运行 Pyright 类型检查
	$(UV_RUN) pyright

test: ## 运行全部测试
	$(UV_RUN) pytest -q

check: format-check lint typecheck test ## 运行提交前的全部质量检查

ci: sync check build ## 执行完整 CI 验证流程

ebook-convert: ## 转换书籍；用法: make ebook-convert SOURCE=book.epub [OUTPUT=docs/ebooks/book.md]
	@test -n "$(SOURCE)" || { printf '错误: 请提供 SOURCE，例如 make ebook-convert SOURCE=book.epub\n' >&2; exit 2; }
	$(UV_RUN) magicpowers ebook-convert "$(SOURCE)" $(OUTPUT_OPTION) $(ASSETS_OPTION) $(OVERWRITE_OPTION)

book-add: ## 转换书籍到 docs/ebooks；用法: make book-add SOURCE=book.epub
	@test -n "$(SOURCE)" || { printf '错误: 请提供 SOURCE，例如 make book-add SOURCE=book.epub\n' >&2; exit 2; }
	$(UV_RUN) magicpowers ebook-convert "$(SOURCE)" --output "$(BOOK_OUTPUT)" $(ASSETS_OPTION) $(OVERWRITE_OPTION)

docs-serve: ## 启动静态 Docsify 阅读站
	$(DOCSIFY) serve "$(DOCS_DIR)" --port "$(PORT)"

clean: ## 清理 Python 构建及测试缓存（不删除书籍和文档）
	find any_harness tests -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist
	find . -maxdepth 2 -type d -name '*.egg-info' -prune -exec rm -rf {} +
