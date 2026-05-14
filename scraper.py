# -*- coding: utf-8 -*-
"""
Audio Bee – Data Extraction Intern Practical Test
Target: https://ekantipur.com

This script uses Playwright (sync API) to:
1) Open the homepage, go to the Entertainment section (मनोरञ्जन / मनो रञ्जन),
   and collect the top five listing cards.
2) Return to the homepage and extract the “Cartoon of the Day” (व्यंग्यचित्र) widget.

Output: output.json (UTF-8, ensure_ascii=False for Nepali text).

How to run (this file lives only inside `project/`):
  cd project
  python scraper.py

From the parent folder (no extra scraper.py at repo root):
  python project/scraper.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

BASE_URL = "https://ekantipur.com"
ENTERTAINMENT_URL = f"{BASE_URL.rstrip('/')}/entertainment"
OUTPUT_PATH = Path(__file__).resolve().parent / "output.json"

# Section label shown in navigation and on the channel page (stable business label).
CATEGORY_LABEL = "मनोरञ्जन"

# Playwright uses ms for timeouts; news sites can keep sockets open for analytics.
NAVIGATION_TIMEOUT_MS = 120_000

# Channel listing pages render each story as a `.category` row inside `.category-wrapper`.
# (Older third-party parsers used `article.normal`; the live 2026 template uses this layout.)
ENTERTAINMENT_CARD_SELECTOR = "div.category-main-wrapper div.category-wrapper > div.category"

# Third-party trackers often keep long-lived connections open, which makes `networkidle`
# hard to reach on news sites. Aborting a small denylist is a pragmatic automation pattern
# and does not affect the article DOM we scrape.
TRACKING_HOST_FRAGMENTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.com/tr",
    "clarity.ms",
    "cloudflareinsights.com",
)


def _install_lightweight_request_blocking(context) -> None:
    """Abort common analytics/beacon calls so `networkidle` can settle faster."""

    def _handle(route) -> None:
        try:
            url = route.request.url
        except PlaywrightError:
            route.continue_()
            return
        if any(part in url for part in TRACKING_HOST_FRAGMENTS):
            route.abort()
        else:
            route.continue_()

    context.route("**/*", _handle)


def _goto_networkidle(page: Page, url: str, *, label: str) -> None:
    """Navigate with `wait_until='networkidle'` (assessment requirement) plus one retry."""
    last_error: PlaywrightTimeoutError | None = None
    for attempt in range(2):
        try:
            print(f"[nav] {label}: {url} (networkidle, attempt {attempt + 1})")
            page.goto(url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            print(f"[nav] WARN: networkidle timeout on attempt {attempt + 1} for {label}.")
    assert last_error is not None
    raise last_error


def absolute_url(base: str, maybe_relative: str | None) -> str:
    """
    Build a full URL from a base page URL and a possibly-relative href/src.

    Examples:
        base='https://ekantipur.com/foo', maybe_relative='/bar' -> 'https://ekantipur.com/bar'
        base='https://ekantipur.com/foo', maybe_relative='https://x.test/a' -> unchanged absolute URL
    """
    if not maybe_relative:
        return ""
    joined = urljoin(base, maybe_relative.strip())
    parsed = urlparse(joined)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return joined
    return joined


def _clean_text(value: str | None) -> str:
    """Collapse whitespace for human-readable titles and author lines."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _author_or_null(raw: str | None) -> str | None:
    """Return None (JSON null) when the author field is missing or blank."""
    cleaned = _clean_text(raw)
    return cleaned if cleaned else None


