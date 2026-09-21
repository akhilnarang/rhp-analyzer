# Registrar Provider Adapters

This document details the interface contracts, request flows, response parsing, and known behaviors for all supported IPO registrars.

The registrars do not provide public developer APIs. Each adapter interacts with undocumented public web page contracts discovered by inspecting browser behavior. When a registrar updates its frontend or backend, the corresponding adapter may require maintenance.

---

## KFintech

- **Provider Key**: `kfintech`
- **Provider Label**: `KFintech`
- **Official Status Page**: `https://ipostatus.kfintech.com/`
- **Legacy Status Page**: `https://ris.kfintech.com/ipostatus/` (contains migration notice to current domain)
- **Allowed Hosts**: `ipostatus.kfintech.com`, `0uz601ms56.execute-api.ap-south-1.amazonaws.com`

### Issue Discovery

- **Upstream Implementation**: The KFintech landing page (`https://ipostatus.kfintech.com/`) serves an HTML shell that loads a hashed JavaScript bundle named `main.<hash>.js`. The issue catalogue is embedded directly inside this script bundle as an escaped JSON string passed to `JSON.parse('...')`.
- **Adapter Behavior**:
  1. GET `https://ipostatus.kfintech.com/`.
  2. Scans the HTML using regular expression `(?:src=["'])?([^"'<>]*main\.[0-9a-f]+\.js)` to locate the active JavaScript bundle.
  3. GET the bundle URL.
  4. Scans the bundle text for `JSON.parse('...')` string literals containing `clientId`.
  5. Unescapes `\'` characters and parses the literal as JSON without evaluating the JavaScript.
  6. Reads each row for `clientId`, `name`, and optional `closeDate` or `close_date`.
  7. Assigns internal issue IDs formatted as `kfintech:<clientId>`.

### Lookup Request Contract

- **Upstream Implementation**: The single-page application dispatches lookups to an AWS API Gateway endpoint using HTTP request headers instead of query parameters or a POST body.
- **Adapter Contract**:
  - **Method**: `GET`
  - **URL**: `https://0uz601ms56.execute-api.ap-south-1.amazonaws.com/prod/api/query?type=pan`
  - **Headers**:
    - `reqparam`: `<PAN>`
    - `client_id`: `<clientId>`
    - `Accept`: `application/json`
    - `User-Agent`: `RHP-Analyzer/0.1 (best-effort allotment status lookup)`
  - **Body**: None.
  - *(Note: Upstream also supports `type=appno` with `reqparam=<application_no>|<PAN>` and `type=dpclid` for demat IDs; our adapter uses PAN mode).*

### Response Parsing

- **Upstream Format**: Returns a JSON object with a `data` list:
  ```json
  {
    "data": [
      {
        "Appln_No": "12345678",
        "Name": "FIRSTNAME LASTNAME",
        "DP_CLID": "1208160012345678",
        "Pan_No": "ABCDE1234F",
        "App_Shares": "100",
        "All_Shares": "50",
        "category": "IND"
      }
    ]
  }
  ```
- **Adapter Rules**:
  - If `data` is empty: Maps to `not_found`.
  - If `data` contains rows, checks for recognized fields (`Appln_No`, `Name`, `DP_CLID`, `Pan_No`, `App_Shares`, `All_Shares`, `category`). If no recognized fields exist, raises `UpstreamChanged`.
  - Parses `App_Shares` and `All_Shares` using strict integer parsing (accepts plain integers and integral floats like `"50.0"`; rejects non-integral floats).
  - Outcome determination:
    - If any record has `All_Shares > 0`: Returns `allotted` with `allocated_shares` set to the sum of positive allotments.
    - If all records have `All_Shares == 0`: Returns `not_allotted`.
    - Otherwise: Returns `pending`.

### Session, Token, and CAPTCHA Requirements

- Stateless. No cookies, sessions, CSRF tokens, or CAPTCHAs are required.

### Retries and Redirects

- **Observed Upstream Behavior**: The KFintech frontend client retries HTTP 429, 500, 502, and 504 responses up to five times with exponential backoff and jitter.
- **Adapter Behavior**:
  - Uses `retry_transient=True` on both bundle fetches and lookup GETs.
  - Bounded to 1 retry (2 total attempts) with a 100ms initial sleep upon `httpx.TransportError` or HTTP 500, 502, 503, 504.
  - HTTP 429 raises `ProviderRateLimited` immediately without retrying.
  - Redirects are restricted to `allowed_hosts` with a maximum of 4 hops.

### Known Limitations

- If KFintech shifts the issue list from the compile-time bundle to a dynamic API or alters the bundle name pattern, catalogue loading will raise `UpstreamChanged`.
- The AWS API Gateway host (`0uz601ms56.execute-api.ap-south-1.amazonaws.com`) is hardcoded in the adapter. If KFintech migrates gateways, the lookup endpoint must be updated.

