import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import db

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()
_JOB_ID = "full_pipeline"
_run_full_fn = None  # set by main.py after import


def _execute():
    if _run_full_fn:
        _run_full_fn()


def init(run_full):
    global _run_full_fn
    _run_full_fn = run_full

    cron_expr = "0 4 * * *"
    is_active = True
    try:
        config = db.get_scraping_config()
    except Exception as e:
        logger.warning(f"Could not load scraping config from DB (using defaults): {e}")
        config = None
    if config:
        cron_expr = config.get("cron_expression", cron_expr)
        is_active = config.get("is_active", True)

    if is_active:
        parts = cron_expr.strip().split()
        if len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
        else:
            minute, hour, day, month, day_of_week = "0", "4", "*", "*", "*"

        _scheduler.add_job(
            _execute,
            CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week),
            id=_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"Scheduler: job '{_JOB_ID}' scheduled with cron '{cron_expr}'")
    else:
        logger.info("Scheduler: is_active=false, no job scheduled")

    _scheduler.start()


def reload():
    config = db.get_scraping_config()
    if not config:
        return

    cron_expr = config.get("cron_expression", "0 4 * * *")
    is_active = config.get("is_active", True)

    if _scheduler.get_job(_JOB_ID):
        _scheduler.remove_job(_JOB_ID)

    if is_active:
        parts = cron_expr.strip().split()
        if len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
        else:
            minute, hour, day, month, day_of_week = "0", "4", "*", "*", "*"
        _scheduler.add_job(
            _execute,
            CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week),
            id=_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"Scheduler reloaded: cron '{cron_expr}'")
    else:
        logger.info("Scheduler paused: is_active=false")


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
