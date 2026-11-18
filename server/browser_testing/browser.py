"""Origin-confined, read-only Playwright evidence collection."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .paths import artifact_file


@dataclass(frozen=True)
class RowCandidate:
    candidate_id: str
    anchor_text: str
    exact_match_count: int
    container_tag: str
    container_text: str
    badge_texts: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value["badge_texts"] = list(self.badge_texts)
        return value


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("preview URL must use http or https")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class BrowserSession:
    """One fresh browser/context/page with no cross-origin requests."""

    def __init__(
        self,
        preview_url: str,
        timeout_ms: int = 10_000,
        *,
        open_preview: bool = True,
    ):
        self.preview_url = preview_url
        self.origin = _origin(preview_url)
        self.timeout_ms = timeout_ms
        self.open_preview = open_preview
        self.candidates: dict[str, RowCandidate] = {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "BrowserSession":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(viewport={"width": 1440, "height": 1000})
        self._context.set_default_timeout(self.timeout_ms)

        def confine(route) -> None:
            request_url = route.request.url
            if request_url.startswith(("data:", "blob:")) or _origin(request_url) == self.origin:
                route.continue_()
            else:
                route.abort("blockedbyclient")

        self._context.route("**/*", confine)
        self.page = self._context.new_page()
        if self.open_preview:
            self.page.goto(self.preview_url, wait_until="domcontentloaded", timeout=15_000)
            self.page.wait_for_load_state("networkidle", timeout=10_000)
            self._assert_origin()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self._context and self._context.close()
        finally:
            try:
                self._browser and self._browser.close()
            finally:
                self._playwright and self._playwright.stop()

    def _assert_origin(self) -> None:
        if self.page is None or _origin(self.page.url) != self.origin:
            raise RuntimeError("browser left the preview origin")

    def page_text(self) -> dict[str, Any]:
        self._assert_origin()
        assert self.page is not None
        text = self.page.locator("body").inner_text(timeout=self.timeout_ms)
        return {"url": self.page.url, "visibleText": text[:12_000], "truncated": len(text) > 12_000}

    def observe_status(
        self, path: str, screenshot_filename: str | None = None
    ) -> dict[str, Any]:
        """Navigate a fresh page on the preview origin and capture its HTTP status."""
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "#" in path
        ):
            raise ValueError("path must be an origin-relative URL path without a fragment")
        if self._context is None:
            raise RuntimeError("browser context is not open")

        page = self._context.new_page()
        try:
            response = page.goto(
                f"{self.origin}{path}",
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            if response is None:
                return {"path": path, "status": None, "error": "navigation returned no response"}
            if _origin(response.url) != self.origin:
                return {"path": path, "status": None, "error": "navigation left the preview origin"}
            if screenshot_filename:
                target = artifact_file(screenshot_filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target), full_page=True, timeout=10_000)
            return {"path": path, "status": response.status, "error": None}
        except PlaywrightTimeoutError as exc:
            return {"path": path, "status": None, "error": f"navigation timed out: {exc}"}
        finally:
            page.close()

    def inspect_row(self, anchor_text: str) -> dict[str, Any]:
        self._assert_origin()
        if not isinstance(anchor_text, str) or not anchor_text.strip() or len(anchor_text) > 256:
            raise ValueError("anchorText must be non-empty text up to 256 characters")
        assert self.page is not None
        locator = self.page.get_by_text(anchor_text.strip(), exact=True)
        total = locator.count()
        if total > 20:
            return {"anchorText": anchor_text, "exactMatchCount": total, "candidates": [], "tooMany": True}

        found = []
        for index in range(total):
            element = locator.nth(index)
            if not element.is_visible():
                continue
            data = element.evaluate(
                """element => {
                  let root = element.closest('article, tr, li, [role="row"]');
                  if (!root) root = element.parentElement;
                  for (let i = 0; root && i < 3 && root.parentElement &&
                       root.innerText.trim() === element.innerText.trim(); i++) root = root.parentElement;
                  root = root || element;
                  const badges = [...root.querySelectorAll('.badge, [data-testid*="badge"], [class*="badge"]')]
                    .filter(node => node instanceof HTMLElement && node.offsetParent !== null)
                    .map(node => node.textContent.trim()).filter(Boolean).slice(0, 10);
                  return { tag: root.tagName.toLowerCase(), text: root.innerText.trim(), badges };
                }"""
            )
            container_text = str(data.get("text", ""))[:2_000]
            badge_texts = tuple(str(item)[:256] for item in data.get("badges", []))
            seed = f"{anchor_text}\0{index}\0{container_text}\0{badge_texts}"
            candidate_id = "row-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            candidate = RowCandidate(
                candidate_id=candidate_id,
                anchor_text=anchor_text.strip(),
                exact_match_count=total,
                container_tag=str(data.get("tag", ""))[:32],
                container_text=container_text,
                badge_texts=badge_texts,
            )
            self.candidates[candidate_id] = candidate
            found.append(candidate.public())
        return {
            "anchorText": anchor_text.strip(),
            "exactMatchCount": total,
            "candidates": found,
            "tooMany": False,
        }

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "read_visible_page":
            if arguments:
                raise ValueError("read_visible_page takes no arguments")
            return self.page_text()
        if name == "inspect_row":
            if set(arguments) != {"anchorText"}:
                raise ValueError("inspect_row requires only anchorText")
            return self.inspect_row(arguments["anchorText"])
        raise ValueError(f"unsupported browser tool: {name}")

    def screenshot(self, filename: str) -> None:
        self._assert_origin()
        assert self.page is not None
        target = artifact_file(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target), full_page=True, timeout=10_000)


BROWSER_TOOLS = [
    {
        "type": "function",
        "name": "read_visible_page",
        "description": "Read the bounded visible text of the current preview page.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "inspect_row",
        "description": (
            "Find visible exact-text anchors and return bounded row/container text and badge readings. "
            "Use the frozen row text exactly."
        ),
        "parameters": {
            "type": "object",
            "properties": {"anchorText": {"type": "string", "minLength": 1, "maxLength": 256}},
            "required": ["anchorText"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]
