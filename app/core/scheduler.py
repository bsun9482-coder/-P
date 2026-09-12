"""APScheduler 定时爬取任务。

策略：
- 单实例文件锁：防止多个进程同时跑调度器重复抓取；
- 启动时立即后台抓取一次（保证新装即用有题库）；
- 每天 CRAWL_TIME（默认 02:00）抓取一次；
- CRAWL_INTERVAL_HOURS > 0 时，另按间隔小时抓取（如每 24 小时）；
- 增量去重由 content_hash 保证，重复抓取不会产生冗余数据；
- 日志使用 RotatingFileHandler 轮转，避免无限增长。
"""

import atexit
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import app.core.config as config
import app.core.db as db
from app.crawler.run import crawl_all

logger = logging.getLogger("interview_coach.scheduler")

_scheduler: BackgroundScheduler | None = None
_lock_handles: list = []  # 保持文件句柄存活，防止锁被 GC 释放


def acquire_scheduler_lock() -> bool:
    """尝试获取单实例文件锁；拿不到说明已有进程在跑调度器。"""
    config.ensure_data_dir()
    lock_path = config.DATA_DIR / "scheduler.lock"
    try:
        fh = open(lock_path, "a+")  # noqa: SIM115 - 句柄需长期持有（_lock_handles）以保持 OS 文件锁
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_handles.append(fh)
    atexit.register(fh.close)
    return True


def _parse_crawl_time(value: str) -> tuple[int, int]:
    """解析 HH:MM，非法值回退到 02:00。"""
    try:
        hour, _, minute = value.partition(":")
        h, m = int(hour), int(minute or 0)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except ValueError:
        logger.warning("CRAWL_TIME=%r 无效，回退到 02:00", value)
        return 2, 0


def _clean_job() -> None:
    """定时清洗：抓取后跑统一清洗（规则全量 + 语义增量一批，成本可控）。"""
    logger.info("定时清洗任务开始")
    try:
        from app.crawler.clean import run_clean

        stats = run_clean()
        logger.info("定时清洗任务完成: %s", stats)
    except Exception as e:
        logger.exception("定时清洗任务失败: %s", e)


def _crawl_job() -> None:
    logger.info("定时爬取任务开始")
    try:
        results = crawl_all()
        logger.info("定时爬取任务完成: %s", results)
    except Exception as e:
        logger.exception("定时爬取任务失败: %s", e)
    # 抓取完成后紧跟一次清洗（新入库数据默认 raw，需尽快洗到可用状态）
    _clean_job()


def start_scheduler() -> BackgroundScheduler | None:
    """启动后台调度器（幂等）。返回 None 表示未启动（被禁用或已有实例）。"""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    if os.getenv("DISABLE_SCHEDULER") == "1":
        return None
    if not acquire_scheduler_lock():
        logger.info("已有其他进程持有调度器锁，本进程不再启动定时抓取")
        return None

    db.init_db()
    _scheduler = BackgroundScheduler(timezone=config.SCHEDULER_TZ)

    # 1) 每天固定时间抓取
    hour, minute = _parse_crawl_time(config.CRAWL_TIME)
    _scheduler.add_job(
        _crawl_job,
        CronTrigger(hour=hour, minute=minute, timezone=config.SCHEDULER_TZ),
        id="daily_crawl",
        replace_existing=True,
    )
    logger.info("已注册每日抓取: %02d:%02d (%s)", hour, minute, config.SCHEDULER_TZ)

    # 2) 间隔抓取（可选）：若间隔是 24h 的倍数，其触发时刻会与每日固定时间
    #    完全重合（bug #8：同刻双 job 并发重复抓取+清洗，双倍 LLM 成本），
    #    此时以每日任务为准，不再额外注册间隔任务。
    if config.CRAWL_INTERVAL_HOURS > 0:
        if config.CRAWL_INTERVAL_HOURS % 24 == 0:
            logger.info(
                "CRAWL_INTERVAL_HOURS=%s 为 24h 倍数，已由每日任务覆盖，跳过间隔注册（bug #8）",
                config.CRAWL_INTERVAL_HOURS,
            )
        else:
            _scheduler.add_job(
                _crawl_job,
                IntervalTrigger(hours=config.CRAWL_INTERVAL_HOURS),
                id="interval_crawl",
                replace_existing=True,
            )
            logger.info("已注册间隔抓取: 每 %s 小时", config.CRAWL_INTERVAL_HOURS)

    _scheduler.start()
    logger.info("调度器已启动")

    # 3) 启动后立即后台抓一次（不阻塞主线程）。
    #    用 UTC now 而非本地墙钟：调度器时区固定为 SCHEDULER_TZ，naive 本地时间会被
    #    按该时区解释，非上海时区主机会导致"启动即抓取"被推迟数小时（bug #9）。
    _scheduler.add_job(_crawl_job, id="bootstrap_crawl", next_run_time=datetime.now(timezone.utc))
    logger.info("已注册启动抓取任务")
    return _scheduler


def setup_logging() -> None:
    """配置日志：控制台 + data/scheduler.log（5MB 轮转，保留 3 份）。

    注意：不能用 basicConfig（Streamlit 启动时会先配置 root logger），
    改为直接挂 RotatingFileHandler。
    """
    config.ensure_data_dir()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(
        isinstance(h, RotatingFileHandler) and getattr(h, "_msya_log", False) for h in root.handlers
    ):
        fh = RotatingFileHandler(
            config.DATA_DIR / "scheduler.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        fh._msya_log = True  # type: ignore[attr-defined]
        root.addHandler(fh)
