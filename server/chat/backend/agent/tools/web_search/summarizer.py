"""
Summarizer and Canonicalizer for Web Search Results

Processes search results to create concise, actionable summaries with proper citations.
"""

import re
import json
import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
import hashlib
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class CanonicalVersion:
    """Represents a canonical version with metadata"""
    raw_version: str
    canonical_version: str
    version_type: str  # "terraform", "provider", "api", "cli"
    is_latest: bool = False
    release_date: Optional[datetime] = None

@dataclass
class CodeSnippet:
    """Represents a code snippet from documentation"""
    language: str
    code: str
    description: str
    source_line: Optional[int] = None

@dataclass
class SummarizedResult:
    """Summarized search result with structured information"""
    url: str
    title: str
    tldr: str  # One-line summary
    key_points: List[str]  # Bullet points
    code_snippets: List[CodeSnippet]
    canonical_versions: List[CanonicalVersion]
    warnings: List[str]  # Breaking changes, deprecations
    citation: str  # Formatted citation
    relevance_score: float
    published_date: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "url": self.url,
            "title": self.title,
            "tldr": self.tldr,
            "key_points": self.key_points,
            "code_snippets": [
                {
                    "language": s.language,
                    "code": s.code,
                    "description": s.description
                } for s in self.code_snippets
            ],
            "canonical_versions": [
                {
                    "raw": v.raw_version,
                    "canonical": v.canonical_version,
                    "type": v.version_type,
                    "is_latest": v.is_latest
                } for v in self.canonical_versions
            ],
            "warnings": self.warnings,
            "citation": self.citation,
            "relevance_score": self.relevance_score,
            "published_date": self.published_date.isoformat() if self.published_date else None
        }

