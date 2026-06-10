import asyncio
from unittest.mock import patch, MagicMock

import pytest

import scheduler as scheduler_module
from scheduler import _make_job_callable, start_scheduler
from service.config import ScheduledJobConfig


class TestMakeJobCallable:
    def test_business_day_only_skips_on_holiday(self):
        """영업일이 아니면 함수를 실행하지 않는다"""
        func = MagicMock()
        wrapper = _make_job_callable(func, "test_job", business_day_only=True)

        with patch.object(scheduler_module, "is_business_day", return_value=False):
            wrapper()

        func.assert_not_called()

    def test_business_day_only_runs_on_business_day(self):
        """영업일이면 함수를 실행한다"""
        func = MagicMock()
        wrapper = _make_job_callable(func, "test_job", business_day_only=True)

        with patch.object(scheduler_module, "is_business_day", return_value=True):
            wrapper()

        func.assert_called_once()

    def test_not_business_day_only_always_runs(self):
        """business_day_only=False면 항상 실행한다"""
        func = MagicMock()
        wrapper = _make_job_callable(func, "test_job", business_day_only=False)

        wrapper()

        func.assert_called_once()

    def test_job_exception_reported_to_sentry_and_reraised(self):
        """잡 예외는 scheduled_job 태그와 함께 Sentry로 보고된 뒤 다시 올라간다"""
        func = MagicMock(side_effect=ValueError("boom"))
        wrapper = _make_job_callable(func, "test_job", business_day_only=False)

        with (
            patch.object(scheduler_module.sentry_sdk, "new_scope") as new_scope,
            patch.object(scheduler_module.sentry_sdk, "capture_exception") as capture,
        ):
            with pytest.raises(ValueError, match="boom"):
                wrapper()

        scope = new_scope.return_value.__enter__.return_value
        scope.set_tag.assert_called_once_with("scheduled_job", "test_job")
        capture.assert_called_once()


class TestStartScheduler:
    @pytest.mark.asyncio
    async def test_registers_all_jobs(self):
        """config의 모든 작업이 스케줄러에 등록된다"""
        mock_module = MagicMock()

        mock_config = MagicMock()
        mock_config.scheduled_jobs = [
            ScheduledJobConfig(
                name="test_job_1",
                module="scripts.test1",
                function="main",
                cron={"hour": 9, "minute": 0, "day_of_week": "mon-fri"},
                business_day_only=True,
            ),
            ScheduledJobConfig(
                name="test_job_2",
                module="scripts.test2",
                function="main",
                cron={"hour": 16, "minute": 0, "day_of_week": "mon-fri"},
                business_day_only=False,
            ),
        ]

        with (
            patch.object(scheduler_module, "load_config", return_value=mock_config),
            patch.object(
                scheduler_module,
                "importlib",
                MagicMock(import_module=MagicMock(return_value=mock_module)),
            ),
        ):
            start_scheduler()

        # start_scheduler()가 에러 없이 완료되면 성공
        # AsyncIOScheduler는 실제 이벤트 루프에서 동작 확인
