import ast
from pathlib import Path
from typing import List, Tuple

import pytest

ROUTES_DIR = Path(__file__).resolve().parent.parent / "routes"

TARGET_FILES = [
    "multi_agent_config.py",
    "model_roles.py",
    "subagent_overrides.py",
    "incident_subagents.py",
]

RBAC_DECORATORS = {"require_permission", "require_auth_only"}


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _find_route_functions(filepath: Path) -> List[Tuple[str, int, bool]]:
    try:
        tree = ast.parse(filepath.read_text(), filename=str(filepath))
    except SyntaxError:
        return []

    results: List[Tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_route = False
        has_rbac = False
        for dec in node.decorator_list:
            dec_name = _decorator_name(dec)
            if dec_name and "route" in dec_name:
                has_route = True
            if dec_name and dec_name in RBAC_DECORATORS:
                has_rbac = True
        if has_route:
            results.append((node.name, node.lineno, has_rbac))
    return results


def test_multi_agent_routes_rbac():
    violations: List[str] = []
    found_any = False

    for name in TARGET_FILES:
        filepath = ROUTES_DIR / name
        if not filepath.is_file():
            continue
        found_any = True
        for func_name, lineno, has_rbac in _find_route_functions(filepath):
            if not has_rbac:
                rel = filepath.relative_to(ROUTES_DIR.parent)
                violations.append(
                    f"{rel}:{lineno} — {func_name}() is missing "
                    f"@require_permission or @require_auth_only"
                )

    if not found_any:
        return

    if violations:
        msg = (
            "Multi-agent routes missing RBAC decorators:\n\n"
            + "\n".join(f"  • {v}" for v in violations)
            + "\n\nSee CLAUDE.md § 'New Connector Checklist' for requirements."
        )
        pytest.fail(msg)
