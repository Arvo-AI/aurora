"""
Query Composer for Web Search

Builds targeted search queries based on agent context and provider-specific needs.
"""

import re
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)

class QueryIntent(Enum):
    """Types of query intents"""
    ERROR_TROUBLESHOOTING = "error_troubleshooting"
    CONFIGURATION_EXAMPLE = "configuration_example"
    BREAKING_CHANGE = "breaking_change"
    BEST_PRACTICE = "best_practice"
    PRICING_COST = "pricing_cost"
    SERVICE_LIMITS = "service_limits"
    AUTHENTICATION = "authentication"
    NETWORKING = "networking"
    GENERAL_INFO = "general_info"
    GENERAL_KNOWLEDGE = "general_knowledge"

class QueryComposer:
    """Compose optimized search queries based on context"""
    
    # Provider-specific documentation sites
    PROVIDER_SITES = {
        "aws": [
            "site:docs.aws.amazon.com",
            "site:aws.amazon.com/blogs",
            "site:registry.terraform.io/providers/hashicorp/aws"
        ],
        "gcp": [
            "site:cloud.google.com",
            "site:registry.terraform.io/providers/hashicorp/google"
        ],
        "azure": [
            "site:docs.microsoft.com",
            "site:learn.microsoft.com",
            "site:registry.terraform.io/providers/hashicorp/azurerm"
        ]
    }
    
    # Service name mappings
    SERVICE_MAPPINGS = {
        "aws": {
            "vm": ["EC2", "instance"],
            "kubernetes": ["EKS", "Elastic Kubernetes Service"],
            "serverless": ["Lambda", "Fargate"],
            "storage": ["S3", "EBS", "EFS"],
            "database": ["RDS", "DynamoDB", "Aurora"],
            "network": ["VPC", "ELB", "CloudFront"],
        },
        "gcp": {
            "vm": ["Compute Engine", "GCE", "instance"],
            "kubernetes": ["GKE", "Google Kubernetes Engine"],
            "serverless": ["Cloud Run", "Cloud Functions"],
            "storage": ["Cloud Storage", "GCS", "Persistent Disk"],
            "database": ["Cloud SQL", "Firestore", "Bigtable"],
            "network": ["VPC", "Load Balancer", "Cloud CDN"],
        },
        "azure": {
            "vm": ["Virtual Machine", "VM", "instance"],
            "kubernetes": ["AKS", "Azure Kubernetes Service"],
            "serverless": ["Functions", "Container Instances"],
            "storage": ["Blob Storage", "Files", "Disk"],
            "database": ["SQL Database", "Cosmos DB", "PostgreSQL"],
            "network": ["Virtual Network", "VNet", "Load Balancer"],
        }
    }
    
    # Common error patterns and their search enhancements
    ERROR_PATTERNS = {
        r"permission.*denied": ["IAM", "role", "policy", "authorization"],
        r"quota.*exceeded": ["limits", "quotas", "increase", "request"],
        r"not.*found|404": ["resource", "exists", "create", "missing"],
        r"timeout": ["timeout", "duration", "performance", "slow"],
        r"invalid.*configuration": ["configuration", "syntax", "example", "format"],
        r"authentication.*failed": ["authentication", "credentials", "auth", "login"],
    }
    
    def __init__(self, model_knowledge_cutoff: Optional[datetime] = None):
        self.model_knowledge_cutoff = model_knowledge_cutoff or datetime(2024, 1, 1, tzinfo=timezone.utc)
        
    def compose_query(
        self,
        base_query: str,
        provider: Optional[str] = None,
        intent: Optional[QueryIntent] = None,
        error_message: Optional[str] = None,
        service_context: Optional[str] = None,
        terraform_version: Optional[str] = None,
        include_recent: bool = True
    ) -> Tuple[str, Dict[str, any]]:
        """
        Compose an optimized search query
        
        Args:
            base_query: The original query from the agent
            provider: Cloud provider (aws, gcp, azure)
            intent: Detected or specified query intent
            error_message: Error message to help with troubleshooting
            service_context: Specific service being queried (e.g., EC2, GKE)
            terraform_version: Terraform version for compatibility queries
            include_recent: Whether to focus on recent content
            
        Returns:
            Tuple of (enhanced_query, metadata)
        """
        # Detect intent if not provided
        if not intent:
            intent = self._detect_intent(base_query, error_message)
            
        # Start with base query
        query_parts = [base_query]
        metadata = {
            "original_query": base_query,
            "intent": intent.value,
            "provider": provider
        }
        
        # Add provider-specific enhancements
        if provider:
            query_parts.extend(self._add_provider_context(provider, service_context))
            
        # Add intent-specific enhancements
        query_parts.extend(self._add_intent_context(intent, error_message))
        
        # Add Terraform context if relevant
        if terraform_version or self._is_terraform_related(base_query):
            query_parts.extend(self._add_terraform_context(terraform_version))
            metadata["terraform_version"] = terraform_version
            
        # Add time-based filters for recent content
        if include_recent:
            time_filter = self._get_time_filter(intent)
            if time_filter:
                query_parts.append(time_filter)
                metadata["time_filter"] = time_filter
                
        # For "latest" queries, add recency boosters
        if any(term in base_query.lower() for term in ["latest", "current", "new", "recent"]):
            query_parts.extend(["latest", "current", "2024", "2025"])
            metadata["latest_query"] = True
                
        # Build final query
        enhanced_query = self._build_final_query(query_parts, provider)
        
        logger.info(f"Composed query: {enhanced_query}")
        logger.debug(f"Query metadata: {metadata}")
        
        return enhanced_query, metadata
        
    def _detect_intent(self, query: str, error_message: Optional[str] = None) -> QueryIntent:
        """Detect the intent of the query"""
        query_lower = query.lower()
        combined_text = f"{query_lower} {(error_message or '').lower()}"
        
        # Check for specific intent indicators
        if any(word in combined_text for word in ["error", "failed", "exception", "issue"]):
            return QueryIntent.ERROR_TROUBLESHOOTING
        elif any(word in combined_text for word in ["example", "sample", "how to", "tutorial"]):
            return QueryIntent.CONFIGURATION_EXAMPLE
        elif any(word in combined_text for word in ["breaking", "deprecat", "migration", "upgrade"]):
            return QueryIntent.BREAKING_CHANGE
        elif any(word in combined_text for word in ["best practice", "recommend", "optimal"]):
            return QueryIntent.BEST_PRACTICE
        elif any(word in combined_text for word in ["price", "cost", "billing", "charge"]):
            return QueryIntent.PRICING_COST
        elif any(word in combined_text for word in ["limit", "quota", "maximum", "threshold"]):
            return QueryIntent.SERVICE_LIMITS
        elif any(word in combined_text for word in ["auth", "iam", "permission", "role"]):
            return QueryIntent.AUTHENTICATION
        elif any(word in combined_text for word in ["network", "vpc", "subnet", "firewall"]):
            return QueryIntent.NETWORKING
            
        return QueryIntent.GENERAL_INFO
        
    def _add_provider_context(self, provider: str, service_context: Optional[str] = None) -> List[str]:
        """Add provider-specific search terms"""
        terms = []
        
        # Add provider name variations
        if provider == "aws":
            terms.append("(AWS OR \"Amazon Web Services\")")
        elif provider == "gcp":
            terms.append("(GCP OR \"Google Cloud Platform\" OR \"Google Cloud\")")
        elif provider == "azure":
            terms.append("(Azure OR \"Microsoft Azure\")")
            
        # Add service-specific terms if we can identify the service
        if service_context:
            service_lower = service_context.lower()
            for service_type, keywords in self.SERVICE_MAPPINGS.get(provider, {}).items():
                if any(kw.lower() in service_lower for kw in keywords):
                    terms.extend([f"\"{kw}\"" for kw in keywords[:2]])  # Add top 2 keywords
                    break
                    
        return terms
        
    def _add_intent_context(self, intent: QueryIntent, error_message: Optional[str] = None) -> List[str]:
        """Add intent-specific search terms"""
        terms = []
        
        if intent == QueryIntent.ERROR_TROUBLESHOOTING:
            terms.extend(["troubleshooting", "solution", "fix"])
            if error_message:
                # Extract key error terms
                for pattern, keywords in self.ERROR_PATTERNS.items():
                    if re.search(pattern, error_message, re.IGNORECASE):
                        terms.extend(keywords[:2])  # Add top 2 keywords
                        break
                        
        elif intent == QueryIntent.CONFIGURATION_EXAMPLE:
            terms.extend(["example", "configuration", "sample"])
        elif intent == QueryIntent.BREAKING_CHANGE:
            terms.extend(["breaking change", "migration", "changelog"])
        elif intent == QueryIntent.BEST_PRACTICE:
            terms.extend(["best practice", "recommended", "guidelines"])
        elif intent == QueryIntent.PRICING_COST:
            terms.extend(["pricing", "cost", "calculator"])
        elif intent == QueryIntent.SERVICE_LIMITS:
            terms.extend(["limits", "quotas", "maximum"])
        elif intent == QueryIntent.AUTHENTICATION:
            terms.extend(["IAM", "authentication", "permissions"])
        elif intent == QueryIntent.NETWORKING:
            terms.extend(["networking", "connectivity", "firewall"])
            
        return terms
        
    def _add_terraform_context(self, terraform_version: Optional[str] = None) -> List[str]:
        """Add Terraform-specific search terms"""
        terms = ["Terraform"]
        
        if terraform_version:
            # Add version-specific terms
            major_version = terraform_version.split('.')[0]
            terms.append(f"\"Terraform {major_version}\"")
            
        terms.extend(["provider", "resource"])
        return terms
        
    def _is_terraform_related(self, query: str) -> bool:
        """Check if query is related to Terraform"""
        terraform_indicators = [
            "terraform", "tf", "hcl", "provider", "resource",
            "module", "variable", "output", "data source"
        ]
        query_lower = query.lower()
        return any(indicator in query_lower for indicator in terraform_indicators)
        
    def _get_time_filter(self, intent: QueryIntent) -> Optional[str]:
        """Get appropriate time filter based on intent"""
        # For breaking changes and recent updates, focus on content after model's knowledge cutoff
        if intent in [QueryIntent.BREAKING_CHANGE, QueryIntent.ERROR_TROUBLESHOOTING]:
            cutoff_date = self.model_knowledge_cutoff.strftime("%Y-%m-%d")
            return f"after:{cutoff_date}"
            
        # For general info queries, don't use restrictive time filters
        # Instead let the search engine prioritize recent content naturally
        if intent == QueryIntent.GENERAL_INFO:
            return None  # No time filter for general info
            
        # For other intents, include recent content (last 1 year) 
        recent_date = datetime.now(timezone.utc).replace(year=datetime.now().year - 1)
        return f"after:{recent_date.strftime('%Y-%m-%d')}"
        
    def _build_final_query(self, query_parts: List[str], provider: Optional[str] = None) -> str:
        """Build the final search query"""
        # Remove duplicates while preserving order
        seen = set()
        unique_parts = []
        for part in query_parts:
            if part not in seen:
                seen.add(part)
                unique_parts.append(part)
                
        # Join parts
        query = " ".join(unique_parts)
        
        # Add site restrictions if provider is specified, but be less restrictive for general queries
        if provider and provider in self.PROVIDER_SITES:
            # For general info queries, use broader site restrictions
            if any(term in query.lower() for term in ["latest", "current", "new", "recent", "ami", "image"]):
                # Use broader search for latest/current information
                if provider == "aws":
                    sites = "site:aws.amazon.com OR site:docs.aws.amazon.com"
                elif provider == "gcp":
                    sites = "site:cloud.google.com"
                elif provider == "azure":
                    sites = "site:docs.microsoft.com OR site:azure.microsoft.com"
                query = f"{query} ({sites})"
            else:
                # Use full site restrictions for specific technical queries
                sites = " OR ".join(self.PROVIDER_SITES[provider])
                query = f"{query} ({sites})"
            
        # Clean up query
        query = re.sub(r'\s+', ' ', query).strip()
        
        # Ensure query isn't too long (some search APIs have limits)
        if len(query) > 500:
            # Truncate smartly by removing less important parts
            query = query[:500].rsplit(' ', 1)[0]
            
        return query
        
    def extract_key_terms(self, query: str) -> List[str]:
        """Extract key terms from a query for caching and analysis"""
        # Remove common words
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
            "being", "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "must", "can", "what", "how", "why",
            "when", "where", "which", "who", "whom", "whose"
        }
        
        # Extract words
        words = re.findall(r'\b\w+\b', query.lower())
        
        # Filter out stop words and short words
        key_terms = [w for w in words if w not in stop_words and len(w) > 2]
        
        # Also extract quoted phrases
        quoted_phrases = re.findall(r'"([^"]+)"', query)
        key_terms.extend(quoted_phrases)
        
        return key_terms
