# Working in this repo (for coding agents)

Read [docs/PRD.md](docs/PRD.md) before changing anything. The PRD is the source of truth for roles, workflows, the harness and acceptance criteria.

## Hard rules

- Never read, print, copy or commit `.env` or any key or token. Use `.env.example` for placeholders.
- Never call Meta models (or any paid API) unless the task you were given says live calls are approved. Default to the offline fake model in tests.
- Never call write tools against a real Meta ad account. Only the sandbox may receive writes, and only through the Operator.
- Synthetic data only. No real ad data, customer data or private Pallas code.
- Only touch the files your task lists. Don't run `git push`, change git config or create branches.
- Every task ends with its test command passing. Report the exact command and its output.

## Conventions

- Python 3.12, managed with `uv`. Run tests with `uv run pytest -q`. Lint with `uv run ruff check`.
- Deliverables are Pydantic models in `src/pgt/schemas/`. Model output is parsed into them and validated in code.
- Numbers shown to users are computed in code and cite evidence IDs. Models never do arithmetic that users see.
- Policy (permissions, budgets, house rules, approvals) lives in `src/pgt/harness/policy.py`, never in prompts.
- Meta Model API notes: base URL `https://api.meta.ai/v1`; `tool_choice` accepts only `"auto"`; strict JSON schema needs `additionalProperties: false` and every property in `required`; no `allOf`/`oneOf`; no recursive schemas; web search is a Responses-API tool `{"type": "web_search"}`.
- Write clear, short comments and docs in English.
