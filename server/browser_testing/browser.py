"""Origin-confined Playwright inspection and interaction tools."""

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


def agent_browser_tools(target_ids: list[str]) -> list[dict[str, Any]]:
    """Return the shared bounded tool set for any frozen browser scenario."""

    target = {"type": "string", "enum": target_ids}
    path = {
        "type": "string",
        "pattern": "^/[^#]*$",
        "maxLength": 2048,
    }
    return [
        {
            "type": "function",
            "name": "navigate_browser",
            "description": "Navigate one configured preview target to an origin-relative path and report the response status and visible text.",
            "parameters": {
                "type": "object",
                "properties": {"target": target, "path": path},
                "required": ["target", "path"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "read_visible_text",
            "description": "Read bounded visible text from the target's current browser page.",
            "parameters": {
                "type": "object",
                "properties": {"target": target},
                "required": ["target"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "read_http_status",
            "description": "Navigate a fresh page to an origin-relative path and record its actual HTTP response status.",
            "parameters": {
                "type": "object",
                "properties": {"target": target, "path": path},
                "required": ["target", "path"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "inspect_row",
            "description": "Inspect a unique visible row by exact anchor text and record its badge text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target,
                    "anchorText": {"type": "string", "minLength": 1, "maxLength": 256},
                },
                "required": ["target", "anchorText"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "click_visible_text",
            "description": "Click one unique visible element whose text exactly matches the supplied text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target,
                    "text": {"type": "string", "minLength": 1, "maxLength": 256},
                },
                "required": ["target", "text"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "fill_by_label",
            "description": "Fill one visible form control selected by its accessible label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target,
                    "label": {"type": "string", "minLength": 1, "maxLength": 256},
                    "value": {"type": "string", "maxLength": 2048},
                },
                "required": ["target", "label", "value"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "take_screenshot",
            "description": "Save a full-page screenshot of the target's current browser page.",
            "parameters": {
                "type": "object",
                "properties": {"target": target},
                "required": ["target"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


@dataclass
class _TargetPage:
    origin: str
    context: BrowserContext
    page: Page


class BrowserWorkspace:
    """One Playwright browser with an isolated, origin-confined context per target."""

    def __init__(self, target_urls: dict[str, str], timeout_ms: int = 10_000):
        if not target_urls:
            raise ValueError("at least one browser target is required")
        self.target_urls = dict(target_urls)
        self.timeout_ms = timeout_ms
        self.candidates: dict[str, RowCandidate] = {}
        self.evidence: list[dict[str, Any]] = []
        self.screenshots: list[str] = []
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._targets: dict[str, _TargetPage] = {}

    @property
    def origins(self) -> dict[str, str]:
        return {target_id: target.origin for target_id, target in self._targets.items()}

    def __enter__(self) -> "BrowserWorkspace":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        for target_id, url in self.target_urls.items():
            origin = _origin(url)
            context = self._browser.new_context(viewport={"width": 1440, "height": 1000})
            context.set_default_timeout(self.timeout_ms)

            def make_confiner(allowed_origin: str):
                def confine(route) -> None:
                    request_url = route.request.url
                    if request_url.startswith(("data:", "blob:")) or _origin(request_url) == allowed_origin:
                        route.continue_()
                    else:
                        route.abort("blockedbyclient")

                return confine

            context.route("**/*", make_confiner(origin))
            self._targets[target_id] = _TargetPage(origin, context, context.new_page())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for target in self._targets.values():
            target.context.close()
        try:
            self._browser and self._browser.close()
        finally:
            self._playwright and self._playwright.stop()

    def _target(self, target_id: Any) -> _TargetPage:
        if not isinstance(target_id, str) or target_id not in self._targets:
            raise ValueError("target must name a configured preview target")
        return self._targets[target_id]

    @staticmethod
    def _path(path: Any) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "#" in path
            or len(path) > 2048
        ):
            raise ValueError("path must be an origin-relative URL path without a fragment")
        return path

    def _navigate(self, target: _TargetPage, path: str, *, persistent: bool) -> dict[str, Any]:
        page = target.page if persistent else target.context.new_page()
        try:
            response = page.goto(
                f"{target.origin}{path}",
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            if response is None:
                return {"path": path, "status": None, "error": "navigation returned no response"}
            if _origin(response.url) != target.origin:
                return {"path": path, "status": None, "error": "navigation left the configured origin"}
            text = page.locator("body").inner_text(timeout=self.timeout_ms)
            return {
                "path": path,
                "url": page.url,
                "status": response.status,
                "visibleText": text[:12_000],
                "truncated": len(text) > 12_000,
                "error": None,
            }
        except PlaywrightTimeoutError as exc:
            return {"path": path, "status": None, "error": f"navigation timed out: {exc}"}
        finally:
            if not persistent:
                page.close()

    def _visible_text(self, target: _TargetPage) -> dict[str, Any]:
        if target.page.url == "about:blank":
            return {"url": target.page.url, "visibleText": "", "error": "target has not been navigated"}
        if _origin(target.page.url) != target.origin:
            raise RuntimeError("browser left the configured origin")
        text = target.page.locator("body").inner_text(timeout=self.timeout_ms)
        return {"url": target.page.url, "visibleText": text[:12_000], "truncated": len(text) > 12_000, "error": None}

    def _inspect_row(self, target_id: str, target: _TargetPage, anchor_text: str) -> dict[str, Any]:
        if target.page.url == "about:blank":
            return {"anchorText": anchor_text, "exactMatchCount": 0, "candidates": [], "error": "target has not been navigated"}
        locator = target.page.get_by_text(anchor_text.strip(), exact=True)
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
            seed = f"{target_id}\0{anchor_text}\0{index}\0{container_text}\0{badge_texts}"
            candidate_id = "row-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            candidate = RowCandidate(
                candidate_id, anchor_text.strip(), total, str(data.get("tag", ""))[:32], container_text, badge_texts
            )
            self.candidates[candidate_id] = candidate
            found.append(candidate.public())
        return {"anchorText": anchor_text.strip(), "exactMatchCount": total, "candidates": found, "tooMany": False}

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        sequence = len(self.evidence) + 1
        target_id = arguments.get("target")
        target = self._target(target_id)
        if name == "navigate_browser":
            result = self._navigate(target, self._path(arguments.get("path")), persistent=True)
        elif name == "read_visible_text":
            result = self._visible_text(target)
        elif name == "read_http_status":
            result = self._navigate(target, self._path(arguments.get("path")), persistent=False)
        elif name == "inspect_row":
            anchor = arguments.get("anchorText")
            if not isinstance(anchor, str) or not anchor.strip() or len(anchor) > 256:
                raise ValueError("anchorText must be non-empty text up to 256 characters")
            result = self._inspect_row(target_id, target, anchor)
        elif name == "click_visible_text":
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 256:
                raise ValueError("text must be non-empty text up to 256 characters")
            locator = target.page.get_by_text(text.strip(), exact=True)
            count = locator.count()
            if count != 1 or not locator.first.is_visible():
                result = {"clicked": False, "exactMatchCount": count, "error": "visible text was missing or ambiguous"}
            else:
                locator.click(timeout=self.timeout_ms)
                result = {"clicked": True, "exactMatchCount": 1, "url": target.page.url, "error": None}
        elif name == "fill_by_label":
            label, value = arguments.get("label"), arguments.get("value")
            if not isinstance(label, str) or not label.strip() or not isinstance(value, str):
                raise ValueError("label and value are required")
            locator = target.page.get_by_label(label.strip(), exact=True)
            count = locator.count()
            if count != 1 or not locator.first.is_visible():
                result = {"filled": False, "exactMatchCount": count, "error": "label was missing or ambiguous"}
            else:
                locator.fill(value, timeout=self.timeout_ms)
                result = {"filled": True, "exactMatchCount": 1, "url": target.page.url, "error": None}
        elif name == "take_screenshot":
            if target.page.url == "about:blank":
                result = {"screenshot": None, "error": "target has not been navigated"}
            else:
                filename = f"browser-evidence-tool-{sequence}.png"
                destination = artifact_file(filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                target.page.screenshot(path=str(destination), full_page=True, timeout=10_000)
                self.screenshots.append(filename)
                result = {"screenshot": filename, "url": target.page.url, "error": None}
        else:
            raise ValueError(f"unsupported browser tool: {name}")

        recorded_arguments = dict(arguments)
        if name == "fill_by_label" and "value" in recorded_arguments:
            recorded_arguments["value"] = "[redacted]"
        self.evidence.append(
            {
                "sequence": sequence,
                "tool": name,
                "target": target_id,
                "arguments": recorded_arguments,
                "result": result,
            }
        )
        return result
