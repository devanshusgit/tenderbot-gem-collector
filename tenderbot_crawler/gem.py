"""Anonymous GeM discovery through the observed public bid form.

Playwright is imported only when opening a portal. No credentials, persistent
browser profile, CAPTCHA solver, proxy rotation or protection bypass is used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import math
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit


ORIGIN = "https://bidplus.gem.gov.in"
LISTING_URL = ORIGIN + "/all-bids"
PAGE_SIZE = 10
MAX_METADATA_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_PDF_BYTES = 25 * 1024 * 1024


class PortalError(RuntimeError):
    """Public source unavailable or an unexpected source response was received."""


class PortalChallengeError(PortalError):
    """Access needs human attention; the adapter stops instead of bypassing it."""


class NonPdfDocumentError(PortalError):
    """An observed source link serves a page/resource rather than PDF bytes."""


class PortalHTTPError(PortalError):
    def __init__(self, status: int, message: str | None = None):
        self.status = status
        super().__init__(message or f"The public GeM source returned HTTP {status}.")


class _RetryableError(Exception):
    def __init__(self, status: int, delay: float):
        self.status, self.delay = status, delay


@dataclass(frozen=True)
class _Response:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else (None if isinstance(value, list) else value)


def _text(value: Any) -> str:
    value = _first(value)
    return str(value) if value is not None else ""


def _number(value: Any) -> int | float | None:
    value = _first(value)
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def gem_date(value: Any) -> str | None:
    """Correct the public index's Z-tagged IST wall clock, verified against PDFs."""
    value = _first(value)
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
        return None
    result = value[:-1] + "+05:30"
    try:
        datetime.fromisoformat(result)
    except ValueError:
        return None
    return result


def safe_gem_url(url: str, base: str = ORIGIN) -> str:
    """Validate original/redirect document URLs before any network request."""
    if not isinstance(url, str) or not url or "\\" in url or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise PortalError("Invalid public GeM URL.")
    absolute = urljoin(base, url)
    try:
        parsed = urlsplit(absolute)
        host, port = (parsed.hostname or "").lower(), parsed.port
    except ValueError as exc:
        raise PortalError("Invalid public GeM URL.") from exc
    if parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None or port not in (None, 443):
        raise PortalError("Only HTTPS public GeM URLs without credentials are permitted.")
    if host != "gem.gov.in" and not host.endswith(".gem.gov.in"):
        raise PortalError("The source URL leaves the official GeM domain.")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _bid_id(record: dict[str, Any]) -> str:
    value = _first(record.get("b_id")) or record.get("gem_bid_id") or record.get("gem_id") or record.get("id", "")
    value = str(value).removeprefix("gem-")
    if not re.fullmatch(r"\d{1,14}", value):
        raise PortalError("A valid public GeM bid identifier is required.")
    return value


def normalize_gem(record: dict[str, Any], fetched_at: str | None = None) -> dict[str, Any]:
    """Normalize observed public fields; missing money/location stays unknown."""
    bid_id = _bid_id(record)
    kind, evaluation = _number(record.get("b_bid_type")), _number(record.get("b_eval_type"))
    path = "showbidDocument"
    if kind == 5:
        path = "showdirectradocumentPdf"
    elif kind == 2:
        path = "list-ra-schedules" if evaluation is not None and evaluation > 0 else "showradocumentPdf"
    document_url = f"{ORIGIN}/{path}/{bid_id}"
    title = _text(record.get("b_category_name"))
    ministry = _text(record.get("ba_official_details_minName"))
    department = _text(record.get("ba_official_details_deptName"))
    product_type = _number(record.get("b_type"))
    return {
        "id": f"gem-{bid_id}", "gem_id": bid_id, "gem_bid_id": bid_id,
        "tender_id": _text(record.get("b_bid_number")), "title": title,
        "description": _text(record.get("bd_category_name")) or title,
        "organisation": department or ministry or "Not disclosed in public listing",
        "ministry": ministry, "department": department, "portal_name": "GeM",
        "state": [], "city": [], "product_category": title,
        "quantity": _number(record.get("b_total_quantity")),
        "tender_value": None, "emd_fee": None,
        "published_at": gem_date(record.get("final_start_date_sort")),
        "closing_at": gem_date(record.get("final_end_date_sort")),
        "status": "cancelled" if _number(record.get("b_status")) in (3, 5) else "active",
        "bid_type": "Reverse auction" if kind in (2, 5) else "Bid",
        "source_url": document_url, "document_url": document_url,
        "source_document_url": document_url, "listing_url": LISTING_URL,
        "documents": [{"name": "Original GeM RA schedules" if path == "list-ra-schedules" else "Original GeM bid document", "url": document_url}],
        "tags": ["Services" if product_type == 1 else "Goods"] if product_type in (0, 1) else [],
        "source_fetched_at": fetched_at or _now(), "demo": False,
        "detail_notice": "Public GeM listing. Amounts, locations, eligibility and technical specifications require the original documents and any corrigenda.",
        "source_data": {key: value for key, value in record.items() if key != "b.b_created_by"},
    }


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    @property
    def text(self):
        return " ".join(" ".join(self.parts).split())


