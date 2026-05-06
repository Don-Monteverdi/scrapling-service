import asyncio
import logging
import os
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv()

import db
import discovery
import price_check
import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SCRAPLING_SERVICE_SECRET = os.environ.get("SCRAPLING_SERVICE_SECRET", "")

_bearer = HTTPBearer(auto_error=False)


def _require_auth(credentials: HTTPAuthorizationCredentials | None) -> None:
    if not SCRAPLING_SERVICE_SECRET:
        return
    if not credentials or credentials.credentials != SCRAPLING_SERVICE_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# ── Background runner ────────────────────────────────────────────────────────

_lock = asyncio.Lock()


async def _run_in_background(phase: str, brand_name: str | None = None):
    """Run discovery and/or price-check, optionally filtered to a single brand."""
    def _ascii_key(s: str) -> str:
        nfkd = unicodedata.normalize("NFKD", s)
        return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()

    job_key = f"{_ascii_key(brand_name)}_pipeline" if brand_name else "full_pipeline"
    async with _lock:
        db.update_scraping_config_run(job_key, "running")
        result = {}
        try:
            if phase in ("full", "discovery"):
                brands = db.get_brands()
                if brand_name:
                    brands = [b for b in brands if b["name"].lower() == brand_name.lower()]
                new_models = 0
                for brand in brands:
                    if not brand.get("discovery_url"):
                        continue
                    scraped = await asyncio.to_thread(discovery.discover_brand_models, brand)
                    if not scraped:
                        continue
                    db_models = db.get_admin_models(brand["id"])
                    new = discovery.find_new_models(brand["name"], scraped, db_models)
                    for m in new:
                        r = await asyncio.to_thread(discovery.process_new_model, brand, m["model_name"], m["pdf_url"])
                        if r.get("ok"):
                            new_models += 1
                result["new_models"] = new_models

            if phase in ("full", "price-check"):
                pc_result = await asyncio.to_thread(price_check.run_price_check, brand_name)
                result.update(pc_result)

            db.update_scraping_config_run(job_key, "ok", result)
            logger.info(f"Phase '{phase}' brand='{brand_name}' complete: {result}")
        except Exception as e:
            logger.error(f"Phase '{phase}' brand='{brand_name}' error: {e}")
            db.update_scraping_config_run(job_key, "error", {"error": str(e)})


def _sync_run_brand(brand_name: str | None = None):
    asyncio.run(_run_in_background("full", brand_name))


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.init(_sync_run_brand)
    yield
    scheduler.shutdown()


app = FastAPI(title="scrapling-service", lifespan=lifespan)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/run/full")
async def run_full(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    _require_auth(credentials)
    asyncio.create_task(_run_in_background("full"))
    return {"ok": True, "message": "Full pipeline started (all brands)"}


@app.post("/run/brand/{brand_name}")
async def run_brand(brand_name: str, credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    _require_auth(credentials)
    asyncio.create_task(_run_in_background("full", brand_name))
    return {"ok": True, "message": f"Full pipeline started for brand: {brand_name}"}


@app.post("/run/discovery")
async def run_discovery(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    _require_auth(credentials)
    asyncio.create_task(_run_in_background("discovery"))
    return {"ok": True, "message": "Discovery phase started (all brands)"}


@app.post("/run/price-check")
async def run_price_check_endpoint(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    _require_auth(credentials)
    asyncio.create_task(_run_in_background("price-check"))
    return {"ok": True, "message": "Price check started (all brands)"}


@app.post("/reload-config")
async def reload_config(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    _require_auth(credentials)
    scheduler.reload()
    return {"ok": True, "message": "Scheduler config reloaded"}
