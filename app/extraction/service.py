"""Render a PDF and extract/classify it with a local Ollama vision model."""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .prompts import SYSTEM_PROMPT, build_prompt


class ExtractionError(Exception):
    """An actionable rendering or inference error."""


def post_json(host, endpoint, payload, timeout):
    request = Request(
        host.rstrip("/") + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise ExtractionError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ExtractionError(
            f"Cannot reach Ollama at {host}: {exc}. Check the server or increase --timeout."
        ) from exc
    except (ValueError, UnicodeError) as exc:
        raise ExtractionError("Ollama returned an invalid JSON response.") from exc
    if not isinstance(result, dict):
        raise ExtractionError("Ollama returned an unexpected response type.")
    if result.get("error"):
        raise ExtractionError(f"Ollama: {result['error']}")
    return result


def run_pdf_tool(command):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise ExtractionError(f"PDF conversion failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("PDF conversion exceeded 120 seconds.") from exc


def extract(args, progress=None, max_pages=None, document_name=None):
    def report(stage, **details):
        if progress:
            progress({"stage": stage, **details})

    report("validating")
    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file():
        raise ExtractionError(f"PDF file not found: {pdf}")
    for tool in ("pdfinfo", "pdftoppm"):
        if not shutil.which(tool):
            raise ExtractionError(
                f"Missing {tool}. Install Poppler: sudo apt install poppler-utils"
            )
    info = run_pdf_tool(["pdfinfo", str(pdf)])
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    if not match:
        raise ExtractionError("Could not determine the PDF page count.")
    total = int(match[1])
    if max_pages is not None and total > max_pages:
        raise ExtractionError(f"PDF exceeds the {max_pages}-page limit.")
    report("validated", total_pages=total)
    prompt = build_prompt(document_name=document_name or pdf.name, total_pages=total)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ExtractionError("build_prompt() must return a non-empty string.")
    model_info = post_json(args.host, "/api/show", {"model": args.model}, args.timeout)
    if "vision" not in model_info.get("capabilities", []):
        raise ExtractionError(
            f"Model {args.model!r} does not report vision support. Choose a vision model."
        )

    # Temporary images are removed even when rendering or inference fails.
    with tempfile.TemporaryDirectory(prefix="pdf-extraction-") as directory:
        images = []
        for page in range(1, total + 1):
            report("rendering", page=page, total_pages=total)
            print(f"Rendering page {page}/{total}...", file=sys.stderr)
            prefix = Path(directory) / f"page-{page}"
            run_pdf_tool(
                [
                    "pdftoppm",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    "-r",
                    str(args.dpi),
                    "-png",
                    str(pdf),
                    str(prefix),
                ]
            )
            images.append(
                base64.b64encode(prefix.with_suffix(".png").read_bytes()).decode(
                    "ascii"
                )
            )
        print(
            f"Sending {len(images)} page image(s) to {args.model}...", file=sys.stderr
        )
        report("inference", total_pages=total)
        response = post_json(
            args.host,
            "/api/chat",
            {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": prompt,
                        "images": images,
                    },
                ],
                "stream": False,
                "format": "json",
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_ctx": getattr(args, "num_ctx", 16384),
                    "num_predict": getattr(args, "num_predict", 8192),
                },
            },
            args.timeout,
        )
    report("parsing", total_pages=total)
    if response.get("done") is not True or response.get("done_reason") == "length":
        raise ExtractionError(
            "Ollama did not finish the response; increase OLLAMA_NUM_CTX / "
            "OLLAMA_NUM_PREDICT (CLI: --num-ctx / --num-predict), or use a smaller PDF. "
            "The incomplete result was not stored."
        )
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ExtractionError("Ollama returned no message content.")
    try:
        result = json.loads(content)
    except ValueError as exc:
        raise ExtractionError(
            "Model output was not valid JSON. Try a clearer JSON extraction prompt."
        ) from exc
    if not isinstance(result, dict):
        raise ExtractionError(
            "Model output must be a JSON object. Specify an object in your prompt."
        )
    return {
        "source": str(pdf),
        "model": args.model,
        "total_pages": total,
        "result": result,
    }


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF to extract and classify")
    parser.add_argument(
        "--model", default="gemma4:e4b", help="Ollama model tag (default: gemma4:e4b)"
    )
    parser.add_argument(
        "--host", default="http://localhost:11434", help="Ollama server URL"
    )
    parser.add_argument(
        "--dpi", type=positive_int, default=150, help="Rendering DPI (default: 150)"
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=600,
        help="Ollama request timeout in seconds",
    )
    parser.add_argument(
        "--num-ctx",
        type=positive_int,
        default=os.getenv("OLLAMA_NUM_CTX", "16384"),
        help="Context window tokens (default: OLLAMA_NUM_CTX or 16384)",
    )
    parser.add_argument(
        "--num-predict",
        type=positive_int,
        default=os.getenv("OLLAMA_NUM_PREDICT", "8192"),
        help="Maximum output tokens (default: OLLAMA_NUM_PREDICT or 8192)",
    )
    parser.add_argument(
        "--output", type=Path, help="Save JSON here; otherwise print to stdout"
    )
    args = parser.parse_args(argv)
    if args.num_predict >= args.num_ctx:
        parser.error("--num-predict must be less than --num-ctx")
    try:
        if args.output and args.output.resolve() in {
            args.pdf.resolve(),
            Path(__file__).resolve().with_name("prompts.py"),
            Path(__file__).resolve().with_name("extraction_prompt.py"),
            Path(__file__).resolve().parents[3] / "extraction_prompt.py",
        }:
            raise ExtractionError(
                "Output must not overwrite the input PDF or prompt file."
            )
        result = extract(args)
        serialized = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(f"Saved result to {args.output}", file=sys.stderr)
        else:
            print(serialized, end="")
    except (ExtractionError, OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
