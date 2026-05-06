import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import db

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()
_run_brand_fn = None  # set by main.py


def _make_job(brand_name: str | None):
    def _execute():
        if _run_brand_fn:
            _run_brand_fn(brand_name)
    return _execute


def _brand_from_job_name(job_name: str) -> str | None:
    """Extract brand name from job_name like 'citroen_pipeline' → 'Citroën'.
    Returns None for 'full_pipeline' (run all brands)."""
    _map = {
        "citroen_pipeline": "Citroën",
        "peugeot_pipeline": "Peugeot",
        "mg_pipeline":      "MG",
    }
    return _map.get(job_name)


def _cron_parts(expr: str) -> dict:
    parts = expr.strip().split()
    if len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
    else:
        minute, hour, day, month, day_of_week = "0", "4", "*", "*", "*"
    return dict(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)


def init(run_brand_fn):
    global _run_brand_fn
    _run_brand_fn = run_brand_fn

    try:
        configs = db.get_all_scraping_configs()
    except Exception as e:
        logger.warning(f"Could not load scraping configs from DB (using defaults): {e}")
        configs = [{"job_name": "citroen_pipeline", "cron_expression": "0 2 * * *", "is_active": True},
                   {"job_name": "peugeot_pipeline", "cron_expression": "0 3 * * *", "is_active": True},
                   {"job_name": "mg_pipeline",      "cron_expression": "0 4 * * *", "is_active": True}]

    scheduled = 0
    for config in configs:
        job_name = config["job_name"]
        if not config.get("is_active"):
            logger.info(f"Scheduler: '{job_name}' is_active=false, skipping")
            continue
        brand = _brand_from_job_name(job_name)
        cron_parts = _cron_parts(config.get("cron_expression", "0 4 * * *"))
        _scheduler.add_job(
            _make_job(brand),
            CronTrigger(**cron_parts),
            id=job_name,
            replace_existing=True,
        )
        logger.info(f"Scheduler: '{job_name}' (brand={brand}) scheduled at {config.get('cron_expression')}")
        scheduled += 1

    _scheduler.start()
    logger.info(f"Scheduler started with {scheduled} active job(s)")


def reload():
    try:
        configs = db.get_all_scraping_configs()
    except Exception as e:
        logger.warning(f"Reload failed to fetch configs: {e}")
        return

    # Remove all existing jobs
    for job in _scheduler.get_jobs():
        _scheduler.remove_job(job.id)

    scheduled = 0
    for config in configs:
        job_name = config["job_name"]
        if not config.get("is_active"):
            continue
        brand = _brand_from_job_name(job_name)
        cron_parts = _cron_parts(config.get("cron_expression", "0 4 * * *"))
        _scheduler.add_job(
            _make_job(brand),
            CronTrigger(**cron_parts),
            id=job_name,
            replace_existing=True,
        )
        scheduled += 1

    logger.info(f"Scheduler reloaded: {scheduled} active job(s)")


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
