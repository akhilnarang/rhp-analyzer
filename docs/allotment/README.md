# IPO Allotment Status Lookup

The allotment status subsystem provides synchronous, best-effort IPO allotment lookups across four Indian registrars. It runs alongside RHP Analyzer's prospectus extraction pipeline.

## Supported Registrars

The system supports four registrars:

| Registrar | Provider Key | Official Status Page | Lookup Mechanism | Challenge |
| --- | --- | --- | --- | --- |
| KFintech | `kfintech` | `https://ipostatus.kfintech.com/` | AWS query API | None |
| MUFG Intime / Link Intime | `mufg` | `https://in.mpms.mufg.com/Initial_Offer/public-issues.html` | ASP.NET XML/JSON endpoints | AES-encrypted token |
| Bigshare Services | `bigshare` | `https://ipo.bigshareonline.com/ipo_status.html` | ASP.NET JSON endpoint | Visual CAPTCHA |
| Purva Sharegistry | `purva` | `https://www.purvashare.com/investor-service/ipo-query` | Django form POST | CSRF token |

These adapters interact with undocumented public page contracts rather than official developer APIs. Registrars can modify their markup, tokens, or network endpoints at any time.

## Architecture and Data Flow

The allotment subsystem is strictly decoupled from the PDF analysis pipeline:

- **No persistence**: Lookups do not read or write the SQLite database or the report cache.
- **No model interaction**: Applicant identifiers are never sent to language models.
- **In-flight only**: Applicant Permanent Account Numbers (PANs) are validated in memory, forwarded to the upstream registrar, and discarded immediately. Response models omit the PAN.
- **Cache prohibition**: All allotment HTTP responses send `Cache-Control: no-store`.

### System Components

- **Web Frontend (`/allotment`)**: A Jinja-rendered page (`src/rhp_analyzer/templates/allotment.html`) that loads active issues, guides the user through Bigshare CAPTCHAs, submits search requests, and displays normalized status tables.
- **API Router (`src/rhp_analyzer/api.py`)**: Public FastAPI routes for issue discovery, challenge generation, and applicant lookup.
- **Service Orchestrator (`src/rhp_analyzer/allotment/service.py`)**: `AllotmentService` maintains an in-memory catalogue cache across all providers, normalizes and validates applicant PANs, resolves company search queries, and dispatches lookups to the correct adapter.
- **Provider Adapters (`src/rhp_analyzer/allotment/`)**: Individual adapters subclassing `AllotmentProvider` (`base.py`) handle registrar-specific network requests, HTML/JSON/XML parsing, token generation, and error mapping.

### Request and Data Flow

1. **Issue Catalogue Aggregation**:
   - The user loads `/allotment` or calls `GET /v1/allotment/issues`.
   - `AllotmentService` queries all four registrars concurrently using `asyncio.gather`.
   - Discovered issues are combined into a sorted catalogue and cached in memory for `IPO_ALLOTMENT_CATALOGUE_TTL_SECONDS` (default: 300 seconds).
   - If an individual registrar fails or times out during catalogue refresh, its error is recorded in provider status while remaining registrars continue to serve results.
2. **Challenge Initiation** (Bigshare only):
   - Bigshare requires a visual CAPTCHA answer for every lookup.
   - The client calls `POST /v1/allotment/challenge` with the selected issue ID.
   - The Bigshare adapter requests a fresh CAPTCHA token and image from Bigshare and returns them as a base64 data URI.
   - The user views the image in the browser and enters the characters. The service does not attempt automated CAPTCHA solving.
3. **Status Lookup**:
   - The client submits `POST /v1/allotment/lookup` containing `issue_id` (or company `query`), `pan`, and optional CAPTCHA fields.
   - The PAN is normalized (whitespace stripped, uppercase) and validated against `^[A-Z]{5}[0-9]{4}[A-Z]$`. Malformed PANs return HTTP 422 immediately without echoing the input.
   - `AllotmentService` resolves the issue and dispatches the lookup to the designated provider adapter.
   - The adapter executes the registrar-specific request flow within its configured timeout and allowed hosts.
   - The registrar response is parsed into normalized records and outcomes.
4. **Transport Guards and Redirect Limits**:
   - Each adapter defines `allowed_hosts`. Redirects outside these hosts trigger an immediate `UpstreamChanged` error.
   - Redirect chains are limited to at most 4 hops within allowed hosts.
   - Upstream calls enforce `IPO_ALLOTMENT_TIMEOUT_SECONDS` (default: 15.0 seconds).
   - Transient retries (1 retry after 100ms) only apply to idempotent catalogue fetches and stateless setup calls. Lookups with single-use CAPTCHAs or POST queries are never retried automatically.

## Public Routes

The following unauthenticated routes serve the allotment feature:

### `GET /allotment`
Returns the status page HTML. Users can search issues, view closing dates when available, solve CAPTCHA challenges, and review allotment results.

### `GET /v1/allotment/issues`
Lists active IPO issues from all registrars.
- **Parameters**:
  - `query` (optional string): Filter issues by company name.
  - `provider` (optional enum: `kfintech`, `mufg`, `bigshare`, `purva`): Restrict results to one registrar.
- **Response**: `AllotmentIssueListResponse` containing `issues` and `providers` status.

### `POST /v1/allotment/challenge`
Requests a fresh CAPTCHA challenge for registrars that enforce one.
- **Payload**: `{"issue_id": "<provider>:<id>"}` or `{"query": "<company_name>"}`.
- **Response**: `AllotmentLookupResult` with `outcome="challenge_required"` and an `AllotmentChallenge` object containing `token` and `image_data_uri`.

### `POST /v1/allotment/lookup`
Executes an applicant allotment status query.
- **Payload**:
  ```json
  {
    "issue_id": "kfintech:18432822190",
    "pan": "ABCDE1234F",
    "captcha_token": null,
    "captcha_answer": null
  }
  ```
  *(Never transmit the PAN in URL query parameters.)*
- **Response**: `AllotmentLookupResult` containing the normalized outcome, applicant records (application number, name, applied shares, allotted shares), and total allocated shares.

## Normalized Outcomes

Registrar responses are mapped to the `AllotmentOutcome` enum:

### Confirmed Applicant Outcomes
- `allotted`: The registrar confirms shares were allotted to this PAN. Total shares appear in `allocated_shares`.
- `not_allotted`: The registrar confirms an application was received, but zero shares were allotted.
- `not_found`: The registrar has no application on file for this PAN in the selected issue.
- `pending`: The registrar returned an applicant record, but allotment figures are not yet finalized.

### Operational and Diagnostic Outcomes
- `challenge_required`: A CAPTCHA challenge must be completed before lookup (or the submitted CAPTCHA answer was rejected).
- `rate_limited`: The registrar is throttling requests (HTTP 429 or status flag). May include `retry_after_seconds`.
- `provider_unavailable`: The registrar is unreachable, timed out, or returned HTTP 5xx.
- `upstream_changed`: The registrar changed its markup, API response structure, or redirected to an unexpected host.
- `upstream_error`: The registrar returned an explicit business or system error.
- `ambiguous`: A company search query matched multiple active issues. The response returns candidate issues to choose from.
- `not_supported`: The requested operation is not supported by this registrar (for example, requesting a CAPTCHA challenge for KFintech).

## Detailed Documentation

- [Provider Protocols and Adapters](providers.md): Specific request structures, response formats, session rules, and known quirks for each registrar.
- [Operations and Maintenance](operations.md): Environment settings, local verification, test fixtures, smoke checks, and troubleshooting upstream changes.
