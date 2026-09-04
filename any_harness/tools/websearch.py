"""Google ADK toolset for web and news search through DDGS."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
import json
import logging
import time
from typing import Any, Literal, cast, override

from bs4 import BeautifulSoup
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_configs import ToolArgsConfig
from google.adk.tools import load_web_page as adk_load_web_page
import requests

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException
except ImportError as exc:  # pragma: no cover - exercised only in broken installations
    raise ImportError("`ddgs` not installed. Please install using `pip install ddgs`") from exc


logger = logging.getLogger(__name__)

Timelimit = Literal["d", "w", "m", "y"]
VALID_TIMELIMITS = frozenset({"d", "w", "m", "y"})


class WebSearchToolset(BaseToolset):
    """Expose DDGS web and news search as Google ADK tools."""

    def __init__(
        self,
        enable_search: bool = True,
        enable_news: bool = True,
        backend: str = "auto",
        modifier: str | None = None,
        fixed_max_results: int | None = None,
        proxy: str | None = None,
        timeout: int | None = 10,
        verify_ssl: bool = True,
        timelimit: Timelimit | None = None,
        region: str | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        **kwargs: Any,
    ) -> None:
        if timelimit is not None and timelimit not in VALID_TIMELIMITS:
            raise ValueError(
                f"Invalid timelimit '{timelimit}'. Must be one of: 'd' (day), 'w' (week), 'm' (month), 'y' (year)."
            )
        if max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be greater than or equal to 0")

        super().__init__(**kwargs)
        self.proxy = proxy
        self.timeout = timeout
        self.fixed_max_results = fixed_max_results
        self.modifier = modifier
        self.verify_ssl = verify_ssl
        self.backend = backend
        self.timelimit = timelimit
        self.region = region
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

        methods = []
        if enable_search:
            methods.append((self.web_search, self.aweb_search))
        if enable_news:
            methods.append((self.load_web_page, self.aload_web_page))
        self._tools = [self._function_tool(sync_method, async_method) for sync_method, async_method in methods]

    @staticmethod
    def _function_tool(sync_method: Callable[..., str], async_method: Callable[..., Any]) -> FunctionTool:
        @wraps(sync_method)
        async def call_async(*args: Any, **kwargs: Any) -> str:
            return await async_method(*args, **kwargs)

        return FunctionTool(call_async)

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        return [tool for tool in self._tools if self._is_tool_selected(tool, readonly_context)]

    def _search_kwargs(self, query: str, max_results: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "query": query,
            "max_results": self.fixed_max_results or max_results,
            "backend": self.backend,
        }
        if self.timelimit is not None:
            kwargs["timelimit"] = self.timelimit
        if self.region is not None:
            kwargs["region"] = self.region
        return kwargs

    def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web for a query.

        Args:
            query: The query to search for.
            max_results: Maximum number of results to return.

        Returns:
            JSON-encoded web search results.
        """
        search_query = f"{self.modifier} {query}" if self.modifier else query
        for attempt in range(self.max_retries + 1):
            try:
                with DDGS(proxy=self.proxy, timeout=self.timeout, verify=self.verify_ssl) as ddgs:
                    results = ddgs.text(**self._search_kwargs(search_query, max_results))
                return json.dumps(results, indent=2, ensure_ascii=False)
            except DDGSException as exc:
                if attempt < self.max_retries:
                    delay = self.retry_backoff_seconds * (2**attempt)
                    logger.warning(
                        "Web search failed for %r (%d/%d); retrying in %.1fs: %s",
                        search_query,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                        exc,
                    )
                    if delay:
                        time.sleep(delay)
                    continue
                logger.error(
                    "Web search unavailable for %r after %d attempts: %s",
                    search_query,
                    self.max_retries + 1,
                    exc,
                )
                return json.dumps(
                    {
                        "results": [],
                        "error": {
                            "code": "WEB_SEARCH_UNAVAILABLE",
                            "message": str(exc),
                            "query": search_query,
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                )

        raise AssertionError("unreachable")

    def load_web_page(self, url: str) -> str:
        """Fetches the content in the url and returns the text in it.

         Args:
             url (str): The url to browse.

         Returns:
             str: The text content of the url.
        """
        fetch_response = cast(
            Callable[[str], requests.Response] | None,
            getattr(adk_load_web_page, "_fetch_response", None),
        )
        if not callable(fetch_response):
            logger.error("The installed google-adk version does not expose its secure web fetcher")
            return f"Failed to fetch url: {url}"

        try:
            response = fetch_response(url)
        except (ValueError, requests.RequestException):
            return f"Failed to fetch url: {url}"

        if response.status_code != 200:
            return f"Failed to fetch url: {url}"

        soup = BeautifulSoup(response.content, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        return "\n".join(line for line in text.splitlines() if line.strip())

    async def aweb_search(self, query: str, max_results: int = 5) -> str:
        """Async variant of ``web_search``."""
        return await asyncio.to_thread(self.web_search, query, max_results)

    async def aload_web_page(self, url: str) -> str:
        """Async variant of ``load_web_page``."""
        return await asyncio.to_thread(self.load_web_page, url)

    @override
    @classmethod
    def from_config(
          cls: type[WebSearchToolset], config: ToolArgsConfig, config_abs_path: str
    ) -> WebSearchToolset:
        del config_abs_path
        return cls(**config.model_dump())