def _reveal_cartoon_lazy_images(page: Page, cartoon_section, slide) -> None:
    """
    Cartoon thumbnails use lazy-loading (`img.lazy` + `data-src`) and often sit below the fold.
    Without scrolling + hydration, the visible browser may show only the grey skeleton even
    though `data-src` is already in the DOM (and our JSON extraction still works).

    This nudges the carousel into view and copies `data-src` → `src` for images inside the
    cartoon slider so a human watching `headless=False` can actually see the picture.
    """
    try:
        cartoon_section.scroll_into_view_if_needed(timeout=10_000)
        slide.scroll_into_view_if_needed(timeout=10_000)
    except PlaywrightError as exc:
        print(f"[task2] WARN: scroll_into_view: {exc}")

    time.sleep(0.4)

    try:
        cartoon_section.locator("div.cartoon-slider").first.evaluate(
            """(root) => {
                root.querySelectorAll("img[data-src]").forEach((img) => {
                    const ds = img.getAttribute("data-src");
                    if (ds) {
                        img.setAttribute("src", ds);
                    }
                    img.classList.remove("lazy");
                    img.classList.add("loaded");
                });
            }"""
        )
    except PlaywrightError as exc:
        print(f"[task2] WARN: lazy-image hydrate script: {exc}")

    # Some themes only swap `src` after a scroll/resize tick.
    try:
        page.evaluate("window.dispatchEvent(new Event('scroll'));")
    except PlaywrightError:
        pass

    time.sleep(0.35)


def _first_img_url(scope_locator, page_url: str) -> str:
    """
    Pick the first reasonable <img> inside a card/section.
    Many sites lazy-load with data-src / srcset; we try common attributes in order.
    """
    img = scope_locator.locator("img").first
    try:
        if not img.count():
            return ""
    except PlaywrightError:
        return ""

    for attr in ("src", "data-src", "data-original"):
        try:
            val = img.get_attribute(attr)
        except PlaywrightError:
            val = None
        if val and not val.startswith("data:"):
            return absolute_url(page_url, val.strip())
    return ""


def navigate_to_entertainment(page: Page) -> None:
    """
    Task 1 navigation: start at the homepage, then open Entertainment.

    We prefer clicking the real navigation link (good for demos / stable UX flow).
    If the menu layout changes, we fall back to the canonical /entertainment URL.
    """
    print(f"[nav] Loading homepage: {BASE_URL}")
    _goto_networkidle(page, BASE_URL, label="homepage")

    # Match both common spellings: मनोरञ्जन (often printed as one word) and मनो रञ्जन.
    entertainment_link = page.get_by_role(
        "link", name=re.compile(r"मनो\s*र?ञ्जन")
    ).first

    try:
        entertainment_link.wait_for(state="visible", timeout=15_000)
        print("[nav] Clicking Entertainment link from the main menu...")
        entertainment_link.click()
        page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
        return
    except PlaywrightTimeoutError:
        print("[nav] Timeout waiting for Entertainment link; trying direct URL...")
    except PlaywrightError as exc:
        print(f"[nav] Could not click Entertainment link ({exc}); trying direct URL...")

    print(f"[nav] Fallback goto: {ENTERTAINMENT_URL}")
    _goto_networkidle(page, ENTERTAINMENT_URL, label="entertainment direct")


