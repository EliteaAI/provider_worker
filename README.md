# provider_worker
External providers: worker

## Reporting failures

When a tool invocation fails, set `status: "Error"` — not `"Failed"`; no provider in this
org's fleet uses it, and the platform's SPI schema (`ToolInvocationResponse` in `elitea_core`)
doesn't accept it.

Also set `error_category` to a short, stable string describing what went wrong (e.g.
`timeout`, `invalid_input`, `authentication_error`). It's free text at the schema level, but
`utils/failure_signals.py::PROVIDER_CATEGORY_CLASSES` is the recognized vocabulary: a category
in that table gets classified into a `would_be_error_class` in shadow-mode logs (#6168); an
unrecognized one just logs unclassified. `error_type` is a free-text sub-type, purely
informational.

A `Completed` response with a non-empty `errors`/`warnings` list is detected the same way as
`status: "Error"` — but that's a fallback for cases that slip through, not a substitute for
setting the status correctly when the invocation genuinely failed.
