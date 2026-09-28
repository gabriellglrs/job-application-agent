"""Demo: adapta o CV master para a JD da Join Fullstack PHP (Gupy) e gera o PDF.
Uso: .venv python demo_tailor.py  (usa LLM do .env)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright

from main import LLM_MODEL, get_llm
from tailor import OUT_DIR, md_to_html, render_pdf, tailor_markdown

# JD real (trecho da pagina Gupy da Join Fullstack PHP Pleno, 19/08/2026):
# manutencao evolutiva/corretiva em legado PHP + React, Laravel, PostgreSQL,
# Git, refatoracao de legado, documentacao, 3+ anos PHP, 100% remoto.
JD_TEXT = """
Desenvolvedor Fullstack PHP Pleno - Join Creative Tech, 100% remoto.
Manutencao evolutiva e corretiva em sistema legado (PHP back, React front).
Analise e resolucao de problemas, melhorias, integracao com APIs externas,
documentacao tecnica atualizada, colaboracao com QA/produto/devs, code review.
Requisitos: graduacao em TI, 3+ anos PHP (legados), Laravel, PostgreSQL,
Git + CI, refatoracao de legado, boa comunicacao. Diferencial: Azure DevOps.
"""

JOB_URL = "https://jointecnologia.gupy.io/job/join-fullstack-php-demo"


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    llm = get_llm()
    print("tailoring para a JD com", LLM_MODEL, "...")
    md = tailor_markdown(llm, LLM_MODEL, JD_TEXT)
    md_path = os.path.join(OUT_DIR, "DEMO_Join_Fullstack_PHP.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    pdf_path = os.path.join(OUT_DIR, "DEMO_Join_Fullstack_PHP.pdf")
    async with async_playwright() as pw:
        await render_pdf(pw, md_to_html(md), pdf_path)
    print("MD :", md_path)
    print("PDF:", pdf_path)
    print("---- SUMMARY ADAPTADO ----")
    for line in md.splitlines():
        print(line)
        if line.startswith("## "):
            break


if __name__ == "__main__":
    asyncio.run(main())
