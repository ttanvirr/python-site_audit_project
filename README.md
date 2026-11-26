# Python Website Audit & Scoring Tool

A reusable command-line auditor for publicly accessible websites. It collects PageSpeed Insights/Lighthouse measurements, optionally queries Chrome UX Report field data, checks real responsive layouts with Playwright, performs a polite same-host crawl, validates HTML through W3C Nu, and reports security/SEO/GEO readiness findings.

The project is designed to make evidence and limitations visible. It does **not** invent values when APIs are unavailable, and it does not treat missing credentials or insufficient CrUX traffic as a score of zero.

## 1. Requirements

- Python 3.10 or newer (tested with Python 3.13).
- Internet access from the machine running the audit.
- Chromium installed for Playwright responsiveness tests.
- Optional API credentials for field data or paid visibility integrations.

## 2. Installation

```bash
# Linux / macOS / WSL
python3 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
# py -m venv .venv
# .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

On Linux hosts that need system browser libraries, use:

```bash
python -m playwright install --with-deps chromium
```

Copy the environment template and edit it only when you have credentials:

```bash
cp .env.example .env
```

PowerShell alternative:

```powershell
Copy-Item .env.example .env
```

The application reads `.env` automatically. Never commit `.env` or paste secrets into source code. For Search Console, add the service-account email to the target property in Search Console and enable the Search Console API in the associated Google Cloud project.

## 3. Run an audit

From the project root:

```bash
python site_audit.py https://techinspection.net/
```

Other examples:

```bash
python site_audit.py https://example.com --pages 20 --output-dir reports
python site_audit.py example.com --pages 10 --timeout 15 --api-timeout 60
python site_audit.py https://example.com --skip-playwright
python site_audit.py https://example.com --skip-pagespeed
python site_audit.py https://example.com --scoring-config scoring.example.json
```

The application accepts a public `http://` or `https://` URL. If the scheme is omitted, HTTPS is assumed. Redirects are followed with a limit, and each destination is checked to prevent requests to localhost/private network addresses. Actual HTTP 4xx/5xx target responses are preserved and scored as site-health failures rather than being mislabeled as DNS/network unavailability. The crawler stays on the target hostname, observes robots.txt for `SiteAuditBot`, crawls at a small delay, and obeys `--pages` (default 20).

### Command-line options

| Option               |             Default | Description                                                   |
| -------------------- | ------------------: | ------------------------------------------------------------- |
| `url`                |            required | Public target URL                                             |
| `--pages`            |                `20` | Maximum same-host HTML pages to crawl (1–500)                 |
| `--output-dir`       |           `reports` | Output parent directory                                       |
| `--timeout`          |                `20` | Website request timeout, seconds                              |
| `--api-timeout`      |                `90` | External API timeout, seconds                                 |
| `--cache-dir`        | `.site_audit_cache` | API response cache directory                                  |
| `--cache-ttl`        |             `86400` | API cache lifetime, seconds                                   |
| `--scoring-config`   |                none | Optional JSON score weights/overrides                         |
| `--enable-paid-apis` |                 off | Explicit opt-in for paid/possibly billable providers          |
| `--enable-ssl-labs`  |                 off | Request a Qualys SSL Labs public TLS assessment               |
| `--geo-query`        |                none | Seed prompt/question for DataForSEO AI visibility; repeatable |
| `--skip-pagespeed`   |                 off | Skip PageSpeed Insights/CrUX requests                         |
| `--skip-playwright`  |                 off | Skip Chromium viewport tests                                  |

### Paid API safety

Paid or potentially billable integrations are not called unless `--enable-paid-apis` is present. DataForSEO additionally requires credentials and at least one `--geo-query`. For example:

```bash
python site_audit.py https://example.com \
  --enable-paid-apis \
  --geo-query "best services for [industry]" \
  --geo-query "how to choose a [service] provider"
```

Review provider pricing and account permissions before enabling this option. The code does not make a paid API call just because a key is present in `.env`.

## 4. Configuration

`.env.example` lists supported environment variables:

| Variable                         | Purpose                                                                       | Needed for basic run?                                           |
| -------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `GOOGLE_PSI_API_KEY`             | PageSpeed Insights API key; recommended for repeated automation               | No; PSI supports requests without a key, subject to lower quota |
| `GOOGLE_CRUX_API_KEY`            | Chrome UX Report real-user field data                                         | No; field data is optional                                      |
| `SECURITY_HEADERS_API_KEY`       | Existing SecurityHeaders.com API key                                          | No; local header checks are used otherwise                      |
| `DATAFORSEO_LOGIN`               | DataForSEO API login                                                          | No; paid, opt-in only                                           |
| `DATAFORSEO_PASSWORD`            | DataForSEO API password                                                       | No; paid, opt-in only                                           |
| `OTTERLY_API_KEY`                | Reserved for a future verified OtterlyAI adapter                              | No; no request is sent in this version                          |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a Google service-account JSON key with Search Console read permission | No; optional                                                    |
| `GSC_PROPERTY_URL`               | Exact Search Console property (URL-prefix or `sc-domain:` property)           | No; optional                                                    |

