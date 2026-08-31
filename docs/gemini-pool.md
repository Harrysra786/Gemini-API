# Standalone Gemini Pool

`gemini_pool_service.py` is deliberately separate from an MCP host. It owns
Gemini clients, account locks, background image/video retrieval, output files,
and persisted job state. It accepts only loopback HTTP requests on
`127.0.0.1` and requires a bearer token stored in a local token file.

## Runtime contract

`POST /v1/media` accepts `kind` (`image` or `video`), `prompt`, optional
`files`, `model`, and `account`, and immediately returns a job with
`status: "generating"`. `GET /v1/jobs/{job_id}` returns the persisted status:
`generating`, `ready`, `no_media_returned`, or `failed`.

The service keeps one lock per account, so concurrent jobs use distinct idle
accounts before waiting on a busy one. A job retains its selected account for
the full Gemini request and media-save phase.

## Controlled deployment procedure

Do not modify an active desktop application's MCP files while it is running.
First run and verify this service independently in a dedicated environment.
Only after that, change an MCP adapter in a separate controlled deployment so
it calls `gemini_pool_client.py`; the adapter must not instantiate Gemini
clients or retain account state.

Example standalone launch, using paths selected by the operator:

```powershell
python gemini_pool_service.py serve --credentials <private-account-dir> --state <private-state-dir> --token-file <private-token-file> --port 8767
```

The service never returns credential values. Keep the credential directory,
state directory, and token file accessible only to the local user.

## Refreshing a browser session

The standalone pool also exposes a one-time connector-compatible enrollment
listener on loopback port `8766`. Arm it through the authenticated pool API,
then click the existing Gemini account connector within ten minutes. The
connector sends the browser session only to this local listener; the pool
updates the seven alias files with `authuser` values `0` through `6` and never
includes cookie material in its API responses or job records.
