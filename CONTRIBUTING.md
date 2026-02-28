# Contributing to LLM-toolkit

Thank you for your interest in contributing.

## How to Contribute

1. **Fork** the repository
2. **Create a branch** for your change (`git checkout -b feature/your-feature`)
3. **Make your changes**
4. **Submit a pull request** against `main`

## Security Notes for Contributors

- **Never commit secrets.** Do not add `.env`, `openclaw/.env`, API keys, tokens, or credentials to the repository.
- Use `.env.example` and `openclaw/.env.example` as templates with placeholder values only.
- If you need to test with real credentials, use local `.env` files (gitignored) and never push them.

## What We Accept

- Bug fixes
- Documentation improvements
- New profiles or service configurations
- Script improvements (ensure_dirs, model pullers)
- Security-related fixes

## Code Style

- PowerShell scripts: follow existing patterns
- Python: PEP 8 style
- YAML: 2-space indent, consistent structure

## Questions

Open an issue for questions or discussions.
