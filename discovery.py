import base64
import re
import time
import httpx
import db
import fetcher as fetch
import gemini as gem

_ARLISTA_PATTERN = re.compile(r"arlista|price|pricelist", re.IGNORECASE)
_PDF_PATTERN = re.compile(r"\.pdf$", re.IGNORECASE)
# Exclude non-HU language CDN paths (NL, DE, FR, etc.)
_EXCLUDE_PATTERN = re.compile(r"/[A-Z]{2}-pricelists/|/[a-z]{2}-[A-Z]{2}/|Prijslijst|Preisliste|Tarif", re.IGNORECASE)
_OPENDATALOADER_URL = None
_OPENDATALOADER_SECRET = None


def _init_opendataloader():
    global _OPENDATALOADER_URL, _OPENDATALOADER_SECRET
    import os
    _OPENDATALOADER_URL = os.environ.get("OPENDATALOADER_URL", "")
    _OPENDATALOADER_SECRET = os.environ.get("OPENDATALOADER_SECRET", "")


def _convert_pdf_to_markdown(pdf_bytes: bytes) -> str | None:
    if not _OPENDATALOADER_URL:
        return None
    try:
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        r = httpx.post(
            f"{_OPENDATALOADER_URL}/convert",
            headers={"Authorization": f"Bearer {_OPENDATALOADER_SECRET}", "Content-Type": "application/json"},
            json={"pdf_base64": pdf_b64},
            timeout=60,
        )
        if not r.is_success:
            print(f"opendataloader {r.status_code} — falling back to native PDF")
            return None
        return r.json().get("markdown")
    except Exception as e:
        print(f"opendataloader unreachable: {e}")
        return None


def _fetch_pdf_bytes(url: str) -> bytes | None:
    data, error = fetch.fetch_bytes(url)
    if error:
        print(f"PDF download failed {url}: {error}")
    return data


def _find_pdf_links_on_page(page) -> list[dict]:
    """Extract (model_name, pdf_url) pairs from a scrapled page."""
    results = []
    for a in page.css("a"):
        href = a.attrib.get("href", "")
        if not href:
            continue
        if _PDF_PATTERN.search(href) and _ARLISTA_PATTERN.search(href) and not _EXCLUDE_PATTERN.search(href):
            text = a.text or ""
            if href.startswith("/"):
                # Relative URL — we can't resolve without base, keep as-is for now
                pass
            results.append({"model_name": text.strip(), "pdf_url": href})
    return results


def _resolve_url(base_url: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def discover_brand_models(brand: dict) -> list[dict]:
    """
    Fetch the brand's discovery_url and extract (model_name, pdf_url) pairs.
    Returns list of dicts: {model_name, pdf_url}
    """
    _init_opendataloader()
    discovery_url = brand.get("discovery_url", "")
    if not discovery_url:
        print(f"No discovery_url for brand {brand['name']}, skipping")
        return []

    print(f"Discovering models for {brand['name']} at {discovery_url}")
    results = []

    try:
        from scrapling.fetchers import StealthyFetcher, Fetcher

        # Try StealthyFetcher first (handles JS-rendered pages + anti-bot)
        try:
            page = StealthyFetcher.fetch(discovery_url, headless=True, network_idle=True)
        except Exception as e:
            print(f"StealthyFetcher failed for {brand['name']}: {e}, falling back to Fetcher")
            page = Fetcher.get(discovery_url)

        # Look for direct arlista PDF links on listing page
        found = _find_pdf_links_on_page(page)
        if found:
            results = [{"model_name": m["model_name"], "pdf_url": _resolve_url(discovery_url, m["pdf_url"])} for m in found]
            print(f"  Direct PDF links found: {len(results)}")
            return results

        # No direct PDF links — follow each model subpage link
        model_links = []
        for a in page.css("a"):
            href = a.attrib.get("href", "")
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            text = (a.text or "").strip()
            # Heuristic: model links typically don't contain 'news', 'blog', 'contact', etc.
            if len(text) > 1 and len(text) < 60 and not re.search(r"kapcsolat|hírek|blog|news|akció|ajánlat|szerviz|alkatrész|financing|insurance", text, re.IGNORECASE):
                model_links.append((text, _resolve_url(discovery_url, href)))

        # Deduplicate
        seen = set()
        unique_links = []
        for text, url in model_links:
            if url not in seen:
                seen.add(url)
                unique_links.append((text, url))

        for model_name, model_url in unique_links[:30]:  # cap at 30 model pages
            try:
                sub = Fetcher.get(model_url)
                sub_found = _find_pdf_links_on_page(sub)
                for m in sub_found:
                    resolved = _resolve_url(model_url, m["pdf_url"])
                    if resolved not in seen:
                        seen.add(resolved)
                        results.append({"model_name": model_name, "pdf_url": resolved})
                time.sleep(0.5)
            except Exception as e:
                print(f"  Subpage error {model_url}: {e}")
                continue

        print(f"  Models discovered via subpages: {len(results)}")

    except Exception as e:
        print(f"Discovery error for {brand['name']}: {e}")

    return results


def find_new_models(brand_name: str, scraped: list[dict], db_models: list[dict]) -> list[dict]:
    """Return subset of scraped that are genuinely new according to Gemini."""
    scraped_names = [m["model_name"] for m in scraped if m["model_name"]]
    db_names = [m["name"] for m in db_models]
    new_names = gem.call_gemini_model_diff(scraped_names, db_names)
    new_set = set(new_names)
    return [m for m in scraped if m["model_name"] in new_set]


def process_new_model(brand: dict, model_name: str, pdf_url: str) -> dict:
    """
    Full pipeline for a newly discovered model:
    1. Download PDF
    2. opendataloader → Markdown (or native PDF fallback)
    3. Gemini → JSON
    4. Write to DB
    Returns summary dict.
    """
    _init_opendataloader()
    brand_name = brand["name"]
    brand_id = brand["id"]

    pdf_bytes = _fetch_pdf_bytes(pdf_url)
    if not pdf_bytes:
        return {"model_name": model_name, "ok": False, "error": "PDF download failed"}

    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    # Try opendataloader → text path first
    markdown = _convert_pdf_to_markdown(pdf_bytes)
    if markdown:
        raw = gem.call_gemini_text(markdown, brand_name, model_name)
    else:
        raw = gem.call_gemini_pdf_native(pdf_b64, brand_name, model_name)

    try:
        parsed = gem.parse_json_response(raw)
    except Exception as e:
        return {"model_name": model_name, "ok": False, "error": f"JSON parse: {e}"}

    if not parsed.get("models"):
        return {"model_name": model_name, "ok": False, "error": "No models in response"}

    # Register URL
    db.upsert_model_price_url(brand_name, model_name, pdf_url, "arlista")

    # Write model + specs via price_check logic
    from price_check import apply_parsed_data
    url_record = db.get_model_price_urls(brand_name)
    url_entry = next((u for u in url_record if u["url"] == pdf_url), {"id": "new", "url": pdf_url})

    result = apply_parsed_data(parsed, brand_name, brand_id, pdf_url, url_entry)
    return {"model_name": model_name, "ok": True, **result}
