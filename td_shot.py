#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Be Trader Academy — Captura del heatmap de Trading Different a Telegram
-----------------------------------------------------------------------
Inicia sesión en tradingdifferent.com, abre el gráfico de BTC, pulsa el botón
de cámara (descarga el PNG) y lo envía a tu grupo de Telegram cada N horas.

Corre en Railway con la imagen oficial de Playwright (ver Dockerfile).
NO uses tu cuenta de Google aquí: login por email + contraseña propios de TD.

Variables de entorno (Railway → Variables):
  TD_EMAIL         (obligatoria)  tu email de Trading Different
  TD_PASSWORD      (obligatoria)  tu contraseña de Trading Different
  BOT_TOKEN        (obligatoria)  el mismo bot de @BotFather que ya usas
  TARGET_CHAT_ID   (obligatoria)  tu grupo/canal (ej. @comunidadfxjotaruiz)

  TD_LOGIN_URL     (opcional)  página de login. Def: https://tradingdifferent.com/login
  TD_CHART_URL     (opcional)  Def: https://tradingdifferent.com/pools/binance-btcusdt
  INTERVAL_HOURS   (opcional)  cada cuántas horas. Def: 2
  ACTIVE_START     (opcional)  hora (0-23) desde la que publica. Def: 0
  ACTIVE_END       (opcional)  hora (0-23) hasta la que publica. Def: 24 (siempre)
  CAPTION          (opcional)  texto del pie de foto. Def: "BTC/USDT · 15m · Liquidaciones"
  DEBUG            (opcional)  "1" = ante un fallo, manda al Telegram lo que ve el navegador
  HEADLESS         (opcional)  "0" para ver el navegador en local. Def: 1

  --- Selectores (AJUSTA si el login o la cámara fallan; ver logs/DEBUG) ---
  SEL_EMAIL        Def: input[type="email"]
  SEL_PASSWORD     Def: input[type="password"]
  SEL_SUBMIT       Def: button[type="submit"]
  SEL_CAMERA       Def: (varios candidatos; el botón de captura arriba a la derecha)