### Provider notes

- **PageSpeed Insights:** mobile and desktop requests use the official v5 `runPagespeed` REST endpoint and request Lighthouse Performance and SEO categories. A key is recommended for automated use. API responses are cached for the configured TTL.
- **CrUX:** the current CrUX `queryRecord` endpoint requires a Google Cloud API key provisioned for the Chrome UX Report API. Results are reported as real-user field data (75th percentile where supplied); a site/page may not have enough data to return a record.
- **MDN HTTP Observatory:** uses the current API v2 endpoint `https://observatory-api.mdn.mozilla.net/api/v2/scan`. The former Mozilla Observatory v1 endpoint is retired. The service has a per-host cooldown; an API response may be a cached scan. The provider can score above 100; its published letter grade is mapped to this application’s 0–100 scale, while the original provider score and grade remain in the raw report.
- **Qualys SSL Labs:** the v4 assessment is optional because it initiates a public TLS server test and can take some time. `--enable-ssl-labs` enables it. A local certificate validation check still runs as part of Site Health.
- **SecurityHeaders.com:** the API requires an `x-api-key`. Its API key page currently says that new subscriptions are discontinued. If an existing key remains valid, the application uses it only with `--enable-paid-apis`; otherwise local response-header checks and MDN Observatory are used.
- **W3C Nu HTML Checker:** the root HTML is submitted to the official Nu Checker API. If the service is unavailable, HTML validation is N/A rather than failed.
- **DataForSEO AI Optimization:** the documented LLM Mentions `search_mentions/live` endpoint is used only when paid-API opt-in, login/password, and at least one `--geo-query` are supplied. Returned `sources[].url` values are matched to the audited domain. ChatGPT results are currently limited by the provider to United States/English, so this is not a Bangladesh-localized visibility test. The query design matters; a small set of seed topics is not a statistically complete visibility study.
- **OtterlyAI:** the API's current public help material describes Bearer authentication and mentions/citations/visibility data. The actual endpoint schemas were not validated for this implementation, so v1 deliberately does not guess an endpoint or send a request based only on `OTTERLY_API_KEY`.
- **Google Search Console:** when both service-account credentials and an authorized property are configured, a read-only query requests aggregate metrics for the previous 28 complete days. This data is informational and does not change the technical SEO score. The report never claims rankings, backlinks, impressions, or organic traffic when the API is not connected.

## 5. Categories and scoring

Each category has an independent score between 0 and 100 when measurable. Each check records a status (`pass`, `warning`, `fail`, `info`, or `na`), measured value, expected value, source, weight, and recommendation where relevant.

