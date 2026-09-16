# Logging

*Read before changing anything that logs, makes an HTTP request, handles an exception, or touches a credential-bearing value.*

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

## Diagnostic Logging Coverage

- Add credential-safe debug logging for every meaningful action, decision,
  skip, external delivery attempt, success, and failure.
- Diagnostic logs must make it possible to trace a workflow end to end without
  logging raw messages, event payloads, request or response bodies, or other
  user-provided content. Prefer sanitized action names, counts, result flags,
  character counts, and exception type names.
- A failure in one diagnostic preview must be logged and must not prevent the
  remaining previews from being attempted.