"""

import os
import time
import tempfile
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# ---------- Config ----------
TD_EMAIL       = os.environ.get("TD_EMAIL", "").strip()
TD_PASSWORD    = os.environ.get("TD_PASSWORD", "").strip()
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "").strip()

TD_LOGIN_URL   = os.environ.get("TD_LOGIN_URL", "https://tradingdifferent.com/login").strip()
TD_CHART_URL   = os.environ.get("TD_CHART_URL", "https://tradingdifferent.com/pools/binance-btcusdt").strip()
INTERVAL_HOURS = float(os.environ.get("INTERVAL_HOURS", "2"))
ACTIVE_START   = int(os.environ.get("ACTIVE_START", "0"))
ACTIVE_END     = int(os.environ.get("ACTIVE_END", "24"))
CAPTION        = os.environ.get("CAPTION", "BTC/USDT · 15m · Liquidaciones (Trading Different)")
DEBUG          = os.environ.get("DEBUG", "0") == "1"
HEADLESS       = os.environ.get("HEADLESS", "1") == "1"

SEL_EMAIL    = os.environ.get("SEL_EMAIL", 'input[type="email"]')
SEL_PASSWORD = os.environ.get("SEL_PASSWORD", 'input[type="password"]')
SEL_SUBMIT   = os.environ.get("SEL_SUBMIT", 'button[type="submit"]')
# El botón de cámara de TD. Probamos varios candidatos hasta que uno exista.
SEL_CAMERA   = os.environ.get(
    "SEL_CAMERA",
    'button[title*="creenshot"], button[aria-label*="creenshot"], '
    'button[title*="aptura"], [class*="camera"], [class*="screenshot"], '
    'svg[class*="camera"]'
)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PROFILE_DIR = "/tmp/td-profile"   # perfil persistente: mantiene sesión y ajustes (15m) durante la vida del worker


def log(*a):
    print(datetime.now().strftime("[%H:%M:%S]"), *a, flush=True)


# ---------- Telegram ----------
def send_photo(path, caption):
    with open(path, "rb") as f:
        r = requests.post(f"{API}/sendPhoto", timeout=60,
                          data={"chat_id": TARGET_CHAT_ID, "caption": caption[:1024]},
                          files={"photo": f})
    r.raise_for_status()


def send_text(text):
    requests.post(f"{API}/sendMessage", timeout=30,
                  data={"chat_id": TARGET_CHAT_ID, "text": text[:4096]})


# ---------- Trading Different ----------
def is_logged_in(page):
    """Heurística: si aún se ven campos de contraseña, no hay sesión."""
    try:
        return page.locator(SEL_PASSWORD).count() == 0
    except Exception:
        return True


def login(page):
    log("Iniciando sesión en Trading Different…")
    page.goto(TD_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    if is_logged_in(page):
        log("  Ya había sesión activa (perfil persistente).")
        return
    page.fill(SEL_EMAIL, TD_EMAIL)
    page.fill(SEL_PASSWORD, TD_PASSWORD)
    page.click(SEL_SUBMIT)
    page.wait_for_timeout(6000)   # espera al redirect tras el login
    log("  Login enviado.")


def capture(page):
    """Abre el gráfico, pulsa la cámara y devuelve la ruta del PNG descargado."""
    log("Abriendo el gráfico…")
    page.goto(TD_CHART_URL, wait_until="domcontentloaded", timeout=60000)
    # el heatmap + TradingView tardan en dibujarse; damos margen
    page.wait_for_timeout(12000)

    # localizar el botón de cámara (probamos los candidatos del selector)
    cam = page.locator(SEL_CAMERA).first
    cam.wait_for(state="visible", timeout=20000)

    log("Pulsando la cámara y esperando la descarga…")
    with page.expect_download(timeout=30000) as dl_info:
        cam.click()
    download = dl_info.value

    out = os.path.join(tempfile.gettempdir(), "td_btc.png")
    download.save_as(out)
    log(f"  PNG guardado: {out}")
    return out


def dump_debug(page, note):
    """En caso de fallo, guarda y (si DEBUG) envía lo que ve el navegador."""
    try:
        dbg = os.path.join(tempfile.gettempdir(), "td_debug.png")
        page.screenshot(path=dbg, full_page=True)
        log(f"  [debug] url={page.url}  title={page.title()!r}")
        if DEBUG:
            send_photo(dbg, f"DEBUG: {note}\nurl={page.url}")
    except Exception as e:
        log("  [debug] no pude capturar debug:", e)


def in_active_window():
    h = datetime.now(timezone.utc).astimezone().hour
    if ACTIVE_START <= ACTIVE_END:
        return ACTIVE_START <= h < ACTIVE_END
    return h >= ACTIVE_START or h < ACTIVE_END   # ventana que cruza medianoche


def run_once(page):
    if not in_active_window():
        log("Fuera de la ventana horaria activa; me salto esta ronda.")
        return
    if not is_logged_in(page):
        login(page)
    path = capture(page)
    send_photo(path, CAPTION)
    log("  → Enviado a Telegram ✓")


def main():
    missing = [k for k, v in {
        "TD_EMAIL": TD_EMAIL, "TD_PASSWORD": TD_PASSWORD,
        "BOT_TOKEN": BOT_TOKEN, "TARGET_CHAT_ID": TARGET_CHAT_ID,
    }.items() if not v]
    if missing:
        raise SystemExit("Faltan variables de entorno: " + ", ".join(missing))

    log(f"Arrancando. Gráfico: {TD_CHART_URL} | cada {INTERVAL_HOURS}h | destino {TARGET_CHAT_ID}")
    os.makedirs(PROFILE_DIR, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=HEADLESS,
            accept_downloads=True,
            viewport={"width": 1600, "height": 950},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # login inicial
        try:
            login(page)
        except Exception as e:
            log("✗ error en el login inicial:", e)
            dump_debug(page, "fallo en login inicial")

        # bucle
        while True:
            try:
                run_once(page)
            except Exception as e:
                log("✗ error en la ronda:", e)
                dump_debug(page, str(e))
                # si parece que se cayó la sesión, reintenta login para la próxima
                try:
                    if not is_logged_in(page):
                        login(page)
                except Exception:
                    pass
            time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
