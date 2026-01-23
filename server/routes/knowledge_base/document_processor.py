"""
Document Processor for Knowledge Base

Handles parsing and chunking of documents for RAG retrieval.
Supports Markdown, Plain Text, and PDF formats.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

TARGET_CHUNK_SIZE = 1500  # ~300-500 tokens
CHUNK_OVERLAP = 200
MIN_CHUNK_SIZE = 100


class DocumentProcessor:
    """Process documents into chunks for vector storage."""

    def __init__(self, user_id: str, document_id: str, original_filename: str):
        self.user_id = user_id
        self.document_id = document_id
        self.original_filename = original_filename

    def process(self, content: bytes, file_type: str) -> list[dict[str, Any]]:
        """
        Process document content into chunks.

        Args:
            content: Raw file content as bytes
            file_type: One of 'markdown', 'plaintext', 'pdf'

        Returns:
            List of chunk dictionaries with 'content', 'heading_context', 'chunk_index'
        """
        try:
            if file_type == "pdf":
                text = self._extract_pdf_text(content)
            else:
                text = self._decode_text(content)

            if not text.strip():
                logger.warning(f"[KB Processor] No text extracted from {self.original_filename}")
                return []

            if file_type == "markdown":
                chunks = self._chunk_markdown(text)
            else:
                chunks = self._chunk_plaintext(text)

            logger.info(
                f"[KB Processor] Processed {self.original_filename}: {len(chunks)} chunks"
            )
            return chunks

        except Exception as e:
            logger.exception(f"[KB Processor] Error processing {self.original_filename}: {e}")
            raise

    def _decode_text(self, content: bytes) -> str:
        """Decode bytes to text, trying multiple encodings."""
        encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
        for encoding in encodings:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        # Fallback: decode with replacement characters
        return content.decode("utf-8", errors="replace")

    def _extract_pdf_text(self, content: bytes) -> str:
        """Extract text from PDF using pypdf."""
        try:
            from pypdf import PdfReader
            import io
        except ImportError:
            logger.error("[KB Processor] pypdf not installed")
            raise RuntimeError("PDF processing requires pypdf. Install with: pip install pypdf")

        try:
            pdf_reader = PdfReader(io.BytesIO(content))
            text_parts = []
            for page_num, page in enumerate(pdf_reader.pages):
                page_text = page.extract_text()
                if page_text.strip():
                    text_parts.append(f"[Page {page_num + 1}]\n{page_text}")
            return "\n\n".join(text_parts)
        except Exception as e:
            logger.error(f"[KB Processor] PDF extraction failed: {e}")
            raise

    def _find_code_block_regions(self, text: str) -> list[tuple[int, int]]:
        """Find all fenced code block regions (``` or ~~~) in the text."""
        regions = []
        fence_pattern = re.compile(r'^(`{3,}|~{3,})', re.MULTILINE)
        matches = list(fence_pattern.finditer(text))

        # Pair up opening and closing fences
        i = 0
        while i < len(matches) - 1:
            start = matches[i].start()
            fence_char = matches[i].group(1)[0]  # ` or ~
            # Find matching closing fence
            for j in range(i + 1, len(matches)):
                if matches[j].group(1)[0] == fence_char:
                    end = matches[j].end()
                    regions.append((start, end))
                    i = j + 1
                    break
            else:
                i += 1

        return regions

    def _is_in_code_block(self, position: int, regions: list[tuple[int, int]]) -> bool:
        """Check if a position falls within any code block region."""
        for start, end in regions:
            if start <= position < end:
                return True
        return False

    def _chunk_markdown(self, text: str) -> list[dict[str, Any]]:
        """
        Chunk markdown text with heading-aware splitting.
        Preserves heading hierarchy as context for each chunk.
        """
        chunks = []
        current_headings: list[tuple[int, str]] = []

        # Find fenced code block regions to exclude from header detection
        code_block_regions = self._find_code_block_regions(text)

        # Split by markdown headers
        header_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

        # Find all headers and their positions (excluding those in code blocks)
        headers = []
        for match in header_pattern.finditer(text):
            # Skip headers inside fenced code blocks
            if self._is_in_code_block(match.start(), code_block_regions):
                continue
            level = len(match.group(1))
            header_text = match.group(2).strip()
            headers.append((match.start(), match.end(), level, header_text))

        if not headers:
            # No headers, treat as plain text
            return self._chunk_plaintext(text)

        # Process sections between headers
        for i, (_, end, level, header_text) in enumerate(headers):
            # Update heading hierarchy
            current_headings = [h for h in current_headings if h[0] < level]
            current_headings.append((level, header_text))

            # Get content until next header or end
            content_start = end
            content_end = headers[i + 1][0] if i + 1 < len(headers) else len(text)
            section_content = text[content_start:content_end].strip()

            if not section_content:
                continue

            # Build heading context string
            heading_context = " > ".join([h[1] for h in current_headings])

            # Chunk the section content
            section_chunks = self._split_text(
                section_content,
                heading_context=heading_context,
                start_index=len(chunks),
            )
            chunks.extend(section_chunks)

        return chunks

    def _chunk_plaintext(self, text: str) -> list[dict[str, Any]]:
        """Chunk plain text with paragraph-aware splitting."""
        return self._split_text(text, heading_context="", start_index=0)

    def _split_text(
        self,
        text: str,
        heading_context: str = "",
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Split text into chunks with overlap.

        Uses a hierarchy of separators for semantic-aware splitting:
        1. Double newlines (paragraphs)
        2. Single newlines
        3. Sentences
        4. Character-based fallback
        """
        chunks = []
        text = text.strip()

        if not text:
            return []

        if len(text) <= TARGET_CHUNK_SIZE:
            return [
                {
                    "content": text,
                    "heading_context": heading_context,
                    "chunk_index": start_index,
                }
            ]

        # Try different separators in order of preference
        separators = ["\n\n", "\n", ". ", " "]

        current_chunk = ""
        chunk_index = start_index

        for separator in separators:
            if separator not in text:
                continue

            parts = text.split(separator)
            current_chunk = ""

            for i, part in enumerate(parts):
                part = part.strip()
                if not part:
                    continue

                # Add separator back (except for the last part)
                if i < len(parts) - 1 and separator not in [" "]:
                    part = part + separator

                potential_chunk = current_chunk + part

                if len(potential_chunk) <= TARGET_CHUNK_SIZE:
                    current_chunk = potential_chunk
                else:
                    # Save current chunk if it's big enough
                    if len(current_chunk) >= MIN_CHUNK_SIZE:
                        chunks.append({
                            "content": current_chunk.strip(),
                            "heading_context": heading_context,
                            "chunk_index": chunk_index,
                        })
                        chunk_index += 1

                        # Handle oversized parts by force-splitting them
                        if len(part) > TARGET_CHUNK_SIZE:
                            part_chunks = self._force_split(part, heading_context, chunk_index)
                            if part_chunks:
                                # Add all but the last chunk
                                for pc in part_chunks[:-1]:
                                    chunks.append(pc)
                                    chunk_index += 1
                                # Use the last chunk as current_chunk for overlap continuity
                                last_chunk = part_chunks[-1]
                                current_chunk = last_chunk.get("content", "")
                            else:
                                current_chunk = part
                        else:
                            # Start new chunk with overlap
                            overlap_text = self._get_overlap(current_chunk)
                            current_chunk = overlap_text + part
                    else:
                        current_chunk = potential_chunk

            # Don't break if we haven't created any chunks yet
            if chunks:
                break

        # Add remaining content
        if current_chunk.strip() and len(current_chunk.strip()) >= MIN_CHUNK_SIZE:
            chunks.append({
                "content": current_chunk.strip(),
                "heading_context": heading_context,
                "chunk_index": chunk_index,
            })

        # Fallback: if no chunks created, force split by character count
        if not chunks:
            chunks = self._force_split(text, heading_context, start_index)

        return chunks

    def _get_overlap(self, text: str) -> str:
        """Get the last CHUNK_OVERLAP characters as overlap for next chunk."""
        if len(text) <= CHUNK_OVERLAP:
            return ""

        overlap_start = len(text) - CHUNK_OVERLAP
        overlap = text[overlap_start:]

        # Only skip to word boundary if we're starting mid-word
        # Check if char before overlap is whitespace (meaning we're at word boundary)
        if overlap_start > 0 and not text[overlap_start - 1].isspace():
            # We're mid-word, skip to first complete word
            space_idx = overlap.find(" ")
            if space_idx > 0:
                overlap = overlap[space_idx + 1:]

        return overlap

    def _force_split(
        self,
        text: str,
        heading_context: str,
        start_index: int,
    ) -> list[dict[str, Any]]:
        """Force split text by character count when other methods fail."""
        chunks = []
        chunk_index = start_index

        i = 0
        while i < len(text):
            end = min(i + TARGET_CHUNK_SIZE, len(text))

            # Try to end at a word boundary
            if end < len(text):
                space_idx = text.rfind(" ", i, end)
                if space_idx > i + MIN_CHUNK_SIZE:
                    end = space_idx

            chunk_text = text[i:end].strip()
            if chunk_text:
                chunks.append({
                    "content": chunk_text,
                    "heading_context": heading_context,
                    "chunk_index": chunk_index,
                })
                chunk_index += 1

            # Move forward, accounting for overlap
            i = end - CHUNK_OVERLAP if end < len(text) else end

        return chunks
