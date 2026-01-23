"""
Web Search Tool for Aurora Agent

Provides up-to-date cloud documentation and infrastructure information
to enhance older/less-capable models with current knowledge.
"""

import os
import json
import logging
import asyncio
from typing import Dict, List, Optional, Any, Union, Tuple
from datetime import datetime, timezone
from functools import wraps
import threading
import re  # <--- added for regex operations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .web_search.web_search_service import WebSearchService, SearchResult, ContentType
from .web_search.query_composer import QueryComposer, QueryIntent
from .web_search.summarizer import Summarizer, SummarizedResult
from .cloud_provider_utils import determine_target_provider_from_context
from ..utils.model_cutoff_manager import model_cutoff_manager

logger = logging.getLogger(__name__)

# Ensure basic logging configuration if not already set by the host application
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

# Helper to abbreviate long content in logs
def _truncate(text: str, length: int = 300) -> str:
    return text if len(text) <= length else text[:length] + "…"

def _get_current_model_from_context(user_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
    """
    Try to get the current model being used from various context sources.
    
    Args:
        user_id: Current user ID
        session_id: Current session ID
        
    Returns:
        Model name if found, None otherwise
    """
    try:
        # Try to get from state context first (most reliable)
        from .cloud_tools import get_state_context
        state = get_state_context()
        if state and hasattr(state, 'model') and state.model:
            logger.debug(f"Found model from state context: {state.model}")
            return state.model
    except Exception as e:
        logger.debug(f"Could not get model from state context: {e}")
    
    try:
        # Try to get from tool context capture if available
        from .cloud_tools import get_tool_capture
        tool_capture = get_tool_capture()
        if tool_capture and hasattr(tool_capture, 'current_model'):
            logger.debug(f"Found model from tool capture: {tool_capture.current_model}")
            return tool_capture.current_model
    except Exception as e:
        logger.debug(f"Could not get model from tool capture: {e}")
    
    # If no model found, return None - the search tool will handle this gracefully
    logger.debug("No model found in context, will use fallback logic")
    return None

class WebSearchArgs(BaseModel):
    """Arguments for web search tool"""
    query: str = Field(description="The search query - be specific about what information you need")
    provider_filter: Optional[str] = Field(
        default=None,
        description="Filter results for specific cloud provider: 'aws', 'gcp', or 'azure'"
    )
    since: Optional[str] = Field(
        default=None,
        description="Only return results published after this date (YYYY-MM-DD format)"
    )
    top_k: int = Field(
        default=5,
        description="Number of results to return (max 10)"
    )
    include_screenshots: bool = Field(
        default=False,
        description="Whether to capture screenshots of documentation pages"
    )
    verify: bool = Field(
        default=False,
        description="Cross-check information across multiple sources for accuracy"
    )
    enable_crawling: bool = Field(
        default=True,
        description="Whether to crawl nested documentation links for comprehensive information"
    )
    crawl_depth: int = Field(
        default=2,
        description="Maximum depth for crawling nested links (0-3, higher values take longer)"
    )
    confirm_external_search: bool = Field(
        default=False,
        description="Whether to ask for user confirmation before searching outside whitelisted domains"
    )

class WebSearchTool:
    """
    Web search tool implementation with cloud provider focus
    """
    
    def __init__(
        self,
        searxng_url: Optional[str] = None,
        model_knowledge_cutoff: Optional[datetime] = None,
        cache_results: bool = True
    ):
        self.searxng_url = searxng_url or os.getenv("SEARXNG_URL")
        assert self.searxng_url is not None, "SEARXNG_URL environment variable not set"
        self.model_knowledge_cutoff = model_knowledge_cutoff or datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.cache_results = cache_results

        # Initialize components
        self.search_service = WebSearchService(self.searxng_url)
        self.query_composer = QueryComposer(self.model_knowledge_cutoff)
        self.summarizer = Summarizer()
        
        # Cache for recent searches (in-memory for now)
        self._search_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        
        logger.info("Initialized WebSearchTool (cache=%s)", cache_results)
    
    async def _perform_specialized_search(
        self,
        query: str,
        provider_filter: str,
        since_date: Optional[datetime],
        top_k: int,
        include_screenshots: bool,
        enable_crawling: bool,
        crawl_depth: int,
        websocket_sender: Optional[Any],
        event_loop: Optional[Any],
        model_name: Optional[str] = None
    ) -> Tuple[List[SearchResult], Dict[str, Any]]:
        """Performs a search focused on a specific cloud provider with fallback logic."""
        
        
        # Compose enhanced query
        await self._send_status(
            "Composing specialized search query...",
            websocket_sender,
            event_loop
        )
        
        model_cutoff = None
        if model_name:
            try:
                model_cutoff = model_cutoff_manager.get_knowledge_cutoff(model_name)
            except Exception:
                pass
        
        original_cutoff = self.query_composer.model_knowledge_cutoff
        if model_cutoff:
            self.query_composer.model_knowledge_cutoff = model_cutoff
            
        try:
            enhanced_query, query_metadata = self.query_composer.compose_query(
                base_query=query,
                provider=provider_filter,
                include_recent=True
            )
        finally:
            self.query_composer.model_knowledge_cutoff = original_cutoff
            
        query_metadata["key_terms"] = self.query_composer.extract_key_terms(query)
        
        # Execute search
        await self._send_status(
            f"Searching for: {enhanced_query[:100]}...",
            websocket_sender,
            event_loop
        )
        
        logger.info(f"[web_search] 🔍 Executing specialized search | query='{enhanced_query}'")
        
        async with self.search_service as service:
            search_results = await service.search(
                query=enhanced_query,
                provider_filter=provider_filter,
                since=since_date,
                top_k=top_k * 2,
                include_screenshots=include_screenshots,
                enable_crawling=enable_crawling,
                crawl_depth=crawl_depth
            )
            
        if not search_results:
            await self._send_status("No results, broadening search...", websocket_sender, event_loop)
            
            def _strip_site_restrictions(q: str) -> str:
                return re.sub(r"\(\s*site:[^)]*\)", "", q).strip()

            fallback_query = _strip_site_restrictions(enhanced_query)

            async with self.search_service as service:
                search_results = await service.search(
                    query=fallback_query, provider_filter=provider_filter, since=since_date,
                    top_k=top_k * 2, include_screenshots=include_screenshots,
                    enable_crawling=enable_crawling, crawl_depth=crawl_depth
                )
        
        if not search_results:
            await self._send_status("Still no results, searching without provider filter...", websocket_sender, event_loop)
            async with self.search_service as service:
                search_results = await service.search(
                    query=query, provider_filter=None, since=since_date,
                    top_k=top_k * 2, include_screenshots=include_screenshots,
                    enable_crawling=enable_crawling, crawl_depth=crawl_depth
                )

        return search_results, query_metadata

    async def search(
        self,
        query: str,
        provider_filter: Optional[str] = None,
        since: Optional[str] = None,
        top_k: int = 5,
        include_screenshots: bool = False,
        verify: bool = False,
        enable_crawling: bool = True,
        crawl_depth: int = 2,
        confirm_external_search: bool = False,
        model_name: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        websocket_sender: Optional[Any] = None,
        event_loop: Optional[Any] = None
    ) -> str:
        """
        Execute web search with streaming updates
        
        Returns JSON string with search results and metadata
        """
        logger.info("[web_search] 🔍 New search | query='%s' provider_filter=%s top_k=%s verify=%s user_id=%s session_id=%s model=%s", _truncate(query,80), provider_filter, top_k, verify, user_id, session_id, model_name)
        
        final_output = {}
        try:
            # User confirmation is disabled - proceed directly with search
                
            # Send initial status
            await self._send_status(
                "Starting web search...",
                websocket_sender,
                event_loop
            )
            
            # Parse since date if provided
            since_date = None
            if since:
                try:
                    since_date = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    logger.warning(f"Invalid date format: {since}")
                    
            # Validate parameters
            top_k = min(max(top_k, 1), 10)  # Limit between 1-10
            crawl_depth = min(max(crawl_depth, 0), 3)  # Limit crawl depth to prevent excessive crawling
            
            # Try to determine provider from context if not specified
            if not provider_filter and user_id:
                provider_filter = self._infer_provider_from_context(query, user_id)

            cache_key = self._get_cache_key(query, provider_filter, since, top_k)
            if self.cache_results:
                cached_result = self._get_cached_result(cache_key)
                if cached_result:
                    logger.info(f"Returning cached result for query: {query}")
                    return json.dumps(cached_result)

            search_results = []
            query_metadata = {}

            if provider_filter:
                logger.info(f"Provider filter '{provider_filter}' detected, using specialized search.")
                search_results, query_metadata = await self._perform_specialized_search(
                    query=query,
                    provider_filter=provider_filter,
                    since_date=since_date,
                    top_k=top_k,
                    include_screenshots=include_screenshots,
                    enable_crawling=enable_crawling,
                    crawl_depth=crawl_depth,
                    websocket_sender=websocket_sender,
                    event_loop=event_loop,
                    model_name=model_name
                )
            else:
                logger.info("No provider filter, performing general web search.")
                await self._send_status(f"Searching for: {query[:100]}...", websocket_sender, event_loop)
                async with self.search_service as service:
                    search_results = await service.search(
                        query=query,
                        provider_filter=None,
                        since=since_date,
                        top_k=top_k,
                        include_screenshots=include_screenshots,
                        enable_crawling=enable_crawling,
                        crawl_depth=crawl_depth
                    )
                # For general search, create basic metadata
                query_metadata = {
                    "intent": QueryIntent.GENERAL_KNOWLEDGE.value,
                    "key_terms": self.query_composer.extract_key_terms(query),
                    "provider": "general"
                }

            if not search_results:
                return json.dumps({
                    "status": "success",
                    "message": "No relevant results found",
                    "results": [],
                    "total_results": 0,
                    "query_metadata": query_metadata,
                    "search_timestamp": datetime.now(timezone.utc).isoformat(),
                    "model_cutoff_info": {
                        "model_used": model_name
                    }
                })
                
            # Send progress update
            await self._send_status(
                f"Found {len(search_results)} results, processing...",
                websocket_sender,
                event_loop
            )
            
            logger.info(f"[web_search] 📝 Processing {len(search_results)} results for summarization")
            
            # Summarize results
            summarized_results = self.summarizer.summarize_results(
                results=search_results,
                query_context=query_metadata,
                max_code_snippets=3
            )
            
            logger.info(f"[web_search] 📝 Summarization returned {len(summarized_results)} results")
            
            # Verify across sources if requested
            if verify and len(summarized_results) > 1:
                await self._send_status(
                    "Cross-checking information across sources...",
                    websocket_sender,
                    event_loop
                )
                verified_results = self._verify_results(summarized_results)
            else:
                logger.info(f"[web_search] 📊 Taking top {top_k} from {len(summarized_results)} summarized results")
                verified_results = summarized_results[:top_k]
                logger.info(f"[web_search] 📊 After slicing: {len(verified_results)} results")
                
            logger.info(f"[web_search] ✅ Final verification returned {len(verified_results)} results")
            logger.info(f"[web_search] 📊 Verification details: verify={verify}, summarized_results={len(summarized_results)}, top_k={top_k}")
            if verified_results:
                logger.info(f"[web_search] 📋 Verified results URLs: {[r.url for r in verified_results[:3]]}")
                logger.info(f"[web_search] 📋 Verified results types: {[type(r).__name__ for r in verified_results[:3]]}")
            else:
                logger.warning("[web_search] ❌ No verified results to process!")
                
            # Merge and format results
            final_output = self.summarizer.merge_summaries(
                verified_results,
                max_results=top_k
            )
            
            logger.info(f"[web_search] 📄 Final output length: {len(final_output) if final_output else 0}")
            if final_output:
                logger.info(f"[web_search] 📄 Final output keys: {list(final_output.keys())}")
                logger.info(f"[web_search] 📄 Final output results count: {len(final_output.get('results', []))}")
                logger.info(f"[web_search] 📄 Final output summary: {final_output.get('summary', 'No summary')}")
            
            # Add metadata
            final_output["query_metadata"] = query_metadata
            final_output["search_timestamp"] = datetime.now(timezone.utc).isoformat()
            final_output["status"] = "success"
            final_output["model_cutoff_info"] = {
                "model_used": model_name,
            }
            
            # Add specific recommendations based on intent
            if query_metadata.get("intent") == QueryIntent.ERROR_TROUBLESHOOTING.value:
                final_output["recommendations"] = self._get_troubleshooting_recommendations(
                    verified_results
                )
            elif query_metadata.get("intent") == QueryIntent.BREAKING_CHANGE.value:
                final_output["migration_notes"] = self._extract_migration_notes(
                    verified_results
                )
                
            # Cache result if enabled
            if self.cache_results:
                self._cache_result(cache_key, final_output)
                
            # Send completion status
            await self._send_status(
                f"Search complete. Found {len(verified_results)} relevant results.",
                websocket_sender,
                event_loop,
                is_complete=True
            )
            
            # Return formatted JSON
            return json.dumps(final_output, indent=2)
            
        except Exception as e:
            logger.error(f"Web search error: {e}", exc_info=True)
            return json.dumps({
                "status": "error",
                "error": str(e),
                "message": "Search failed due to an error"
            })
        finally:
            # Log summary (truncate to avoid massive logs)
            try:
                logger.info("[web_search] ✅ Completed search for '%s' | status=%s results=%s", _truncate(query,60), final_output.get("status", "unknown") if 'final_output' in locals() else 'error', len(final_output.get("results", [])) if 'final_output' in locals() else 0)
            except Exception:
                pass
            
    def _infer_provider_from_context(self, query: str, user_id: str) -> Optional[str]:
        """Infer cloud provider from query and user context"""
        query_lower = query.lower()
        
        # Check query for provider mentions
        if any(term in query_lower for term in ["aws", "amazon", "ec2", "s3", "lambda"]):
            return "aws"
        elif any(term in query_lower for term in ["gcp", "google cloud", "gke", "compute engine"]):
            return "gcp"
        elif any(term in query_lower for term in ["azure", "microsoft", "aks", "blob storage"]):
            return "azure"
            
        # Try to get from user's provider preference
        try:
            # This would integrate with the existing provider preference system
            return determine_target_provider_from_context()
        except:
            return None
            
    def _get_cache_key(
        self,
        query: str,
        provider: Optional[str],
        since: Optional[str],
        top_k: int
    ) -> str:
        """Generate cache key for search query"""
        key_parts = [
            query.lower(),
            provider or "any",
            since or "all",
            str(top_k)
        ]
        return "|".join(key_parts)
        
    def _get_cached_result(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get cached search result if available and fresh"""
        with self._cache_lock:
            if cache_key in self._search_cache:
                cached = self._search_cache[cache_key]
                # Check if cache is fresh (1 hour TTL)
                cache_time = datetime.fromisoformat(cached["search_timestamp"])
                if (datetime.now(timezone.utc) - cache_time).seconds < 3600:
                    return cached
                else:
                    # Remove stale cache
                    del self._search_cache[cache_key]
        return None
        
    def _cache_result(self, cache_key: str, result: Dict[str, Any]) -> None:
        """Cache search result"""
        with self._cache_lock:
            # Limit cache size
            if len(self._search_cache) > 100:
                # Remove oldest entries
                sorted_keys = sorted(
                    self._search_cache.keys(),
                    key=lambda k: self._search_cache[k].get("search_timestamp", "")
                )
                for key in sorted_keys[:20]:
                    del self._search_cache[key]
                    
            self._search_cache[cache_key] = result
            
    def _verify_results(
        self,
        results: List[SummarizedResult]
    ) -> List[SummarizedResult]:
        """Verify information across multiple sources"""
        logger.info(f"[verify] 🔍 Starting verification of {len(results)} results")
        verified = []
        
        # Group results by similar content
        for i, result in enumerate(results):
            # Count how many other results mention similar versions/information
            corroboration_count = 0
            
            for j, other in enumerate(results):
                if i != j:
                    # Check for matching versions
                    matching_versions = set(
                        v.canonical_version for v in result.canonical_versions
                    ) & set(
                        v.canonical_version for v in other.canonical_versions
                    )
                    if matching_versions:
                        corroboration_count += 1
                        
            # Boost relevance score for corroborated results
            if corroboration_count > 0:
                result.relevance_score *= (1 + 0.1 * corroboration_count)
                logger.debug(f"[verify] ✅ Result {i+1} has {corroboration_count} corroborations, score boosted to {result.relevance_score}")
            else:
                logger.debug(f"[verify] ⚠️ Result {i+1} has no corroborations, keeping original score {result.relevance_score}")
                
            # Include ALL results, not just corroborated ones
            verified.append(result)
                
        # Re-sort by updated relevance
        verified.sort(key=lambda x: x.relevance_score, reverse=True)
        
        logger.info(f"[verify] ✅ Verification complete: {len(verified)} results verified")
        return verified
        
    def _get_troubleshooting_recommendations(
        self,
        results: List[SummarizedResult]
    ) -> List[str]:
        """Extract troubleshooting recommendations from results"""
        recommendations = []
        
        for result in results:
            # Look for action items in key points
            for point in result.key_points:
                point_lower = point.lower()
                if any(word in point_lower for word in ["fix", "solve", "resolution", "workaround"]):
                    recommendations.append(point)
                    
        # Deduplicate and limit
        return list(dict.fromkeys(recommendations))[:5]
        
    def _extract_migration_notes(
        self,
        results: List[SummarizedResult]
    ) -> List[str]:
        """Extract migration notes from results"""
        migration_notes = []
        
        for result in results:
            # Collect warnings that mention migration
            for warning in result.warnings:
                if "migrat" in warning.lower():
                    migration_notes.append(warning)
                    
            # Look for migration steps in key points
            for point in result.key_points:
                if any(word in point.lower() for word in ["migrate", "upgrade", "transition"]):
                    migration_notes.append(point)
                    
        # Deduplicate and limit
        return list(dict.fromkeys(migration_notes))[:5]
        
    async def _send_status(
        self,
        message: str,
        websocket_sender: Optional[Any],
        event_loop: Optional[Any],
        is_complete: bool = False
    ) -> None:
        """Send status update via websocket"""
        if not websocket_sender or not event_loop:
            return
            
        try:
            status_message = {
                "type": "tool_status",
                "tool": "web_search",
                "status": "complete" if is_complete else "in_progress",
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            # Send via websocket
            if asyncio.iscoroutinefunction(websocket_sender):
                await websocket_sender(json.dumps(status_message))
            else:
                # Handle sync websocket sender
                event_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        websocket_sender(json.dumps(status_message))
                    )
                )
        except Exception as e:
            logger.warning(f"Failed to send websocket status: {e}")
            
    def _needs_external_search_confirmation(
        self, 
        query: str, 
        provider_filter: Optional[str], 
        confirm_external_search: bool
    ) -> bool:
        """
        Determine if the search query would likely need to go outside trusted domains
        and therefore requires user confirmation.
        """
        # User confirmation is disabled - always return False
        return False


# Create the tool instance
web_search_tool_instance = WebSearchTool()

def web_search(
    query: str,
    provider_filter: Optional[str] = None,
    since: Optional[str] = None,
    top_k: int = 5,
    include_screenshots: bool = False,
    verify: bool = False,
    enable_crawling: bool = True,
    crawl_depth: int = 2,
    confirm_external_search: bool = False,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> str:
    """
    Search the web for up-to-date information on any topic.
    
    Use this tool when you need current information about:
    - Any topic, including technology, software development, and current events.
    - Cloud provider services, APIs, or configurations (if provider_filter is used).
    - Recent updates, breaking changes, or deprecations.
    - Error messages and troubleshooting steps.
    - Best practices and recommendations.
    
    By default, the tool searches the entire web. If you provide a `provider_filter`,
    it will focus the search on that provider's documentation. The tool also crawls
    nested links for comprehensive information gathering.
    
    Args:
        query: Your search query - be specific about what information you need
        provider_filter: Optional filter for cloud provider ('aws', 'gcp', 'azure'). If provided, search will be focused on this provider.
        since: Optional date filter (YYYY-MM-DD) to get only recent results
        top_k: Number of results to return (max 10, default 5)
        include_screenshots: Whether to capture screenshots of pages
        verify: Cross-check information across multiple sources
        enable_crawling: Whether to crawl nested documentation links for comprehensive information (default True)
        crawl_depth: Maximum depth for crawling nested links (0-3, default 2)
        confirm_external_search: Whether to ask for confirmation before searching outside whitelisted domains.
        user_id: User ID (automatically injected)
        session_id: Session ID (automatically injected)
        
    Returns:
        JSON string containing:
        - Summarized search results with key points
        - Code snippets and configuration examples
        - Version information and compatibility notes
        - Warnings about breaking changes or deprecations
        - Citations with source URLs and dates
    """
    # Get websocket context if available
    from .cloud_tools import get_websocket_context
    # get_websocket_context returns a tuple: (websocket_sender, event_loop)
    websocket_sender, event_loop = get_websocket_context()
    
    # Try to get current model from context if available
    model_name = _get_current_model_from_context(user_id, session_id)
    
    logger.info("[web_search] 📞 Wrapper called | query='%s' provider=%s top_k=%s verify=%s model=%s", _truncate(query,80), provider_filter, top_k, verify, model_name)
    
    # Run async search safely whether or not an event loop is already running
    loop = event_loop or asyncio.new_event_loop()

    # Case 1: we created a fresh loop (no active loop in this thread)
    if not event_loop:
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                web_search_tool_instance.search(
                    query=query,
                    provider_filter=provider_filter,
                    since=since,
                    top_k=top_k,
                    include_screenshots=include_screenshots,
                    verify=verify,
                    enable_crawling=enable_crawling,
                    crawl_depth=crawl_depth,
                    confirm_external_search=confirm_external_search,
                    model_name=model_name,
                    user_id=user_id,
                    session_id=session_id,
                    websocket_sender=websocket_sender,
                    event_loop=loop  # use the newly created loop for status callbacks
                )
            )
        finally:
            loop.close()
    # Case 2: an event loop was provided and is already running (typical in async contexts)
    elif loop.is_running():
        # Schedule the coroutine on the running loop and wait for the result
        future = asyncio.run_coroutine_threadsafe(
            web_search_tool_instance.search(
                query=query,
                provider_filter=provider_filter,
                since=since,
                top_k=top_k,
                include_screenshots=include_screenshots,
                verify=verify,
                enable_crawling=enable_crawling,
                crawl_depth=crawl_depth,
                confirm_external_search=confirm_external_search,
                model_name=model_name,
                user_id=user_id,
                session_id=session_id,
                websocket_sender=websocket_sender,
                event_loop=loop  # status callbacks will use the same running loop
            ),
            loop,
        )
        result = future.result()
    # Case 3: loop exists but isn't running (unlikely) – we can run it directly
    else:
        result = loop.run_until_complete(
            web_search_tool_instance.search(
                query=query,
                provider_filter=provider_filter,
                since=since,
                top_k=top_k,
                include_screenshots=include_screenshots,
                verify=verify,
                enable_crawling=enable_crawling,
                crawl_depth=crawl_depth,
                confirm_external_search=confirm_external_search,
                model_name=model_name,
                user_id=user_id,
                session_id=session_id,
                websocket_sender=websocket_sender,
                event_loop=loop
            )
        )

    # Log result length
    try:
        parsed = json.loads(result)
        logger.info("[web_search] 🎯 Wrapper finished | status=%s results=%s", parsed.get("status"), len(parsed.get("results", [])))
    except Exception:
        pass

    return result
