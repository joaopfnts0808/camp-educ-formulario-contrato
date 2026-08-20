#!/usr/bin/env python3
"""
Robo diario: entra no admin da Cativa, abre o log do webhook "Integracao - Guru"
(evento "Criar usuario"), extrai os pagamentos recebidos e manda pro Make (que
escreve na Google Sheet) só os que ainda não tinham sido enviados antes.

Nao mexe em NADA no painel da Cativa alem de logar e clicar em "Ver logs" --
nao ativa/desativa webhook, nao cria nada.

Credenciais NUNCA ficam neste arquivo. Tudo vem de variaveis de ambiente
(no GitHub Actions, isso sao "Secrets" do repositorio).

Variaveis de ambiente necessarias:
  CATIVA_EMAIL      - email de login no admin da Cativa
  CATIVA_PASSWORD   - senha de login
  MAKE_WEBHOOK_URL  - URL do webhook do Make que recebe os registros

Controle de duplicidade: mantém um arquivo `processed_emails.json` no
repositório (o workflow do GitHub Actions faz commit dele de volta depois de
cada execução). Só manda pro Make e-mails que ainda não estão nesse arquivo.

Uso local (teste antes de confiar no agendamento):
  HEADLESS=false python scrape_cativa_logs.py
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

CATIVA_LOGIN_URL = "https://comunidade.campeduc.com/auth/login"
CATIVA_WEBHOOKS_URL = "https://comunidade.campeduc.com/admin/webhooklisteners"
WEBHOOK_ROW_TEXT = "Integração - Guru"

STATE_FILE = Path(__file__).parent / "processed_emails.json"

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

    email_selectors = [
        "input[type=email]",
        "input[name=email]",
        "input[placeholder*='mail' i]",
    ]
    password_selectors = [
        "input[type=password]",
        "input[name=password]",
    ]
    submit_texts = re.compile(r"entrar|login|acessar|continuar|próximo|next|avan", re.I)

    def fill_first_match(selectors, value):
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(value)
                return True
        return False

    def click_submit_button():
        for btn in page.locator("button").all():
            try:
                text = btn.inner_text(timeout=500)
            except Exception:
                continue
            if submit_texts.search(text or ""):
                btn.click()
                return True
        submit_btn = page.locator("button[type=submit]").first
        if submit_btn.count() > 0:
            submit_btn.click()
            return True
        return False

    # Login em duas etapas: primeiro só o e-mail + "Entrar", depois a senha
    # aparece na mesma tela + "Entrar" de novo.

    # Passo 1: e-mail
    if not fill_first_match(email_selectors, email):
        page.screenshot(path="debug_login_email_not_found.png")
        raise RuntimeError(
            "Não encontrei o campo de e-mail no login. "
            "Veja debug_login_email_not_found.png e ajuste os seletores."
        )

    if not click_submit_button():
        page.screenshot(path="debug_login_step1_button_not_found.png")
        raise RuntimeError(
            "Não encontrei o botão pra confirmar o e-mail (1o passo do login). "
            "Veja debug_login_step1_button_not_found.png."
        )

    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")

    # Passo 2: senha (só aparece depois do e-mail confirmado)
    if not fill_first_match(password_selectors, password):
        page.screenshot(path="debug_login_password_not_found.png")
        raise RuntimeError(
            "Não encontrei o campo de senha no login (2o passo, depois do e-mail). "
            "Veja debug_login_password_not_found.png e ajuste os seletores."
        )

    if not click_submit_button():
        page.screenshot(path="debug_login_step2_button_not_found.png")
        raise RuntimeError(
            "Não encontrei o botão pra confirmar a senha (2o passo do login). "
            "Veja debug_login_step2_button_not_found.png."
        )

    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    if "login" in page.url.lower():
        # Ainda na tela de login: a autenticação não colou (senha errada,
        # captcha, ou algum aviso de erro). Loga o texto da tela pra ver o motivo.
        page.screenshot(path="debug_login_failed.png")
        body_text = page.inner_text("body")
        log(f"URL apos tentar logar: {page.url}")
        log("Ainda na tela de login. Texto visivel da pagina:")
        log(body_text[:1000])
        raise RuntimeError(
            "O login não completou (continua em uma URL de /login/auth). "
            "Veja debug_login_failed.png e o texto logado acima — provavelmente "
            "a senha está incorreta ou tem uma etapa extra (captcha, 2FA)."
        )

    log("Login feito. Indo pra tela de webhooks...")

    page.goto(CATIVA_WEBHOOKS_URL, wait_until="networkidle")

    row = page.locator(f"text={WEBHOOK_ROW_TEXT}").first
    try:
        row.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    if row.count() == 0:
        page.screenshot(path="debug_row_not_found.png")
        Path("debug_row_not_found.html").write_text(page.content(), encoding="utf-8")
        body_text = page.inner_text("body")
        log(f"URL atual: {page.url}")
        log(f"Titulo da pagina: {page.title()}")
        log("Texto visivel da pagina (primeiros 1500 caracteres):")
        log(body_text[:1500])
        raise RuntimeError(
            f"Não encontrei a linha '{WEBHOOK_ROW_TEXT}'. "
            "Veja debug_row_not_found.png / debug_row_not_found.html e o texto "
            "logado acima."
        )

    row_container = row
    for _ in range(4):
        candidate = row_container.locator("xpath=..")
        if candidate.count() > 0:
            row_container = candidate

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


def blob_to_entry(blob):
    subscriber = blob.get("subscriber") or {}
    if not subscriber.get("email"):
        return None

    ddi = subscriber.get("phone_local_code", "55") or "55"
    numero = subscriber.get("phone_number", "") or ""
    telefone = f"{ddi}{numero}" if numero else ""

    forma = PAYMENT_METHOD_MAP.get(blob.get("payment_method", ""), "")

    return {
        "nome": subscriber.get("name", ""),
        "email": subscriber.get("email", ""),
        "cpf": subscriber.get("doc", ""),
        "telefone": telefone,
        "cep": subscriber.get("address_zip_code", ""),
        "estado": subscriber.get("address_state", ""),
        "cidade": subscriber.get("address_city", ""),
        "rua": subscriber.get("address", ""),
        "numero": subscriber.get("address_number", ""),
        "complemento": subscriber.get("address_comp", ""),
        "bairro": subscriber.get("address_district", ""),
        "forma_pagamento": forma,
    }


def load_processed_emails():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def save_processed_emails(emails):
    STATE_FILE.write_text(
        json.dumps(sorted(emails), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    webhook_url = os.environ["MAKE_WEBHOOK_URL"]

    with sync_playwright() as playwright:
        raw_text = login_and_get_logs_text(playwright)

    blobs = extract_json_blobs(raw_text)
    log(f"Encontrados {len(blobs)} registros no log.")

    processed = load_processed_emails()
    log(f"{len(processed)} e-mail(s) já processados em execuções anteriores.")

    entries = []
    for blob in blobs:
        entry = blob_to_entry(blob)
        if entry is None:
            continue
        if entry["email"] in processed:
            continue
        entries.append(entry)
        processed.add(entry["email"])

    if not entries:
        log("Nenhum registro novo. Nada pra enviar.")
        return

    log(f"Enviando {len(entries)} registro(s) novo(s) pro Make...")
    resp = requests.post(webhook_url, json={"entries": entries}, timeout=30)
    resp.raise_for_status()
    log(f"Make respondeu {resp.status_code}.")

    save_processed_emails(processed)
    log("processed_emails.json atualizado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERRO: {exc}")
        sys.exit(1)
