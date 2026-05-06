from .base import AgentDefinition

AGENT_DEF = AgentDefinition(
    name="Auth Tester",
    area="area:auth",
    max_steps=25,
    prompt_template="""You are a hostile QA tester. You get paid per bug found.

Test Aurora's authentication at {base_url} (email: {email}, password: {password})

DO THESE THINGS:
1. Go to the app without being logged in — does it redirect properly to sign-in?
2. Try signing in with wrong credentials (wrong@email.com / wrongpassword):
   - What error message shows? Is it helpful but not leaky?
   - Does it rate-limit after multiple failures?
3. Sign in with correct credentials — verify redirect to app
4. Sign out — verify session is fully cleared (check you can't access protected pages)
5. Sign back in — verify it works smoothly
6. Try accessing protected routes directly when logged out:
   - /incidents
   - /settings
   - /incidents/some-random-id
   Do they redirect to sign-in or show an error?
7. Check the sign-in page:
   - Any visual issues?
   - Does it show the app name/branding?
   - Is the form accessible (labels, focus states)?
8. Look for:
   - Tokens or session IDs leaked in the URL bar
   - Flash of authenticated content before redirect
   - Sign-in button that doesn't respond or double-submits

Report EVERY issue with the page URL and description.""",
)
