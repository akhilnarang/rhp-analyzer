# RHP benchmark results

Test date: 2026-08-12.

The configured proxy reported the costs in this file. Direct OpenAI costs can be different.

## LAPL model test

The test used the same three section windows for each model. The sections were offer, financial summary, and basis for price.

| Model | Requests | Total time | Input tokens | Output tokens | Reported cost | Exact evidence |
|---|---:|---:|---:|---:|---:|---:|
| `gpt-5.6-terra` | 3 | 33.9 s | 50,961 | 3,459 | $0.1689 | 18/21 (85.7%) |
| `gpt-5.6-sol` | 4 | 116.6 s | 72,167 | 6,748 | $0.5410 | 14/20 (70.0%) |

Sol found one more borrowing series. Sol also found a conflict in the source data for the peer table.

Terra was faster and had a lower cost. Terra also made shorter structured output. Use Terra as the first model.

Use Sol if Terra gives an unclear result. Also use Sol if the local evidence check fails.

## Whole PDF test

The test sent the full 12.6 MB LAPL PDF to Terra. The PDF has 360 pages.

| Method | Questions | Requests | Time | Input tokens | Output tokens | Reported cost | Exact evidence |
|---|---:|---:|---:|---:|---:|---:|---:|
| Whole PDF | 29 | 3 | 244.4 s | 2,027,244 | 21,025 | $4.3291 | 41/66 (62.1%) |
| Three section windows | 15 | 3 | 33.9 s | 50,961 | 3,459 | $0.1689 | 18/21 (85.7%) |

The whole-PDF request completed. It needed two retries. The input count includes the repeated and cached input for these retries.

Use section windows as the standard method.

## Terra tests for all PDFs

| Document test | Sections | Requests | Total time | Reported cost | Exact evidence |
|---|---:|---:|---:|---:|---:|
| LAPL first test | 3 | 3 | 33.9 s | $0.1689 | 18/21 (85.7%) |
| LAPL other sections | 4 | 5 | 84.8 s | $0.3531 | 52/94 (55.3%) |
| Dhoot | 7 | 9 | 139.9 s | $0.6296 | 61/115 (53.0%) |
| Meridian | 7 | 9 | 128.9 s | $0.6018 | 68/107 (63.6%) |

Most evidence failures had the correct page number. The model added ellipses between separate table values. Thus, the quote was not one continuous text string.

The analyzer now rejects a quote that contains these added ellipses. A retry can then correct the quote.

After this change, the LAPL financial-summary test had five exact quotes from five quotes. The test used one request. It also found the long-term and short-term borrowing values.

## Decision

- Use `gpt-5.6-terra` with medium reasoning for the first extraction.
- Use `gpt-5.6-sol` to review unclear sections.
- Use `gpt-5.6-luna` only for simple route and health checks.
- Use fixed section windows with physical PDF page markers.
- Extract the data, check the evidence, and retry failed sections.
- Make the report only from checked records.

## Concurrent API test

The test used the full Meridian PDF and four concurrent section calls.

| Stage | Sequential test | Four-call test |
|---|---:|---:|
| Section extraction | 140.0 s | 49.4 s |
| Report | 46.9 s | 44.6 s |
| Complete request | 186.9 s | 94.0 s |

The proxy accepted four concurrent calls. Seven sections used eight extraction requests. One section needed one retry. Report generation used one more request.

Use four concurrent section calls as the default. Process one PDF job at a time unless the proxy has more capacity.
