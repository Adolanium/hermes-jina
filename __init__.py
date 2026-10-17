"""Jina Reader backend for Hermes' web_search and web_extract tools."""


def register(ctx):
    from .provider import JinaWebSearchProvider

    ctx.register_web_search_provider(JinaWebSearchProvider())
