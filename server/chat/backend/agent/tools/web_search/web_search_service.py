"""
Web Search Service Adapter

This module provides a unified interface for external search APIs,
with focus on cloud provider documentation and infrastructure tooling.
"""

import os
import json
import logging
import asyncio
import aiohttp
from typing import Dict, List, Optional, Any, Tuple, Set
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, urljoin, urlunparse
import hashlib
import re
from enum import Enum

from bs4 import BeautifulSoup
import warnings
from bs4 import XMLParsedAsHTMLWarning

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

class ContentType(Enum):
    """Content type classification for search results"""
    DOCUMENTATION = "documentation"
    API_REFERENCE = "api_reference"
    TUTORIAL = "tutorial"
    CHANGELOG = "changelog"
    GITHUB_ISSUE = "github_issue"
    STACKOVERFLOW = "stackoverflow"
    BLOG_POST = "blog_post"
    UNKNOWN = "unknown"

class SearchResult:
    """Structured search result with metadata"""
    def __init__(
        self,
        url: str,
        title: str,
        snippet: str,
        domain: str,
        content_type: ContentType,
        published_date: Optional[datetime] = None,
        fetch_time: Optional[datetime] = None,
        full_content: Optional[str] = None,
        screenshot_url: Optional[str] = None,
        relevance_score: float = 0.0
    ):
        self.url = url
        self.title = title
        self.snippet = snippet
        self.domain = domain
        self.content_type = content_type
        self.published_date = published_date
        self.fetch_time = fetch_time or datetime.now(timezone.utc)
        self.full_content = full_content
        self.screenshot_url = screenshot_url
        self.relevance_score = relevance_score
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "domain": self.domain,
            "content_type": self.content_type.value,
            "published_date": self.published_date.isoformat() if self.published_date else None,
            "fetch_time": self.fetch_time.isoformat(),
            "full_content": self.full_content,
            "screenshot_url": self.screenshot_url,
            "relevance_score": self.relevance_score
        }

