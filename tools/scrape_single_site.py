"""
Tool: scrape_single_site
Purpose: Fetch a web page and save its plain-text content to .tmp/scraped.json.
Inputs:  --url  (required) full URL to fetch
         --output  (optional) destination file, default .tmp/scraped.json
Outputs: JSON file with keys: url, status_code, text
"""

import argparse
import json
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TMP = Path(".tmp")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Full URL of the page to scrape")
    parser.add_argument("--output", default=str(TMP / "scraped.json"), help="Output file path")
    return parser.parse_args()


def extract_text(html: str) -> str:
    """Strip tags from HTML, collapse whitespace."""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def main() -> None:
    args = parse_args()
    TMP.mkdir(exist_ok=True)

    headers = {"User-Agent": "Mozilla/5.0 (compatible; WAT-scraper/1.0)"}
    response = requests.get(args.url, headers=headers, timeout=15)

    if response.status_code != 200:
        print(f"ERROR: HTTP {response.status_code} for {args.url}", file=sys.stderr)
        sys.exit(1)

    text = extract_text(response.text)
    result = {
        "url": args.url,
        "status_code": response.status_code,
        "text": text,
    }

    out = Path(args.output)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Scraped {args.url} → {out}  ({len(text)} chars)")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: network error — {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
