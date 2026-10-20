$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Write-File([string]$Path, [string]$Content) {
    $full = Join-Path (Get-Location).Path $Path
    $dir  = Split-Path $full -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lf = $Content -replace "`r`n", "`n"
    [IO.File]::WriteAllText($full, $lf, (New-Object System.Text.UTF8Encoding($false)))
}

# ============================== stealth/waf.py v2 ===========================
Write-File "stealth\waf.py" @'
"""Warmup con navegador real, captura de trafico y deteccion de bloqueos.

v2: deriva los endpoints de las RESPUESTAS JSON REALES en vez de adivinar
rutas, y conserva las URLs exactas con su token. Si el trafico no trae
offerings, deja el volcado completo en state/traffic.json para diagnostico.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any

from .patches import context_options, launch_args, stealth_script

log = logging.getLogger("stealth.waf")

BASE = "https://www.daznbet.es"
HOME = f"{BASE}/es-es"
SPORT_URLS = [
    f"{BASE}/es-es/sports",
    f"{BASE}/es-es/deportes",
    f"{BASE}/es-es/sportsbook",
    f"{BASE}/es-es/live",
    f"{BASE}/es-es",
]
SB_FALLBACK = ["https://sb-pp-esfe.daznbet.es"]
OFFER_HINTS = ("offering", "betoffer", "listview", "coupon", "market", "odds")
TRAFFIC_FILE = Path("state/traffic.json")


def pw_module():
    """Devuelve el modulo de playwright (o patchright si USE_PATCHRIGHT)."""
    try:
        if os.getenv("USE_PATCHRIGHT"):
            from patchright.async_api import async_playwright
            return async_playwright
        raise ImportError
    except ImportError:
        from playwright.async_api import async_playwright
        return async_playwright


class Blocked(Exception):
    """WAF o rate-limit."""


def is_blocked(r) -> bool:
    if r.status_code in (401, 403, 405, 407, 429, 503):
        return True
    h = {k.lower(): v for k, v in r.headers.items()}
    if (h.get("x-amzn-waf-action") or "").lower() in ("challenge", "captcha"):
        return True
    if (h.get("x-amzn-errortype") or "").lower().startswith("challenge"):
        return True
    body = (r.text or "")[:600].lower()
    return any(t in body for t in ("challenge.js", "awswaf", "captcha", "just a moment"))


def templateize(url: str) -> str:
    u = re.sub(r"/v3/[A-Za-z0-9_\-]{1,10}/", "/v3/{gid}/", url)
    u = re.sub(r"/betoffer/\d+", "/betoffer/{betofferId}", u)
    u = re.sub(r"/event/\d+", "/event/{eventId}", u)
    return u


def _dedup(seq):
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _looks_like_offer(body: str) -> bool:
    head = body[:6000]
    return any(k in head for k in ("betOffers", '"outcomes"', "betOfferType", "listView"))


def build_endpoints(captured: list, js: str) -> tuple[dict, str | None, list]:
    """Endpoints a partir del trafico JSON real. Las URLs exactas van primero."""
    raw, lists, deeps, hosts = [], [], [], []
    token = None

    for c in captured:
        u, body = c["url"], c["body"]
        low = u.lower()
        m = re.search(r"[?&]token=([^&]+)", u)
        if m and not token:
            token = m.group(1)
        hm = re.match(r"(https://[^/]+)", u)
        if hm:
            hosts.append(hm.group(1))

        if not (_looks_like_offer(body) or any(h in low for h in OFFER_HINTS)):
            continue
        t = templateize(u)
        if "/betoffer" in low or "/event/" in low:
            deeps.append(t)
        else:
            raw.append(u)
            lists.append(t)

    hosts = _dedup(hosts)
    for host in (hosts or SB_FALLBACK):
        for gid in ("{gid}", "2000", "1000", "4000"):
            for last in ("current.json", "live.json", "all.json"):
                lists.append(f"{host}/offering/api/v3/{gid}/listView/all/all/all/all/{last}")
        deeps.append(f"{host}/offering/api/v3/{{gid}}/betoffer/{{betofferId}}.json")

    for gid in re.findall(r"offering/api/v3/([A-Za-z0-9_\-]{1,10})/", js or ""):
        for host in (hosts or SB_FALLBACK):
            lists.append(f"{host}/offering/api/v3/{gid}/listView/all/all/all/all/current.json")

    endpoints = {"list": _dedup(raw + lists), "deep": _dedup(deeps),
                 "raw": _dedup(raw), "hosts": hosts}
    return endpoints, token, hosts


async def _humanize(page) -> None:
    for _ in range(random.randint(2, 4)):
        try:
            await page.mouse.move(random.randint(80, 1300), random.randint(80, 800),
                                  steps=random.randint(8, 25))
            await page.mouse.wheel(0, random.randint(400, 1500))
            await page.wait_for_timeout(random.randint(350, 1000))
        except Exception:
            pass


async def _navigate(page) -> None:
    await page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(random.randint(2500, 4000))
    for url in SPORT_URLS:
        try:
            if page.url.rstrip("/") == url.rstrip("/"):
                continue
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(random.randint(2500, 4500))
            await _humanize(page)
        except Exception as e:
            log.debug("goto %s: %s", url, e)


async def _interact(page) -> None:
    """Intenta entrar al sportsbook desde la propia UI."""
    for sel in ["text=/deportes|sports/i", "a[href*='sport']", "a[href*='deporte']"]:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=5000)
                await page.wait_for_timeout(random.randint(2000, 3500))
                await _humanize(page)
                return
        except Exception:
            continue


async def warmup(identity, store, proxy=None, headless: bool = True,
                 playwright=None, dump: bool = False) -> dict[str, Any]:
    """Abre Chrome real, pasa el WAF y captura cookies + trafico + endpoints reales."""
    pw_factory = pw_module()
    owns = playwright is None
    pw = playwright or await pw_factory().start()

    captured: list[dict] = []
    seen_req: list[dict] = []
    cookies: list = []
    html = ""
    engine = "patchright" if os.getenv("USE_PATCHRIGHT") else "playwright"

    try:
        launch = {"headless": headless, "args": launch_args()}
        if proxy:
            launch["proxy"] = proxy.playwright()
        try:
            browser = await pw.chromium.launch(channel="chrome", **launch)
        except Exception:
            browser = await pw.chromium.launch(**launch)

        ctx = await browser.new_context(**context_options(identity))
        await ctx.add_init_script(stealth_script(identity))
        if store.as_playwright():
            try:
                await ctx.add_cookies(store.as_playwright())
            except Exception:
                pass

        page = await ctx.new_page()

        async def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "").lower()
                if "json" not in ct or resp.status != 200:
                    return
                body = await resp.text()
                if not body or len(body) > 5_000_000:
                    return
                captured.append({"url": resp.url, "status": resp.status,
                                 "ct": ct, "body": body})
            except Exception:
                pass

        def on_request(r):
            try:
                if r.resource_type in ("xhr", "fetch"):
                    seen_req.append({"url": r.url, "method": r.method,
                                     "type": r.resource_type,
                                     "headers": dict(r.headers)})
            except Exception:
                pass

        page.on("response", on_response)
        page.on("request", on_request)
        page.on("crash", lambda _: log.warning("pagina crasheada durante el warmup"))

        try:
            await asyncio_wait_for(_navigate(page), 100)
        except Exception as e:
            log.debug("navegacion: %s", e)
        try:
            await _interact(page)
        except Exception:
            pass

        real_headers = {r["url"]: r["headers"] for r in seen_req
                        if "offering/api" in r["url"] or "/ajax/" in r["url"]}

        try:
            cookies = await ctx.cookies()
        except Exception:
            pass
        try:
            html = await page.content()
        except Exception:
            try:
                html = await page.evaluate("() => document.documentElement.outerHTML")
            except Exception:
                html = ""

        js = ""
        m = re.search(r"(main\.[0-9a-f]+\.js)", html or "")
        if m:
            try:
                js = await page.evaluate("async u => (await fetch(u)).text()", f"/{m.group(1)}")
            except Exception:
                pass

        await browser.close()
    finally:
        if owns:
            try:
                await pw.stop()
            except Exception:
                pass

    if cookies:
        store.ingest_playwright(cookies)

    endpoints, token, hosts = build_endpoints(captured, js)
    log.info("warmup [%s] %d cookies | %d respuestas JSON | %d urls oferta | waf=%s",
             engine, len(cookies), len(captured), len(endpoints["raw"]),
             store.has("aws-waf-token"))
    if not endpoints["raw"]:
        log.warning("el navegador no llamo a ningun endpoint de cuotas; "
                    "mira state/traffic.json con: python discover.py")

    traffic = []
    for c in captured:
        keys, snippet = [], ""
        try:
            data = json.loads(c["body"])
            if isinstance(data, dict):
                keys = list(data.keys())[:10]
        except Exception:
            pass
        snippet = c["body"][:180].replace("\n", " ")
        traffic.append({"url": c["url"], "status": c["status"], "ct": c["ct"],
                        "size": len(c["body"]), "keys": keys, "snippet": snippet})

    if dump:
        TRAFFIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRAFFIC_FILE.write_text(json.dumps(
            {"hosts": hosts, "token": token, "endpoints": endpoints,
             "json_responses": traffic,
             "xhr": [{"url": r["url"], "method": r["method"]} for r in seen_req]},
            indent=2, ensure_ascii=False))

    return {"endpoints": endpoints, "headers": real_headers,
            "engine": engine, "token": token, "traffic": traffic}


async def asyncio_wait_for(coro, timeout: float):
    import asyncio
    return await asyncio.wait_for(coro, timeout)
'@

# ================================ discover.py ===============================
Write-File "discover.py" @'
#!/usr/bin/env python3
"""Diagnostico: abre DAZN Bet con navegador real y volca TODO el trafico JSON.

