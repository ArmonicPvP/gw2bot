# Project Security Requirements

## Credential-Safe Logging

- Never log credentials or secret-bearing objects. This includes API keys,
  Discord tokens, authorization headers, request objects, response objects,
  complete request URLs with query strings, and raw response bodies.
- HTTP diagnostics may log only sanitized route paths without query strings,
  status codes, result types, and result counts.
- All console logging must retain the redacting formatter configured by
  `gw2bot.main.configure_logging`. Do not add independent handlers that bypass
  it.
- Every new credential or token environment variable must be supplied to the
  redacting formatter during startup.
- Add regression tests whenever request, response, exception, or logging code
  changes to prove secrets cannot appear in console output.
- Never read, print, commit, or include the local `.env` file in diagnostics.

## Python Verification

- Create and maintain tests with pytest, not unittest. Use pytest fixtures,
  native `assert` statements, and `pytest.raises` instead of
  `unittest.TestCase`; `unittest.mock` remains acceptable for mocking.
- VS Code uses Pylance with `python.analysis.typeCheckingMode` set to
  `standard`. The matching CLI configuration is `pyrightconfig.json`, which
  targets the project's Python 3.13 CI and Docker runtime.
- Before completing Python changes, run both `python -m pytest` and
  `pyright`. Do not consider a change complete while either command reports
  errors.
- Keep annotations valid for both production code and tests. Prefer precise
  protocols, casts, and typed fixtures over broad `Any` or new
  `# type: ignore` comments.
- When a suppression is unavoidable, scope it to the specific expression and
  diagnostic rule, and include a short reason. Do not disable a Pyright rule
  globally to hide a local typing problem.
- Keep `.vscode/settings.json` and `pyrightconfig.json` aligned so local
  Pylance diagnostics match CI and command-line verification.
