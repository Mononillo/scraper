"""Warmup v3: captura trafico con metodo, headers y body exactos.

Genera candidatos ordenados por probabilidad y NO inventa plantillas para
hosts de terceros (firebase, analytics...). Los candidatos se verifican en
discover.py antes de usarse.
"""
from __future__ import annotations

import asyncio
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
OFFER_HINTS = ("offering", "betoffer", "listview", "coupon")
TRAFFIC_FILE = Path("state/traffic.json")
ENDPOINTS_FILE = Path("state/endpoints.json")


def pw_module():
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


def fill(url: str, token: str | None = None, **kw) -> str:
    u = url.replace("{token}", token or "") if "{token}" in url else url
    for k, v in kw.items():
        u = u.replace("{" + k + "}", str(v))
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


def clean_headers(h: dict, ua: str | None = None, al: str | None = None) -> dict:
    out = {k: v for k, v in (h or {}).items()
           if k.lower() not in ("host", "content-length", "accept-encoding",
                                "cookie", ":authority", ":method", ":path", ":scheme")}
    if ua:
        out["user-agent"] = ua
    if al:
        out["accept-language"] = al
    return out


def build_candidates(captured: list) -> list:
    """Candidatos ordenados por probabilidad de ser el endpoint de cuotas."""
    cands = []
    for c in captured:
        u, low, body = c["url"], c["url"].lower(), c["body"]
        score = 0
        if "daznbet" in low:
            score += 6
        if "offering/api" in low:
            score += 12
        if "listview" in low:
            score += 9
        if "betoffer" in low:
            score += 7
        if any(h in low for h in OFFER_HINTS):
            score += 3
        if _looks_like_offer(body):
            score += 14
        if score < 6:
            continue
        cands.append({
            "url": u,
            "method": (c.get("method") or "GET").upper(),
            "body": c.get("body_raw"),
            "headers": clean_headers(c.get("headers") or {}),
            "score": score,
            "size": len(body),
            "kind": "deep" if ("/betoffer/" in low or "/event/" in low) else "list",
        })
    cands.sort(key=lambda x: (-x["score"], -x["size"]))
    return cands


def build_fallbacks(captured: list, js: str) -> list:
    """Plantillas genericas, solo para hosts de DAZN (nada de firebase)."""
    hosts = []
    for c in captured:
        hm = re.match(r"(https://[^/]+)", c["url"])
        if hm and "daznbet" in hm.group(1):
            hosts.append(hm.group(1))
    hosts += re.findall(r"https://sb-[a-z0-9.\-]+\.daznbet\.[a-z]+", js or "")
    hosts = _dedup(hosts) or SB_FALLBACK

    out = []
    for host in hosts:
        for gid in ("{gid}", "2000", "1000", "4000"):
            for last in ("current.json", "live.json", "all.json"):
                out.append({"url": f"{host}/offering/api/v3/{gid}/listView/all/all/all/all/{last}",
                            "method": "GET", "body": None, "headers": {}, "kind": "list",
                            "score": 1, "size": 0})
        out.append({"url": f"{host}/offering/api/v3/{{gid}}/betoffer/{{betofferId}}.json",
                    "method": "GET", "body": None, "headers": {}, "kind": "deep",
                    "score": 1, "size": 0})
    for gid in re.findall(r"offering/api/v3/([A-Za-z0-9_\-]{1,10})/", js or ""):
        for host in hosts:
            out.append({"url": f"{host}/offering/api/v3/{gid}/listView/all/all/all/all/current.json",
                        "method": "GET", "body": None, "headers": {}, "kind": "list",
                        "score": 1, "size": 0})
    return out


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
    pw_factory = pw_module()
    owns = playwright is None
    pw = playwright or await pw_factory().start()

    captured: list[dict] = []
    req_meta: dict[str, dict] = {}
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

        def on_request(r):
            try:
                if r.resource_type not in ("xhr", "fetch"):
                    return
                try:
                    body_raw = r.post_data
                except Exception:
                    body_raw = None
                req_meta[r.url] = {"method": r.method, "headers": dict(r.headers),
                                   "body_raw": body_raw}
            except Exception:
                pass

        async def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "").lower()
                if "json" not in ct or resp.status != 200:
                    return
                body = await resp.text()
                if not body or len(body) > 5_000_000:
                    return
                meta = req_meta.get(resp.url, {})
                captured.append({"url": resp.url, "status": resp.status, "ct": ct,
                                 "body": body, "method": meta.get("method", "GET"),
                                 "headers": meta.get("headers", {}),
                                 "body_raw": meta.get("body_raw")})
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("crash", lambda _: log.warning("pagina crasheada durante el warmup"))

        try:
            await asyncio.wait_for(_navigate(page), 100)
        except Exception as e:
            log.debug("navegacion: %s", e)
        try:
            await _interact(page)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

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

    token = None
    for c in captured:
        mm = re.search(r"[?&]token=([^&]+)", c["url"])
        if mm:
            token = mm.group(1)
            break

    cands = build_candidates(captured)
    falls = build_fallbacks(captured, js)
    lists = [c["url"] for c in cands if c["kind"] == "list"]
    deeps = [c["url"] for c in cands if c["kind"] == "deep"]
    endpoints = {
        "raw": _dedup(lists),
        "list": _dedup(lists + [f["url"] for f in falls if f["kind"] == "list"]),
        "deep": _dedup(deeps + [f["url"] for f in falls if f["kind"] == "deep"]),
    }

    log.info("warmup [%s] %d cookies | %d respuestas JSON | %d candidatos | %d POST | waf=%s",
             engine, len(cookies), len(captured), len(cands),
             sum(1 for c in cands if c["method"] == "POST"), store.has("aws-waf-token"))
    if not cands:
        log.warning("sin candidatos: revisa state/traffic.json")

    traffic = []
    for c in captured:
        keys = []
        try:
            data = json.loads(c["body"])
            if isinstance(data, dict):
                keys = list(data.keys())[:10]
        except Exception:
            pass
        traffic.append({"url": c["url"], "status": c["status"], "ct": c["ct"],
                        "method": c["method"], "size": len(c["body"]),
                        "keys": keys, "snippet": c["body"][:180].replace("\n", " ")})

    if dump:
        TRAFFIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRAFFIC_FILE.write_text(json.dumps(
            {"token": token, "candidates": cands, "traffic": traffic},
            indent=2, ensure_ascii=False), encoding="utf-8")

    return {"candidates": cands, "endpoints": endpoints, "engine": engine,
            "token": token, "traffic": traffic}