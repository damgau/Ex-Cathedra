"""
Tool: [tool_name]
Purpose: [One sentence describing what this script does.]
Inputs:  [List CLI args and what they mean.]
Outputs: [What files are written and where (e.g. .tmp/output.json).]
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TMP = Path(".tmp")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Primary input (e.g. URL, file path, ID)")
    parser.add_argument("--output", default=str(TMP / "output.json"), help="Where to write results")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    TMP.mkdir(exist_ok=True)

    # --- main logic here ---
    result = {"input": args.input, "status": "ok"}
    # -----------------------

    out = Path(args.output)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Wrote output to {out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
