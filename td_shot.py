#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Be Trader Academy — Captura del heatmap de Trading Different a Telegram (multi-destino)
"""

import os
import time
import tempfile
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import requests
from playwright.sync_api import sync_playwright

# ---------- Config ----------
TD_EMAIL     = os.environ.get("TD_EMAIL", "").strip()
TD_PASSWORD  = os.environ.get("TD_PASSWORD", "").strip()
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "").strip()
DESTINOS_RAW = os.environ.get("DESTINOS", "").strip()
TZ_NAME      = os.environ.get("TZ_NAME", "Europe/Madrid").strip()

TD_LOGIN_URL = os.environ.get("TD_LOGIN_URL", "https://tradingdifferent.com/login").strip()
TD_CHART_URL = os.environ.get("TD_CHART_URL", "https://tradingdifferent.com/pools/binance-btcusdt").strip()
DEBUG        = os.environ.get("DEBUG", "0") == "1"
HEADLESS     = os.environ.get("HEADLESS", "1") == "1"

SEL_EMAIL    = os.environ.get("SEL_EMAIL", 'input[type="email"]')
SEL_PASSWORD = os.environ.get("SEL_PASSWORD", 'input[type="password"]')
SEL_SUBMIT   = os.environ.get("SEL_SUBMIT", 'button[type="submit"]')
SEL_CAMERA   = os.environ.get("SEL_CAMERA", '#captureBtn')
SEL_TF_OPEN  = os.environ.get("SEL_TF_OPEN", "").strip()
# Tamaño de la ventana de captura. Más ANCHO y menos ALTO = gráfico más "achatado" con más histórico.
VP_WIDTH     = int(os.environ.get("VP_WIDTH", "2200"))
VP_HEIGHT    = int(os.environ.get("VP_HEIGHT", "760"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PROFILE_DIR = "/tmp/td-profile"
TZ = ZoneInfo(TZ_NAME) if ZoneInfo else None


def log(*a):
    print(datetime.now(TZ).strftime("[%H:%M:%S]"), *a, flush=True)

def now_local():
    return datetime.now(TZ)


def parse_destinos(raw):
    dest = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 4:
            log(f"  destino mal formado (ignorado): {chunk!r}")
            continue
        chat_part, cuando, valor, tf = parts
        # chat_part puede ser "chat_id" o "chat_id/tema" (tema = message_thread_id)
        if "/" in chat_part:
            chat_id, thread = chat_part.split("/", 1)
            thread = thread.strip()
        else:
            chat_id, thread = chat_part, None
        dest.append({"chat_id": chat_id.strip(), "thread": thread,
                     "cuando": cuando.lower(), "valor": valor, "tf": tf, "last": None})
    return dest


def is_due(d, now):
    if d["cuando"] == "daily":
        hh, mm = d["valor"].split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        # ¿ya se envió hoy?
        if d["last"] is not None and d["last"].date() == now.date():
            return False
        # dispara desde la hora objetivo y hasta 6h después (tolerante a reinicios/ocupación)
        delta = (now - target).total_seconds()
        return 0 <= delta <= 6 * 3600
    elif d["cuando"] == "every":
        n = float(d["valor"])
        if d["last"] is None:
            return True
        return (now - d["last"]).total_seconds() >= n * 3600
    return False


def send_photo(chat_id, path, caption, thread=None):
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    if thread:
        data["message_thread_id"] = thread
    with open(path, "rb") as f:
        r = requests.post(f"{API}/sendPhoto", timeout=60, data=data,
                          files={"photo": f})
    r.raise_for_status()


def is_logged_in(page):
    try:
        return page.locator(SEL_PASSWORD).count() == 0
    except Exception:
        return True


def login(page):
    log("Iniciando sesión en Trading Different…")
    page.goto(TD_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    if is_logged_in(page):
        log("  Ya había sesión activa.")
        return
    page.fill(SEL_EMAIL, TD_EMAIL)
    page.fill(SEL_PASSWORD, TD_PASSWORD)
    page.click(SEL_SUBMIT)
    page.wait_for_timeout(6000)
    log("  Login enviado.")


def set_timeframe(page, tf):
    if not SEL_TF_OPEN:
        return
    try:
        page.click(SEL_TF_OPEN, timeout=8000)
        page.wait_for_timeout(800)
        page.get_by_text(tf, exact=True).first.click(timeout=8000)
        page.wait_for_timeout(4000)
        log(f"  temporalidad puesta en {tf}")
    except Exception as e:
        log(f"  aviso: no pude cambiar a {tf} ({e}); capturo la actual")


def capture(page, tf):
    log("Abriendo el gráfico…")
    page.goto(TD_CHART_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(12000)
    set_timeframe(page, tf)
    cam = page.locator(SEL_CAMERA).first
    cam.wait_for(state="visible", timeout=20000)
    log("Pulsando la cámara…")
    with page.expect_download(timeout=30000) as dl_info:
        cam.click()
    out = os.path.join(tempfile.gettempdir(), "td_btc.png")
    dl_info.value.save_as(out)
    return out


def dump_debug(page, chat_id, note):
    try:
        dbg = os.path.join(tempfile.gettempdir(), "td_debug.png")
        page.screenshot(path=dbg, full_page=True)
        log(f"  [debug] url={page.url}  title={page.title()!r}")
        if DEBUG:
            send_photo(chat_id, dbg, f"DEBUG: {note}\nurl={page.url}")
    except Exception as e:
        log("  [debug] no pude capturar debug:", e)


def main():
    missing = [k for k, v in {
        "TD_EMAIL": TD_EMAIL, "TD_PASSWORD": TD_PASSWORD,
        "BOT_TOKEN": BOT_TOKEN, "DESTINOS": DESTINOS_RAW,
    }.items() if not v]
    if missing:
        raise SystemExit("Faltan variables de entorno: " + ", ".join(missing))

    destinos = parse_destinos(DESTINOS_RAW)
    if not destinos:
        raise SystemExit("DESTINOS no tiene ningún destino válido.")

    log(f"Arrancando. TZ={TZ_NAME}. Destinos:")
    for d in destinos:
        tema = f" (tema {d['thread']})" if d.get('thread') else ""
        log(f"  {d['chat_id']}{tema} | {d['cuando']} {d['valor']} | {d['tf']}")

    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS, accept_downloads=True,
            viewport={"width": VP_WIDTH, "height": VP_HEIGHT},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            login(page)
        except Exception as e:
            log("✗ error en el login inicial:", e)
            dump_debug(page, destinos[0]["chat_id"], "fallo login inicial")

        while True:
            now = now_local()
            for d in destinos:
                if not is_due(d, now):
                    continue
                try:
                    if not is_logged_in(page):
                        login(page)
                    caption = f"BTC/USDT · {d['tf']} · Liquidaciones (Trading Different)"
                    path = capture(page, d["tf"])
                    send_photo(d["chat_id"], path, caption, d.get("thread"))
                    d["last"] = now
                    log(f"  -> Enviado a {d['chat_id']} ({d['tf']}) OK")
                except Exception as e:
                    log(f"X error enviando a {d['chat_id']}:", e)
                    dump_debug(page, d["chat_id"], str(e))
            time.sleep(30)


if __name__ == "__main__":
    main()