def scrape_entertainment_top_five(page: Page) -> list[dict[str, Any]]:
    """
    Extract the first five entertainment cards from the listing page.

    Stable selectors (2026 ekantipur.com template):
    - `div.category-wrapper > div.category`: one row per story (title + blurb + image).
    - Headline lives in `.category-description h2 a` (primary human-visible title).
    - Author credit is usually `.author-name` (not always present on every template).
    - Hero image is under `.category-image img` (lazy-loaded via `data-src` on some rows).
    """
    print("[task1] Waiting for entertainment listing rows...")
    page.wait_for_selector(f"{ENTERTAINMENT_CARD_SELECTOR} .category-description h2 a", timeout=45_000)

    cards = page.locator(ENTERTAINMENT_CARD_SELECTOR)
    total = cards.count()
    print(f"[task1] Found {total} cards; taking top {min(5, total)}.")

    results: list[dict[str, Any]] = []
    for i in range(min(5, total)):
        card = cards.nth(i)
        try:
            link = card.locator(".category-description h2 a").first
            title = _clean_text(link.inner_text(timeout=5_000))
            href = link.get_attribute("href") or ""
            page_url = absolute_url(BASE_URL, href) or page.url

            if not title:
                # Rare fallback: any prominent heading inside the text column.
                try:
                    title = _clean_text(
                        card.locator(".category-description h2, .category-description h3")
                        .first.inner_text(timeout=2_000)
                    )
                except PlaywrightError:
                    title = ""

            author_el = card.locator("div.author-name").first
            author_text = None
            try:
                if author_el.count():
                    author_text = _author_or_null(author_el.inner_text(timeout=2_000))
            except PlaywrightError:
                author_text = None

            # Prefer the visual column used for thumbnails on channel pages.
            image_url = _first_img_url(card.locator(".category-image"), page_url)

            results.append(
                {
                    "title": title,
                    "image_url": image_url,
                    "category": CATEGORY_LABEL,
                    "author": author_text,
                }
            )
            print(f"[task1] OK row {i + 1}: {title[:60]!r}...")
        except PlaywrightError as exc:
            print(f"[task1] WARN: card {i + 1} skipped due to: {exc}")

    return results


def scrape_cartoon_of_the_day(page: Page) -> dict[str, Any]:
    """
    Task 2: Cartoon of the Day (Nepali editorial cartoon).

    On the current ekantipur.com homepage, this module is labeled “कार्टुन” and implemented
    as a Swiper carousel (`.cartoon-slider`). Some older/internal copy may still say
    “व्यंग्यचित्र”; we try the modern widget first, then fall back to text-based discovery.
    """
    print(f"[task2] Loading homepage for cartoon widget: {BASE_URL}")
    _goto_networkidle(page, BASE_URL, label="homepage (cartoon)")

    empty = {"title": "", "image_url": "", "author": None}

    # Primary: stable homepage widget (see `section.e-section` + `.cartoon-slider`).
    cartoon_section = page.locator("section.e-section").filter(has=page.locator("div.cartoon-slider")).first
    has_modern_widget = False
    try:
        cartoon_section.wait_for(state="visible", timeout=25_000)
        has_modern_widget = True
    except PlaywrightTimeoutError:
        print("[task2] Swiper cartoon block not visible in time; trying legacy 'vyangya' text fallback...")

    if has_modern_widget:
        slide = cartoon_section.locator("div.cartoon-slider .swiper-slide-active").first
        try:
            if not slide.count():
                slide = cartoon_section.locator("div.cartoon-slider .swiper-slide").first
        except PlaywrightError:
            slide = cartoon_section.locator("div.cartoon-slider .swiper-slide").first

        print("[task2] Scrolling cartoon into view and loading lazy images (for visible browser)...")
        _reveal_cartoon_lazy_images(page, cartoon_section, slide)

        title = ""
        image_url = ""
        try:
            img = slide.locator("img").first
            if img.count():
                title = _clean_text(img.get_attribute("alt") or "")
                image_url = (
                    absolute_url(BASE_URL, img.get_attribute("src") or "")
                    or absolute_url(BASE_URL, img.get_attribute("data-src") or "")
                )
            if not image_url:
                link = slide.locator("a.loading-img").first
                if link.count():
                    image_url = absolute_url(BASE_URL, link.get_attribute("href") or "")
        except PlaywrightError as exc:
            print(f"[task2] WARN: failed reading swiper slide: {exc}")

        author = None
        try:
            credit = cartoon_section.locator(".author-name, .cartoon-author, .section-news .author").first
            if credit.count():
                author = _author_or_null(credit.inner_text(timeout=2_000))
        except PlaywrightError:
            author = None

        print(f"[task2] Cartoon title: {title[:80]!r}")
        print(f"[task2] Cartoon image present: {bool(image_url)}")
        return {"title": title, "image_url": image_url, "author": author}

    # Fallback: legacy/alt copy that literally contains “व्यंग्यचित्र”.
    marker = page.get_by_text("व्यंग्यचित्र", exact=False).first
    try:
        marker.wait_for(state="visible", timeout=12_000)
    except PlaywrightTimeoutError:
        print("[task2] Cartoon widget not found (no swiper block, no व्यंग्यचित्र label).")
        return empty

    container = marker.locator("xpath=ancestor-or-self::*[.//img][1]")
    try:
        if not container.count():
            return empty
    except PlaywrightError:
        return empty

    payload = container.evaluate(
        """(root) => {
            const titleCandidates = [];
            for (const sel of ["h2", "h3", "h4", "img"]) {
                for (const el of root.querySelectorAll(sel)) {
                    const t = (el.innerText || el.getAttribute("alt") || "").trim();
                    if (t && t.length <= 200) titleCandidates.push(t);
                }
            }
            const imgEl = root.querySelector("img");
            let img = "";
            if (imgEl) {
                img = imgEl.getAttribute("src")
                    || imgEl.getAttribute("data-src")
                    || imgEl.getAttribute("data-original")
                    || "";
            }
            const authorEl = root.querySelector(".author, [class*='author']");
            const author = authorEl ? (authorEl.innerText || "").trim() : "";
            const title =
                titleCandidates.find((t) => !t.includes("व्यंग्यचित्र")) ||
                titleCandidates[0] ||
                "";
            return { title, img, author };
        }""",
    )

    title = _clean_text(str(payload.get("title", "")))
    image_rel = str(payload.get("img") or "").strip()
    image_url = absolute_url(BASE_URL, image_rel) if image_rel else ""
    author = _author_or_null(str(payload.get("author", "")))

    print(f"[task2] Cartoon title (fallback): {title[:80]!r}")
    print(f"[task2] Cartoon image present: {bool(image_url)}")
    return {"title": title, "image_url": image_url, "author": author}