Uso:  python discover.py --show-browser
Salida: state/traffic.json + resumen por consola
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

from stealth import CookieStore, Identity, ProxyPool, is_blocked, warmup
from stealth.waf import pw_module
from curl_cffi.requests import AsyncSession


async def probe(fetcher_urls, headers, token) -> list:
    out = []
    async with AsyncSession(impersonate="chrome124", timeout=20) as s:
        for u in fetcher_urls:
            url = u.replace("{token}", token or "") if "{token}" in u else u
            try:
                r = await s.get(url, headers=headers)
                verdict = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
                if is_blocked(r):
                    verdict += " (bloqueado)"
                out.append((url, verdict, len(r.text)))
            except Exception as e:
                out.append((url, f"error: {type(e).__name__}", 0))
    return out


async def main_async(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    Path("state").mkdir(exist_ok=True)

    pool = ProxyPool(Path("proxies.txt"))
    identity = Identity()
    store = CookieStore(Path("state/cookies.json"))
    proxy = pool.current()
    await pool.geo(proxy, identity)

    warmed = await warmup(identity, store, proxy,
                          headless=not args.show_browser, dump=True)
    ep, token, traffic = warmed["endpoints"], warmed["token"], warmed["traffic"]

    print("\n================= RESUMEN DE TRAFICO =================")
    print(f"respuestas JSON capturadas : {len(traffic)}")
    print(f"hosts contactados          : {ep['hosts'] or 'ninguno'}")
    print(f"token detectado            : {'si' if token else 'no'}")
    print(f"urls de oferta encontradas : {len(ep['raw'])}")

    if traffic:
        print("\n--- top hosts por numero de respuestas JSON ---")
        for host, n in Counter(
                t["url"].split("/")[2] for t in traffic).most_common(10):
            print(f"  {n:>4}  {host}")

        print("\n--- respuestas mas grandes ---")
        for t in sorted(traffic, key=lambda x: -x["size"])[:12]:
            print(f"  {t['size']:>8} B  {t['url'][:110]}")
            if t["keys"]:
                print(f"            claves: {t['keys']}")

    print("\n--- endpoints de oferta elegidos ---")
    for u in ep["raw"][:10]:
        print(f"  {u[:130]}")
    if not ep["raw"]:
        print("  (ninguno: se usaran plantillas adivinadas)")

    if args.probe:
        print("\n--- prueba directa de candidatos ---")
        headers = identity.headers("https://www.daznbet.es/es-es/sports")
        for url, verdict, size in await probe(ep["list"][:12], headers, token):
            print(f"  [{verdict:<22}] {size:>8} B  {url[:110]}")

    print("\nvolcado completo en state/traffic.json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--probe", action="store_true", help="prueba los candidatos y muestra el status")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))
