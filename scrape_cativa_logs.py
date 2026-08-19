#!/usr/bin/env python3
"""
Robo diario: entra no admin da Cativa, abre o log do webhook "Integracao - Guru"
(evento "Criar usuario"), extrai os pagamentos recebidos e joga as linhas novas
numa Google Sheet (a mesma que ja usamos: colunas Nome, Email, CPF, Telefone,
CEP, Estado, Cidade, Rua, Numero, Complemento, Bairro, FormaPagamento).

Nao mexe em NADA no painel da Cativa alem de logar e clicar em "Ver logs" --
nao ativa/desativa webhook, nao cria nada.

Credenciais NUNCA ficam neste arquivo. Tudo vem de variaveis de ambiente
(no GitHub Actions, isso sao "Secrets" do repositorio).

Variaveis de ambiente necessarias:
  CATIVA_EMAIL              - email de login no admin da Cativa
  CATIVA_PASSWORD           - senha de login
  GOOGLE_SERVICE_ACCOUNT_JSON - conteudo (JSON completo, em uma linha) da
                                 chave de uma conta de servico do Google com
                                 acesso de Editor na planilha
  SHEET_ID                  - ID da planilha (o trecho entre /d/ e /edit na URL)

Uso local (teste antes de confiar no agendamento):
  HEADLESS=false python scrape_cativa_logs.py
"""

import json
import os
import re
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials

CATIVA_LOGIN_URL = "https://comunidade.campeduc.com/login"
CATIVA_WEBHOOKS_URL = "https://comunidade.campeduc.com/admin/webhooklisteners"
WEBHOOK_ROW_TEXT = "Integração - Guru"

SHEET_HEADERS = [
    "Nome", "Email", "CPF", "Telefone", "CEP", "Estado", "Cidade",
    "Rua", "Numero", "Complemento", "Bairro", "FormaPagamento",
]

