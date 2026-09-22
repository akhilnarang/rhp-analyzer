# Allotment Operations and Maintenance

This guide explains configuration, verification, testing, troubleshooting, and extension of the IPO allotment status subsystem.

---

## Configuration

Allotment settings are defined in `src/rhp_analyzer/config.py` and configured via `.env` or system environment variables:

| Variable | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `IPO_ALLOTMENT_TIMEOUT_SECONDS` | Float | `15.0` | `0 < x <= 60` | HTTP request timeout for individual registrar queries. Connection timeout is capped at `min(timeout, 5.0)`. |
| `IPO_ALLOTMENT_CATALOGUE_TTL_SECONDS` | Integer | `300` | `0 <= x <= 3600` | Duration in seconds to retain the combined in-memory registrar issue catalogue before refreshing. |

### CAPTCHA auto-solving

Bigshare lookups always try to read the CAPTCHA with OCR first. Bigshare permits this automation for personal use. Install the OCR stack to use it:

```bash
sudo apt install tesseract-ocr
uv sync --group captcha
```

The reader accepts an answer only when it reads exactly six digits. Without Tesseract, or on any uncertain read, the lookup returns `challenge_required` and the human flow still works.

Example `.env` configuration:

```dotenv
# Allotment subsystem settings
IPO_ALLOTMENT_TIMEOUT_SECONDS=15.0
IPO_ALLOTMENT_CATALOGUE_TTL_SECONDS=300
```

---

## Local Verification

Start the development server:

```bash
uv run fastapi dev
```

1. Open `http://127.0.0.1:8000/allotment` in a browser.
2. Verify that the **Company or IPO** select box populates with issues across all four registrars.
3. Test search filtering by typing a company name in the filter input.
4. Select a Bigshare issue and verify that the CAPTCHA image panel appears. Click **New image** to confirm CAPTCHA challenge refreshes.
5. Enter an invalid PAN (for example, `ABC123`) and verify that the form validation stops submission before hitting the network.

---

## Behavioral Contract Testing

`tests/test_allotment.py` exercises the public API and provider HTTP boundaries with a mocked transport. Minimal upstream responses are kept beside the tests so a failing case points to one broken product contract. The tests do not contact registrar networks.

Run the allotment tests:

```bash
uv run pytest tests/test_allotment.py
```

The focused cases protect these contracts:

- catalogue aggregation and a KFintech lookup through the public API;
- MUFG session-token encryption at the upstream HTTP boundary;
- Purva CSRF forwarding and result values;
- Bigshare's human CAPTCHA flow and pending-result handling;
- invalid PAN rejection without echoing the value;
- schema drift reported as `upstream_changed` instead of `not_found`;
- isolation of one failed catalogue and no retry after consuming a CAPTCHA.

---

## Live Catalogue Smoke Check (No Applicant Identifiers)

To verify upstream connectivity without transmitting applicant PANs or personal identifiers, query the public issue catalogue endpoints:

### 1. Check Combined Catalogue and Provider Health

```bash
curl -s http://localhost:8000/v1/allotment/issues | jq .
```

Confirm that `providers` reports `catalogue_available: true` for active registrars.

### 2. Check Individual Registrars

```bash
# KFintech
curl -s 'http://localhost:8000/v1/allotment/issues?provider=kfintech' | jq .issues[:3]

# MUFG Intime
curl -s 'http://localhost:8000/v1/allotment/issues?provider=mufg' | jq .issues[:3]

# Bigshare
curl -s 'http://localhost:8000/v1/allotment/issues?provider=bigshare' | jq .issues[:3]

# Purva Sharegistry
curl -s 'http://localhost:8000/v1/allotment/issues?provider=purva' | jq .issues[:3]
```

### 3. Check Bigshare CAPTCHA Issuance

```bash
BIGSHARE_ISSUE=$(curl -s 'http://localhost:8000/v1/allotment/issues?provider=bigshare' | jq -r '.issues[0].issue_id')

curl -s -X POST http://localhost:8000/v1/allotment/challenge \
  -H 'Content-Type: application/json' \
  -d "{\"issue_id\": \"$BIGSHARE_ISSUE\"}" | jq '{outcome, challenge: {token: .challenge.token, has_image: (.challenge.image_data_uri != null)}}'
```

*Never use real applicant PANs in automated smoke check scripts or CI pipelines.*

---

## Upstream Change Troubleshooting

When a registrar modifies its frontend or API, lookups return `outcome="upstream_changed"` or `provider_statuses()` marks `catalogue_available: false`.

### Diagnostic Workflow

