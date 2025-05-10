# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build/Lint/Test Commands
- Run application: `python app.py`
- Format code: `black .`
- Run a single file: `python path/to/file.py`
- No formal test framework; files can be tested directly via `python filename.py`
- Some scripts support `--dry-run` flag; this option will be gradually implemented in more scripts for local testing

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
- GitHub Actions are used for running scripts on schedule
- Workflow files are stored in `.github/workflows/` directory
- Each workflow corresponds to a script in the root directory