# Run as: PYTHONPATH=. python scripts/generate_pdf.py [--url URL]
#
# generate_pdf.py — Velvet Knowledge Hub PDF generation
#
# Renders the live GitHub Pages dashboard (or a local HTML file) to a
# print-ready A4 PDF using Playwright headless Chromium.
#
# Rules applied:
#   L-7 — playwright install --with-deps chromium (handled in workflow)
#   L-8 — system fonts only; no Google Fonts CDN
#   --no-sandbox flag required on GitHub Actions Ubuntu runner
#
# Output: docs/velvet-knowledge-hub.pdf
# PDF is committed alongside docs/index.html by the existing commit step.
#
# Security: no credentials in this file.

import argparse
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "velvet-knowledge-hub.pdf"
LOCAL_HTML_PATH = REPO_ROOT / "docs" / "index.html"

# GitHub Pages URL — used when --url is not provided.
GITHUB_PAGES_URL = "https://wkdjk.github.io/velvet-knowledge-hub/"

# Minimum acceptable PDF file size (bytes).
# A blank / failed render produces a near-empty file; 10 KB is a safe floor.
MIN_PDF_BYTES = 10_000


def _build_cover_html(build_date: str) -> str:
    """
    Return an HTML string for the PDF cover page.

    Uses system fonts only (L-8). Inline CSS so the cover renders correctly
    inside a Playwright page.pdf() call without any external CSS dependency.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    /* System fonts only — L-8: no Google Fonts CDN in headless Chromium */
    body {{
      font-family: system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
      margin: 0;
      padding: 0;
      background: #ffffff;
      color: #111111;
    }}
    .cover {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: flex-start;
      height: 100vh;
      padding: 40mm 22mm 22mm 22mm;
      box-sizing: border-box;
    }}
    .label {{
      font-size: 11pt;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #555555;
      margin-bottom: 8mm;
    }}
    h1 {{
      font-size: 28pt;
      font-weight: 700;
      line-height: 1.2;
      margin: 0 0 6mm 0;
    }}
    .sub {{
      font-size: 13pt;
      color: #444444;
      margin-bottom: 16mm;
    }}
    .rule {{
      width: 40mm;
      height: 2px;
      background: #111111;
      margin-bottom: 16mm;
    }}
    .meta {{
      font-size: 10pt;
      color: #777777;
      line-height: 1.8;
    }}
    .confidential {{
      margin-top: 24mm;
      font-size: 9pt;
      color: #999999;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
  </style>
</head>
<body>
  <div class="cover">
    <p class="label">DINZ Quarterly Intelligence</p>
    <h1>Velvet Knowledge Hub</h1>
    <p class="sub">Korea deer velvet market intelligence</p>
    <div class="rule"></div>
    <div class="meta">
      <p>Generated: {build_date}</p>
      <p>Source: wkdjk.github.io/velvet-knowledge-hub</p>
    </div>
    <p class="confidential">Confidential — for DINZ internal use</p>
  </div>
</body>
</html>"""


