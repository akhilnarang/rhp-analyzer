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
/home/ubuntu/rhp-analyzer/gunicorn.sock
```

Install and start the unit with these commands:

```bash
install -Dm644 deploy/rhp-analyzer.service \
  ~/.config/systemd/user/rhp-analyzer.service
systemctl --user daemon-reload
systemctl --user enable --now rhp-analyzer.service
```

## Analyze a PDF

Send a PDF to `POST /v1/analyze`.

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -F 'file=@RHP_LAPL_29072026.PDF;type=application/pdf' \
  -F 'sections=all'
```

You can send a public PDF URL instead of a file:

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -F 'url=https://www.bseindia.com/downloads/ipo/Final%20Fund%20Raising%20Document_LOLT_060820262008.pdf' \
  -F 'sections=all'
```

Send either `file` or `url`. Do not send both fields in one request.

The default URL host list contains `www.bseindia.com` and `bseindia.com`. Set `RHP_ALLOWED_PDF_HOSTS` to use a different comma-separated list. The service checks each redirect against this list. This rule prevents the public API from fetching private network data.

The API calculates the checksum and creates a job. It then returns `202 Accepted` without waiting for the analysis.

```json
{
  "url": "http://localhost:8000/analysis/ANALYSIS_ID"
}
```

The response also has a `Location` header with the same URL. A Telegram bot can send this URL to the user at once.

Open the URL in a browser. The page shows the number of completed sections. It gets new status data every two seconds. It shows the report when the analysis is complete.

The service builds the public URL from the request host, scheme, and proxy headers. Configure the HTTPS proxy to send the correct forwarded headers.

Get the job status with this request:

```bash
curl --fail http://localhost:8000/v1/analyses/ANALYSIS_ID/status
```

Get the complete JSON analysis with this request:

```bash
curl --fail http://localhost:8000/v1/analyses/ANALYSIS_ID
```

The API does not require an application bearer token. Control access at the network or HTTPS proxy layer if you publish the service.

## Source limits

The upload route accepts PDF data only. It does not accept GMP, subscription, price, or other external market data.

The report uses only facts from the PDF. It identifies a value as not available when the PDF does not contain it.

The default file-size limit is 50,000,000 bytes. Set `RHP_MAX_PDF_BYTES` to change this limit.

## Cache operation

The service calculates the SHA-256 checksum during the upload or download. It uses SQLite to store jobs, progress, section records, and reports.

The default database file is `data/rhp-cache.sqlite3`. Set `RHP_DATABASE_PATH` to use a different file.

The extraction cache key contains these values:

- PDF checksum.
- Model name.
- Retry limit.
- Selected sections.
- Extraction prompt version.

The report cache key also contains the report model and the report prompt version.

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
