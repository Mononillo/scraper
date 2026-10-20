#!/usr/bin/env python3
"""Recon: mapa completo de la API de DAZN Bet con claves y estructura JSON.

Uso:  python recon.py --show-browser --probe
Salida: informe por consola + state/recon.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from curl_cffi.requests import AsyncSession

from stealth import CookieStore, Identity, ProxyPool
from stealth.patches import context_options, launch_args, stealth_script
from stealth.waf import _interact, _navigate, clean_headers, pw_module

STATE = Path("state")
RECON = STATE / "recon.json"
MAX_BODY = 3_000_000
ODDS_WORDS = ("odds", "price", "market", "selection", "outcome", "runner",
              "betoffer", "bet_offer", "line", "handicap", "coupon", "event")


def struct(o, depth: int = 3) -> str:
    if isinstance(o, dict):
        if depth <= 0:
            return "{...}"
        items = list(o.items())[:10]
        return "{" + ", ".join(f"{k}: {struct(v, depth - 1)}" for k, v in items) + "}"
    if isinstance(o, list):
        if not o:
            return "[]"
        if depth <= 0:
            return "[...]"
        return "[" + struct(o[0], depth - 1) + f" ...x{len(o)}]"
    if isinstance(o, bool):
        return "bool"
    if isinstance(o, int):
        return "int"
    if isinstance(o, float):
        return "float"
    if isinstance(o, str):
        return f'"{o[:28]}"'
    return type(o).__name__


def path_key(url: str) -> tuple:
    parts = url.split("/")
    host = parts[2] if len(parts) > 2 else ""
    path = "/" + "/".join(parts[3:]).split("?")[0]
    path = re.sub(r"/\d{3,}", "/{id}", path)
    return host, path


async def main_async(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    STATE.mkdir(exist_ok=True)

    pool = ProxyPool(Path("proxies.txt"))
    identity = Identity()
    store = CookieStore(STATE / "cookies.json")
    proxy = pool.current()
    await pool.geo(proxy, identity)

    groups: dict = defaultdict(lambda: {
        "count": 0, "methods": Counter(), "statuses": Counter(),
        "cts": Counter(), "sizes": [], "keys": None, "struct": None,
        "sample": None, "example": None, "method": "GET", "body": None,
        "headers": {},
    })
    reqmeta: dict = {}
    ws_urls: list = []

    pw = await pw_module()().start()
    try:
        launch = {"headless": not args.show_browser, "args": launch_args()}
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

        def on_request(r):
            try:
                if r.resource_type in ("xhr", "fetch"):
                    try:
                        pd = r.post_data
                    except Exception:
                        pd = None
                    reqmeta[r.url] = {"method": r.method, "headers": dict(r.headers), "body": pd}
            except Exception:
                pass

        async def on_response(resp):
            try:
                url = resp.url
                if not url.startswith("http"):
                    return
                meta = reqmeta.get(url, {})
                host, path = path_key(url)
                g = groups[f"{host}{path}"]
                g["count"] += 1
                g["methods"][meta.get("method", "GET")] += 1
                g["statuses"][resp.status] += 1
                ct = (resp.headers or {}).get("content-type", "").split(";")[0]
                g["cts"][ct or "?"] += 1
                g["example"] = g["example"] or url
                g["method"] = meta.get("method", "GET")
                g["body"] = meta.get("body")
                g["headers"] = clean_headers(meta.get("headers") or {})
                clen = int((resp.headers or {}).get("content-length") or 0)
                if clen and clen > MAX_BODY:
                    return
                if "json" in ct:
                    body = await resp.text()
                    g["sizes"].append(len(body))
                    if g["keys"] is None:
                        try:
                            d = json.loads(body)
                            if isinstance(d, dict):
                                g["keys"] = list(d.keys())[:12]
                            g["struct"] = struct(d)
                            g["sample"] = body[:500]
                        except Exception:
                            pass
                elif ct in ("text/plain", "application/octet-stream", ""):
                    body = await resp.text()
                    g["sizes"].append(len(body))
                    if g["struct"] is None:
                        g["struct"] = f"<{ct or 'bin'} {len(body)}B>"
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", lambda ws: ws_urls.append(ws.url))

        try:
            await asyncio.wait_for(_navigate(page), 110)
        except Exception as e:
            print("navegacion:", e)
        try:
            await _interact(page)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        try:
            store.ingest_playwright(await ctx.cookies())
        except Exception:
            pass
        await browser.close()
    finally:
        try:
            await pw.stop()
        except Exception:
            pass

    # ------------------------------- informe -------------------------------
    by_host: dict = defaultdict(list)
    for key, g in groups.items():
        by_host[key.split("/")[0]].append((key, g))

    print("\n============ API DE DAZN BET (agrupada) ============")
    for host in sorted(by_host, key=lambda h: -sum(sum(g["sizes"]) for _, g in by_host[h])):
        items = by_host[host]
        total = sum(sum(g["sizes"]) for _, g in items)
        print(f"\n### {host}  ({len(items)} rutas, {total // 1024} KB)")

        def oddsy(kv):
            _, g = kv
            blob = (str(g.get("keys")) + str(g.get("struct")) + str(g.get("example"))).lower()
            return any(w in blob for w in ODDS_WORDS)

        for key, g in sorted(items, key=lambda kv: (-oddsy(kv), -max(kv[1]["sizes"] or [0]))):
            path = "/" + key.split("/", 1)[1] if "/" in key else key
            size = max(g["sizes"] or [0])
            mark = "*" if oddsy((key, g)) else " "
            print(f"{mark}[{g['method']:<4}] x{g['count']:<3} {dict(g['statuses'])} "
                  f"{list(g['cts'])[0]:<28} {size:>8}B  {path[:95]}")
            if g["keys"]:
                print(f"      claves: {g['keys']}")
            if g["struct"]:
                print(f"      struct: {g['struct'][:300]}")

    if ws_urls:
        print("\n============ WEBSOCKETS ============")
        for u in sorted(set(ws_urls)):
            print(" ", u[:140])

    # ------------------------------- probe --------------------------------
    if args.probe:
        print("\n============ REPLAY GET/POST DE CANDIDATOS * ============")
        cands = []
        for key, g in groups.items():
            blob = (str(g.get("keys")) + str(g.get("struct")) + str(g.get("example"))).lower()
            if not any(w in blob for w in ODDS_WORDS):
                continue
            if not g["example"] or max(g["sizes"] or [0]) > 600_000:
                continue
            cands.append({"url": g["example"], "method": g["method"],
                          "body": g["body"], "headers": g["headers"],
                          "size": max(g["sizes"] or [0])})
        cands.sort(key=lambda c: -c["size"])

        async with AsyncSession(impersonate=identity.imp, timeout=25,
                                proxies=proxy.curl() if proxy else None) as s:
            store.apply(s)
            for c in cands[:14]:
                for method in ("GET", "POST"):
                    try:
                        if method == "POST":
                            r = await s.post(c["url"], data=c["body"],
                                             headers=c["headers"] or identity.headers("https://www.daznbet.es/es-es/sports"),
                                             timeout=25)
                        else:
                            r = await s.get(c["url"],
                                            headers=c["headers"] or identity.headers("https://www.daznbet.es/es-es/sports"),
                                            timeout=25)
                        head = (r.text or "")[:160].replace("\n", " ")
                        print(f"  [{method:<4}] HTTP {r.status_code:<3} {len(r.text):>8}B "
                              f"{c['url'][:90]}")
                        print(f"         {head[:150]}")
                    except Exception as e:
                        print(f"  [{method:<4}] err {type(e).__name__:<10} {c['url'][:90]}")

    out = {k: {**v, "sizes": v["sizes"][:20], "methods": dict(v["methods"]),
               "statuses": dict(v["statuses"]), "cts": dict(v["cts"])}
           for k, v in groups.items()}
    RECON.write_text(json.dumps({"routes": out, "websockets": sorted(set(ws_urls))},
                                indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nescrito {RECON}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--probe", action="store_true", help="replay GET/POST de los candidatos")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))