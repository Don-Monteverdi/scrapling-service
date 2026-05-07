import base64
import hashlib
import os
import time
from datetime import datetime, timezone

import httpx

import db
import gemini as gem

OPENDATALOADER_URL = os.environ.get("OPENDATALOADER_URL", "")
OPENDATALOADER_SECRET = os.environ.get("OPENDATALOADER_SECRET", "")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _convert_pdf_to_markdown(pdf_bytes: bytes) -> str | None:
    if not OPENDATALOADER_URL:
        return None
    try:
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        r = httpx.post(
            f"{OPENDATALOADER_URL}/convert",
            headers={"Authorization": f"Bearer {OPENDATALOADER_SECRET}", "Content-Type": "application/json"},
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


def _extract_price_content(html: str) -> str:
    import re
    clean = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    clean = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<nav[^>]*>[\s\S]*?</nav>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<footer[^>]*>[\s\S]*?</footer>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    price_pats = re.findall(r"[\d\s.,]+(?:Ft|HUF|EUR|€|forint)", clean, re.IGNORECASE) or []
    all_nums = re.findall(r"\d[\d\s.,]{2,}", clean) or []
    return "|".join(price_pats + all_nums).lower()


def _normalized_engine_prices(model_data: dict) -> list[dict]:
    raw = model_data.get("engine_prices", [])
    trim_levels = model_data.get("trim_levels", [])
    trim_names = [t if isinstance(t, str) else t.get("name", "") for t in trim_levels]
    result = []
    for ep in raw:
        if ep.get("trim") and ep.get("engine_name"):
            result.append(ep)
        else:
            e_name = ep.get("engine_name") or ep.get("name") or "Ismeretlen"
            e_id = ep.get("engine_id") or re.sub(r"[^a-z0-9]+", "-", e_name.lower())
            base_price = ep.get("list_price") or ep.get("price") or 0
            targets = trim_names if trim_names else ["Alap"]
            for tn in targets:
                result.append({**ep, "trim": tn, "engine_id": e_id, "engine_name": e_name, "list_price": base_price})
    return result


import re as _re


def apply_parsed_data(parsed: dict, brand_name: str, brand_id: str | None, price_pdf_url: str | None, url_record: dict) -> dict:
    """
    Write parsed Gemini output to DB:
    - Updates admin_models + admin_pricing_configs + admin_colors
    - Updates admin_model_specs with per-variant prices
    - Logs changes to price_change_log
    - Upserts admin_promotions
    Returns summary dict.
    """
    now = datetime.now(timezone.utc).isoformat()
    details = []
    models_updated = 0
    changes_logged = 0
    specs_upserted = 0
    promos_written = 0

    for model_data in parsed.get("models", []):
        model_name: str = model_data.get("name", "")
        if not model_name:
            continue
        try:
            # ── admin_models ──
            existing_model = None
            if brand_id:
                existing_model = db.get_admin_model_by_name(brand_id, model_name)

            if existing_model:
                raw_price = model_data.get("base_price") or existing_model.get("base_price")
                new_price = int(raw_price) if raw_price is not None else 0
                update_payload: dict = {"base_price": new_price}
                if model_data.get("description"):
                    update_payload["description"] = model_data["description"]
                if model_data.get("category"):
                    update_payload["category"] = model_data["category"]
                if model_data.get("engine_options"):
                    update_payload["engine_options"] = model_data["engine_options"]
                if price_pdf_url:
                    update_payload["price_pdf_url"] = price_pdf_url
                db.update_admin_model(existing_model["id"], update_payload)
                model_id = existing_model["id"]
                if existing_model.get("base_price") != new_price:
                    details.append(f"📊 {model_name}: {existing_model.get('base_price'):,} → {new_price:,} Ft")
                else:
                    details.append(f"✅ {model_name}: adatok frissítve")
            else:
                if brand_id:
                    new_model = db.upsert_admin_model(
                        brand_id, model_name,
                        int(model_data.get("base_price") or 0),
                        model_data.get("description", ""),
                        model_data.get("category", "személygépjármű"),
                        model_data.get("engine_options", []),
                        price_pdf_url or "",
                    )
                    model_id = new_model.get("id", "")
                else:
                    model_id = ""
                details.append(f"🆕 {model_name}: új modell ({model_data.get('base_price', 0):,} Ft)")
            models_updated += 1

            # ── admin_pricing_configs ──
            if model_id:
                trim_levels = model_data.get("trim_levels", [])
                trim_names = [t if isinstance(t, str) else t.get("name", "") for t in trim_levels]
                engine_prices = _normalized_engine_prices(model_data)
                options = model_data.get("options", [])
                if engine_prices or trim_names or options:
                    db.upsert_pricing_config(model_id, trim_names, engine_prices, options)

            # ── admin_colors ──
            if model_id:
                for color in model_data.get("colors", []):
                    db.upsert_color(model_id, color.get("name", ""), {
                        "hex_code": color.get("hex_code", "#000000"),
                        "price_modifier": color.get("price_modifier", 0),
                        "is_two_tone": color.get("is_two_tone", False),
                        "roof_hex": color.get("roof_hex"),
                    })

            # ── admin_model_specs + price_change_log ──
            trim_levels = model_data.get("trim_levels", [])
            trim_summary = " / ".join(t if isinstance(t, str) else t.get("name", "") for t in trim_levels)

            for ep in _normalized_engine_prices(model_data):
                trim: str = ep.get("trim") or "Alap"
                engine_name: str = ep.get("engine_name") or "Ismeretlen"
                variant_name = f"{model_name} {trim} {engine_name}".strip()
                new_price = ep.get("list_price") or ep.get("price")

                existing_spec = db.get_model_spec(brand_name, model_name, variant_name)
                spec_payload: dict = {
                    "brand_name": brand_name,
                    "model_name": model_name,
                    "variant_name": variant_name,
                    "trim_levels": trim_summary,
                    "source_url": url_record.get("url", ""),
                    "source_name": f"PDF árlista – {brand_name}",
                    "raw_data": ep,
                    "updated_at": now,
                }
                if new_price is not None:
                    spec_payload["base_price_huf"] = new_price
                for field, col in [("fuel", "fuel"), ("powertrain", "powertrain"), ("power_hp", "power_hp"), ("power_kw", "power_kw"), ("transmission", "transmission"), ("consumption_wltp", "consumption_wltp"), ("co2_g_km", "co2_g_km")]:
                    if ep.get(field) is not None:
                        spec_payload[col] = ep[field]
                if ep.get("ev_range_km") is not None:
                    spec_payload["ev_range_wltp_km"] = str(ep["ev_range_km"])

                if existing_spec:
                    db.update_model_spec(existing_spec["id"], spec_payload)
                    old_price = existing_spec.get("base_price_huf")
                    if new_price is not None and old_price != new_price:
                        db.log_price_change({
                            "brand_name": brand_name, "model_name": model_name, "variant_name": variant_name,
                            "change_type": "price_updated", "field_name": "base_price_huf",
                            "old_value": str(old_price or ""), "new_value": str(new_price),
                            "source_url_id": url_record.get("id"),
                        })
                        details.append(f"💰 {variant_name}: {old_price:,} → {new_price:,} Ft")
                        changes_logged += 1
                else:
                    db.upsert_model_spec(spec_payload)
                    db.log_price_change({
                        "brand_name": brand_name, "model_name": model_name, "variant_name": variant_name,
                        "change_type": "variant_added", "field_name": "base_price_huf",
                        "old_value": None, "new_value": str(new_price) if new_price else None,
                        "source_url_id": url_record.get("id"),
                    })
                    details.append(f"🆕 {variant_name}: új variáns" + (f" – {new_price:,} Ft" if new_price else ""))
                    changes_logged += 1
                specs_upserted += 1

        except Exception as e:
            print(f"Error processing model {model_name}: {e}")
            details.append(f"❌ {model_name}: hiba – {e}")

    # ── admin_promotions ──
    for promo in parsed.get("promotions", []):
        if not promo.get("name"):
            continue
        try:
            db.upsert_promotion({
                "brand_id": brand_id,
                "name": promo["name"],
                "model_name": promo.get("model_name", ""),
                "discount_type": promo.get("discount_type", "amount"),
                "discount_value": promo.get("discount_value", 0),
                "valid_from": promo.get("valid_from"),
                "valid_until": promo.get("valid_until"),
                "customer_type": promo.get("customer_type", "mindketto"),
                "description": promo.get("description", ""),
                "is_active": True,
                "source": "pdf",
                "modell_id": promo.get("modell_id", ""),
            })
            promos_written += 1
        except Exception as e:
            print(f"Promotion upsert error {promo.get('name')}: {e}")

    if promos_written:
        details.append(f"🏷️ {promos_written} akció szinkronizálva")

    return {
        "models_updated": models_updated,
        "specs_upserted": specs_upserted,
        "changes_logged": changes_logged,
        "promotions_written": promos_written,
        "details": details,
    }


def run_price_check(brand_name: str | None = None) -> dict:
    """
    Phase 2: iterate all active model_price_urls, check for changes, update DB.
    Optionally filtered to a single brand.
    """
    urls = db.get_all_active_urls()
    if brand_name:
        urls = [u for u in urls if u.get("brand_name", "").lower() == brand_name.lower()]
    print(f"Price check: {len(urls)} URLs")

    brands_r = httpx.get(
        f"{os.environ['SUPABASE_URL']}/rest/v1/admin_brands?select=id,name",
        headers={"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"], "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}"},
        timeout=15,
    )
    brand_id_map = {b["name"].lower(): b["id"] for b in brands_r.json()}

    checked = 0
    changes = 0
    errors = []
    details_all = []
    now = datetime.now(timezone.utc).isoformat()

    for url_record in urls:
        try:
            print(f"Checking: {url_record['brand_name']} / {url_record['model_label']}")
            is_pdf = url_record["url"].lower().endswith(".pdf")

            r = httpx.get(
                url_record["url"],
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "hu-HU,hu;q=0.9"},
                follow_redirects=True,
                timeout=60,
            )
            if not r.is_success:
                errors.append({"model_label": url_record["model_label"], "error": f"HTTP {r.status_code}"})
                db.update_url_hash(url_record["id"], None, now, last_error=f"HTTP {r.status_code}")
                continue

            if is_pdf:
                content_bytes = r.content
                new_hash = _sha256(content_bytes)
            else:
                html = r.text
                new_hash = _sha256(_extract_price_content(html).encode())

            old_hash = url_record.get("content_hash")
            is_first = not old_hash
            has_changed = bool(old_hash) and old_hash != new_hash

            if not is_first and not has_changed:
                db.update_url_hash(url_record["id"], None, now)
                checked += 1
                continue

            # ── Technical spec PDFs — just update spec_pdf_url ──
            if url_record.get("doc_type") == "muszaki":
                # Update spec_pdf_url on matching admin_models
                db.update_url_hash(url_record["id"], new_hash, now, last_changed=now)
                checked += 1
                continue

            # ── AI parse ──
            if is_pdf:
                markdown = _convert_pdf_to_markdown(content_bytes)
                if markdown:
                    raw_ai = gem.call_gemini_text(markdown, url_record["brand_name"], url_record["model_label"])
                else:
                    pdf_b64 = base64.b64encode(content_bytes).decode()
                    raw_ai = gem.call_gemini_pdf_native(pdf_b64, url_record["brand_name"], url_record["model_label"])
            else:
                raw_ai = gem.call_gemini_text(html, url_record["brand_name"], url_record["model_label"])

            try:
                parsed = gem.parse_json_response(raw_ai)
            except Exception as e:
                msg = f"JSON parse: {str(e)[:200]}"
                errors.append({"model_label": url_record["model_label"], "error": msg})
                db.update_url_hash(url_record["id"], None, now, last_error=msg)
                continue

            if not parsed.get("models"):
                msg = "AI: no models in response"
                errors.append({"model_label": url_record["model_label"], "error": msg})
                db.update_url_hash(url_record["id"], None, now, last_error=msg)
                continue

            brand_id = brand_id_map.get(url_record["brand_name"].lower())
            result = apply_parsed_data(parsed, url_record["brand_name"], brand_id, url_record["url"] if is_pdf else None, url_record)

            details_all.extend(result["details"])
            changes += 1
            db.update_url_hash(url_record["id"], new_hash, now, last_changed=now, last_error=None)

            print(f"Applied {url_record['brand_name']}/{url_record['model_label']}: {result['models_updated']} models, {result['specs_upserted']} specs, {result['changes_logged']} changes")
            checked += 1

            # Rate limit between Gemini calls
            time.sleep(3)

        except Exception as e:
            msg = str(e)
            print(f"Error {url_record['model_label']}: {msg}")
            errors.append({"model_label": url_record["model_label"], "error": msg})
            if "429" in msg:
                break  # quota exhausted

    return {"checked": checked, "changes": changes, "errors": len(errors), "error_details": errors, "details": details_all}
