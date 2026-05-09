from browser_use import BrowserSession

from e2e_agents.config.settings import Settings


def create_browser_session(settings: Settings) -> BrowserSession:
    return BrowserSession(
        headless=settings.headless,
        viewport={"width": settings.viewport_width, "height": settings.viewport_height},
        wait_for_network_idle_page_load_time=3.0,
        minimum_wait_page_load_time=1.0,
        disable_security=True,
    )