class WebSearchService:
    """Main web search service adapter"""
    
    # Domain whitelist for infrastructure documentation and trusted sources
    TRUSTED_DOMAINS = {
        # Cloud Providers - Official Documentation
        "docs.aws.amazon.com",
        "aws.amazon.com",
        "cloud.google.com",
        "docs.microsoft.com",
        "azure.microsoft.com",
        "learn.microsoft.com",
        
        # Infrastructure Tools - Official Documentation
        "registry.terraform.io",
        "www.terraform.io",
        "developer.hashicorp.com",
        "kubernetes.io",
        "helm.sh",
        "docs.docker.com",
        "docs.docker.io",
        "docs.gitlab.com",
        "docs.github.com",
        
        # Community and Developer Resources
        "github.com",
        "stackoverflow.com",
        "serverfault.com",
        "superuser.com",
        "reddit.com",
        "dev.to",
        "medium.com",
        "hashnode.com",
        
        # Technical Publications and Blogs
        "aws.amazon.com/blogs",
        "cloud.google.com/blog",
        "azure.microsoft.com/blog",
        "techcrunch.com",
        "arstechnica.com",
        "wired.com",
        "theverge.com",
        "hackernews.ycombinator.com",
        
        # Educational and Reference
        "en.wikipedia.org",
        "web.archive.org",
        "archive.org",
        
        # Professional Networks and Documentation
        "atlassian.com",
        "jetbrains.com",
        "vmware.com",
        "redhat.com",
        "canonical.com",
        "nginx.com",
        "apache.org",
        "mongodb.com",
        "postgresql.org",
        "mysql.com",
        
        # Security and Compliance
        "owasp.org",
        "cve.mitre.org",
        "nvd.nist.gov",
        
        # General News and Government (for broader context)
        "www.reuters.com",
        "www.ap.org", 
        "www.bbc.com",
        "www.whitehouse.gov",
        "www.congress.gov",
        "www.gov.uk",
        "europa.eu",
        "news.google.com",
        "www.npr.org"
    }
    
    # Rate limiting configuration
    RATE_LIMIT_WINDOW = 60  # seconds
    RATE_LIMIT_MAX_REQUESTS = 30
    
    def __init__(self, searxng_url: Optional[str] = None):
        self.searxng_url = searxng_url or os.getenv("SEARXNG_URL")
        assert self.searxng_url is not None, "SEARXNG_URL environment variable not set"
        logger.info(f"Initializing WebSearchService with SearXNG URL: {self.searxng_url}")
        self.session: Optional[aiohttp.ClientSession] = None
        self.connector: Optional[aiohttp.TCPConnector] = None
        self._rate_limit_tracker: Dict[str, List[float]] = {}

        # Crawling configuration
        self.max_crawl_depth = 2
        self.max_links_per_page = 10
        self.crawl_timeout = 30  # seconds per page
        
    async def __aenter__(self):
        """Async context manager entry"""
        self.connector = aiohttp.TCPConnector(limit=20, limit_per_host=5, force_close=True)
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=10)
        self.session = aiohttp.ClientSession(connector=self.connector, timeout=timeout)
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - properly close session and connector"""
        if self.session:
            await self.session.close()
        if hasattr(self, 'connector') and self.connector:
            await self.connector.close()
        # Give asyncio time to close all transports
        await asyncio.sleep(0.5)
            
    def _check_rate_limit(self, key: str = "default") -> bool:
        """Check if we're within rate limits"""
        now = datetime.now().timestamp()
        if key not in self._rate_limit_tracker:
            self._rate_limit_tracker[key] = []
            
        # Clean old requests
        self._rate_limit_tracker[key] = [
            t for t in self._rate_limit_tracker[key] 
            if now - t < self.RATE_LIMIT_WINDOW
        ]
        
        if len(self._rate_limit_tracker[key]) >= self.RATE_LIMIT_MAX_REQUESTS:
            return False
            
        self._rate_limit_tracker[key].append(now)
        return True
        
    def _classify_content_type(self, url: str, title: str = "", snippet: str = "") -> ContentType:
        """Classify content type based on URL and content"""
        url_lower = url.lower()
        title_lower = title.lower()
        snippet_lower = snippet.lower()
        
        # Check URL patterns
        if "api-reference" in url_lower or "/api/" in url_lower:
            return ContentType.API_REFERENCE
        elif "changelog" in url_lower or "release-notes" in url_lower:
            return ContentType.CHANGELOG
        elif "github.com/issues" in url_lower:
            return ContentType.GITHUB_ISSUE
        elif "stackoverflow.com" in url_lower:
            return ContentType.STACKOVERFLOW
        elif "/blog/" in url_lower or "blog." in url_lower:
            return ContentType.BLOG_POST
        elif "/tutorial" in url_lower or "/guide" in url_lower:
            return ContentType.TUTORIAL
        elif "/docs/" in url_lower or "documentation" in title_lower:
            return ContentType.DOCUMENTATION
            
        return ContentType.UNKNOWN
        
    def _is_trusted_domain(self, url: str) -> bool:
        """Check if URL is from a trusted domain"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            
            # Check exact match
            if domain in self.TRUSTED_DOMAINS:
                return True
                
            # Check subdomain match (e.g., docs.aws.amazon.com matches aws.amazon.com)
            for trusted in self.TRUSTED_DOMAINS:
                if domain.endswith(f".{trusted}") or domain == trusted:
                    return True
                    
            return False
        except Exception:
            return False
    
    def _is_acceptable_domain(self, url: str) -> bool:
        """Check if URL is from an acceptable domain for general queries (less restrictive)"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            
            # First check if it's in trusted domains (always accept these)
            if self._is_trusted_domain(url):
                return True
            
            # Block obvious spam/malicious domains
            spam_indicators = [
                "malware", "virus", "spam", "scam", "casino", "porn", "xxx",
                "pills", "viagra", "lottery", "winner", "fake"
            ]
            
            for indicator in spam_indicators:
                if indicator in domain:
                    return False
            
            # Block domains with suspicious patterns
            if domain.count('.') > 3:  # Too many subdomains
                return False
                
            # Accept domains from major TLDs and reputable sources
            major_tlds = [".com", ".org", ".edu", ".gov", ".net", ".uk", ".ca", ".au"]
            if any(domain.endswith(tld) for tld in major_tlds):
                return True
                
            # Accept subdomains of major sites
            major_sites = ["google.com", "youtube.com", "reddit.com", "medium.com", 
                          "forbes.com", "techcrunch.com", "wired.com"]
            for site in major_sites:
                if domain.endswith(f".{site}") or domain == site:
                    return True
            
            # Default to accepting (better to be permissive for general queries)
            return True
            
        except Exception:
            return False
            
    async def search(
        self,
        query: str,
        provider_filter: Optional[str] = None,
        since: Optional[datetime] = None,
        top_k: int = 10,
        include_screenshots: bool = False,
        domain_filter: Optional[List[str]] = None,
        enable_crawling: bool = True,
        crawl_depth: int = 2
    ) -> List[SearchResult]:
        """
        Execute web search with filters
        
        Args:
            query: Search query
            provider_filter: Filter for specific cloud provider (aws, gcp, azure)
            since: Only return results published after this date
            top_k: Number of results to return
            include_screenshots: Whether to capture screenshots of pages
            domain_filter: Additional domains to search within
            
        Returns:
            List of SearchResult objects
        """
        if not self._check_rate_limit():
            logger.warning("Rate limit exceeded for web search")
            return []
            
        # Build enhanced query
        enhanced_query = self._enhance_query(query, provider_filter, since)
        
        # Add domain restrictions if specified
        if domain_filter:
            site_queries = " OR ".join([f"site:{domain}" for domain in domain_filter])
            enhanced_query = f"{enhanced_query} ({site_queries})"
            
        try:
            results = await self._execute_search(enhanced_query, top_k)
            
            # Apply trusted-domain filtering only for cloud-provider specific searches.
            # When provider_filter is None we assume a general-purpose query and keep
            # all results so we don't accidentally discard relevant sources
            # (e.g. news outlets or Wikipedia) that aren't in our whitelisted list.
            # However, we still apply some basic filtering to remove spam/low-quality domains.

            initial_count = len(results)
            logger.info(f"[web_search] 🔍 Domain filtering: starting with {initial_count} results")

            if provider_filter:
                # For provider-specific searches, be more permissive for "latest" queries
                is_latest_query = any(term in query.lower() for term in ["latest", "current", "new", "recent"])
                if is_latest_query:
                    # Allow more domains for latest information
                    results = [r for r in results if self._is_acceptable_domain(r.url)]
                    logger.info(f"[web_search] 🔍 Domain filtering: latest query, using acceptable_domain filter, {len(results)} results remaining")
                else:
                    results = [r for r in results if self._is_trusted_domain(r.url)]
                    logger.info(f"[web_search] 🔍 Domain filtering: using trusted_domain filter, {len(results)} results remaining")
            else:
                # For general queries, apply loose filtering to remove obvious spam
                results = [r for r in results if self._is_acceptable_domain(r.url)]
                logger.info(f"[web_search] 🔍 Domain filtering: general query, using acceptable_domain filter, {len(results)} results remaining")

            # Fetch full content and crawl for additional information if enabled
            if enable_crawling:
                await self._crawl_and_extract_content(results[:5], crawl_depth, query)
            elif include_screenshots or any(r.full_content is None for r in results[:5]):
                await self._fetch_full_content(results[:5], include_screenshots)

            # Ensure all results have at least snippet content
            for result in results:
                if not result.full_content and result.snippet:
                    result.full_content = result.snippet

            final_results = results[:top_k]
            logger.info(f"[web_search] 🎯 Returning {len(final_results)} results from search service")
            
            return final_results
            
        except Exception as e:
            logger.error(f"Search error: {e}")
            logger.error(f"Query: {enhanced_query}")
            logger.error(f"Provider filter: {provider_filter}")
            logger.error(f"Domain filter: {domain_filter}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return []
            
    def _enhance_query(
        self, 
        query: str, 
        provider_filter: Optional[str] = None,
        since: Optional[datetime] = None
    ) -> str:
        """Enhance query with provider-specific terms and filters"""
        enhanced = query
        
        # Add provider-specific enhancements
        if provider_filter:
            provider_lower = provider_filter.lower()
            if provider_lower == "aws":
                enhanced = f"{query} (AWS OR Amazon Web Services)"
            elif provider_lower == "gcp":
                enhanced = f"{query} (GCP OR Google Cloud Platform)"
            elif provider_lower == "azure":
                enhanced = f"{query} (Azure OR Microsoft Azure)"
                
        # Add date filter if specified
        if since:
            # Format: after:YYYY-MM-DD
            date_str = since.strftime("%Y-%m-%d")
            enhanced = f"{enhanced} after:{date_str}"
            
        return enhanced
        
    async def _execute_search(self, query: str, top_k: int) -> List[SearchResult]:
        """Execute the actual search request using SearXNG Search API"""
        logger.info(f"Executing SearXNG search for: {query}")
        logger.info(f"SearXNG URL: {self.searxng_url}")

        try:
            # SearXNG JSON API endpoint
            search_url = f"{self.searxng_url}/search"
            params = {
                "q": query,
                "format": "json",
                "pageno": 1,
                "language": "en"
            }

            logger.debug(f"SearXNG request: {search_url} with params: {params}")

            timeout = aiohttp.ClientTimeout(total=30, sock_read=30)
            async with self.session.get(search_url, params=params, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()

                    # Debug: Log the raw response
                    logger.debug(f"Raw SearXNG response type: {type(data)}")
                    logger.debug(f"Raw SearXNG response keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")

                    if not data:
                        logger.warning("SearXNG returned empty response")
                        return []

                    return self._parse_searxng_results(data, top_k)
                else:
                    logger.error(f"SearXNG returned HTTP {response.status}")
                    error_text = await response.text()
                    logger.error(f"SearXNG error response: {error_text}")
                    return []

        except Exception as e:
            logger.error(f"An unexpected error occurred during SearXNG search: {e}")
            logger.error(f"Exception type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return []
        
    def _parse_searxng_results(self, data: Dict[str, Any], top_k: int) -> List[SearchResult]:
        """Parse search results from SearXNG API response"""
        # Debug: Log the raw response structure
        logger.debug(f"Raw SearXNG response keys: {list(data.keys())}")
        logger.debug(f"Raw SearXNG response: {data}")

        results = []
        results_data = data.get("results", [])
        logger.debug(f"Number of results in response: {len(results_data)}")

        for i, item in enumerate(results_data[:top_k]):
            logger.debug(f"Processing result {i+1}: {item}")
            try:
                url = item.get("url", "")
                title = item.get("title", "")
                snippet = item.get("content", "")

                # Parse published date if available
                published_date = None
                if "publishedDate" in item:
                    try:
                        from dateutil import parser
                        published_date = parser.parse(item["publishedDate"])
                    except Exception as e:
                        logger.debug(f"Failed to parse date: {e}")

                # Calculate a simple relevance score based on position (inverse)
                relevance_score = 1.0 - (i / max(len(results_data), 1))

                result = SearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    domain=urlparse(url).netloc if url else "",
                    content_type=self._classify_content_type(url, title, snippet),
                    published_date=published_date,
                    relevance_score=relevance_score,
                )
                results.append(result)
                logger.debug(f"Successfully parsed result {i+1}: {result.title}")
            except Exception as e:
                logger.warning(f"Failed to parse search result item: {item}. Error: {e}")

        logger.debug(f"Final parsed results count: {len(results)}")
        return results
        
    async def _fetch_full_content(
        self, 
        results: List[SearchResult], 
        include_screenshots: bool = False
    ) -> None:
        """Fetch full page content for results"""
        tasks = []
        for result in results:
            if result.full_content is None:
                tasks.append(self._fetch_page_content(result, include_screenshots))
                
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    async def _fetch_page_content(
        self, 
        result: SearchResult, 
        include_screenshot: bool = False
    ) -> None:
        """Fetch full content of a single page"""
        try:
            timeout = aiohttp.ClientTimeout(total=10, sock_read=10)
            async with self.session.get(result.url, timeout=timeout) as response:
                if response.status == 200:
                    # Check content type to avoid processing binary files
                    content_type = response.headers.get('content-type', '').lower()
                    
                    # Skip binary files and PDFs
                    if any(binary_type in content_type for binary_type in ['pdf', 'application/octet-stream', 'image/', 'video/', 'audio/']):
                        logger.debug(f"Skipping binary content for {result.url}: {content_type}")
                        return
                        
                    # Check if URL indicates binary file
                    if any(ext in result.url.lower() for ext in ['.pdf', '.zip', '.tar', '.gz', '.exe', '.dmg', '.png', '.jpg', '.jpeg', '.gif']):
                        logger.debug(f"Skipping binary file URL: {result.url}")
                        return
                        
                    try:
                        content = await response.text()
                        # Extract meaningful text content (simplified)
                        result.full_content = self._extract_text_content(content)
                    except UnicodeDecodeError as ude:
                        logger.warning(f"Unicode decode error for {result.url}: {ude}")
                        # Try to decode with different encoding
                        try:
                            content = await response.read()
                            result.full_content = content.decode('latin-1', errors='ignore')
                        except Exception as e2:
                            logger.warning(f"Failed to decode content for {result.url}: {e2}")
                            return
                    
                    if include_screenshot:
                        # TODO: Implement screenshot capture
                        pass
                elif response.status == 403:
                    logger.warning(f"Access forbidden (403) for {result.url} - likely requires authentication")
                elif response.status == 404:
                    logger.warning(f"Page not found (404) for {result.url}")
                else:
                    logger.warning(f"HTTP {response.status} for {result.url}")
                        
        except Exception as e:
            logger.warning(f"Failed to fetch content for {result.url}: {e}")
            
    def _extract_text_content(self, html: str) -> str:
        """Extract meaningful text from HTML using BeautifulSoup"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Remove script and style elements
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            
            # Get text content
            text = soup.get_text(separator=' ', strip=True)
            
            # Clean up whitespace
            text = re.sub(r'\s+', ' ', text)
            text = text.strip()
            
            # Limit to first 8000 characters for better context
            return text[:8000]
        except Exception as e:
            logger.warning(f"BeautifulSoup extraction failed, falling back to simple parsing: {e}")
        
        # Fallback to simple extraction
        html = re.sub(r'<script[^>]*>.*?</script[^>]*>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = ' '.join(text.split())
        return text[:5000]
        
    async def _crawl_and_extract_content(
        self, 
        results: List[SearchResult], 
        depth: int,
        query: str
    ) -> None:
        """Enhanced content extraction with crawling for nested links"""
        tasks = []
        for result in results:
            if result.full_content is None:
                tasks.append(self._crawl_page_with_depth(result, depth, query))
                
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    async def _crawl_page_with_depth(
        self, 
        result: SearchResult, 
        depth: int,
        query: str
    ) -> None:
        """Crawl a page and its nested links for comprehensive content extraction"""
        try:
            logger.debug(f"[crawler] 🕷️ Starting crawl for {result.url} with depth {depth}")
            
            # First extract content from the main page
            timeout = aiohttp.ClientTimeout(total=self.crawl_timeout, sock_read=self.crawl_timeout)
            async with self.session.get(result.url, timeout=timeout) as response:
                if response.status != 200:
                    logger.warning(f"[crawler] ❌ HTTP {response.status} for {result.url}")
                    return
                    
                # Check content type to avoid processing binary files
                content_type = response.headers.get('content-type', '').lower()
                if any(binary_type in content_type for binary_type in ['pdf', 'application/octet-stream', 'image/', 'video/', 'audio/']):
                    logger.debug(f"[crawler] ⏭️ Skipping binary content for {result.url}: {content_type}")
                    return
                    
                # Check if URL indicates binary file
                if any(ext in result.url.lower() for ext in ['.pdf', '.zip', '.tar', '.gz', '.exe', '.dmg', '.png', '.jpg', '.jpeg', '.gif']):
                    logger.debug(f"[crawler] ⏭️ Skipping binary file URL: {result.url}")
                    return
                    
                try:
                    html_content = await response.text()
                    logger.debug(f"[crawler] 📄 Got {len(html_content)} chars from {result.url}")
                    
                    # Extract main content using BeautifulSoup
                    main_content = self._extract_text_content(html_content)
                    logger.debug(f"[crawler] 📝 Extracted {len(main_content)} chars from {result.url}")
                except UnicodeDecodeError as ude:
                    logger.warning(f"[crawler] ❌ Unicode decode error for {result.url}: {ude}")
                    return
                
                # Find relevant nested links if we have depth remaining
                nested_content = ""
                if depth > 0:
                    nested_links = self._extract_relevant_links(
                        html_content, result.url, query
                    )
                    logger.debug(f"[crawler] 🔗 Found {len(nested_links)} relevant links for {result.url}")
                    
                    # Crawl nested links
                    nested_contents = await self._crawl_nested_links(
                        nested_links, depth - 1, query
                    )
                    nested_content = "\n\n".join(nested_contents)
                    logger.debug(f"[crawler] 📄 Got {len(nested_content)} chars from nested links")
                
                # Combine main and nested content
                full_content = main_content
                if nested_content:
                    full_content += f"\n\n--- Related Documentation ---\n{nested_content}"
                
                result.full_content = full_content
                logger.debug(f"[crawler] ✅ Successfully crawled {result.url}, total content: {len(full_content)} chars")
                
        except Exception as e:
            logger.warning(f"Failed to crawl page {result.url}: {e}")
            # Fallback to simple content fetch
            await self._fetch_page_content(result, include_screenshot=False)
            
        # Ensure we have at least snippet content if crawling failed
        if not result.full_content and result.snippet:
            logger.debug(f"[crawler] 🔄 Using snippet as fallback for {result.url}")
            result.full_content = result.snippet
            
    def _extract_relevant_links(
        self, 
        html_content: str, 
        base_url: str, 
        query: str
    ) -> List[str]:
        """Extract relevant documentation links from a page"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            base_domain = urlparse(base_url).netloc
            
            relevant_links = []
            query_terms = set(query.lower().split())
            
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if not href:
                    continue
                    
                # Convert relative URLs to absolute
                full_url = urljoin(base_url, href)
                link_domain = urlparse(full_url).netloc
                
                # Only crawl links from the same domain (stay within documentation site)
                if link_domain != base_domain:
                    continue
                    
                # Skip non-documentation links
                if self._is_irrelevant_link(full_url):
                    continue
                    
                # Check if link text or URL contains relevant terms
                link_text = link.get_text().lower()
                url_path = urlparse(full_url).path.lower()
                
                # Look for documentation-specific patterns
                doc_patterns = [
                    'api', 'reference', 'guide', 'tutorial', 'docs', 'documentation',
                    'getting-started', 'quickstart', 'examples', 'samples'
                ]
                
                # Check relevance based on query terms or documentation patterns
                is_relevant = (
                    any(term in link_text for term in query_terms) or
                    any(term in url_path for term in query_terms) or
                    any(pattern in url_path for pattern in doc_patterns) or
                    any(pattern in link_text for pattern in doc_patterns)
                )
                
                if is_relevant and full_url not in relevant_links:
                    relevant_links.append(full_url)
                    
                    # Limit to prevent excessive crawling
                    if len(relevant_links) >= self.max_links_per_page:
                        break
                        
            return relevant_links
            
        except Exception as e:
            logger.warning(f"Failed to extract links from {base_url}: {e}")
            return []
            
    def _is_irrelevant_link(self, url: str) -> bool:
        """Check if a link should be skipped during crawling"""
        url_lower = url.lower()
        
        # Skip certain file types and sections
        skip_patterns = [
            '.pdf', '.zip', '.tar', '.gz', '.exe', '.dmg', '.png', '.jpg', '.jpeg', '.gif', '.svg',
            '/download', '/login', '/logout', '/register', '/signup',
            '/search', '/contact', '/about', '/privacy', '/terms',
            '/blog/', '/news/', '/press/', '#', 'javascript:', 'mailto:',
            '/pricing', '/billing', '/marketplace', '/store',
            '/questions/', '/answers/', '/forum/', '/community/',  # Skip Q&A sites that might require auth
            'repost.aws', 'stackoverflow.com/questions'  # Skip sites that often block crawlers
        ]
        
        return any(pattern in url_lower for pattern in skip_patterns)
        
    async def _crawl_nested_links(
        self, 
        links: List[str], 
        depth: int,
        query: str
    ) -> List[str]:
        """Crawl nested links and extract their content"""
        if not links or depth <= 0:
            return []
            
        contents = []
        semaphore = asyncio.Semaphore(5)  # Limit concurrent requests
        
        async def crawl_single_link(url: str) -> str:
            async with semaphore:
                try:
                    # Skip binary files and problematic URLs
                    if self._is_irrelevant_link(url):
                        logger.debug(f"[crawler] ⏭️ Skipping irrelevant nested link: {url}")
                        return ""
                        
                    timeout = aiohttp.ClientTimeout(total=self.crawl_timeout, sock_read=self.crawl_timeout)
                    async with self.session.get(url, timeout=timeout) as response:
                        if response.status == 200:
                            # Check content type
                            content_type = response.headers.get('content-type', '').lower()
                            if any(binary_type in content_type for binary_type in ['pdf', 'application/octet-stream', 'image/', 'video/', 'audio/']):
                                logger.debug(f"[crawler] ⏭️ Skipping binary nested link: {url}")
                                return ""
                                
                            try:
                                html = await response.text()
                                content = self._extract_text_content(html)
                                if content and len(content.strip()) > 100:  # Only include substantial content
                                    return f"[From {url}]:\n{content[:2000]}"  # Limit nested content size
                            except UnicodeDecodeError:
                                logger.debug(f"[crawler] ❌ Unicode error for nested link: {url}")
                                return ""
                        elif response.status == 403:
                            logger.debug(f"[crawler] ❌ 403 Forbidden for nested link: {url}")
                        elif response.status == 404:
                            logger.debug(f"[crawler] ❌ 404 Not Found for nested link: {url}")
                        else:
                            logger.debug(f"[crawler] ❌ HTTP {response.status} for nested link: {url}")
                except Exception as e:
                    logger.debug(f"[crawler] ❌ Failed to crawl nested link {url}: {e}")
                return ""
        
        # Crawl links concurrently
        tasks = [crawl_single_link(link) for link in links[:5]]  # Limit to top 5 links
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, str) and result.strip():
                contents.append(result)
                
        return contents
        
    def get_cache_key(self, query: str, filters: Dict[str, Any]) -> str:
        """Generate cache key for a search query"""
        key_parts = [query]
        for k, v in sorted(filters.items()):
            if v is not None:
                key_parts.append(f"{k}:{v}")
                
        key_string = "|".join(key_parts)
        return hashlib.sha256(key_string.encode()).hexdigest()
