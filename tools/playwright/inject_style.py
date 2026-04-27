from playwright.sync_api import sync_playwright, Browser, BrowserContext

STYLE = """
/* Green theme injected by inject_style.py */
html, body, .application-main, #repo-content-pjax-container { background: #e6ffea !important; color: #022a00 !important; }
a, a:hover { color: #006600 !important; }
header, .Header, .pagehead { background: #006622 !important; color: #fff !important; }
pre, code, .blob-wrapper, .markdown-body { background: #e8f8ee !important; color: #022a00 !important; border-color: #b7f0c9 !important; }
.btn, button { background: #007a2e !important; color: #fff !important; border-color: #00581a !important; }
table, input, textarea, select { background: #f0fff5 !important; color: #022a00 !important; }
"""


def get_style() -> str:
    """Return the CSS payload for injection."""
    return STYLE


def _load_cookies(context: BrowserContext, cookies_file: str):
    import json
    with open(cookies_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Expect a list of cookie dicts compatible with Playwright add_cookies
    if not isinstance(data, list):
        raise ValueError('cookies file must contain a JSON list of cookie objects')
    context.add_cookies(data)


def inject(url: str, headless: bool = False, wait_ms: int = 30000, user_data_dir: str | None = None, cookies_file: str | None = None) -> None:
    """Open the page at `url` and inject the green theme CSS.

    Supports reusing an existing browser profile via `user_data_dir`, or loading
    cookies from a JSON file via `cookies_file` for authenticated pages.

    Args:
        url: target page URL
        headless: whether to launch browser headless
        wait_ms: milliseconds to keep the page open after injection
        user_data_dir: path to Playwright persistent profile directory (optional)
        cookies_file: path to a JSON file with cookies (optional)
    """
    with sync_playwright() as p:
        if user_data_dir:
            # Launch a persistent context that reuses the profile
            context = p.chromium.launch_persistent_context(user_data_dir=user_data_dir, headless=headless)
            page = context.new_page()
            page.goto(url, wait_until="networkidle")
            page.add_style_tag(content=STYLE)
            page.wait_for_timeout(wait_ms)
            context.close()
            return

        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        if cookies_file:
            _load_cookies(context, cookies_file)
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        page.add_style_tag(content=STYLE)
        page.wait_for_timeout(wait_ms)
        browser.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Inject green theme CSS into a page using Playwright')
    parser.add_argument('--url', required=True, help='Target page URL')
    parser.add_argument('--headless', action='store_true', help='Run browser headless')
    parser.add_argument('--wait', type=int, default=30000, help='Milliseconds to keep the page open after injection')
    parser.add_argument('--user-data-dir', dest='user_data_dir', help='Path to Playwright user data dir to reuse an authenticated profile')
    parser.add_argument('--cookies-file', dest='cookies_file', help='Path to JSON file with cookies to load into a fresh context')
    args = parser.parse_args()

    inject(args.url, headless=args.headless, wait_ms=args.wait, user_data_dir=args.user_data_dir, cookies_file=args.cookies_file)
