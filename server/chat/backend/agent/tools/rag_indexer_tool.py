import os
import io
import zipfile
import logging
from typing import List, Optional, Dict, Any, Tuple

from pydantic import BaseModel, Field
from chat.backend.agent.weaviate_client import WeaviateClient
from chat.backend.agent.db import PostgreSQLClient

logger = logging.getLogger(__name__)


class RAGIndexZipArgs(BaseModel):
    attachment_index: int = Field(0, description="Index of the ZIP attachment to index")
    max_files: int = Field(200, description="Safety cap on number of files to index")
    max_file_bytes: int = Field(750_000, description="Max bytes per file to index")
    include_patterns: Optional[List[str]] = Field(
        default=[".md", ".txt", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".sh", ".tf"],
        description="File extensions to include"
    )
    exclude_dirs: Optional[List[str]] = Field(
        default=["node_modules", ".git", "__pycache__", "dist", "build", "__MACOSX"],
        description="Directory names to skip"
    )


def _should_index_file(path: str, include_exts: List[str], exclude_dirs: List[str]) -> bool:
    norm = path.strip()
    parts = norm.split("/")
    for d in exclude_dirs:
        if d in parts:
            return False
    _, ext = os.path.splitext(norm.lower())
    return (ext in include_exts) and not norm.endswith("/")


def _chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> List[str]:
    if not text:
        return []
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        end = min(n, i + chunk_size)
        chunks.append(text[i:end])
        i = end - overlap if end - overlap > i else end
    return chunks


def rag_index_zip(attachment_index: int = 0, max_files: int = 200, max_file_bytes: int = 750_000,
                  include_patterns: Optional[List[str]] = None, exclude_dirs: Optional[List[str]] = None,
                  user_id: Optional[str] = None, session_id: Optional[str] = None) -> str:
    """Index a ZIP attachment's text/code files into Weaviate for RAG.

    - Reads attachment from state (supports server_path or storage URI).
    - Filters by extension and excluded directories.
    - Chunks text and upserts with minimal metadata: user_id hash, session_id, filename, path.
    """
    try:
        # Lazy import to avoid circular import with cloud_tools during module initialization
        from .cloud_tools import get_state_context, send_tool_start, send_tool_completion

        state = get_state_context()
        if not state or not getattr(state, 'attachments', None):
            return "No attachments found in state. Please upload a ZIP."

        if attachment_index < 0 or attachment_index >= len(state.attachments):
            return f"Invalid attachment_index {attachment_index}."

        attachment = state.attachments[attachment_index]
        server_path = attachment.get('server_path') or attachment.get('path') or attachment.get('storage_uri')
        filename = attachment.get('filename', 'archive.zip')

        if not server_path:
            return "Attachment does not include a server_path or storage URI."

        # Generate consistent tool_call_id for start/completion matching
        import hashlib
        import json
        tool_input_data = {"attachment_index": attachment_index, "filename": filename}
        # Use JSON serialization with sorted keys for deterministic hashing
        signature = f"rag_index_zip_{json.dumps(tool_input_data, sort_keys=True, default=str)}"
        # Use longer hash (16 chars) to reduce collision risk
        signature_hash = hashlib.sha256(signature.encode()).hexdigest()[:16]
        tool_call_id = f"rag_index_zip_{signature_hash}"
        
        send_tool_start("rag_index_zip", tool_input_data, tool_call_id)

        # Open the zip locally (download from storage if needed)
        local_path = server_path
        temp_to_delete = None
        if server_path.startswith('s3://'):
            from utils.storage.storage import download_zip_from_storage
            local_path, _ = download_zip_from_storage(server_path, user_id=user_id)
            temp_to_delete = local_path

        include_exts = include_patterns or [".md", ".txt", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".sh", ".tf"]
        skip_dirs = exclude_dirs or ["node_modules", ".git", "__pycache__", "dist", "build", "__MACOSX"]

        # Collect files and prepare chunks
        indexed_files = 0
        indexed_chunks = 0
        errors = 0
        vectors: List[Tuple[str, Dict[str, Any]]] = []

        with zipfile.ZipFile(local_path, 'r') as zip_ref:
            for info in zip_ref.infolist():
                # Respect caps
                if indexed_files >= max_files:
                    break
                path = info.filename
                if not _should_index_file(path, include_exts, skip_dirs):
                    continue
                try:
                    data = zip_ref.read(info)
                    if len(data) > max_file_bytes:
                        continue
                    try:
                        text = data.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            text = data.decode('latin-1')
                        except Exception:
                            continue
                    chunks = _chunk_text(text)
                    if not chunks:
                        continue
                    # Metadata
                    meta = {
                        "filename": os.path.basename(path),
                        "path": path,
                        "archive": filename,
                        "session_id": session_id or getattr(state, 'session_id', None),
                        "user_id": user_id or getattr(state, 'user_id', None),
                    }
                    for c in chunks:
                        vectors.append((c, meta))
                    indexed_files += 1
                    indexed_chunks += len(chunks)
                except Exception as e:
                    errors += 1
                    logger.warning(f"Failed to index file {path}: {e}")

        # Upsert into Weaviate
        if vectors:
            try:
                pg = PostgreSQLClient()
                wv = WeaviateClient(pg)
                # Use a simple collection for RAG documents if not present
                # We'll create a generic 'RAGDoc' class on demand using weaviate client directly
                client = wv.client
                if not client.collections.exists("RAGDoc"):
                    from weaviate.classes.config import Configure, Property, DataType
                    client.collections.create(
                        name="RAGDoc",
                        vectorizer_config=Configure.Vectorizer.text2vec_openai(),
                        properties=[
                            Property(name="text", data_type=DataType.TEXT),
                            Property(name="filename", data_type=DataType.TEXT),
                            Property(name="path", data_type=DataType.TEXT),
                            Property(name="archive", data_type=DataType.TEXT),
                            Property(name="session_id", data_type=DataType.TEXT),
                            Property(name="user_id", data_type=DataType.TEXT),
                        ],
                    )
                coll = client.collections.get("RAGDoc")
                import uuid as _uuid
                # Insert in batches
                batch = coll.batch.dynamic()
                for text, meta in vectors:
                    batch.add_object(properties={"text": text, **meta}, uuid=_uuid.uuid4())
                batch.flush()
            except Exception as e:
                errors += 1
                logger.error(f"Weaviate upsert failed: {e}")

        if temp_to_delete and os.path.exists(temp_to_delete):
            try:
                os.remove(temp_to_delete)
            except Exception:
                pass

        out = (
            f"Indexed {indexed_files} files and {indexed_chunks} chunks from {filename} into RAG."
        )
        if errors:
            out += f" Encountered {errors} errors."
        send_tool_completion("rag_index_zip", out, "completed", tool_call_id)
        return out

    except Exception as e:
        logger.error(f"Error in rag_index_zip: {e}")
        return f"Error indexing ZIP: {str(e)}" 