PAYMENT_METHOD_MAP = {
    "pix": "PIX",
    "credit_card": "CARTAO",
    "billet": "BOLETO",
}


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def login_and_get_logs_text(playwright):
    """Loga no admin da Cativa e retorna o texto bruto da tela de logs
    do webhook 'Integração - Guru'."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page()

    email = os.environ["CATIVA_EMAIL"]
    password = os.environ["CATIVA_PASSWORD"]

    log("Abrindo tela de login...")
    page.goto(CATIVA_LOGIN_URL, wait_until="networkidle")

    # Tenta algumas variações comuns de seletor -- ajuste aqui se a Cativa
    # mudar o formulário de login.
    email_selectors = [
        "input[type=email]",
        "input[name=email]",
        "input[placeholder*='mail' i]",
    ]
    password_selectors = [
        "input[type=password]",
        "input[name=password]",
    ]

    def fill_first_match(selectors, value):
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(value)
                return True
        return False

    if not fill_first_match(email_selectors, email):
        page.screenshot(path="debug_login_email_not_found.png")
        raise RuntimeError(
            "Não encontrei o campo de e-mail no login. "
            "Veja debug_login_email_not_found.png e ajuste os seletores."
        )
    if not fill_first_match(password_selectors, password):
        page.screenshot(path="debug_login_password_not_found.png")
        raise RuntimeError(
            "Não encontrei o campo de senha no login. "
            "Veja debug_login_password_not_found.png e ajuste os seletores."
        )

    # Botão de submit: tenta por texto comum, senão o primeiro botão do form
    submit_texts = re.compile(r"entrar|login|acessar|continuar", re.I)
    clicked = False
    for btn in page.locator("button").all():
        try:
            text = btn.inner_text(timeout=500)
        except Exception:
            continue
        if submit_texts.search(text or ""):
            btn.click()
            clicked = True
            break
    if not clicked:
        page.locator("button[type=submit]").first.click()

    page.wait_for_load_state("networkidle")
    log("Login feito (ou pelo menos tentado). Indo pra tela de webhooks...")

    page.goto(CATIVA_WEBHOOKS_URL, wait_until="networkidle")

    # Acha a linha da tabela que contém o texto do webhook alvo
    row = page.locator(f"text={WEBHOOK_ROW_TEXT}").first
    if row.count() == 0:
        page.screenshot(path="debug_row_not_found.png")
        raise RuntimeError(
            f"Não encontrei a linha '{WEBHOOK_ROW_TEXT}'. "
            "Veja debug_row_not_found.png."
        )

    # Sobe até o container da linha (tenta algumas alturas de ancestral)
    row_container = row
    for _ in range(4):
        candidate = row_container.locator("xpath=..")
        if candidate.count() > 0:
            row_container = candidate

    # Ícones clicáveis na linha: 0=copiar URL, 1=Ver logs, 2=toggle, 3=editar, 4=apagar
    clickable = row_container.locator("svg, button, [role=button], input[type=checkbox]")
    if clickable.count() < 2:
        page.screenshot(path="debug_row_icons_not_found.png")
        raise RuntimeError(
            "Não encontrei os ícones da linha (Ver logs). "
            "Veja debug_row_icons_not_found.png."
        )

    log("Clicando em 'Ver logs'...")
    clickable.nth(1).click()
    page.wait_for_timeout(2000)

    # Pode abrir modal na mesma página OU uma nova aba/página
    context = page.context
    if len(context.pages) > 1:
        logs_page = context.pages[-1]
        logs_page.wait_for_load_state("networkidle")
        text = logs_page.inner_text("body")
    else:
        text = page.inner_text("body")

    if "Dados recebidos" not in text and "webhook_type" not in text:
        page.screenshot(path="debug_logs_view_unexpected.png")
        log(
            "Aviso: o texto capturado não parece ter os logs esperados. "
            "Salvei debug_logs_view_unexpected.png pra você conferir."
        )

    browser.close()
    return text


def extract_json_blobs(raw_text):
    """Extrai todos os objetos JSON `{...}` soltos no meio do texto da tela de logs."""
    blobs = []
    depth = 0
    start = None
    for i, ch in enumerate(raw_text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = raw_text[start:i + 1]
                    try:
                        blobs.append(json.loads(candidate))
                    except json.JSONDecodeError:
                        pass
                    start = None
    return blobs


def blob_to_row(blob):
    """Converte um payload da Guru (evento subscription) numa linha da planilha."""
    subscriber = blob.get("subscriber") or {}
    if not subscriber.get("email"):
        return None

    ddi = subscriber.get("phone_local_code", "55") or "55"
    numero = subscriber.get("phone_number", "") or ""
    telefone = f"{ddi}{numero}" if numero else ""

    forma = PAYMENT_METHOD_MAP.get(blob.get("payment_method", ""), "")

    return [
        subscriber.get("name", ""),
        subscriber.get("email", ""),
        subscriber.get("doc", ""),
        telefone,
        subscriber.get("address_zip_code", ""),
        subscriber.get("address_state", ""),
        subscriber.get("address_city", ""),
        subscriber.get("address", ""),
        subscriber.get("address_number", ""),
        subscriber.get("address_comp", ""),
        subscriber.get("address_district", ""),
        forma,
    ]


def get_sheet():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    sheet_id = os.environ["SHEET_ID"]
    info = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    return sh.sheet1


def main():
    with sync_playwright() as playwright:
        raw_text = login_and_get_logs_text(playwright)

    blobs = extract_json_blobs(raw_text)
    log(f"Encontrados {len(blobs)} registros no log.")

    ws = get_sheet()
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(SHEET_HEADERS)
        existing = [SHEET_HEADERS]

    existing_emails = {row[1] for row in existing[1:] if len(row) > 1}

    new_rows = []
    for blob in blobs:
        row = blob_to_row(blob)
        if row is None:
            continue
        email = row[1]
        if email in existing_emails:
            continue
        new_rows.append(row)
        existing_emails.add(email)

    if new_rows:
        ws.append_rows(new_rows)
        log(f"Adicionadas {len(new_rows)} linha(s) nova(s) na planilha.")
    else:
        log("Nenhuma linha nova pra adicionar hoje.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERRO: {exc}")
        sys.exit(1)
