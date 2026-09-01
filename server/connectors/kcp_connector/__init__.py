"""KCP (Knowledge Context Protocol) connector for Aurora's knowledge base.

Reads a KCP ``knowledge.yaml`` manifest and ingests its units into
Aurora's Weaviate-backed KB, preserving structured metadata (intent,
triggers, audience, temporal validity) that would otherwise be lost
in a flat document upload.
"""
