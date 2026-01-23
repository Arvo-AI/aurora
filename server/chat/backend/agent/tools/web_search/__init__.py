"""
Web Search Tool Package

Provides web search functionality for the Aurora agent to fetch
up-to-date cloud provider documentation and infrastructure information.
"""

from .web_search_service import WebSearchService, SearchResult, ContentType
from .query_composer import QueryComposer, QueryIntent
from .summarizer import Summarizer, SummarizedResult, CanonicalVersion, CodeSnippet

__all__ = [
    "WebSearchService",
    "SearchResult", 
    "ContentType",
    "QueryComposer",
    "QueryIntent",
    "Summarizer",
    "SummarizedResult",
    "CanonicalVersion",
    "CodeSnippet"
]