def _challenge_reason(body: bytes, content_type: str, url: str) -> str | None:
    if urlsplit(url).hostname == "sso.gem.gov.in" or re.search(r"/(?:login|signin|oauth)(?:/|$)", urlsplit(url).path, re.I):
        return "GeM redirected the public request to sign-in."
    if "html" not in content_type.lower() and not body.lstrip().startswith(b"<"):
        return None
    text = body[:MAX_METADATA_BYTES].decode("utf-8", errors="replace")
    if re.search(r"cf-chl-|challenge-platform|g-recaptcha|hcaptcha|cf-turnstile|verify (?:that )?you are human|checking your browser", text, re.I):
        return "The public GeM source presented a CAPTCHA or browser challenge."
    parsed = _HTML()
    parsed.feed(text)
    if re.search(r"\bcaptcha\b", parsed.text, re.I):
        return "The public GeM source presented a CAPTCHA."
    if re.search(r"access denied|request (?:was )?blocked|you (?:have been|are) blocked|permission denied|requested URL was rejected", parsed.text, re.I):
        return "The public GeM source restricted this request."
    if re.search(r"<input\b[^>]*\btype\s*=\s*['\"]?password\b", text, re.I):
        return "The public GeM source requires sign-in."
    return None


def _csrf(html: str) -> str | None:
    match = re.search(r"['\"]csrf_bd_gem_nk['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
    if not match:
        match = re.search(r"id=['\"]chash['\"][^>]*value=['\"]([^'\"]+)['\"]", html)
    if not match:
        match = re.search(r"name=['\"]csrf_bd_gem_nk['\"][^>]*value=['\"]([^'\"]+)['\"]", html)
    return match.group(1) if match else None


class GemPortal:
    """One explicitly owned, anonymous Playwright browser/context per crawl."""

    def __init__(self, headless=True, browser_channel="msedge", delay=1.0, timeout_ms=45000):
        if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
            raise ValueError("delay must be a non-negative finite number")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms must be a positive integer")
        self.headless, self.browser_channel = headless, browser_channel
        self.delay, self.timeout_ms = float(delay), timeout_ms
        self._playwright = self._browser = self._context = self._page = None
        self._csrf_token: str | None = None
        self._session_at = 0.0
        self._last_request = 0.0
        self._session_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._sleep = asyncio.sleep
        self._blocked_navigation = False

    async def __aenter__(self):
        if self._context is not None:
            raise PortalError("This GeM portal is already open.")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PortalError("Playwright is required for crawling. Install the project's Python requirements and a supported Playwright browser.") from exc
        try:
            self._playwright = await async_playwright().start()
            options = {"headless": bool(self.headless)}
            if self.browser_channel and self.browser_channel != "chromium":
                options["channel"] = self.browser_channel
            self._browser = await self._playwright.chromium.launch(**options)
            self._context = await self._browser.new_context(locale="en-US", timezone_id="Asia/Kolkata", service_workers="block", ignore_https_errors=False)
            await self._context.route("**/*", self._guard_navigation)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
        except Exception as exc:
            await self.close()
            raise PortalError("Could not open the selected Playwright browser. Check browser installation and browser_channel.") from exc
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()
        return False

    async def close(self):
        failures = []
        for name, method in (("_context", "close"), ("_browser", "close"), ("_playwright", "stop")):
            resource = getattr(self, name)
            setattr(self, name, None)
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception as exc:
                    failures.append(type(exc).__name__)
        self._page, self._csrf_token, self._session_at = None, None, 0.0
        if failures:
            raise PortalError("Browser cleanup was incomplete: " + ", ".join(failures))

    async def _guard_navigation(self, route):
        if route.request.is_navigation_request():
            try:
                safe_gem_url(route.request.url)
            except PortalError:
                self._blocked_navigation = True
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    def _require_open(self):
        if self._context is None or self._page is None:
            raise PortalError("Use GemPortal inside an async with block before crawling.")

    async def _pace(self, backoff=0.0):
        wait = max(self.delay - (time.monotonic() - self._last_request), backoff, 0.0)
        if wait:
            await self._sleep(wait)
        self._last_request = time.monotonic()

    def _retry_delay(self, headers: dict[str, str], attempt: int, status: int) -> float:
        delay = max(self.delay, float(2 ** attempt))
        retry_after = headers.get("retry-after", "")
        if retry_after:
            try:
                duration = float(retry_after)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    duration = (when - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    duration = 0.0
            if math.isfinite(duration):
                delay = max(delay, duration)
        if delay > 60:
            raise PortalHTTPError(status, "GeM asked for a longer wait. Stop this crawl and retry later.")
        return delay

    async def _ensure_session(self):
        self._require_open()
        async with self._session_lock:
            if self._csrf_token and time.monotonic() - self._session_at < 600:
                return
            async with self._request_lock:
                backoff = 0.0
                for attempt in range(3):
                    await self._pace(backoff)
                    self._blocked_navigation = False
                    try:
                        response = await self._page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
                        if response is None:
                            raise PortalError("GeM returned no public listing response.")
                        safe_gem_url(self._page.url)
                        status = response.status
                        headers = {key.lower(): val for key, val in response.headers.items()}
                        if status in (401, 403, 407):
                            raise PortalChallengeError(f"The public GeM source restricted access (HTTP {status}).")
                        html = await self._page.content()
                        reason = _challenge_reason(html.encode("utf-8"), "text/html", self._page.url)
                        if reason:
                            raise PortalChallengeError(reason)
                        if status in (429, 500, 502, 503, 504):
                            raise _RetryableError(status, self._retry_delay(headers, attempt, status))
                        if status < 200 or status >= 300:
                            raise PortalHTTPError(status)
                        token = _csrf(html)
                        if not token:
                            raise PortalError("GeM did not provide its anonymous public bid form. The source format may have changed or access may require human attention.")
                        self._csrf_token, self._session_at = token, time.monotonic()
                        return
                    except _RetryableError as exc:
                        if attempt == 2:
                            raise PortalHTTPError(exc.status, "GeM remains temporarily unavailable after three attempts.") from exc
                        backoff = exc.delay
                    except PortalError:
                        raise
                    except Exception as exc:
                        if self._blocked_navigation:
                            raise PortalChallengeError("The public listing tried to leave the official GeM domain.") from exc
                        if attempt == 2:
                            raise PortalError("GeM public listing navigation failed after three attempts.") from exc
                        backoff = float(2 ** attempt)

    async def _request(self, method: str, url: str, *, form=None, max_bytes=MAX_METADATA_BYTES) -> _Response:
        self._require_open()
        original_url = safe_gem_url(url)
        async with self._request_lock:
            backoff = 0.0
            for attempt in range(3):
                current, current_method, current_form = original_url, method.upper(), form
                try:
                    for redirect in range(6):
                        await self._pace(backoff)
                        backoff = 0.0
                        options = {"method": current_method, "timeout": self.timeout_ms, "max_redirects": 0,
                                   "max_retries": 0, "fail_on_status_code": False,
                                   "headers": {"Referer": LISTING_URL, "Accept": "application/json, text/html, application/pdf;q=0.9"}}
                        if current_form is not None:
                            options["form"] = current_form
                            options["headers"]["X-Requested-With"] = "XMLHttpRequest"
                        response = await self._context.request.fetch(current, **options)
                        try:
                            final_url = safe_gem_url(response.url)
                            status = response.status
                            headers = {key.lower(): val for key, val in response.headers.items()}
                            if status in (401, 403, 407):
                                raise PortalChallengeError(f"The public GeM source restricted access (HTTP {status}).")
                            if status in (301, 302, 303, 307, 308):
                                location = headers.get("location")
                                if not location or redirect == 5:
                                    raise PortalError("Invalid or excessive redirects from the public GeM source.")
                                destination = safe_gem_url(location, final_url)
                                if current_form is not None and status in (307, 308) and urlsplit(destination).netloc != urlsplit(current).netloc:
                                    raise PortalError("GeM attempted to redirect public form data to a different host.")
                                if status == 303 or (status in (301, 302) and current_method == "POST"):
                                    current_method, current_form = "GET", None
                                current = destination
                                continue
                            length = headers.get("content-length", "")
                            if length.isdigit() and int(length) > max_bytes:
                                raise PortalError("GeM response exceeds the configured byte limit.")
                            body = await response.body()
                            if len(body) > max_bytes:
                                raise PortalError("GeM response exceeds the configured byte limit.")
                            reason = _challenge_reason(body, headers.get("content-type", ""), final_url)
                            if reason:
                                raise PortalChallengeError(reason)
                            if status in (429, 500, 502, 503, 504):
                                raise _RetryableError(status, self._retry_delay(headers, attempt, status))
                            if status < 200 or status >= 300:
                                raise PortalHTTPError(status)
                            return _Response(status, final_url, headers, body)
                        finally:
                            await response.dispose()
                except _RetryableError as exc:
                    if attempt == 2:
                        raise PortalHTTPError(exc.status, "GeM remains temporarily unavailable after three attempts.") from exc
                    backoff = exc.delay
                except PortalError:
                    raise
                except Exception as exc:
                    if attempt == 2:
                        raise PortalError("GeM request failed after three attempts.") from exc
                    backoff = float(2 ** attempt)
        raise PortalError("GeM request did not complete.")

    async def _post(self, path: str, values=None) -> _Response:
        await self._ensure_session()
        return await self._request("POST", ORIGIN + path, form={**(values or {}), "csrf_bd_gem_nk": self._csrf_token})

    @staticmethod
    def _json(response: _Response) -> dict[str, Any]:
        try:
            result = json.loads(response.body.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise PortalError("GeM returned an unexpected non-JSON response.") from exc
        if not isinstance(result, dict):
            raise PortalError("GeM returned an unexpected response shape.")
        return result

    async def search(self, search="", page=1, sort="Bid-End-Date-Oldest") -> dict[str, Any]:
        if not isinstance(search, str) or len(search.strip()) > 200:
            raise ValueError("search must be text with at most 200 characters")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100000:
            raise ValueError("page must be a positive integer")
        if sort not in ("Bid-End-Date-Oldest", "Bid-Start-Date-Latest"):
            raise ValueError("Unsupported public GeM sort")
        payload = {"page": page, "param": {"searchBid": search.strip(), "searchType": "fullText"},
                   "filter": {"bidStatusType": "ongoing_bids", "byType": "all", "highBidValue": "", "byEndDate": {"from": "", "to": ""}, "sort": sort}}
        result = self._json(await self._post("/all-bids-data", {"payload": json.dumps(payload, separators=(",", ":"))}))
        data = result.get("response", {})
        data = data.get("response", {}) if isinstance(data, dict) else {}
        total = data.get("numFound") if isinstance(data, dict) else None
        docs = data.get("docs") if isinstance(data, dict) else None
        if result.get("code") != 200 or isinstance(total, bool) or not isinstance(total, int) or total < 0 or not isinstance(docs, list) or any(not isinstance(doc, dict) for doc in docs):
            raise PortalError("GeM returned an unexpected public bid listing.")
        fetched_at = _now()
        return {"tenders": [normalize_gem(doc, fetched_at) for doc in docs], "total": total, "page": page,
                "pageSize": PAGE_SIZE, "source": "GeM", "provider": "gem-public-playwright", "source_url": LISTING_URL,
                "fetched_at": fetched_at, "mode": "live", "has_more": page * PAGE_SIZE < total}

    async def details(self, record: dict[str, Any]) -> dict[str, Any]:
        bid_id = _bid_id(record)
        result = self._json(await self._post(f"/public-bid-other-details/{bid_id}"))
        flags = result.get("response")
        if result.get("code") != 200 or not isinstance(flags, dict):
            raise PortalError("GeM public bid metadata is unavailable.")
        other = {key: flags.get(key) if isinstance(flags.get(key), bool) else None for key in ("corrigendum", "representation")}
        documents = []
        for doc in record.get("documents", []):
            if isinstance(doc, dict) and isinstance(doc.get("url"), str):
                safe_gem_url(doc["url"])
                documents.append(dict(doc))
        corrections, correction_text = [], ""
        if other["corrigendum"] is True:
            response = await self._post(f"/bidding/bid/viewCorrigendum/{bid_id}")
            parsed = _HTML()
            parsed.feed(response.body.decode("utf-8", errors="replace"))
            correction_text = parsed.text
            for href in parsed.links:
                try:
                    url = safe_gem_url(href, response.url)
                except PortalError:
                    continue
                if not any(doc["url"] == url for doc in corrections):
                    corrections.append({"name": "GeM corrigendum document", "url": url})
        seen = {doc["url"] for doc in documents}
        documents.extend(doc for doc in corrections if doc["url"] not in seen)
        return {**record, "documents": documents, "other_details": other, "corrigenda": corrections,
                "corrigendum_text": correction_text,
                "representation_url": f"{ORIGIN}/publish-representations/{bid_id}" if other["representation"] is True else None,
                "other_details_fetched_at": _now(),
                "detail_notice": "Public GeM metadata and original document links. Listing dates retain their original fetch time. Download the PDFs and check corrigenda for complete current terms."}

    async def download(self, url: str, max_bytes=DEFAULT_MAX_PDF_BYTES) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        url = safe_gem_url(url)
        await self._ensure_session()
        response = await self._request("GET", url, max_bytes=max_bytes)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("application/pdf", "application/octet-stream", "binary/octet-stream"):
            raise NonPdfDocumentError("The source did not return a PDF document.")
        if not response.body[:1024].lstrip().startswith(b"%PDF-"):
            raise NonPdfDocumentError("The source response is not a valid PDF byte stream.")
        return response.body
