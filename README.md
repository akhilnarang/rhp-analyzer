# RHP Analyzer

Use this project to analyze Indian IPO RHP and DRHP files.

The service extracts facts from each PDF. It gives the PDF page for each fact. It also makes an investment report.

## Set the environment variables

Copy `.env.example` to `.env`.

Set this required value:

```dotenv
OPENAI_API_KEY=your-openai-key
```

`OPENAI_API_KEY` is the only required setting. The service uses `https://api.openai.com/v1` by default.

Set one or more bearer tokens when the service is public:

```dotenv
RHP_API_TOKENS=first-token,second-token
```

Separate multiple tokens with commas. If this setting is empty, the analysis request does not require a token.

The application loads the key as a masked Pydantic secret. Git does not track the `.env` file.

## Start the API in development mode

Run this command:

```bash
uv run fastapi dev
```

The API starts at `http://127.0.0.1:8000`. The API documents are at `http://127.0.0.1:8000/docs`.

Open `http://127.0.0.1:8000/` for the home page. Open `http://127.0.0.1:8000/analysis` for the latest analysis jobs.

The public pages use Jinja templates and the [Oat stylesheet](https://oat.ink/). Small project-specific rules are in `src/rhp_analyzer/static/site.css`.

For production, run this command:

```bash
uv run fastapi run
```

Put an HTTPS proxy in front of the production service.

## Run the user service

The production unit is in `deploy/rhp-analyzer.service`. It uses this path:

```text
/home/ubuntu/rhp-analyzer
```

It creates this Unix socket for nginx:

```text
/home/ubuntu/rhp-analyzer/data/gunicorn.sock
```

Install and start the unit with these commands:

```bash
install -Dm644 deploy/rhp-analyzer.service \
  ~/.config/systemd/user/rhp-analyzer.service
systemctl --user daemon-reload
systemctl --user enable --now rhp-analyzer.service
```

## Analyze a PDF or ZIP

Send a PDF or ZIP to `POST /v1/analyze`.

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -F 'file=@RHP_LAPL_29072026.PDF;type=application/pdf' \
  -F 'sections=all'
```

You can add optional market data. The service sends these values to the final report step:

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -F 'file=@RHP_LAPL_29072026.PDF;type=application/pdf' \
  -F 'sections=all' \
  -F 'lot_size=1200' \
  -F 'price=₹94' \
  -F 'issue_size=₹32.40 Cr' \
  -F 'gmp=₹39' \
  -F 'gmp_percent=41.49%' \
  -F 'open_date=06 Aug 2026' \
  -F 'close_date=10 Aug 2026' \
  -F 'allotment_date=11 Aug 2026' \
  -F 'subscription=258.4x' \
  -F 'qib_subscription=94.62x' \
  -F 'nii_subscription=303.49x' \
  -F 'snii_subscription=239.34x' \
  -F 'bnii_subscription=335.56x' \
  -F 'rii_subscription=276.53x'
```

You can also send `employee_subscription`. Each market field is optional. Each value has a 100-character limit. The report labels these values as user-provided market data. It does not present them as PDF facts.

You can send a public PDF or ZIP URL instead of a file:

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -F 'url=https://www.bseindia.com/downloads/ipo/Final%20Fund%20Raising%20Document_LOLT_060820262008.pdf' \
  -F 'sections=all'
```

Send either `file` or `url`. Do not send both fields in one request.

The service accepts an HTTP or HTTPS PDF or ZIP URL from any public internet host. It checks the address for the first request and for every redirect. It blocks local, private, link-local, reserved, and metadata-service addresses. It pins each connection to a checked public address.

For a ZIP, the service checks every archive entry before extraction. It rejects unsafe paths, duplicate paths, links, special files, encrypted files, too many entries, too many PDFs, large expanded sizes, and unsafe compression ratios. It then extracts each PDF to a temporary file inside a Bubblewrap sandbox. It does not write archive member paths to the file system.

The service checks the first pages of every PDF. It identifies an RHP, DRHP, or final prospectus from the document title and offer text. It does not trust the ZIP path or PDF filename. It prefers one RHP over a final prospectus or DRHP. It rejects the ZIP when no offer document is found or when several documents have the same highest priority.

Some document sites block cloud-server downloads. The API returns `424 Failed Dependency` when the source site refuses or fails the download. Upload the PDF file in this case.

The API calculates the checksum and creates a job. It then returns `202 Accepted` without waiting for the analysis.

```json
{
  "url": "http://localhost:8000/analysis/company-name"
}
```

The public URL uses a cleaned form of the PDF filename. For example,
`Molbio Diagnostics Limited - Red Herring Prospectus.PDF` becomes
`/analysis/molbio-diagnostics-limited`. If two analyses need the same slug, the
newer one gets a short hash suffix. Existing hash URLs redirect to the public URL.

The response also has a `Location` header with the same URL. A Telegram bot can send this URL to the user at once.

Open the URL in a browser. The page shows the number of completed sections. It gets new status data every two seconds. It shows the report when the analysis is complete.

## Use Telegram Instant View

Completed analysis pages contain a static article with a separate title, description, date, and body. The progress and failure pages do not contain this article.

Paste `deploy/telegram-instant-view.txt` into the [Telegram Instant View Editor](https://instantview.telegram.org/). Save the template and copy its `rhash` value.

Send the normal analysis URL while the job runs. After the status API returns `completed`, send or edit the message to use this URL:

```text
https://t.me/iv?url=ENCODED_ANALYSIS_URL&rhash=YOUR_TEMPLATE_RHASH
```

Do not send the Instant View URL before completion. Telegram caches Instant View pages, and a running analysis is not a static article.

If Telegram approves the template for the public domain, ordinary analysis links can show the Instant View button without the `t.me/iv` wrapper.

The service builds the public URL from the request host, scheme, and proxy headers. Configure the HTTPS proxy to send the correct forwarded headers.

Get the job status with this request:

```bash
curl --fail http://localhost:8000/v1/analyses/ANALYSIS_ID/status
```

Get the complete JSON analysis with this request:

```bash
curl --fail http://localhost:8000/v1/analyses/ANALYSIS_ID
```

Only `POST /v1/analyze` uses bearer-token authentication. The report pages, analysis list, status API, result API, documentation, and health endpoint remain public. This lets a Telegram user open an analysis URL without a token.

## Source limits

The source must contain PDF or ZIP data. A ZIP must contain one content-verified offer document. The route also accepts the optional market fields shown above. The PDF extraction does not use this data. Only the final report uses it.

The report uses only facts from the PDF. It identifies a value as not available when the PDF does not contain it.

The default source and PDF size limit is 50,000,000 bytes. Set `RHP_MAX_PDF_BYTES` to change this limit. A ZIP can contain up to 1,000 entries and 20 PDFs. Its total expanded size cannot exceed five times the PDF limit.

The service runs ZIP extraction and PDF parsing in Bubblewrap sandboxes. These processes have no network access. They cannot read the `.env` file or application files. Each process can read only system libraries and its current input file. It sees a new minimal device directory. CPU, memory, output-size, file, process, and time limits also apply. The production systemd unit blocks privilege changes and limits memory and task use. The service runs as the unprivileged deployment user.

## Cache operation

The service calculates the SHA-256 checksum of the selected PDF. It uses SQLite to store jobs, progress, section records, and reports.

The default database file is `data/rhp-cache.sqlite3`. Set `RHP_DATABASE_PATH` to use a different file.

The extraction cache key contains these values:

- PDF checksum.
- Model name.
- Retry limit.
- Selected sections.
- Extraction prompt version.

The report cache key also contains the report model, report prompt version, and optional market data.

The service returns the same analysis URL after a full cache hit. It does not call the model again.

The service stores a queued PDF in `data/uploads/`. It deletes the PDF after the job completes or fails. SQLite does not contain the PDF data.

The service keeps a queued PDF during a normal restart. It starts the queued job when the service starts again.

The development server logs each analysis stage. It logs the upload, cache checks, section extraction, report generation, and completion.

The service extracts up to four sections at the same time. Set `RHP_SECTION_CONCURRENCY` to change this limit. Use a value from 1 to 10.

The service processes one PDF job at a time. Set `RHP_JOB_CONCURRENCY` to change this limit. Keep the value at 1 unless the proxy can handle more calls.

Use one application worker with SQLite. Multiple application workers can start the same queued job. Use PostgreSQL and a separate job queue before you add more workers.

## Inspect a PDF without an API call

Run this command:

```bash
uv run rhp-analyzer inspect *.PDF *.pdf
```

This command uses `pdftotext`. It finds the main sections and estimates the text token count. It does not call a model.

## Run a benchmark

First, run a test with a small set of sections:

```bash
uv run rhp-analyzer benchmark RHP_LAPL_29072026.PDF \
  --model gpt-5.6-terra \
  --sections offer,financial_summary,basis_for_price
```

Run all sections after you review the first result:

```bash
uv run rhp-analyzer benchmark *.PDF *.pdf --model gpt-5.6-terra
```

The command writes JSON files to `benchmark-results/`. Each file contains usage data and evidence checks.

The program does not send a whole PDF if its estimated text is more than 500,000 tokens. PDF page images can add more tokens.

## Design

Use `gpt-5.6-terra` for the first extraction. Use `gpt-5.6-sol` only when a section is not clear.

The service processes each main section separately. It checks each evidence quote against the specified PDF page. It then makes the report from the checked records.

This method uses fewer tokens than one whole-PDF request. It also makes retries easier.

See the [Terra model page](https://developers.openai.com/api/docs/models/gpt-5.6-terra) and the [file input guide](https://developers.openai.com/api/docs/guides/file-inputs).

The active report instructions are in `src/rhp_analyzer/synthesis.py`.
