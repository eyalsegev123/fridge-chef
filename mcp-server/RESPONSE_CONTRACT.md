# Tool response contract

Every tool returns the same outer envelope in both MCP `content[0].text` and
`structuredContent`:

```json
{
  "status": "success | user_fixable | system_error",
  "data": {},
  "error": null
}
```

For an error, `error` has this shape:

```json
{
  "code": "STABLE_MACHINE_READABLE_CODE",
  "message": "Diagnostic description for the caller",
  "retryable": false
}
```

- `success`: the operation completed, including valid business outcomes such as
  an empty result set.
- `user_fixable`: the caller can change input or complete a required business
  step and call again.
- `system_error`: configuration, network, backend, or unexpected code failure.

Keep customer-facing wording out of the envelope. Return facts, stable result
codes, and structured data so each consuming channel can present them correctly.
