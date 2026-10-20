#!/usr/bin/env python3
"""Verifica los endpoints de DAZN Bet y escribe state/endpoints.json.

Uso:  python discover.py --show-browser
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi.requests import AsyncSession

from stealth import CookieStore, Identity, ProxyPool, warmup
from stealth.waf import ENDPOINTS_FILE, clean_headers, fill, is_blocked

OFFER_KEYS = ("betOffers", '"outcomes"', "betOfferType", "listView")
GOOD = "\033[32m"; BAD = "\033[31m"; DIM = "\033[2m"; END = "\033[0m"


def looks_good(r) -> bool:
    if r.status_code != 200:
        return False
    head = (r.text or "")[:6000]
    return any(k in head for k in OFFER_KEYS)


async def try_spec(s, spec, identity, token, **fills) -> tuple:
    url = fill(spec["url"], token, **fills)
    headers = clean_headers(spec.get("headers"), identity.ua, identity.accept_language)
    if not headers:
        headers = identity.headers("https://www.daznbet.es/es-es/sports")
    try:
        if (spec.get("method") or "GET").upper() == "POST":
            r = await s.post(url, data=spec.get("body"), headers=headers, timeout=25)
        else:
            r = await s.get(url, headers=headers, timeout=25)
    except Exception as e:
        return url, f"error {type(e).__name__}", 0, False
    return url, f"HTTP {r.status_code}", len(r.text), looks_good(r)


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
    cands, traffic, token = warmed["candidates"], warmed["traffic"], warmed["token"]

    print("\n================= TRAFICO CAPTURADO =================")
    print(f"respuestas JSON : {len(traffic)}")
    print(f"candidatos      : {len(cands)}")
    print(f"token           : {'si' if token else 'no'}")
    if traffic:
        print("\n--- hosts con mas respuestas JSON ---")
        for host, n in Counter(t["url"].split("/")[2] for t in traffic).most_common(8):
            print(f"  {n:>4}  {host}")

    print("\n--- top 20 candidatos ---")
    for c in cands[:20]:
        print(f"  [{c['method']:<4}] score={c['score']:<3} {c['size']:>8} B  {c['url'][:100]}")

    # ------------------- verificacion real -------------------
    print("\n================= VERIFICACION =================")
    headers = identity.headers("https://www.daznbet.es/es-es/sports")
    ok_list, ok_deep, betoffer_id = [], [], None

    async with AsyncSession(impersonate=identity.imp, timeout=25) as s:
        for spec in cands[:24]:
            url, verdict, size, good = await try_spec(s, spec, identity, token)
            tag = f"{GOOD}OK{END}" if good else f"{BAD}{verdict}{END}"
            print(f"  [{tag:<28}] {size:>8} B  {url[:100]}")
            if good and spec["kind"] == "list" and len(ok_list) < 3:
                ok_list.append(spec)
            elif good and spec["kind"] == "deep" and len(ok_deep) < 2:
                ok_deep.append(spec)

        # si hay list OK, saca un betofferId real y prueba el deep
        if ok_list and not ok_deep:
            try:
                r = await try_spec(s, ok_list[0], identity, token)
                pass
            except Exception:
                pass
            try:
                headers2 = clean_headers(ok_list[0].get("headers"), identity.ua,
                                         identity.accept_language) or headers
                url = fill(ok_list[0]["url"], token)
                r = await s.get(url, headers=headers2, timeout=25)
                data = r.json()
                for bo in (data.get("betOffers") or [])[:1]:
                    betoffer_id = str(bo.get("id"))
            except Exception as e:
                print(f"  {DIM}no se pudo extraer betofferId: {e}{END}")

        if betoffer_id:
            tpl = {"url": ok_list[0]["url"].replace("/listView/", "/betoffer/")
                   if False else None}
            host = ok_list[0]["url"].split("/offering/")[0]
            deep_url = f"{host}/offering/api/v3/{{gid}}/betoffer/{{betofferId}}.json"
            for gid in ("{gid}", "2000", "1000", "4000"):
                spec = {"url": deep_url, "method": "GET", "body": None,
                        "headers": ok_list[0].get("headers")}
                url, verdict, size, good = await try_spec(s, spec, identity, token,
                                                          gid=gid, betofferId=betoffer_id)
                tag = f"{GOOD}OK{END}" if good else f"{BAD}{verdict}{END}"
                print(f"  [{tag:<28}] deep {size:>8} B  {url[:100]}")
                if good:
                    ok_deep.append(spec)
                    break

    print("\n================= RESULTADO =================")
    if not ok_list:
        print(f"{BAD}ningun endpoint de listado respondio.{END}")
        print("revisa state/traffic.json y pasame el volcado.")
    else:
        print(f"{GOOD}{len(ok_list)} endpoint(s) de listado verificados{END}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "token": token,
        "list": ok_list,
        "deep": ok_deep,
        "betoffer_id_sample": betoffer_id,
    }
    ENDPOINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENDPOINTS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"escrito {ENDPOINTS_FILE}")
    return 0 if ok_list else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-browser", action="store_true")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))