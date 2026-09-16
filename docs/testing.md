# Python verification

*Read before finishing any Python change: what must pass, and how annotations and suppressions are expected to be written.*

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