'@

# ============================ audit_stealth.py fix ==========================
Write-File "audit_stealth.py" @'
#!/usr/bin/env python3
"""Auditoria de sigilo: verifica que no hay fugas de automatizacion."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from curl_cffi.requests import AsyncSession

from stealth import CookieStore, Identity, ProxyPool, warmup
from stealth.patches import context_options, launch_args, stealth_script
from stealth.waf import pw_module

RESET, RED, GREEN, YEL = "\033[0m", "\033[31m", "\033[32m", "\033[33m"


def report(results: list) -> int:
    fails = 0
    print("\n=== AUDITORIA DE SIGILO ===\n")
    for name, ok, detail in results:
        tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        if not ok:
            fails += 1
        print(f"[{tag}] {name:<34} {detail}")
    total = len(results)
    print(f"\nresultado: {total - fails}/{total} comprobaciones OK")
    if fails:
        print(f"{YEL}revisa los FAIL antes de dejar el scraper en produccion{RESET}")
    return fails


async def tls_check(identity, proxy) -> tuple:
    kw = {"impersonate": identity.imp, "timeout": 15}
    if proxy:
        kw["proxies"] = proxy.curl()
    try:
        async with AsyncSession(**kw) as s:
            r = await s.get("https://tls.peet.ws/api/all")
            d = r.json()
        ja3 = d.get("tls", {}).get("ja3_hash")
        akamai = (d.get("http2") or {}).get("akamai_fingerprint_hash")
        proto = d.get("http_version")
        ok = bool(ja3) and proto == "h2"
        return ("TLS/JA3 + HTTP2 fingerprint", ok,
                f"ja3={ja3} akamai={akamai} proto={proto}")
    except Exception as e:
        return ("TLS/JA3 + HTTP2 fingerprint", False, f"error: {e}")


async def browser_check(identity, proxy, headless, pw) -> list:
    launch = {"headless": headless, "args": launch_args()}
    if proxy:
        launch["proxy"] = proxy.playwright()
    try:
        browser = await pw.chromium.launch(channel="chrome", **launch)
    except Exception:
        browser = await pw.chromium.launch(**launch)

    ctx = await browser.new_context(**context_options(identity))
    await ctx.add_init_script(stealth_script(identity))
    page = await ctx.new_page()
    await page.goto("https://www.daznbet.es/robots.txt", wait_until="domcontentloaded")

    probe_js = """() => {
      const gl = document.createElement('canvas').getContext('webgl');
      const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
      return {
        webdriver: navigator.webdriver,
        plugins: navigator.plugins.length,
        vendor: navigator.vendor,
        languages: navigator.languages,
        cores: navigator.hardwareConcurrency,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        glVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
        uaData: navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand) : null,
        chromeRuntime: !!(window.chrome && window.chrome.runtime),
        notifPerm: Notification.permission,
        outerW: window.outerWidth, innerW: window.innerWidth,
      };
    }"""
    v = await page.evaluate(probe_js)
    ua = await page.evaluate("() => navigator.userAgent")
    await browser.close()

    return [
        ("navigator.webdriver", v["webdriver"] is None, f"valor={v['webdriver']}"),
        ("plugins presentes", v["plugins"] > 0, f"{v['plugins']} plugins"),
        ("navigator.vendor", v["vendor"] == "Google Inc.", v["vendor"]),
        ("languages coherentes", v["languages"][0] == identity.locale, str(v["languages"][:2])),
        ("hardwareConcurrency", v["cores"] == identity.profile["cores"], str(v["cores"])),
        ("timezone == identidad", v["tz"] == identity.timezone_id, v["tz"]),
        ("WebGL vendor spoof", v["glVendor"] == "Google Inc. (Intel)", str(v["glVendor"])),
        ("userAgentData coherente",
         v["uaData"] == [b["brand"] for b in identity.brands_json()], str(v["uaData"])),
        ("chrome.runtime", bool(v["chromeRuntime"]), "presente"),
        ("Notification.permission", v["notifPerm"] == "default", v["notifPerm"]),
        ("geometria de ventana", v["outerW"] == v["innerW"], f"{v['outerW']}/{v['innerW']}"),
        ("UA sin HeadlessChrome", "HeadlessChrome" not in ua, ua[:60]),
    ]


async def main_async(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pool = ProxyPool(Path("proxies.txt"))
    identity = Identity()
    store = CookieStore(Path("state/cookies.json"))
    proxy = pool.current()
    await pool.geo(proxy, identity)

    results = [("pais de salida detectado", identity.country is not None, str(identity.country))]
    results.append(await tls_check(identity, proxy))

    # una sola instancia de Playwright para todo (evita TargetClosedError en Windows)
    pw = await pw_module()().start()
    try:
        results += await browser_check(identity, proxy, not args.show_browser, pw)
        if args.warm:
            warmed = await warmup(identity, store, proxy,
                                  headless=not args.show_browser, playwright=pw)
            results.append(("aws-waf-token obtenido", store.has("aws-waf-token"),
                            ",".join(store.names())[:80]))
            results.append(("endpoints de oferta descubiertos",
                            bool(warmed["endpoints"]["raw"]),
                            f"{len(warmed['endpoints']['raw'])} urls reales"))
    finally:
        try:
            await pw.stop()
        except Exception:
            pass

    return report(results)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--warm", action="store_true", help="incluye warmup y comprobacion de WAF")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))
'@

Write-Host "parche aplicado." -ForegroundColor Green
Write-Host "Siguiente:  python discover.py --show-browser --probe" -ForegroundColor Cyan