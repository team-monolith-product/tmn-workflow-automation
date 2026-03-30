# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build/Lint/Test Commands
- Run application: `python app.py`
- Format code: `black .`
- Run a single file: `python path/to/file.py`
- Run tests: `pytest` or `pytest -v` for verbose output
- Run specific test file: `pytest test_filename.py`
- Some scripts support `--dry-run` flag; this option will be gradually implemented in more scripts for local testing

## Testing
- Use pytest for unit testing
- Test files should be named with `test_` prefix (e.g., `test_notify_worktime_left.py`)
- Tests can be placed in the root directory or in a `tests/` directory
- Write comprehensive tests for business logic, especially for complex calculations and API interactions
- Mock external API calls in tests to ensure reliability and speed

## Code Style Guidelines
- **Formatting**: Use Black for code formatting
- **Imports**: Group in order: stdlib, third-party, local modules
- **Types**:
  - Use comprehensive type hints, including Annotated and Literal types
  - Use lowercase generics (list, dict, set) instead of importing List, Dict, Set
  - Use `| None` syntax instead of Optional types (e.g., `str | None` instead of `Optional[str]`)
- **Naming**:
  - snake_case for functions/variables, UPPER_SNAKE_CASE for constants
  - File names should use verb_noun.py format (e.g., collect_review_stats.py, notify_worktime_left.py)
- **API Design**:
  - API functions should follow the `{method}_{resource}` naming pattern (e.g., `get_event_codes`, `get_worktime`)
  - API functions should return raw JSON responses without transformations
  - The `api/` folder should only contain direct API wrapper functions
  - Data transformation and business logic should be placed in appropriate modules (utils, services, etc.)
- **Logging & Error Handling**:
  - Keep it simple, use print() statements for scripts
  - Avoid complex logging configuration for these internal tools
  - Minimal error handling - never use try/except without understanding the error
  - Do not silently ignore exceptions - allow runtime errors to occur rather than silently skipping code
  - Avoid defensive programming patterns that hide errors (e.g., never use `if response and "ts" in response` to check API responses)
- **Docstrings**: Use triple quotes with description, Args, Returns sections
- **Language**: Code structure in English, comments/docstrings in Korean
- **Environment**: Use python-dotenv for environment variables
- **Tools**: Use LangChain-style tool decorators for AI agent functions
- **Architecture**: Follow service layer pattern for separation of concerns

## Automated Workflows
- APScheduler(`AsyncIOScheduler`)가 `app.py` 프로세스 내에서 크론 작업을 실행
- 스케줄 설정은 `config.yaml`의 `scheduled_jobs` 섹션에 KST 시간으로 정의
- 스케줄러 코드는 `scheduler.py`에 위치
- 각 스크립트는 독립 실행 가능: `python scripts/foo.py` (--dry-run 지원)
- GitHub Actions는 CI/CD 전용 (pytest, black, Docker build)