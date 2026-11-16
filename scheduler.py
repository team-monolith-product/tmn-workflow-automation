"""
APScheduler 기반 크론 작업 스케줄러

GitHub Actions 크론 워크플로우를 대체하여 슬랙 봇 프로세스 내에서 정기 작업을 실행합니다.
"""

import importlib
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from service.business_days import is_business_day
from service.config import load_config

TIMEZONE = "Asia/Seoul"
KST = timezone(timedelta(hours=9))


def _make_job_callable(func, job_name: str, business_day_only: bool):
    """
    스크립트 함수를 스케줄러 작업으로 감싸는 래퍼 생성

    영업일 체크가 필요한 작업은 실행 전에 is_business_day()를 확인합니다.
    """

    def wrapper():
        if business_day_only:
            today = datetime.now(KST).date()
            if not is_business_day(today):
                print(f"[scheduler] {job_name}: 영업일 아님. 건너뜀.")
                return
        print(f"[scheduler] {job_name}: 실행 시작")
        func()
        print(f"[scheduler] {job_name}: 실행 완료")

    return wrapper


def start_scheduler():
    """스케줄러를 생성하고 config.yaml의 작업을 등록한 뒤 시작"""
    config = load_config()
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    for job_config in config.scheduled_jobs:
        module = importlib.import_module(job_config.module)
        func = getattr(module, job_config.function)
        wrapped = _make_job_callable(
            func, job_config.name, job_config.business_day_only
        )

        trigger = CronTrigger(timezone=TIMEZONE, **job_config.cron)
        scheduler.add_job(wrapped, trigger, id=job_config.name, name=job_config.name)
        print(f"[scheduler] 작업 등록: {job_config.name} ({trigger})")

    scheduler.start()
    print(f"[scheduler] {len(config.scheduled_jobs)}개 작업으로 스케줄러 시작")