class Summarizer:
    """Summarize and canonicalize web search results"""
    
    # Patterns for extracting versions
    VERSION_PATTERNS = {
        "terraform": [
            r"terraform\s+v?(\d+\.\d+(?:\.\d+)?)",
            r"terraform\s+version\s+(\d+\.\d+(?:\.\d+)?)",
            r"terraform\s+>=?\s*(\d+\.\d+(?:\.\d+)?)"
        ],
        "provider": [
            r"provider\s+.*?v?(\d+\.\d+(?:\.\d+)?)",
            r"version\s*=\s*[\"']?v?(\d+\.\d+(?:\.\d+)?)[\"']?",
            r"~>\s*(\d+\.\d+)"
        ],
        "api": [
            r"api\s+version\s+(\d{4}-\d{2}-\d{2})",
            r"api/v(\d+)",
            r"version:\s*(\d+\.\d+)"
        ],
        "cli": [
            r"aws\s+cli\s+v?(\d+\.\d+(?:\.\d+)?)",
            r"gcloud\s+v?(\d+\.\d+(?:\.\d+)?)",
            r"az\s+v?(\d+\.\d+(?:\.\d+)?)"
        ]
    }
    
    # Code block patterns
    CODE_PATTERNS = [
        r"```(\w*)\n(.*?)```",  # Markdown code blocks
        r"<pre><code[^>]*>(.*?)</code></pre>",  # HTML code blocks
        r"<code>(.*?)</code>",  # Inline code
    ]
    
    # Warning indicators
    WARNING_INDICATORS = [
        "breaking change",
        "deprecated",
        "will be removed",
        "no longer supported",
        "migration required",
        "incompatible",
        "security vulnerability",
        "end of life",
        "eol"
    ]
    
    def __init__(self, max_snippet_length: int = 500):
        self.max_snippet_length = max_snippet_length
        
    def summarize_results(
        self,
        results: List[Any],  # SearchResult objects
        query_context: Dict[str, Any],
        max_code_snippets: int = 3
    ) -> List[SummarizedResult]:
        """
        Summarize a list of search results
        
        Args:
            results: List of SearchResult objects
            query_context: Context from query composer
            max_code_snippets: Maximum code snippets per result
            
        Returns:
            List of SummarizedResult objects
        """
        logger.info(f"[summarizer] 📝 Starting summarization of {len(results)} results")
        summarized = []
        
        for i, result in enumerate(results):
            try:
                logger.debug(f"[summarizer] 📝 Processing result {i+1}/{len(results)}: {result.url}")
                summary = self._summarize_single_result(
                    result,
                    query_context,
                    max_code_snippets
                )
                if summary:
                    summarized.append(summary)
                    logger.debug(f"[summarizer] ✅ Successfully summarized result {i+1}")
                else:
                    logger.debug(f"[summarizer] ❌ Failed to summarize result {i+1}: no content")
            except Exception as e:
                logger.warning(f"Failed to summarize {result.url}: {e}")
                
        # Sort by relevance score
        summarized.sort(key=lambda x: x.relevance_score, reverse=True)
        
        logger.info(f"[summarizer] 📝 Summarization complete: {len(summarized)}/{len(results)} results summarized")
        return summarized
        
    def _summarize_single_result(
        self,
        result: Any,
        query_context: Dict[str, Any],
        max_code_snippets: int
    ) -> Optional[SummarizedResult]:
        """Summarize a single search result"""
        content = result.full_content or result.snippet
        if not content:
            logger.warning(f"No content found for result: {result.url} | full_content: {bool(result.full_content)} | snippet: {bool(result.snippet)}")
            return None
            
        # Extract key components
        code_snippets = self._extract_code_snippets(content, max_code_snippets)
        versions = self._extract_versions(content)
        warnings = self._extract_warnings(content)
        key_points = self._extract_key_points(content, query_context)
        
        # Generate TL;DR
        tldr = self._generate_tldr(result.title, result.snippet, key_points)
        
        # Create citation
        citation = self._create_citation(result)
        
        return SummarizedResult(
            url=result.url,
            title=result.title,
            tldr=tldr,
            key_points=key_points,
            code_snippets=code_snippets,
            canonical_versions=versions,
            warnings=warnings,
            citation=citation,
            relevance_score=result.relevance_score,
            published_date=result.published_date
        )
        
    def _extract_code_snippets(
        self,
        content: str,
        max_snippets: int
    ) -> List[CodeSnippet]:
        """Extract code snippets from content"""
        snippets = []
        
        # Try each pattern
        for pattern in self.CODE_PATTERNS:
            matches = re.finditer(pattern, content, re.DOTALL | re.IGNORECASE)
            for match in matches:
                if len(snippets) >= max_snippets:
                    break
                    
                if len(match.groups()) >= 2:
                    # Markdown style with language
                    language = match.group(1) or "unknown"
                    code = match.group(2).strip()
                else:
                    # HTML or inline style
                    language = self._detect_language(match.group(1))
                    code = match.group(1).strip()
                    
                # Clean HTML entities if present
                code = self._clean_html_entities(code)
                
                # Skip if too short or too long
                if 10 < len(code) < self.max_snippet_length:
                    # Extract description from surrounding text
                    description = self._extract_snippet_description(
                        content,
                        match.start()
                    )
                    
                    snippets.append(CodeSnippet(
                        language=language,
                        code=code,
                        description=description
                    ))
                    
        return snippets
        
    def _detect_language(self, code: str) -> str:
        """Detect programming language from code snippet"""
        # Simple heuristics
        if "resource " in code or "provider " in code:
            return "terraform"
        elif "import " in code or "def " in code:
            return "python"
        elif "const " in code or "function " in code:
            return "javascript"
        elif "<?php" in code:
            return "php"
        elif "<" in code and ">" in code:
            return "xml"
        elif "{" in code and "}" in code:
            return "json"
        else:
            return "bash"
            
    def _clean_html_entities(self, text: str) -> str:
        """Clean common HTML entities"""
        replacements = {
            "&lt;": "<",
            "&gt;": ">",
            "&amp;": "&",
            "&quot;": '"',
            "&#39;": "'",
            "&nbsp;": " "
        }
        for entity, char in replacements.items():
            text = text.replace(entity, char)
        return text
        
    def _extract_snippet_description(
        self,
        content: str,
        snippet_position: int,
        context_chars: int = 200
    ) -> str:
        """Extract description from text around code snippet"""
        # Get text before the snippet
        start = max(0, snippet_position - context_chars)
        context_before = content[start:snippet_position].strip()
        
        # Find the last sentence
        sentences = re.split(r'[.!?]\s+', context_before)
        if sentences:
            description = sentences[-1].strip()
            # Clean up
            description = re.sub(r'\s+', ' ', description)
            if len(description) > 100:
                description = description[:97] + "..."
            return description
            
        return "Code example from documentation"
        
    def _extract_versions(self, content: str) -> List[CanonicalVersion]:
        """Extract and canonicalize version numbers"""
        versions = []
        seen_versions = set()
        
        for version_type, patterns in self.VERSION_PATTERNS.items():
            for pattern in patterns:
                matches = re.finditer(pattern, content, re.IGNORECASE)
                for match in matches:
                    raw_version = match.group(1)
                    canonical = self._canonicalize_version(raw_version, version_type)
                    
                    # Avoid duplicates
                    version_key = f"{version_type}:{canonical}"
                    if version_key not in seen_versions:
                        seen_versions.add(version_key)
                        versions.append(CanonicalVersion(
                            raw_version=raw_version,
                            canonical_version=canonical,
                            version_type=version_type,
                            is_latest=self._check_if_latest(canonical, version_type)
                        ))
                        
        return versions
        
    def _canonicalize_version(self, version: str, version_type: str) -> str:
        """Convert version to canonical format"""
        # Remove 'v' prefix
        version = version.lstrip('v')
        
        # Handle different version formats
        if version_type == "api" and "-" in version:
            # API dates: keep as-is
            return version
        elif "~>" in version:
            # Terraform constraint: convert to minimum version
            return version.replace("~>", "").strip()
        else:
            # Semantic versioning: ensure 3 parts
            parts = version.split('.')
            while len(parts) < 3:
                parts.append('0')
            return '.'.join(parts[:3])
            
    def _check_if_latest(self, version: str, version_type: str) -> bool:
        """Check if version appears to be latest (heuristic)"""
        # This is a simplified check - in production, you'd check against
        # actual latest versions from package registries
        if version_type == "terraform":
            # As of 2024, Terraform 1.x is current
            return version.startswith("1.")
        elif version_type == "provider":
            # Most providers are on 4.x or 5.x
            major = version.split('.')[0]
            return int(major) >= 4 if major.isdigit() else False
            
        return False
        
    def _extract_warnings(self, content: str) -> List[str]:
        """Extract warnings about breaking changes, deprecations, etc."""
        warnings = []
        content_lower = content.lower()
        
        for indicator in self.WARNING_INDICATORS:
            if indicator in content_lower:
                # Find sentences containing the indicator
                sentences = re.split(r'[.!?]\s+', content)
                for sentence in sentences:
                    if indicator in sentence.lower():
                        # Clean and add warning
                        warning = re.sub(r'\s+', ' ', sentence).strip()
                        if len(warning) > 20 and len(warning) < 200:
                            warnings.append(warning)
                            if len(warnings) >= 3:  # Limit warnings
                                break
                                
        return warnings
        
    def _extract_key_points(
        self,
        content: str,
        query_context: Dict[str, Any]
    ) -> List[str]:
        """Extract key points relevant to the query"""
        key_points = []
        
        # Split into sentences
        sentences = re.split(r'[.!?]\s+', content)
        
        # Score sentences based on relevance
        scored_sentences = []
        query_terms = query_context.get("key_terms", [])
        
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 20 or len(sentence) > 300:
                continue
                
            # Calculate relevance score
            score = 0
            sentence_lower = sentence.lower()
            
            # Check for query terms
            for term in query_terms:
                if term.lower() in sentence_lower:
                    score += 2
                    
            # Check for action words
            action_words = ["must", "should", "required", "need", "ensure", "configure", "set", "enable", "disable"]
            for word in action_words:
                if word in sentence_lower:
                    score += 1
                    
            # Check for technical terms
            if any(pattern in sentence_lower for pattern in ["aws", "gcp", "azure", "terraform", "iam", "vpc"]):
                score += 1
                
            if score > 0:
                scored_sentences.append((score, sentence))
                
        # Sort by score and take top sentences
        scored_sentences.sort(key=lambda x: x[0], reverse=True)
        
        for score, sentence in scored_sentences[:5]:
            # Clean up sentence
            sentence = re.sub(r'\s+', ' ', sentence)
            if sentence not in key_points:
                key_points.append(sentence)
                
        return key_points
        
    def _generate_tldr(
        self,
        title: str,
        snippet: str,
        key_points: List[str]
    ) -> str:
        """Generate one-line TL;DR summary"""
        # Simple heuristic: combine title insight with most relevant key point
        if key_points:
            # Use first key point as base
            tldr = key_points[0]
        else:
            # Fall back to snippet
            tldr = snippet
            
        # Truncate to one line
        tldr = tldr.split('.')[0].strip()
        
        # Ensure reasonable length
        if len(tldr) > 150:
            tldr = tldr[:147] + "..."
            
        return tldr
        
    def _create_citation(self, result: Any) -> str:
        """Create a formatted citation"""
        # Extract domain for short citation
        from urllib.parse import urlparse
        domain = urlparse(result.url).netloc
        
        # Format date if available
        date_str = ""
        if result.published_date:
            date_str = f" ({result.published_date.strftime('%Y-%m-%d')})"
            
        # Create citation
        citation = f"[{result.title}]({result.url}) - {domain}{date_str}"
        
        return citation
        
    def merge_summaries(
        self,
        summaries: List[SummarizedResult],
        max_results: int = 5
    ) -> Dict[str, Any]:
        """Merge multiple summaries into a consolidated response"""
        logger.info(f"[merge] 📝 Starting merge with {len(summaries)} summaries, max_results={max_results}")
        
        if not summaries:
            logger.warning("[merge] ❌ No summaries provided to merge")
            return {
                "summary": "No relevant results found.",
                "results": [],
                "total_results": 0
            }
            
        # Take top results
        top_summaries = summaries[:max_results]
        logger.info(f"[merge] 📊 Taking top {len(top_summaries)} summaries")
        
        # Consolidate findings
        all_versions = []
        all_warnings = []
        all_code_snippets = []
        
        for summary in top_summaries:
            all_versions.extend(summary.canonical_versions)
            all_warnings.extend(summary.warnings)
            all_code_snippets.extend(summary.code_snippets[:1])  # One snippet per result
            
        logger.info(f"[merge] 📊 Consolidated: {len(all_versions)} versions, {len(all_warnings)} warnings, {len(all_code_snippets)} code snippets")
            
        # Create consolidated summary
        consolidated = {
            "summary": f"Found {len(summaries)} relevant results. Top findings:",
            "results": [s.to_dict() for s in top_summaries],
            "total_results": len(summaries),
            "consolidated_versions": self._deduplicate_versions(all_versions),
            "important_warnings": list(set(all_warnings))[:3],
            "example_snippets": [
                {
                    "language": s.language,
                    "code": s.code,
                    "description": s.description
                } for s in all_code_snippets[:3]
            ]
        }
        
        logger.info(f"[merge] ✅ Merge complete: {len(consolidated['results'])} results in final output")
        return consolidated
        
    def _deduplicate_versions(
        self,
        versions: List[CanonicalVersion]
    ) -> List[Dict[str, Any]]:
        """Deduplicate and organize versions by type"""
        version_map = {}
        
        for version in versions:
            key = f"{version.version_type}:{version.canonical_version}"
            if key not in version_map:
                version_map[key] = {
                    "type": version.version_type,
                    "version": version.canonical_version,
                    "is_latest": version.is_latest,
                    "occurrences": 1
                }
            else:
                version_map[key]["occurrences"] += 1
                
        # Sort by occurrence count and version
        sorted_versions = sorted(
            version_map.values(),
            key=lambda x: (x["occurrences"], x["version"]),
            reverse=True
        )
        
        return sorted_versions[:5]  # Return top 5 versions
