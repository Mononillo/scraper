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