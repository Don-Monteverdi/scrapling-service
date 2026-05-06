import os
import httpx
from datetime import datetime, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _rest(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}"


def get_brands() -> list[dict]:
    r = httpx.get(_rest("admin_brands?select=id,name,discovery_url"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_model_price_urls(brand_name: str | None = None) -> list[dict]:
    url = "model_price_urls?select=*&is_active=eq.true&doc_type=eq.arlista"
    if brand_name:
        url += f"&brand_name=eq.{httpx.URL(brand_name)}"
    r = httpx.get(_rest(url), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_all_active_urls() -> list[dict]:
    r = httpx.get(_rest("model_price_urls?select=*&is_active=eq.true"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_admin_models(brand_id: str) -> list[dict]:
    r = httpx.get(_rest(f"admin_models?select=id,name,base_price&brand_id=eq.{brand_id}"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_admin_model_by_name(brand_id: str, name: str) -> dict | None:
    encoded = httpx.URL(name)
    r = httpx.get(_rest(f"admin_models?select=id,name,base_price&brand_id=eq.{brand_id}&name=ilike.{encoded}"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def get_model_spec(brand_name: str, model_name: str, variant_name: str) -> dict | None:
    r = httpx.get(
        _rest(f"admin_model_specs?select=id,base_price_huf&brand_name=eq.{httpx.URL(brand_name)}&model_name=eq.{httpx.URL(model_name)}&variant_name=eq.{httpx.URL(variant_name)}&limit=1"),
        headers=_HEADERS, timeout=15
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def upsert_model_price_url(brand_name: str, model_label: str, url: str, doc_type: str = "arlista") -> dict:
    payload = {"brand_name": brand_name, "model_label": model_label, "url": url, "doc_type": doc_type, "is_active": True}
    r = httpx.post(_rest("model_price_urls"), headers={**_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"}, json=payload, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else payload


def update_url_hash(url_id: str, content_hash: str | None, last_checked: str, last_changed: str | None = None, last_error: str | None = None) -> None:
    payload: dict = {"last_checked_at": last_checked, "last_error": last_error}
    if content_hash:
        payload["content_hash"] = content_hash
    if last_changed:
        payload["last_changed_at"] = last_changed
    r = httpx.patch(_rest(f"model_price_urls?id=eq.{url_id}"), headers=_HEADERS, json=payload, timeout=15)
    r.raise_for_status()


def upsert_admin_model(brand_id: str, name: str, base_price: int, description: str = "", category: str = "személygépjármű", engine_options: list | None = None, price_pdf_url: str = "") -> dict:
    payload = {
        "brand_id": brand_id,
        "name": name,
        "base_price": base_price,
        "description": description,
        "category": category,
        "engine_options": engine_options or [],
        "price_pdf_url": price_pdf_url,
    }
    r = httpx.post(_rest("admin_models"), headers=_HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else payload


def update_admin_model(model_id: str, payload: dict) -> None:
    r = httpx.patch(_rest(f"admin_models?id=eq.{model_id}"), headers=_HEADERS, json=payload, timeout=15)
    r.raise_for_status()


def upsert_model_spec(payload: dict) -> None:
    r = httpx.post(_rest("admin_model_specs"), headers={**_HEADERS, "Prefer": "return=minimal,resolution=merge-duplicates"}, json=payload, timeout=15)
    r.raise_for_status()


def update_model_spec(spec_id: str, payload: dict) -> None:
    r = httpx.patch(_rest(f"admin_model_specs?id=eq.{spec_id}"), headers=_HEADERS, json=payload, timeout=15)
    r.raise_for_status()


def log_price_change(payload: dict) -> None:
    r = httpx.post(_rest("price_change_log"), headers={**_HEADERS, "Prefer": "return=minimal"}, json=payload, timeout=15)
    r.raise_for_status()


def upsert_promotion(payload: dict) -> None:
    r = httpx.post(_rest("admin_promotions"), headers={**_HEADERS, "Prefer": "return=minimal,resolution=merge-duplicates"}, json=payload, timeout=15)
    r.raise_for_status()


def upsert_pricing_config(model_id: str, trim_levels: list, engine_prices: list, options: list) -> None:
    payload = {
        "model_id": model_id,
        "trim_levels": trim_levels,
        "engine_prices": engine_prices,
        "options": options,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    r = httpx.post(_rest("admin_pricing_configs"), headers={**_HEADERS, "Prefer": "return=minimal,resolution=merge-duplicates"}, json=payload, timeout=15)
    r.raise_for_status()


def get_color(model_id: str, name: str) -> dict | None:
    r = httpx.get(_rest(f"admin_colors?select=id&model_id=eq.{model_id}&name=eq.{httpx.URL(name)}&limit=1"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def upsert_color(model_id: str, name: str, payload: dict) -> None:
    existing = get_color(model_id, name)
    if existing:
        r = httpx.patch(_rest(f"admin_colors?id=eq.{existing['id']}"), headers=_HEADERS, json=payload, timeout=15)
    else:
        r = httpx.post(_rest("admin_colors"), headers={**_HEADERS, "Prefer": "return=minimal"}, json={"model_id": model_id, "name": name, **payload}, timeout=15)
    r.raise_for_status()


def update_scraping_config_run(job_name: str, status: str, result: dict | None = None) -> None:
    payload: dict = {
        "last_run_status": status,
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if result is not None:
        payload["last_run_result"] = result
    r = httpx.patch(_rest(f"scraping_config?job_name=eq.{job_name}"), headers=_HEADERS, json=payload, timeout=15)
    r.raise_for_status()


def get_scraping_config() -> dict | None:
    r = httpx.get(_rest("scraping_config?job_name=eq.full_pipeline&limit=1"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def get_all_scraping_configs() -> list[dict]:
    r = httpx.get(_rest("scraping_config?select=*&order=job_name"), headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()