---

## MUFG Intime / Link Intime

- **Provider Key**: `mufg`
- **Provider Label**: `MUFG / Link Intime`
- **Official Status Page**: `https://in.mpms.mufg.com/Initial_Offer/public-issues.html`
- **Allowed Hosts**: `in.mpms.mufg.com`

### Issue Discovery

- **Upstream Implementation**: The landing page issues an ASP.NET WebMethod POST to retrieve company details.
- **Adapter Behavior**:
  - **Method**: `POST`
  - **URL**: `https://in.mpms.mufg.com/Initial_Offer/IPO.aspx/GetDetails`
  - **Headers**: `Content-Type: application/json; charset=utf-8`, `Accept: application/json`
  - **Body**: `{}`
  - The response payload contains JSON field `d`, which holds an XML document (`<NewDataSet><Table>...</Table></NewDataSet>`).
  - The adapter inspects the XML to block DTD/entity injection, parses `<Table>` elements, and extracts `company_id`, `companyname`, and optional close dates (`close_date`, `closedate`, `issue_close_date`, `CLOSE_DATE`).
  - Assigns internal issue IDs formatted as `mufg:<company_id>`.

### Lookup Request Contract

- **Upstream Implementation**: Browser lookups execute a four-step sequence within a single HTTP session:
  1. `POST /Initial_Offer/IPO.aspx/GetDetails` to establish the `ASP.NET_SessionId` cookie.
  2. `POST /Initial_Offer/IPO.aspx/generateToken` with an empty JSON body `{}` to obtain a numeric string in field `d`.
  3. Client-side encryption: The page runs CryptoJS AES-128-CBC encryption with PKCS7 padding, using UTF-8 key `8080808080808080` and IV `8080808080808080`. The serialized `CipherParams` becomes a Base64-encoded ciphertext string.
  4. `POST /Initial_Offer/IPO.aspx/SearchOnPan` sending the encrypted token.
- **Adapter Contract**:
  - Step 1: POST `GetDetails` with body `{}` using a single `httpx.AsyncClient` cookie jar.
  - Step 2: POST `generateToken` with body `{}`, reading token string `d`.
  - Step 3: Generates Base64 AES-CBC ciphertext using Python's `cryptography` library (`mufg_token`).
  - Step 4:
    - **Method**: `POST`
    - **URL**: `https://in.mpms.mufg.com/Initial_Offer/IPO.aspx/SearchOnPan`
    - **Headers**: `Content-Type: application/json; charset=utf-8`, `Accept: application/json`
    - **Body**:
      ```json
      {
        "clientid": "<provider_issue_id>",
        "PAN": "<PAN>",
        "IFSC": "",
        "CHKVAL": "1",
        "token": "<base64_encrypted_token>"
      }
      ```
      (`CHKVAL="1"` specifies PAN lookup mode; other upstream modes include `"2"` for application number, `"3"` for DP/client ID, and `"4"` for bank account with IFSC.)

### Response Parsing

- **Upstream Format**: The response payload is JSON containing an XML string in field `d`.
- **Adapter Rules**:
  - Inspects XML tags named `msg` or `message`:
    - If the message contains `"captcha"` (case-insensitive): Returns `challenge_required`.
    - If the message matches no-record markers (`"no record"`, `"record not found"`, `"not found"`, `"invalid pan"`, `"no data"`, `"does not exist"`, `"no details"`, `"enter valid"`): Returns `not_found`.
    - Any other message raises `UpstreamResponseError` (surfaced as `upstream_error`).
  - Parses `<Table>` row elements:
    - Mapped fields: name (`NAME1`, `Name`, `NAME`), application number (`APPNO`, `APP_NO`, `ApplicationNo`, `APPLICATION_NO`), applied shares (`SHARES`, `Shares`, `App_Shares`), allotted shares (`ALLOT`, `Allot`, `Allotted`), remarks (`PEMNDG`, `RFNDAMT`, `Remarks`).
    - If no tables match known fields: returns `not_found` if no tables exist, or raises `UpstreamChanged` if tables with unfamiliar tags exist.
  - Outcome determination:
    - Any positive `allotted_shares`: Returns `allotted` with total `allocated_shares`.
    - All records have `allotted_shares == 0`: Returns `not_allotted`.
    - Otherwise: Returns `pending`.

### Session, Token, and CAPTCHA Requirements

- The `ASP.NET_SessionId` cookie must be retained across `GetDetails`, `generateToken`, and `SearchOnPan`.
- Token derivation requires AES-128-CBC encryption of the server-issued token.
- **CAPTCHA Observation**: The served public page contains commented-out CAPTCHA markup and validation scripts (`CaptchaImage.aspx/CheckCaptcha`). The adapter treats standard lookups as unattended. However, if the server returns an XML message referencing a CAPTCHA, the adapter returns `challenge_required`.

