# Utils package initialization

import json
from typing import Any, Dict


def safe_json_dump(data: Dict[str, Any]) -> str:
    """JSON-serialize data with a fallback to str() if serialization fails."""
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)