def write_output(data: dict[str, Any], path: Path) -> None:
    """Persist JSON with UTF-8 Nepali support (ensure_ascii=False)."""
    print(f"[io] Writing {path}")
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Entry point: launch Chromium, scrape, always attempt to write JSON."""
    # Windows consoles often default to a legacy code page; reconfigure when possible so
    # Nepali debug lines do not crash the scraper mid-run.
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8")
            except OSError:
                pass

    result: dict[str, Any] = {
        "entertainment_news": [],
        "cartoon_of_the_day": {"title": "", "image_url": "", "author": None},
    }

    try:
        with sync_playwright() as p:
            print("[browser] Launching Chromium (headless=False)...")
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                locale="ne-NP",
                # A normal desktop UA reduces odd bot walls compared to the default Playwright string.
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            _install_lightweight_request_blocking(context)
            page = context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            try:
                navigate_to_entertainment(page)
                result["entertainment_news"] = scrape_entertainment_top_five(page)
                result["cartoon_of_the_day"] = scrape_cartoon_of_the_day(page)
                # Homepage is left open; give you a moment to visually confirm the cartoon module.
                print("[browser] Pausing 8 seconds on the homepage so you can verify the cartoon image...")
                time.sleep(8)
            finally:
                context.close()
                browser.close()
                print("[browser] Closed browser context.")

    except PlaywrightTimeoutError:
        print(
            "[fatal] A Playwright timeout occurred (often `networkidle` on ad-heavy pages "
            "or a missing selector). See the stack trace below."
        )
        traceback.print_exc()
    except PlaywrightError:
        print("[fatal] Playwright error while scraping.")
        traceback.print_exc()
    except Exception:  # noqa: BLE001 - top-level safety for assessment robustness
        print("[fatal] Unexpected error while scraping.")
        traceback.print_exc()

    write_output(result, OUTPUT_PATH)
    print("[done] Scraping finished (see output.json).")


if __name__ == "__main__":
    main()
