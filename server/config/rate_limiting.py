"""
Rate Limiting Configuration for Aurora

This module defines rate limiting defaults and per-endpoint policies shared by
Flask-Limiter. All logic that actually applies the limits lives in
`utils/limiter_ext.py` so this file stays focused on configuration values.

Security Purpose:
- Prevent credential brute force attacks
- Mitigate DDoS and resource exhaustion
- Protect against session exhaustion
- Ensure fair resource allocation

Created: 2025-10-16
Security Level: CRITICAL
"""

import os


# Redis configuration for distributed rate limiting - REQUIRED
RATE_LIMIT_STORAGE_URL = os.getenv("REDIS_URL")
if not RATE_LIMIT_STORAGE_URL:
    raise RuntimeError(
        "REDIS_URL environment variable is required for rate limiting. "
        "Set REDIS_URL in your .env file (e.g., REDIS_URL=redis://localhost:6379/0)"
    )

# Rate limiting strategy (fixed window keeps behaviour predictable for now)
RATE_LIMIT_STRATEGY = "fixed-window"

# Default rate limits (applied globally unless a route overrides them)
DEFAULT_RATE_LIMITS = [
    "2000 per day",    # Baseline guardrail until we collect real usage data
    "500 per hour",    # Conservative starting point; safe to adjust once metrics exist
]

# Global toggles so we can disable/inspect limits in lower environments quickly
RATE_LIMITING_ENABLED = os.getenv("RATE_LIMITING_ENABLED", "true").lower() == "true"
RATE_LIMIT_HEADERS_ENABLED = os.getenv("RATE_LIMIT_HEADERS_ENABLED", "true").lower() == "true"


# ============================================================================
# Endpoint-Specific Rate Limits
# ============================================================================

# OVH Authentication Endpoints (High Risk - Strict Limits)
OVH_AUTH_LIMITS = "10 per minute;50 per hour;200 per day"

# OVH OAuth2 Flow Endpoints (Medium-High Risk)
OVH_OAUTH2_LIMITS = "5 per minute;20 per hour;100 per day"

# OVH Onboarding Endpoints (High Risk - Account Creation)
OVH_ONBOARDING_LIMITS = "3 per minute;10 per hour;30 per day"

# OVH Read Endpoints (Medium Risk)
OVH_READ_LIMITS = "30 per minute;500 per hour;5000 per day"

# OVH Write Endpoints (Medium-High Risk)
OVH_WRITE_LIMITS = "15 per minute;100 per hour;1000 per day"

# Azure Authentication Endpoints
AZURE_AUTH_LIMITS = [
    "10 per minute",
    "50 per hour",
    "200 per day",
]

# GCP Authentication Endpoints
GCP_AUTH_LIMITS = [
    "10 per minute",
    "50 per hour",
    "200 per day",
]

# AWS Authentication Endpoints
AWS_AUTH_LIMITS = [
    "10 per minute",
    "50 per hour",
    "200 per day",
]

# General API Endpoints
GENERAL_API_LIMITS = [
    "60 per minute",
    "1000 per hour",
    "10000 per day",
]


# ==========================================================================
# Error Response Template
# ==========================================================================

RATE_LIMIT_ERROR_RESPONSE = {
    "error": "Rate limit exceeded",
    "message": "Too many requests. Please try again later.",
    "documentation": "https://docs.aurora.app/rate-limits",
    "hint": "Rate limits protect against abuse. Contact support if you need higher limits.",
}
