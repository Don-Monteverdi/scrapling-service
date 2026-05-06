import os
import json
import time
import httpx

GOOGLE_AI_API_KEY = os.environ["GOOGLE_AI_API_KEY"]
_MODEL = "gemini-2.5-flash"
_GENERATE_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent?key={GOOGLE_AI_API_KEY}"
_OPENAI_URL = f"https://generativelanguage.googleapis.com/v1beta/openai/chat/completions?key={GOOGLE_AI_API_KEY}"

AI_SYSTEM_PROMPT = """Te egy autóipari árlista PDF elemző vagy. A felhasználó egy gyártói árlistát ad meg PDF vagy HTML formátumban.

A feladatod: kinyerni a strukturált árlistát JSON formátumban.

Elvárt JSON formátum:
{
  "brand_name": "Citroën",
  "models": [
    {
      "name": "C3",
      "category": "személygépjármű",
      "description": "Kompakt városi autó · 30-as modellév",
      "base_price": 5990000,
      "engine_options": ["Turbo 100 LE manuális", "Elektromos 113 LE"],
      "trim_levels": [
        {
          "name": "YOU",
          "description": "Alapszintű felszereltség",
          "standard_equipment": ["LED nappali menetfény", "7' érintőképernyő", "Apple CarPlay / Android Auto"]
        }
      ],
      "engine_prices": [
        {
          "trim": "YOU",
          "engine_id": "turbo-100-man",
          "engine_name": "Turbo 100 LE manuális",
          "list_price": 5990000,
          "private_price": null,
          "fuel": "benzin",
          "powertrain": "ICE",
          "power_hp": 100,
          "power_kw": 74,
          "transmission": "manuális",
          "consumption_wltp": "5.8",
          "co2_g_km": "131",
          "ev_range_km": null
        }
      ],
      "options": [
        {
          "id": "c3-opt-1",
          "code": "WF25",
          "name": "Téli csomag",
          "note": "PLUS-on alapfelszereltség",
          "condition": null,
          "prices": { "YOU": 350000, "PLUS": 0, "MAX": null }
        }
      ],
      "colors": [{"name": "Fehér", "hex_code": "#f5f5f0", "price_modifier": 0, "is_two_tone": false, "roof_hex": null}]
    }
  ],
  "promotions": [
    {
      "name": "Tavaszi akció – C3",
      "model_name": "C3",
      "discount_type": "amount",
      "discount_value": 500000,
      "description": "500.000 Ft kedvezmény C3 PLUS felszereltségre",
      "valid_from": "2025-03-01",
      "valid_until": "2025-06-30",
      "customer_type": "maganszemely",
      "modell_id": ""
    }
  ]
}

SZABÁLYOK:
- Árakat Ft-ban, egész számként (pl. 5990000)
- engine_prices[].fuel: "benzin" | "dízel" | "hibrid" | "elektromos" | "plug-in hibrid"
- engine_prices[].powertrain: "ICE" | "MHEV" | "HEV" | "PHEV" | "BEV"
- engine_prices[].transmission: "manuális" | "automata"
- engine_prices[].consumption_wltp: WLTP kombinált fogyasztás l/100km vagy kWh/100km, szövegként (pl. "5.8")
- engine_prices[].co2_g_km: CO2 kibocsátás g/km szövegként, null ha BEV
- engine_prices[].ev_range_km: WLTP elektromos hatótáv km-ben, null ha nem BEV/PHEV
- Ha tech adat nincs a PDF-ben, maradjon null — NE találj ki adatot
- price_modifier: szín felár Ft-ban (0 = alapszín)
- hex_code: becsüld meg a szín neve alapján
- is_two_tone: true ha kétszínű (más tető)
- category: "személygépjármű" vagy "tehergépjármű" vagy "motor"
- options[].prices: 0 = alapfelszereltségben benne van, szám = felár Ft-ban, null = nem elérhető
- promotions[].customer_type: "maganszemely" | "ceg" | "mindketto" — ha nem derül ki, legyen "mindketto"
- promotions[].modell_id: ha szerepel a PDF-ben (pl. "PEU-208-STYLE-HY110"), add meg, különben ""
- CSAK a dokumentumban ténylegesen szereplő adatokat add meg!
- Ha valamit nem találsz, hagyj null-t vagy üres tömböt
- A válasz KIZÁRÓLAG valid JSON legyen!"""


def _retry_gemini(fn, retries: int = 3, delay: float = 5.0):
    for attempt in range(retries):
        resp = fn()
        if resp.status_code == 503 and attempt < retries - 1:
            print(f"Gemini 503, retry {attempt + 1}/{retries}")
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def call_gemini_text(content: str, brand_name: str = "", model_label: str = "") -> str:
    """Call Gemini with Markdown text content (from opendataloader output)."""
    user_msg = f"Ez a(z) {brand_name} \"{model_label}\" gyártói árlista tartalma. Elemezd és add meg a strukturált adatokat JSON-ban:\n\n{content}"
    resp = _retry_gemini(lambda: httpx.post(
        _OPENAI_URL,
        json={
            "model": _MODEL,
            "messages": [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 65536,
            "temperature": 0.1,
        },
        timeout=120,
    ))
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def call_gemini_pdf_native(pdf_base64: str, brand_name: str, model_label: str) -> str:
    """Call Gemini with raw PDF bytes (fallback when opendataloader is unavailable)."""
    resp = _retry_gemini(lambda: httpx.post(
        _GENERATE_URL,
        json={
            "contents": [{"role": "user", "parts": [
                {
                    "text": f"{AI_SYSTEM_PROMPT}\n\nEz a(z) {brand_name} \"{model_label}\" gyártói árlistája PDF formátumban. Elemezd és add meg a strukturált adatokat JSON-ban. Az összes modellt, felszereltségi szintet, motort, árat, színt, műszaki adatot és opciót kinyerd pontosan."
                },
                {"inline_data": {"mime_type": "application/pdf", "data": pdf_base64}},
            ]}],
            "generationConfig": {"maxOutputTokens": 65536, "temperature": 0.1},
        },
        timeout=300,
    ))
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_gemini_model_diff(scraped_models: list[str], db_models: list[str]) -> list[str]:
    """Return genuinely new model names from scraped_models not already in db_models."""
    if not scraped_models:
        return []
    prompt = (
        f"Given these models currently on the brand's website: {json.dumps(scraped_models, ensure_ascii=False)}\n"
        f"And these models already in our database: {json.dumps(db_models, ensure_ascii=False)}\n"
        "Which scraped models are genuinely new (not just alternate names or electric variants of existing models)?\n"
        "Return ONLY the new model names as a JSON array. Example: [\"ë-C3 Aircross Pro\", \"New Model X\"]\n"
        "If nothing is new, return []."
    )
    resp = httpx.post(
        _OPENAI_URL,
        json={
            "model": _MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    try:
        import re
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        return json.loads(match.group(0)) if match else []
    except Exception:
        return []


def parse_json_response(raw: str) -> dict:
    """Extract and parse the JSON block from a Gemini response string."""
    import re
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        raw = match.group(1).strip()
    return json.loads(raw)
