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
# Compresión del eje de PRECIO: arrastra la escala de la derecha para ver más rango arriba/abajo.
# Nº de "pasos" de arrastre (más = más rango de precio). 0 = no comprime.
PRICE_COMPRESS = int(os.environ.get("PRICE_COMPRESS", "6"))
# --- Comando /mapa bajo demanda ---
MAPA_ENABLE     = os.environ.get("MAPA_ENABLE", "1") == "1"
MAPA_COMMAND    = os.environ.get("MAPA_COMMAND", "/mapa").strip()
MAPA_CHAT       = os.environ.get("MAPA_CHAT", "").strip()        # id del grupo donde escucha (VIP)
MAPA_TF         = os.environ.get("MAPA_TF", "15m").strip()
MAPA_COOLDOWN_MIN = float(os.environ.get("MAPA_COOLDOWN_MIN", "10")) # minutos entre generaciones NUEVAS
# Pares permitidos en /mapa (lista blanca): símbolo -> URL de Trading Different.
# Para añadir pares en el futuro, amplía este dict.
MAPA_PARES = {
    "btc": "https://tradingdifferent.com/pools/binance-btcusdt",
    "eth": "https://tradingdifferent.com/pools/binance-ethusdt",
    "sol": "https://tradingdifferent.com/pools/binance-solusdt",
}
MAPA_PAR_DEFECTO = "btc"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PROFILE_DIR = "/tmp/td-profile"
TZ = ZoneInfo(TZ_NAME) if ZoneInfo else None

# estado del comando /mapa: caché por par -> {"btc": {"last": dt, "file_id": str}, ...}
mapa_cache = {}
tg_offset  = None   # offset de getUpdates


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
    try:
        return r.json()["result"]["photo"][-1]["file_id"]
    except Exception:
        return None


def send_cached(chat_id, file_id, caption, thread=None):
    data = {"chat_id": chat_id, "caption": caption[:1024], "photo": file_id}
    if thread:
        data["message_thread_id"] = thread
    requests.post(f"{API}/sendPhoto", timeout=30, data=data).raise_for_status()


def send_text_to(chat_id, text, thread=None):
    data = {"chat_id": chat_id, "text": text[:4096]}
    if thread:
        data["message_thread_id"] = thread
    try:
        requests.post(f"{API}/sendMessage", timeout=20, data=data)
    except Exception:
        pass


def tg_get_updates(offset):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API}/getUpdates", params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("result", [])


RESTART_NEEDED = False

def _is_crash(e):
    s = str(e).lower()
    return any(k in s for k in ("crash", "target closed", "target crashed",
                                "has been closed", "browser has been closed",
                                "connection closed"))


def handle_commands(page):
    """Escucha /mapa en el grupo MAPA_CHAT y responde con el mapa (cache o nuevo)."""
    global tg_offset, mapa_cache, RESTART_NEEDED
    if not (MAPA_ENABLE and MAPA_CHAT):
        return
    try:
        updates = tg_get_updates(tg_offset)
    except Exception as e:
        log("  aviso getUpdates:", e)
        return
    for u in updates:
        tg_offset = u["update_id"] + 1
        msg = u.get("message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id")) != str(MAPA_CHAT):
            continue
        text = (msg.get("text") or "").strip()
        parts = text.split()
        cmd = parts[0].split("@")[0] if parts else ""
        if cmd != MAPA_COMMAND:
            continue
        thread = msg.get("message_thread_id")   # responder en el mismo tema
        # par pedido (ej. "/mapa eth"); si no ponen nada -> por defecto
        par = parts[1].lower() if len(parts) > 1 else MAPA_PAR_DEFECTO
        if par not in MAPA_PARES:
            disponibles = ", ".join(MAPA_PARES.keys())
            send_text_to(MAPA_CHAT, f"No tengo el par \"{par}\". Disponibles: {disponibles}", thread)
            log(f"  /mapa {par} -> par no permitido")
            continue
        url = MAPA_PARES[par]
        cap = f"{par.upper()}/USDT · {MAPA_TF} · Liquidaciones (Trading Different)"
        now = now_local()
        c = mapa_cache.get(par)
        try:
            fresco = c and c["file_id"] and (now - c["last"]).total_seconds() < MAPA_COOLDOWN_MIN * 60
            if fresco:
                send_cached(MAPA_CHAT, c["file_id"], cap, thread)
                log(f"  /mapa {par} -> imagen en caché")
            else:
                if not is_logged_in(page):
                    login(page)
                path = capture(page, MAPA_TF, url)
                fid = send_photo(MAPA_CHAT, path, cap, thread)
                if fid:
                    mapa_cache[par] = {"last": now, "file_id": fid}
                log(f"  /mapa {par} -> imagen nueva generada")
        except Exception as e:
            log(f"  X error atendiendo /mapa {par}:", e)
            if _is_crash(e):
                RESTART_NEEDED = True


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