The category score is the **weighted mean of numeric check scores**. A check marked N/A or informational without a score is excluded from the category calculation. Pass/fail/warning scores are defined in the relevant audit module; measured provider scores are normalized only when there is an explicit scale (for example Lighthouse's 0–1 score is converted to 0–100, and MDN Observatory letter grades use a fixed documented mapping). The original provider values are retained in the JSON report.

| Category       | Main evidence                                                                                                                                                                       | How it is scored                                                                                                                                               |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Speed          | Lighthouse Performance mobile/desktop; optional CrUX field metrics                                                                                                                  | Lighthouse scores carry the largest weights; available CrUX p75 metrics add smaller weighted checks. Lab and field values remain separate.                     |
| Responsiveness | Playwright Chromium at 320, 375, 390, 768, 1024, 1280, and 1440 CSS pixels                                                                                                          | Weighted viewport checks for horizontal overflow and images outside parent bounds, plus viewport metadata and a basic navigation landmark check.               |
| Optimization   | Lighthouse audits for image formats/sizing, compression, caching, render-blocking, unused CSS/JS, transfer size, and third-party work; root HTML resource inventory                 | Numeric Lighthouse audit scores and explicit source-level heuristics; intentionally separate from the final performance score.                                 |
| Site Health    | HTTP status, latency, redirects, DNS, HTTPS, local TLS validation/expiry, robots.txt, sitemap, bounded crawl, W3C Nu Checker                                                        | Weighted technical checks; broken internal pages are scored based on observed count within the configured crawl sample.                                        |
| Security       | Local response headers, clickjacking protection, mixed-content references, MDN Observatory; optional SSL Labs/SecurityHeaders                                                       | Weighted individual header controls plus actual external service results where available. This is not a penetration test.                                      |
| SEO            | Lighthouse SEO; titles/descriptions, headings, canonical, indexability, language, alt coverage, Open Graph, JSON-LD, duplicates, broken links                                       | Weighted measurable on-page and crawl checks. Character ranges are explicitly treated as heuristics, not search ranking rules.                                 |
| GEO            | AI crawler robots rules, Organization/LocalBusiness and Schema.org, author/date signals, content hierarchy, Q&A signals, references, static HTML text, informational llms.txt check | GEO Readiness Score from observable technical/content signals. llms.txt presence does not earn score points and is not treated as a guaranteed ranking factor. |

### Grade bands

- 90–100: Excellent
- 80–89: Very Good
- 70–79: Good
- 60–69: Needs Improvement
- 40–59: Poor
- 0–39: Critical

### Overall score and coverage

All seven category weights default to `1`, so the overall score is an equal-weight mean of categories with measurable scores. N/A categories are excluded. Coverage is `measured categories / 7 × 100`. When one or more categories are N/A, the overall score is labelled **Provisional**.

### Custom scoring

Use `scoring.example.json` as a template. The file supports:

- `category_weights`: change each category's contribution to the overall score. Leave all at `1` for equal weighting.
- `check_weight_multipliers`: multiply a named check's weight.
- `check_overrides`: set a named check's explicit `weight` and/or override score for statuses (`pass`, `warning`, `fail`).

Example:

```json
{
  "category_weights": { "Speed": 2, "Security": 2 },
  "check_weight_multipliers": { "Horizontal overflow at 320px": 2 },
  "check_overrides": { "Clickjacking protection": { "fail": 0, "weight": 2 } }
}
```

Unspecified categories retain a weight of `1`. Use check names exactly as shown in the report.

## 6. Reports

Every run creates a new timestamped directory:

```text
reports/
└── techinspection.net_2026-11-09_120000/
    ├── report.html
    ├── report.json
    └── scores.csv
```

- `report.html`: business-friendly summary, scorecards, detailed check results, findings, recommendations, external links, and limitations.
- `report.json`: complete structured report, including raw/processed provider data, crawl details, scoring configuration, and limitations.
- `scores.csv`: one row per category plus the overall score and test counts.

## 7. Project layout

```text
site_audit_project/
├── site_audit.py
├── auditor/
│   ├── audits/
│   │   ├── speed.py
│   │   ├── responsiveness.py
│   │   ├── optimization.py
│   │   ├── health.py
│   │   ├── security.py
│   │   ├── seo.py
│   │   └── geo.py
│   ├── services/
│   │   ├── cache.py
│   │   ├── google.py
│   │   └── external.py
│   ├── context.py
│   ├── network.py
│   ├── models.py
│   ├── scoring.py
│   ├── checks.py
│   ├── configuration.py
│   └── reporting/generate.py
├── templates/report.html
├── tests/
├── .env.example
├── scoring.example.json
├── requirements.txt
└── README.md
```

## 8. Tests

Run unit tests (both commands are supported from the project root):

```bash
pytest -q
# or
python -m pytest -q
```

Tests use synthetic API payloads and do not consume provider quota. The first real website audit is an integration run and depends on external service availability.

## 9. Scope and limitations

- This is a technical audit, not an exhaustive security review. It does not run exploits, password attacks, authenticated tests, or intrusive penetration testing.
- The crawl is bounded by `--pages`, uses same-host URLs, and can miss pages that are not linked from the discovered crawl frontier or blocked by robots.txt.
- The Nu Checker validates only the root document in this version. MDN Observatory and SSL Labs return provider-specific letter grades; these are converted to the application scale with fixed mappings, and the raw provider grades are retained for traceability.
- The responsive audit is one browser/session run at specified viewport widths. It does not cover all browsers, operating systems, assistive technologies, or real physical devices.
- Image lazy-loading and static-vs-JavaScript content checks are heuristics; they do not infer actual viewport placement or crawler execution behavior with certainty.
- Some PSI audits are unavailable for certain pages or Lighthouse versions. Their absence is not a zero score.
- CrUX data is only available for origins/pages with sufficient Chrome user data and requires its API key.
- AI crawler user-agent permissions can change. The audit checks a declared list and reports the exact rules it sees.
- GEO technical readiness is not equivalent to AI search visibility. Actual visibility needs measured prompts, providers, platforms, locations, and a repeatable monitoring design.
- API availability, rate limits, provider terms, account permissions, and pricing can change. Review each provider's current terms before scheduled use.

## 10. Official API and documentation links

- [PageSpeed Insights REST API](https://developers.google.com/speed/docs/insights/rest)
- [PageSpeed Insights getting started](https://developers.google.com/speed/docs/insights/v5/get-started)
- [Chrome UX Report API](https://developer.chrome.com/docs/crux/api)
- [MDN HTTP Observatory API and migration notes](https://github.com/mdn/mdn-http-observatory)
- [MDN HTTP Observatory FAQ](https://developer.mozilla.org/en-US/observatory/docs/faq)
- [Qualys SSL Labs API](https://www.ssllabs.com/projects/ssllabs-apis)
- [SSL Labs API v4 reference](https://github.com/ssllabs/ssllabs-scan/blob/master/ssllabs-api-docs-v4.md)
- [W3C Nu Checker API](https://validator.w3.org/docs/api.html)
- [SecurityHeaders API docs](https://securityheaders.com/api/docs/)
- [DataForSEO AI Optimization API](https://docs.dataforseo.com/v3/ai_optimization-overview/)
- [DataForSEO LLM Mentions API](https://docs.dataforseo.com/v3/ai_optimization/llm_mentions/overview/)
- [OtterlyAI API availability and authentication notes](https://help.otterly.ai/do-you-provide-an-api-for-otterlyai)
