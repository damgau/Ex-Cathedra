# Workflow: Scrape Single Website

Fetch the raw text content of a single web page and save it for downstream processing.

---

## Objective

Given a URL, download the page HTML, strip it to readable text, and save the result to `.tmp/` for the agent to read or pass to another tool.

## Required Inputs

- `URL` — the full URL of the page to scrape (e.g. `https://example.com/page`)

## Tools Used

- `tools/scrape_single_site.py` — fetches the URL and saves text content to `.tmp/scraped.json`

## Steps

1. Confirm the URL is accessible (not behind a login wall or CAPTCHA).
2. Run: `python tools/scrape_single_site.py --url <URL>`
3. Read `.tmp/scraped.json` — it contains `url`, `status_code`, and `text`.
4. Pass `text` to the next workflow step (summarise, extract, etc.).

## Expected Outputs

`.tmp/scraped.json` with the shape:
```json
{
  "url": "https://...",
  "status_code": 200,
  "text": "plain text content of the page..."
}
```

## Edge Cases & Known Issues

- **Non-200 responses**: the script exits with code 1 and prints the status code. Check the URL.
- **JavaScript-rendered pages**: `requests` fetches raw HTML only; JS-rendered content won't appear. Use a Playwright-based tool for those pages.
- **Rate limiting**: add a `--delay` flag (not yet implemented) if scraping multiple pages in a loop.

## Changelog

- 2026-06-10 — Initial version.
