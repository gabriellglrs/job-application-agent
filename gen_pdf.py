"""Single-purpose: render a markdown resume/cover-letter to PDF.

Usage: python gen_pdf.py <input.md> <output.pdf>

Does ONLY markdown -> PDF via the existing tailor.py renderer. Safe to
allowlist because it cannot do anything else (no shell, no eval, no deletes).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.async_api import async_playwright  # noqa: E402
from tailor import md_to_html, render_pdf  # noqa: E402


async def main(md_path: str, pdf_path: str) -> None:
    # encoding explícito: no Windows o open() usa cp1252 e corrompe UTF-8
    # (medido: "Brasília" virava "Bras[C3] lia" no PDF — ATS lia lixo)
    with open(md_path, encoding="utf-8") as f:
        md = f.read()
    out = os.path.expanduser(pdf_path)
    async with async_playwright() as pw:
        await render_pdf(pw, md_to_html(md), out)
    print("rendered:", out)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python gen_pdf.py <input.md> <output.pdf>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