### Retries and Redirects

- Setup calls (`GetDetails`, `generateToken`) and catalogue listings enable transient retries (`retry_transient=True`).
- `SearchOnPan` lookup queries run with `retry_transient=False` (no automatic retry).
- Redirects are restricted to `in.mpms.mufg.com` with a maximum of 4 hops.

### Known Limitations

- High request concurrency from cloud IPs may trigger Akamai edge bot-protection challenges.
- Upstream relies on a static AES encryption key hardcoded in the frontend. If MUFG updates this key or IV, lookups will fail until the cipher settings are updated.

---

## Bigshare Services

- **Provider Key**: `bigshare`
- **Provider Label**: `Bigshare`
- **Official Status Page**: `https://ipo.bigshareonline.com/ipo_status.html`
- **Alternate Status Pages**: `https://ipo1.bigshareonline.com/ipo_status.html`, `https://ipo2.bigshareonline.com/ipo_status.html`
- **Allowed Hosts**: `ipo.bigshareonline.com`, `ipo1.bigshareonline.com`, `ipo2.bigshareonline.com`

### Issue Discovery

- **Upstream Implementation**: The status page provides a standard dropdown `<select id="ddlCompany">`. Stale or inactive issues are frequently left inside HTML comments.
- **Adapter Behavior**:
  - GET `https://ipo.bigshareonline.com/ipo_status.html`.
  - Parses `<option>` tags under `select#ddlCompany` using `HTMLParser`. HTML comments are discarded automatically.
  - Assigns internal issue IDs formatted as `bigshare:<company_id>`.
  - Flags every issue with `requires_challenge=True`.

### CAPTCHA Challenge Contract

- **Upstream Implementation**: The frontend fetches a challenge image and token via `GET /Captcha.ashx`.
- **Adapter Contract**:
  - **Method**: `GET`
  - **URL**: `https://ipo.bigshareonline.com/Captcha.ashx`
  - **Headers**: `User-Agent: RHP-Analyzer/0.1 (best-effort allotment status lookup)`
  - Returns JSON containing `token` and `image` (base64 string or data URI).
  - Adapter normalizes image into `data:image/png;base64,...` and returns `AllotmentChallenge`.
  - The client displays the image to the applicant. Automated CAPTCHA solving is intentionally not implemented.

### Lookup Request Contract

- **Upstream Implementation**: Browser sends an ASP.NET WebMethod POST with human CAPTCHA input.
- **Adapter Contract**:
  - **Method**: `POST`
  - **URL**: `https://ipo.bigshareonline.com/Data.aspx/FetchIpodetails`
  - **Headers**:
    - `Content-Type`: `application/json; charset=utf-8`
    - `Accept`: `application/json`
    - `Referer`: `https://ipo.bigshareonline.com/ipo_status.html`
  - **Body**:
    ```json
    {
      "Applicationno": "",
      "Company": "<provider_issue_id>",
      "SelectionType": "PN",
      "PanNo": "<PAN>",
      "txtcsdl": "",
      "txtDPID": "",
      "txtClId": "",
      "ddlType": "0",
      "lang": "en",
      "CaptchaToken": "<captcha_token>",
      "CaptchaAnswer": "<captcha_answer>",
      "ResultToken": ""
    }
    ```
    (`SelectionType="PN"` specifies PAN mode; `"AP"` represents application number, and `"BN"` represents beneficiary details.)

### Response Parsing

- **Upstream Format**: Response returns JSON (either directly or wrapped in `d`).
- **Adapter Rules**:
  - Unwraps `d` if present and parses embedded JSON strings.
  - Inspects upstream `Status` field:
    - `"OK"`: Inspects `Records` array or root applicant fields (`APPLICATION_NO`, `Name`, `DPID`, `Category`, `APPLIED`, `ALLOTED`):
      - If allotted shares > 0: Returns `allotted` with total `allocated_shares`.
      - If allotted shares == 0 or remarks text matches negative markers: Returns `not_allotted`.
      - If records exist without numeric quantities: Returns `pending`.
      - If records list is empty: Returns `not_found`.
    - `"NOTFOUND"`: Returns `not_found`.
    - `"CAPTCHA"`: Returns `challenge_required` (indicates an incorrect CAPTCHA answer; prompt user for a new challenge).
    - `"RATELIMIT"`: Returns `rate_limited`.
    - `"WARMING"`, `"PROCESSING"`, `"INITIALIZING"`: Returns `provider_unavailable` (upstream is still preparing allotment files).
  - Legacy Sentinel Fallback: If `Status` is absent, the adapter checks string values for sentinel phrases (`"please enter valid pan"`, `"invalid pan"`, `"no record found"`, `"record not found"`, `"not found"`). If found, returns `not_found`.
  - Unrecognized status values raise `UpstreamChanged`.

