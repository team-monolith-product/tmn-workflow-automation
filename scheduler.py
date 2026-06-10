"""
APScheduler 기반 크론 작업 스케줄러

GitHub Actions 크론 워크플로우를 대체하여 슬랙 봇 프로세스 내에서 정기 작업을 실행합니다.
"""

import importlib
from datetime import datetime, timezone, timedelta

import sentry_sdk
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sentry_sdk.integrations.logging import ignore_logger

from service.business_days import is_business_day
from service.config import load_config

TIMEZONE = "Asia/Seoul"
KST = timezone(timedelta(hours=9))

# 잡 예외는 _make_job_callable의 wrapper가 scheduled_job 태그와 함께 직접 보고한다.
# executor의 ERROR 로그가 LoggingIntegration을 거쳐 만드는 무태그 중복 이벤트는 막는다.
ignore_logger("apscheduler.executors.default")


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
        try:
            func()
        except Exception:
            # 어떤 잡이 실패했는지 식별할 수 있도록 태그를 붙여 보고한 뒤 다시 올린다.
            # executor 로그 경유 이벤트는 위 ignore_logger로 차단되어 이 경로가 유일하다.
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("scheduled_job", job_name)
                sentry_sdk.capture_exception()
            raise
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
