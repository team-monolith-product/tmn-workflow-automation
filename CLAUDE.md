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
- **Types**: Use comprehensive type hints, including Annotated and Literal types
- **Naming**: snake_case for functions/variables, UPPER_SNAKE_CASE for constants
- **Docstrings**: Use triple quotes with description, Args, Returns sections
- **Language**: Code structure in English, comments/docstrings in Korean
- **Environment**: Use python-dotenv for environment variables
- **Tools**: Use LangChain-style tool decorators for AI agent functions
- **Architecture**: Follow service layer pattern for separation of concerns