### Session, Token, and CAPTCHA Requirements

- Every single lookup requires a fresh, unconsumed CAPTCHA token and answer. Tokens cannot be reused across multiple requests.

### Retries and Redirects

- Catalogue fetches and CAPTCHA generation enable transient retries (`retry_transient=True`).
- Lookup queries set `retry_transient=False`. Single-use CAPTCHA tokens are never retried automatically.
- Redirects are restricted to `allowed_hosts` (`ipo.bigshareonline.com`, `ipo1.bigshareonline.com`, `ipo2.bigshareonline.com`) with a maximum of 4 hops.

### Known Limitations

- Automated, unattended lookups cannot be performed against Bigshare due to the mandatory visual CAPTCHA requirement.

---

## Purva Sharegistry

- **Provider Key**: `purva`
- **Provider Label**: `Purva Sharegistry`
- **Official Status Page**: `https://www.purvashare.com/investor-service/ipo-query`
- **Allowed Hosts**: `www.purvashare.com`, `purvashare.com`

### Issue Discovery

- **Upstream Implementation**: A Django server-rendered page providing `<select name="company_id">`.
- **Adapter Behavior**:
  - GET `https://www.purvashare.com/investor-service/ipo-query`.
  - Parses `<option>` tags under `select[name=company_id]` using `HTMLParser`.
  - Assigns internal issue IDs formatted as `purva:<company_id>`.
  - If no active companies are present, returns an empty issue list. This is normal between allotment cycles.

### Lookup Request Contract

- **Upstream Implementation**: Standard Django HTML form POST protected by CSRF middleware.
- **Adapter Contract**:
  - Step 1: GET `https://www.purvashare.com/investor-service/ipo-query` using an `httpx.AsyncClient` cookie jar to capture the `csrftoken` cookie.
  - Step 2: Extracts the hidden input `<input type="hidden" name="csrfmiddlewaretoken" value="...">`.
  - Step 3:
    - **Method**: `POST`
    - **URL**: `https://www.purvashare.com/investor-service/ipo-query`
    - **Headers**:
      - `Content-Type`: `application/x-www-form-urlencoded`
      - `Referer`: `https://www.purvashare.com/investor-service/ipo-query`
    - **Form Body**:
      ```text
      csrfmiddlewaretoken=<token>
      company_id=<provider_issue_id>
      applicationNumber=
      panNumber=<PAN>
      ```

### Response Parsing

- **Upstream Format**: Server-rendered HTML document containing an informational table.
- **Adapter Rules**:
  - Parses HTML table rows into `(label, value)` pairs using `HTMLParser`.
  - Matches row labels against known variants (case-insensitive, normalized):
    - Name: `name`, `applicant name`, `investor name`, `shareholder name`
    - Application number: `application no`, `application number`, `appl no`, `appln no`, `applicationno`
    - DP ID: `dp id`, `dp_id`, `dpid`, `dpclid`, `dp client id`, `beneficiary id`
    - Category: `category`, `applicant category`, `investor category`
    - Applied shares: `applied`, `shares applied`, `application shares`, `no of shares applied`, `applied shares`
    - Allotted shares: `allotted`, `allotment`, `shares allotted`, `allotted shares`, `no of shares allotted`, `allotment shares`
    - Status text: `status`, `allotment status`, `allot status`, `result`
  - Sentinel checks: Inspects status text and rendered visible text for negative markers (`"invalid pan"`, `"please enter valid"`, `"no record"`, `"record not found"`, `"not found"`, `"no data"`). If present, returns `not_found`.
  - If neither table rows nor sentinel text are recognized, raises `UpstreamChanged`.
  - Outcome determination:
    - If `allotted_shares > 0`: Returns `allotted` with total `allocated_shares`.
    - If `allotted_shares == 0` or status text explicitly says the application was not allotted or was rejected: Returns `not_allotted`.
    - Otherwise: Returns `pending`.

### Session, Token, and CAPTCHA Requirements

- Requires Django CSRF synchronization: the `csrftoken` cookie and the `csrfmiddlewaretoken` form field must match.
- No CAPTCHA is currently enforced.

### Retries and Redirects

- Initial GET requests for catalogue listing and CSRF tokens enable transient retries (`retry_transient=True`).
- Form POST queries run with `retry_transient=False`.
- Redirects are restricted to `www.purvashare.com` and `purvashare.com` with a maximum of 4 hops.

### Known Limitations

- Purva frequently empties the active issue list between IPO allotment cycles, leaving an empty dropdown until a new allotment goes live.
- Any reorganization of table row labels or structural markup changes will trigger `upstream_changed`.
