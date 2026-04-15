"""Headless-browser PDF downloader via Playwright.

Fallback for sites that require JavaScript execution to pass Cloudflare
Turnstile / similar anti-bot challenges (ChemRxiv, bioRxiv, medRxiv).

Playwright is a **soft dependency**: imported lazily the first time a
caller needs it. If not installed we print a clear install recipe and
return None, so the main install stays light (~170MB extra for Chromium
when the user opts in).

Install recipe:
    uv run --with playwright python -m playwright install chromium
    # or, globally:
    pip install playwright
    playwright install chromium

At runtime the tool handles both cases by launching whichever browser
binary Playwright finds; users who can't spare disk will still be able
to use all non-CF-protected features.
"""

from __future__ import annotations

import sys


_BROWSER_INSTALL_HINT = """\
Playwright + a browser binary are needed for Cloudflare-protected sites.
Install once:
    pip install playwright
    playwright install chromium

If running via uv:
    uv run --with playwright python -m playwright install chromium
"""


def _import_playwright_sync():
    """Return the sync_api module, or ``None`` if Playwright isn't installed."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print(
            "\nPlaywright is not installed, so Cloudflare-protected full-text "
            "downloads can't be attempted.\n" + _BROWSER_INSTALL_HINT,
            file=sys.stderr,
        )
        return None
    return sync_playwright


def browser_download_via_click(
    landing_url: str,
    *,
    link_selector: str = 'a[href*=".pdf"]',
    timeout_ms: int = 60_000,
    user_agent: str | None = None,
) -> bytes | None:
    """Open a landing page in Chromium, click a PDF download link,
    and return the downloaded bytes.

    This is the "act like a real user" path — visiting the HTML article
    page sets up Cloudflare cookies, then clicking a same-origin download
    link stays inside the cleared zone (publisher asset gateways often
    block direct hits but allow clicks from the article page).
    """
    sync_playwright = _import_playwright_sync()
    if sync_playwright is None:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=user_agent
                or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                accept_downloads=True,
            )
            page = context.new_page()
            try:
                print(f"  click-path: loading {landing_url[:80]}...",
                      file=sys.stderr)
                page.goto(landing_url, wait_until="domcontentloaded",
                          timeout=timeout_ms)
                # Let Cloudflare/JS finish.
                page.wait_for_timeout(4000)
                title = (page.title() or "")
                if "moment" in title.lower() or "challenge" in title.lower():
                    try:
                        page.wait_for_function(
                            "!document.title.toLowerCase().includes('moment')",
                            timeout=15_000,
                        )
                    except Exception:
                        pass
                print(f"  click-path: landed on {page.title()[:60]!r}",
                      file=sys.stderr)

                # Find any PDF download link. Publishers vary: try the generic
                # selector, then a few common specific ones.
                candidates = [
                    link_selector,
                    'a[download][href*="asset"]',
                    'a[href*="/original/"]',
                    'button:has-text("Download")',
                ]
                download = None
                for sel in candidates:
                    try:
                        with page.expect_download(timeout=timeout_ms) as dl_info:
                            page.click(sel, timeout=5000)
                        download = dl_info.value
                        print(f"  click-path: download started via {sel!r}",
                              file=sys.stderr)
                        break
                    except Exception:
                        continue
                if download is None:
                    print("  click-path: no PDF download link matched",
                          file=sys.stderr)
                    browser.close()
                    return None

                path = download.path()
                if path:
                    data = path.read_bytes() if hasattr(path, "read_bytes") \
                        else open(path, "rb").read()
                    if data[:4] == b"%PDF":
                        browser.close()
                        return data
            except Exception as e:
                print(f"  click-path failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
            browser.close()
    except Exception as e:
        print(f"Playwright session failed: {type(e).__name__}: {e}", file=sys.stderr)
    return None


def browser_download_pdf(
    url: str,
    *,
    warmup_url: str | None = None,
    timeout_ms: int = 60_000,
    user_agent: str | None = None,
) -> bytes | None:
    """Launch a headless Chromium and return PDF bytes from ``url``.

    Strategy:
    1. (Optional) visit ``warmup_url`` first so Cloudflare sets a
       ``cf_clearance`` cookie on the shared context. Many Cloudflare
       sites serve the JS challenge only on HTML pages; hitting the
       landing page first "warms up" the cookie jar so the direct PDF
       URL below works.
    2. Try ``context.request.get(url)`` — simpler and faster than a full
       page navigation when the target is actually a PDF.
    3. If that didn't yield a ``%PDF``-magic response, fall back to
       ``page.goto(url)`` with a response handler watching for any PDF
       content-type.

    Returns ``None`` on every failure (missing Playwright, launch error,
    challenge not passed, no PDF captured).
    """
    sync_playwright = _import_playwright_sync()
    if sync_playwright is None:
        return None

    captured: list[bytes] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=user_agent
                or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

            if warmup_url:
                try:
                    warm_page = context.new_page()
                    print(f"  warmup: loading {warmup_url[:80]}...", file=sys.stderr)
                    resp = warm_page.goto(
                        warmup_url, wait_until="domcontentloaded", timeout=timeout_ms,
                    )
                    status = resp.status if resp else "?"
                    title = warm_page.title() or "?"
                    print(f"  warmup: status={status} title={title[:60]!r}",
                          file=sys.stderr)
                    # Cloudflare's challenge page auto-solves in ~5-10s; give it time.
                    # If the initial title is 'Just a moment...' we wait longer.
                    if "moment" in title.lower() or "challenge" in title.lower():
                        print("  warmup: detected CF challenge, waiting up to 15s...",
                              file=sys.stderr)
                        try:
                            warm_page.wait_for_function(
                                "document.title && !document.title.toLowerCase().includes('moment')",
                                timeout=15_000,
                            )
                            print(f"  warmup: challenge passed, title now={warm_page.title()[:60]!r}",
                                  file=sys.stderr)
                        except Exception:
                            print("  warmup: challenge didn't clear in 15s",
                                  file=sys.stderr)
                    else:
                        warm_page.wait_for_timeout(2000)
                    warm_page.close()
                except Exception as e:
                    print(f"  warmup navigation failed: {type(e).__name__}: {e}",
                          file=sys.stderr)

            # Path A: use the context's APIRequestContext to fetch the PDF
            # with the cookies already gathered during warmup.
            try:
                resp = context.request.get(url, timeout=timeout_ms)
                body = resp.body()
                print(f"  request.get status={resp.status} "
                      f"body_prefix={body[:8]!r} len={len(body)}",
                      file=sys.stderr)
                if body and body[:4] == b"%PDF":
                    captured.append(body)
            except Exception as e:
                print(f"  request.get failed: {type(e).__name__}: {e}",
                      file=sys.stderr)

            # Path B: full page navigation with a response handler as fallback.
            # Use wait_until='load' not 'networkidle' — networkidle hangs
            # indefinitely on PDF URLs (Chromium's PDF viewer keeps a socket open).
            if not captured:
                page = context.new_page()

                def _on_response(resp):
                    try:
                        ct = (resp.headers or {}).get("content-type", "").lower()
                        u = resp.url or ""
                        if "pdf" in ct or u.lower().split("?")[0].endswith(".pdf"):
                            body = resp.body()
                            if body and body[:4] == b"%PDF":
                                captured.append(body)
                    except Exception:
                        pass

                page.on("response", _on_response)
                try:
                    page.goto(url, wait_until="load", timeout=timeout_ms)
                except Exception as e:
                    if not captured:
                        print(f"  page.goto failed: {type(e).__name__}: {e}",
                              file=sys.stderr)
                page.close()

            browser.close()
    except Exception as e:
        print(f"Playwright session failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    if not captured:
        return None
    # Prefer the largest captured PDF — avoids tiny cover/preview PDFs
    # some publishers also serve from the page.
    return max(captured, key=len)


def is_playwright_available() -> bool:
    """Quick check without attempting to launch a browser."""
    try:
        import playwright.sync_api  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False