def compress_price(page):
    """Comprime el eje de PRECIO arrastrando la escala de la derecha hacia arriba.
    Así se ve más rango de precio (más zonas de liquidación arriba y abajo)."""
    if PRICE_COMPRESS <= 0:
        return
    try:
        box = page.viewport_size
        # el eje de precios está en el borde derecho; agarramos ahí, a media altura
        x = box["width"] - 25          # muy pegado al borde derecho (escala de precios)
        y = box["height"] * 0.5
        for _ in range(PRICE_COMPRESS):
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x, y + 120, steps=8)   # arrastrar hacia ABAJO = comprimir (ver más rango)
            page.mouse.up()
            page.wait_for_timeout(300)
        page.wait_for_timeout(1500)
        log(f"  compresión de precio x{PRICE_COMPRESS}")
    except Exception as e:
        log(f"  aviso: no pude comprimir el eje de precio ({e})")


def capture(page, tf, url=None):
    log("Abriendo el gráfico…")
    page.goto(url or TD_CHART_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(12000)
    set_timeframe(page, tf)
    compress_price(page)
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

    CHROME_ARGS = ["--no-sandbox", "--disable-dev-shm-usage",
                   "--disable-gpu", "--disable-extensions",
                   "--disable-background-networking"]

    def _clear_lock():
        # si Chromium se cerró mal, deja el perfil bloqueado y no reabre; limpiamos
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(PROFILE_DIR, name))
            except Exception:
                pass

    def make_ctx(p):
        _clear_lock()
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS, accept_downloads=True,
            viewport={"width": VP_WIDTH, "height": VP_HEIGHT}, args=CHROME_ARGS,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, page

    global RESTART_NEEDED
    with sync_playwright() as p:
        ctx, page = make_ctx(p)
        try:
            login(page)
        except Exception as e:
            log("X error en el login inicial:", e)
            dump_debug(page, destinos[0]["chat_id"], "fallo login inicial")

        if MAPA_ENABLE and MAPA_CHAT:
            try:
                ups = tg_get_updates(None)
                if ups:
                    tg_offset = ups[-1]["update_id"] + 1
                log(f"Comando {MAPA_COMMAND} activo en {MAPA_CHAT} (cooldown {MAPA_COOLDOWN_MIN} min)")
            except Exception as e:
                log("  aviso baseline getUpdates:", e)

        fail_streak = 0
        MAX_FAILS = 6   # tras tantos fallos seguidos, salir para que Railway reinicie limpio

        while True:
            now = now_local()

            # reinicio del navegador si un crash lo dejó tocado
            if RESTART_NEEDED:
                log("  reiniciando el navegador tras un crash...")
                try:
                    ctx.close()
                except Exception:
                    pass
                try:
                    ctx, page = make_ctx(p)
                    login(page)
                    log("  navegador reiniciado OK")
                    fail_streak = 0
                except Exception as e:
                    log("  X no pude reiniciar el navegador:", e)
                    fail_streak += 1
                RESTART_NEEDED = False

            if fail_streak >= MAX_FAILS:
                log(f"  {fail_streak} fallos seguidos: salgo para que Railway reinicie el contenedor limpio.")
                raise SystemExit(1)

            handle_commands(page)

            for d in destinos:
                if not is_due(d, now):
                    continue
                caption = f"BTC/USDT · {d['tf']} · Liquidaciones (Trading Different)"
                try:
                    if not is_logged_in(page):
                        login(page)
                    path = capture(page, d["tf"])
                    send_photo(d["chat_id"], path, caption, d.get("thread"))
                    d["last"] = now
                    fail_streak = 0
                    log(f"  -> Enviado a {d['chat_id']} ({d['tf']}) OK")
                except Exception as e:
                    log(f"X error enviando a {d['chat_id']}:", e)
                    if _is_crash(e):
                        # reinicia y reintenta una vez para no perder el envío
                        try:
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            ctx, page = make_ctx(p)
                            login(page)
                            path = capture(page, d["tf"])
                            send_photo(d["chat_id"], path, caption, d.get("thread"))
                            d["last"] = now
                            fail_streak = 0
                            log(f"  -> Enviado a {d['chat_id']} ({d['tf']}) OK (tras reinicio)")
                        except Exception as e2:
                            fail_streak += 1
                            log("  X sigue fallando tras reinicio:", e2)
                    else:
                        dump_debug(page, d["chat_id"], str(e))
            time.sleep(30)


if __name__ == "__main__":
    main()