1. **Check Application Logs**:
   Look for warnings from `AllotmentProvider._guard` in the server output:
   ```text
   Allotment request parser failed: provider=kfintech error=UpstreamChanged
   ```
2. **Inspect the Registrar Website**:
   Open the registrar's official status page in a browser with Developer Tools open (Network tab):
   - **KFintech**: Check whether `main.<hash>.js` has been renamed, or if the issue list format inside `JSON.parse('...')` has changed. Verify if the API Gateway host (`0uz601ms56.execute-api.ap-south-1.amazonaws.com`) is still targeted.
   - **MUFG Intime**: Check whether `GetDetails` or `generateToken` WebMethods have changed endpoints, or if the AES encryption key/IV in client scripts has been updated.
   - **Bigshare**: Inspect `select#ddlCompany` on `ipo_status.html` and verify keys in `POST /Data.aspx/FetchIpodetails`.
   - **Purva Sharegistry**: Check if `select[name=company_id]` or Django CSRF input names have changed. If querying an allotment, check whether the result table structure has been modified.
3. **Capture a Minimal Sanitized Response**:
   Reduce the changed HTML, JS, or JSON to the smallest response that reproduces the contract. Remove all applicant information.
4. **Update the Provider Adapter**:
   Adjust parsing logic in `src/rhp_analyzer/allotment/<provider>.py`.
5. **Verify Tests**:
   Extend the existing focused case in `tests/test_allotment.py`, then run `uv run pytest tests/test_allotment.py`.

---

## Adding a Provider

Follow these steps to add a new IPO registrar adapter:

1. **Add Provider Enum**:
   In `src/rhp_analyzer/api_schemas.py`, add the registrar key to `ProviderName`:
   ```python
   class ProviderName(StrEnum):
       ...
       NEWREGISTRAR = "newregistrar"
   ```
2. **Implement the Adapter**:
   Create `src/rhp_analyzer/allotment/newregistrar.py`. Subclass `AllotmentProvider` (`base.py`) and implement:
   - `name`: Matches `ProviderName.NEWREGISTRAR`.
   - `label`: Human-readable display label.
   - `status_page_url`: Official public status page URL.
   - `allowed_hosts`: Frozenset of valid domain names for requests and redirects.
   - `list_issues(self)`: Discovers current issues.
   - `_lookup(self, issue, pan, ...)`: Executes lookup and parses response into normalized `AllotmentLookupResult`.
   - `start_challenge(self, issue)`: (Optional) If the registrar requires a CAPTCHA.
3. **Register the Adapter**:
   In `src/rhp_analyzer/allotment/service.py`, import the adapter and add it to `build_providers`:
   ```python
   from .newregistrar import NewRegistrarProvider

   def build_providers(client_factory: ClientFactory) -> list[AllotmentProvider]:
       return [
           ...
           NewRegistrarProvider(client_factory),
       ]
   ```
4. **Add Contract Coverage**:
   Extend the mocked registrar boundary in `tests/test_allotment.py`. Cover the hot path and one meaningful failure that would otherwise be unprotected.

---

## Deployment Notes

### Reverse Proxy Rate Limiting

The `/v1/allotment/lookup` and `/v1/allotment/challenge` routes are unauthenticated to allow seamless public access from web pages and bots. Because lookups query upstream registrar infrastructure synchronously, protect the application with rate limiting at your reverse proxy (such as nginx or Caddy).

Example nginx rate-limiting configuration:

```nginx
# Rate limit allotment queries per client IP
limit_req_zone $binary_remote_addr zone=allotment_lookup:10m rate=5r/m;
limit_req_zone $binary_remote_addr zone=allotment_challenge:10m rate=10r/m;

location = /v1/allotment/lookup {
    limit_req zone=allotment_lookup burst=3 nodelay;
    proxy_pass http://unix:/home/ubuntu/rhp-analyzer/data/gunicorn.sock;
}

location = /v1/allotment/challenge {
    limit_req zone=allotment_challenge burst=5 nodelay;
    proxy_pass http://unix:/home/ubuntu/rhp-analyzer/data/gunicorn.sock;
}
```

### Response Caching Prohibition

Every allotment route emits `Cache-Control: no-store`. Ensure that edge proxies, CDNs, or downstream caching tiers do not override this header or cache lookup results.

### Resource Independence

The allotment subsystem does not share queues, SQLite transactions, or worker limits with PDF processing:
- Allotment lookups execute asynchronously within FastAPI's event loop.
- It does not consume slots from `RHP_JOB_CONCURRENCY` or `RHP_SECTION_CONCURRENCY`.
- SQLite database locks and PDF processing delays do not affect allotment query response times.
