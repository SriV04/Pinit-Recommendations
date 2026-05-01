"""Test package setup.

Some production modules import optional infrastructure dependencies at module
load time. The API tests patch those integration points, so lightweight stubs
are enough to make imports deterministic in a local unit-test environment.
"""

from __future__ import annotations

import sys
import types


def install_optional_dependency_stubs() -> None:
    if "redis" not in sys.modules:
        redis_module = types.ModuleType("redis")
        exceptions_module = types.ModuleType("redis.exceptions")

        class RedisError(Exception):
            pass

        class Redis:
            def __init__(self, *args, **kwargs) -> None:
                raise RedisError("redis stub is not connected")

        exceptions_module.RedisError = RedisError
        redis_module.Redis = Redis
        redis_module.exceptions = exceptions_module
        sys.modules["redis"] = redis_module
        sys.modules["redis.exceptions"] = exceptions_module

    if "pdfplumber" not in sys.modules:
        pdfplumber_module = types.ModuleType("pdfplumber")

        def _open(*args, **kwargs):
            raise RuntimeError("pdfplumber stub cannot open files")

        pdfplumber_module.open = _open
        sys.modules["pdfplumber"] = pdfplumber_module

    if "crawl4ai" not in sys.modules:
        crawl4ai_module = types.ModuleType("crawl4ai")

        class AsyncWebCrawler:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

        class BrowserConfig:
            def __init__(self, *args, **kwargs) -> None:
                pass

        class CrawlerRunConfig:
            def __init__(self, *args, **kwargs) -> None:
                pass

        crawl4ai_module.AsyncWebCrawler = AsyncWebCrawler
        crawl4ai_module.BrowserConfig = BrowserConfig
        crawl4ai_module.CrawlerRunConfig = CrawlerRunConfig
        sys.modules["crawl4ai"] = crawl4ai_module


install_optional_dependency_stubs()
