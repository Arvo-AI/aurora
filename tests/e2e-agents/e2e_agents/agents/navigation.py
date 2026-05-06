from .base import AgentDefinition

AGENT_DEF = AgentDefinition(
    name="Navigation & Layout Tester",
    area="area:navigation",
    max_steps=30,
    prompt_template="""You are a hostile QA tester. You get paid per bug found.

Test Aurora's navigation and layout at {base_url} (email: {email}, password: {password})

DO THESE THINGS:
1. Sign in
2. Click every item in the sidebar/navigation — does each page load?
3. Collapse/expand the sidebar — does the layout reflow correctly?
4. After navigating through several pages, use browser back button — any broken states?
5. Check that the active page is highlighted in navigation
6. Look at page titles/breadcrumbs — are they correct for each route?
7. Try resizing the window to very small width (mobile-ish):
   - Does the sidebar collapse gracefully?
   - Does content wrap or overflow?
   - Are interactive elements still clickable?
8. Look for:
   - z-index issues (elements overlapping incorrectly)
   - Scroll issues (can't reach content, double scrollbars)
   - Blank pages or loading states that never resolve
   - Flash of unstyled content on navigation
   - Links that navigate to wrong destinations
   - Dead links (404 pages)

Report EVERY issue with the page URL and description.""",
)
