Playwright injector

Files created/updated:
- tools/playwright/inject_style.py  # Playwright-Python script exposing get_style() and CLI (supports user-data-dir and cookies)
- tools/playwright/run_inject.sh    # Convenience wrapper for ad-hoc runs
- tools/playwright/run_obscura_task.sh # Wrapper suitable for Obscura task integration (uses env vars)
- tools/playwright/requirements.txt # Minimal requirements
- tests/test_playwright_inject.py   # Basic unit test for the CSS payload

How to use:
1. Install dependencies: python -m pip install -r tools/playwright/requirements.txt
2. Install playwright browsers: python -m playwright install
3. For a quick headed run:
   ./tools/playwright/run_inject.sh "https://github.com/openai/codex/tree/main/sdk/python"
4. To run from Obscura or CI and reuse an authenticated profile or cookies:
   export OBSCURA_PLAYWRIGHT_URL="https://github.com/openai/codex/tree/main/sdk/python"
   export OBSCURA_PLAYWRIGHT_PROFILE="/path/to/playwright/profile"   # optional
   export OBSCURA_PLAYWRIGHT_COOKIES="/path/to/cookies.json"        # optional
   export OBSCURA_PLAYWRIGHT_HEADLESS=1                              # optional
   ./tools/playwright/run_obscura_task.sh

Cookies file format
- A JSON array of cookie objects compatible with Playwright's add_cookies API. Example:
  [
    {"name": "user_session", "value": "abc", "domain": ".github.com", "path": "/", "httpOnly": true, "secure": true}
  ]

Notes:
- Persistent profile: use Playwright's user-data-dir created by a prior browser run. That preserves login state.
- If both user-data-dir and cookies-file are provided, user-data-dir takes precedence.
