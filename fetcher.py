"""
Shared HTTP fetcher with automatic browser fallback on HTTP 403.
Used by price_check.py and discovery.py — single place to maintain
the CDN hotlink protection bypass.
"""
import base64
import json
import urllib.parse

import httpx

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "hu-HU,hu;q=0.9",
}


def fetch_bytes(url: str, timeout: int = 120) -> tuple[bytes | None, str | None]:
    """
    Download URL as raw bytes.
    On HTTP 403 automatically retries with a Playwright browser session.

    Returns (bytes, None) on success, (None, error_message) on failure.
    """
    try:
        r = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=timeout)
        if r.status_code == 403:
            print(f"HTTP 403 — retrying with browser: {url}")
            data = _fetch_with_browser(url)
            if data is None:
                return None, "HTTP 403 (browser fallback also failed)"
            return data, None
        if r.is_success:
            return r.content, None
        return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)


def _fetch_with_browser(url: str) -> bytes | None:
    """
    Fetch a CDN-protected URL using a real browser session.
    Navigates to the brand homepage first to establish session cookies,
    then fetches the target URL via JS fetch() with credentials included.
    """
    parsed = urllib.parse.urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = context.new_page()
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass  # homepage failure is non-fatal — cookies may still be set

            b64_chunks = page.evaluate(f"""
                async () => {{
                    const r = await fetch({json.dumps(url)}, {{credentials: 'include'}});
                    if (!r.ok) return null;
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    const chunks = [];
                    for (let i = 0; i < bytes.length; i += 8192) {{
                        chunks.push(btoa(String.fromCharCode(...bytes.subarray(i, i + 8192))));
                    }}
                    return chunks;
                }}
            """)
            browser.close()

            if not b64_chunks:
                return None
            return base64.b64decode("".join(b64_chunks))
    except Exception as e:
        print(f"Browser fetch failed for {url}: {e}")
        return None