def generate_pdf(source_url: str, output_path: Path, build_date: str) -> int:
    """
    Render source_url to a PDF at output_path.

    Returns the number of bytes written.
    Raises RuntimeError if the output file is smaller than MIN_PDF_BYTES.

    Playwright flags applied:
      --no-sandbox      required on GitHub Actions Ubuntu runner
      --disable-dev-shm-usage   prevents shared-memory OOM on constrained runners
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        print(
            "ERROR: playwright not installed. Run: pip install playwright && playwright install --with-deps chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cover_html = _build_cover_html(build_date)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context()

        # ── Page 1: cover ────────────────────────────────────────────────────
        cover_page = context.new_page()
        cover_page.set_content(cover_html, wait_until="networkidle")
        cover_pdf_bytes = cover_page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
        )
        cover_page.close()

        # ── Page 2+: dashboard ───────────────────────────────────────────────
        dash_page = context.new_page()
        dash_page.goto(source_url, wait_until="networkidle", timeout=60_000)
        # Extra settle time for Chart.js canvas renders (important for print).
        dash_page.wait_for_timeout(2_000)
        # PS-1: Force all <details> elements open before PDF capture.
        # CSS display:block on the body div is insufficient when the native
        # <details> element is in the closed state — the browser hides non-summary
        # children regardless of CSS. Setting .open = true via JS ensures the DOM
        # state matches what the print CSS expects.
        dash_page.evaluate("document.querySelectorAll('details').forEach(el => el.open = true)")
        # PDF: show only last-90-day import records rows; hide older ones.
        dash_page.evaluate(
            "(function(){"
            "  var tbody=document.getElementById('import-records-tbody');"
            "  if(!tbody)return;"
            "  Array.from(tbody.querySelectorAll('tr.import-row')).forEach(function(r){"
            "    r.style.display=r.classList.contains('row-90d')?'':'none';"
            "  });"
            "})();"
        )
        # PDF: hide placeholder supplement/market-presence section.
        dash_page.evaluate(
            "var mp=document.getElementById('market-presence');"
            "if(mp)mp.style.display='none';"
        )
        dashboard_pdf_bytes = dash_page.pdf(
            format="A4",
            print_background=True,
            margin={
                "top": "15mm",
                "bottom": "22mm",   # L-7: bottom ≥22mm for footer clearance
                "left": "15mm",
                "right": "15mm",
            },
        )
        dash_page.close()
        browser.close()

    # ── Merge cover + dashboard pages using PyPDF2 if available, ─────────────
    # otherwise fall back to writing the dashboard PDF only with a simple
    # first-page cover injection via reportlab (not required — Playwright
    # multi-page approach is sufficient for this use case).
    #
    # Simplest reliable approach: write cover then dashboard as separate PDFs,
    # then merge. If pypdf is not installed, write dashboard PDF only with
    # a text note on the first page.
    merged_bytes = _merge_pdfs(cover_pdf_bytes, dashboard_pdf_bytes)

    output_path.write_bytes(merged_bytes)
    size = output_path.stat().st_size

    if size < MIN_PDF_BYTES:
        raise RuntimeError(
            f"PDF file size {size} bytes is below the minimum {MIN_PDF_BYTES} bytes threshold. "
            "The render may have failed silently."
        )

    return size


def _merge_pdfs(cover_bytes: bytes, body_bytes: bytes) -> bytes:
    """
    Merge cover PDF and body PDF into a single byte string.

    Attempts pypdf first. If not installed, returns body_bytes only
    (the cover is then omitted; the build still succeeds — L-12 graceful
    degradation).
    """
    try:
        from pypdf import PdfWriter, PdfReader  # noqa: PLC0415
        import io

        writer = PdfWriter()
        for source in (cover_bytes, body_bytes):
            reader = PdfReader(io.BytesIO(source))
            for page in reader.pages:
                writer.add_page(page)

        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    except ImportError:
        # pypdf not installed — return body only (cover skipped, not a fatal failure).
        print(
            "  WARNING: pypdf not installed — cover page skipped. "
            "Add 'pypdf' to requirements.txt to include cover page.",
            file=sys.stderr,
        )
        return body_bytes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a PDF of the Velvet Knowledge Hub dashboard."
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "URL to render. Defaults to the live GitHub Pages URL. "
            "For local testing, pass a file:// URL: "
            f"file://{LOCAL_HTML_PATH}"
        ),
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help=f"Output PDF path. Defaults to {OUTPUT_PATH}",
    )
    args = parser.parse_args()

    build_date = date.today().isoformat()
    source_url = args.url or GITHUB_PAGES_URL
    output_path = Path(args.output)

    print("generate_pdf.py — VKH PDF generation")
    print(f"  source : {source_url}")
    print(f"  output : {output_path}")
    print(f"  date   : {build_date}")

    try:
        size = generate_pdf(source_url, output_path, build_date)
        print(f"  result : OK — {size:,} bytes written to {output_path}")
    except RuntimeError as exc:
        print(f"  WARNING: {exc}", file=sys.stderr)
        print("  PDF generation failed size check — deleting output file.", file=sys.stderr)
        if output_path.exists():
            output_path.unlink()
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        # L-12: graceful degradation — PDF failure must not block HTML publish.
        # The caller (workflow) uses continue-on-error: true for this step.
        print(f"  ERROR: PDF generation failed — {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
