$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Write-File([string]$Path, [string]$Content) {
    $full = Join-Path (Get-Location).Path $Path
    $dir  = Split-Path $full -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lf = $Content -replace "`r`n", "`n"
    [IO.File]::WriteAllText($full, $lf, (New-Object System.Text.UTF8Encoding($false)))
}

# ============================== stealth/waf.py v3 ===========================
Write-File "stealth\waf.py" @'
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
            indent=2, ensure_ascii=False))

    return {"candidates": cands, "endpoints": endpoints, "engine": engine,
            "token": token, "traffic": traffic}
'@

# ================================ discover.py ===============================
Write-File "discover.py" @'
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
    ENDPOINTS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"escrito {ENDPOINTS_FILE}")
    return 0 if ok_list else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-browser", action="store_true")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))
'@

# ============================ daznbet_odds.py v3 ============================
Write-File "daznbet_odds.py" @'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DAZN Bet -> todas las cuotas en JSON, cada 5 s, modo sigilo.

Usa los endpoints verificados en state/endpoints.json (generados por discover.py).
Si no existen o caducan, los reconstruye con un warmup de navegador real.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests import errors as cerrors

from stealth import Blocked, CookieStore, Identity, ProxyPool, is_blocked, warmup
from stealth.waf import ENDPOINTS_FILE, clean_headers, fill

INTERVAL = float(os.getenv("INTERVAL", "5"))
JITTER = float(os.getenv("JITTER", "0.35"))
TIMEOUT = 25
MAX_CONC = 3
DEEP_PER_CYCLE = int(os.getenv("DEEP_PER_CYCLE", "10"))
EVENT_TTL = 900
WARM_EVERY = float(os.getenv("WARM_EVERY", "1200"))
SESSION_RESET = float(os.getenv("SESSION_RESET", "1800"))
PAUSE_EVERY = 40
REFERER = "https://www.daznbet.es/es-es/sports"

DATA = Path("data"); SNAP = DATA / "snapshots"; STATE = Path("state")
for d in (DATA, SNAP, STATE):
    d.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("daznbet")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Fetcher:
    def __init__(self, identity: Identity, store: CookieStore, proxy):
        self.identity, self.store, self.proxy = identity, store, proxy
        self.session: AsyncSession | None = None
        self.sem = asyncio.Semaphore(MAX_CONC)
        self._last = 0.0
        self.token: str | None = None

    async def start(self) -> None:
        kw: dict[str, Any] = {"impersonate": self.identity.imp, "timeout": TIMEOUT}
        if self.proxy:
            kw["proxies"] = self.proxy.curl()
        self.session = AsyncSession(**kw)
        self.store.apply(self.session)

    async def close(self) -> None:
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None

    def headers(self, spec: dict) -> dict:
        h = clean_headers(spec.get("headers"), self.identity.ua, self.identity.accept_language)
        return h or self.identity.headers(referer=REFERER)

    async def request(self, spec: dict, **fills) -> Any:
        async with self.sem:
            gap = 0.12 + random.uniform(0, 0.2)
            dt = time.monotonic() - self._last
            if dt < gap:
                await asyncio.sleep(gap - dt)
            self._last = time.monotonic()

            assert self.session
            url = fill(spec["url"], self.token, **fills)
            headers = self.headers(spec)
            method = (spec.get("method") or "GET").upper()
            if method == "POST":
                r = await self.session.post(url, data=spec.get("body"),
                                            headers=headers, timeout=TIMEOUT)
            else:
                r = await self.session.get(url, headers=headers,
                                           allow_redirects=True, timeout=TIMEOUT)
            self.store.ingest_response(r)
            if is_blocked(r):
                raise Blocked(f"HTTP {r.status_code} {r.headers.get('x-amzn-waf-action', '')}")
            if r.status_code >= 400:
                raise Blocked(f"HTTP {r.status_code} {url[:80]}")
            try:
                return r.json()
            except Exception:
                return {"_raw": r.text[:20000]}


def new_event(ev: dict) -> dict:
    path = ev.get("path") or []

    def term(t):
        return next((p.get("term") for p in path if p.get("termType") == t), None)

    def key(t):
        return next((p.get("termKey") for p in path if p.get("termType") == t), None)

    state = ev.get("state") or ("STARTED" if ev.get("liveData") else "UNKNOWN")
    return {
        "id": str(ev.get("id")),
        "sport": term("SPORT"), "sport_key": key("SPORT") or ev.get("sport"),
        "region": term("REGION"), "league": term("COMPETITION"),
        "name": ev.get("name") or " - ".join(
            filter(None, [ev.get("homeName"), ev.get("awayName")])),
        "home": ev.get("homeName"), "away": ev.get("awayName"),
        "start": ev.get("start"), "state": state, "live": state == "STARTED",
        "score": ev.get("score") or (ev.get("liveData") or {}).get("score"),
        "seen_at": utc(), "markets": {},
    }


def add_offer(node: dict, bo: dict, source: str) -> int:
    if not isinstance(bo, dict) or not bo.get("id"):
        return 0
    outcomes = [o for o in (bo.get("outcomes") or []) if isinstance(o, dict)]
    if not outcomes:
        return 0
    crit = bo.get("criterion") or {}
    btype = bo.get("betOfferType") or {}
    node["markets"][str(bo["id"])] = {
        "id": str(bo["id"]),
        "type": btype.get("englishName") or btype.get("name"),
        "name": btype.get("name") or crit.get("label") or "Mercado",
        "criterion": crit.get("label") or crit.get("englishLabel"),
        "line": crit.get("line") if crit.get("line") is not None else bo.get("line"),
        "state": bo.get("state"), "source": source, "updated": utc(),
        "outcomes": [{"id": str(o.get("id")), "label": o.get("label"),
                      "line": o.get("line"), "odds": o.get("odds"),
                      "status": o.get("status"), "participant": o.get("participant"),
                      "type": o.get("type")} for o in outcomes],
    }
    return len(outcomes)


def parse_payload(payload: Any, state: dict, source: str) -> int:
    if not isinstance(payload, dict):
        return 0
    offers = payload.get("betOffers") or payload.get("betoffers") or []
    single = payload.get("betOffer") or payload.get("betoffer")
    if isinstance(single, dict):
        offers = [single] + list(offers)

    odds = 0
    for e in payload.get("events") or []:
        ev = e.get("event") if isinstance(e, dict) and "event" in e else e
        if not isinstance(ev, dict) or not ev.get("id"):
            continue
        eid = str(ev["id"])
        fresh = new_event(ev)
        if eid in state:
            state[eid].update({k: v for k, v in fresh.items() if k != "markets"})
        else:
            state[eid] = fresh
        for bo in (ev.get("betOffers") or []):
            odds += add_offer(state[eid], bo, source)

    for bo in offers:
        if not isinstance(bo, dict):
            continue
        eid = str(bo.get("eventId") or "")
        if not eid:
            continue
        if eid not in state:
            state[eid] = {**new_event({"id": eid}), "id": eid, "name": f"evento {eid}"}
        odds += add_offer(state[eid], bo, source)
    return odds


def prune(state: dict) -> None:
    cut = time.time() - EVENT_TTL
    for eid in list(state):
        try:
            ts = datetime.fromisoformat(state[eid].get("seen_at", utc())).timestamp()
        except Exception:
            ts = time.time()
        if ts < cut:
            del state[eid]


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)


class ChangeLog:
    def __init__(self, path: Path):
        self.path = path
        self.prev: dict[str, Any] = {}

    def diff(self, state: dict) -> int:
        n = 0
        with self.path.open("a", encoding="utf-8") as fh:
            for ev in state.values():
                for mk, m in ev["markets"].items():
                    for o in m["outcomes"]:
                        k = f"{ev['id']}|{mk}|{o['id']}"
                        cur = o.get("odds")
                        if k in self.prev and self.prev[k] != cur:
                            fh.write(json.dumps({
                                "ts": utc(), "event": ev["name"], "event_id": ev["id"],
                                "market": m["name"], "criterion": m["criterion"],
                                "label": o["label"], "line": o["line"],
                                "old": self.prev[k], "new": cur,
                            }, ensure_ascii=False) + "\n")
                            n += 1
                        self.prev[k] = cur
        return n


def build_document(state: dict, meta: dict) -> dict:
    events = {eid: ev for eid, ev in state.items() if ev["markets"]}
    total = sum(len(m["outcomes"]) for ev in events.values() for m in ev["markets"].values())
    return {"meta": {**meta, "events": len(events), "odds": total,
                     "generated_at": utc()}, "events": events}


def load_endpoints() -> dict | None:
    if not ENDPOINTS_FILE.exists():
        return None
    try:
        d = json.loads(ENDPOINTS_FILE.read_text())
        if d.get("list"):
            return d
    except Exception:
        pass
    return None


def endpoints_from_warm(warmed: dict) -> dict:
    cands = warmed.get("candidates") or []
    lists = [c for c in cands if c["kind"] == "list"]
    deeps = [c for c in cands if c["kind"] == "deep"]
    return {"list": lists, "deep": deeps, "token": warmed.get("token")}


async def probe(fetcher: Fetcher, eps: dict) -> tuple[list, list]:
    """Valida los endpoints y devuelve los que responden con cuotas."""
    good_list, good_deep = [], []
    for spec in eps.get("list", [])[:24]:
        try:
            payload = await fetcher.request(spec)
            if isinstance(payload, dict) and ("events" in payload or "betOffers" in payload):
                good_list.append(spec)
                log.info("list OK [%s] %s", spec.get("method", "GET"), fill(spec["url"])[:100])
                if len(good_list) >= 3:
                    break
        except Blocked as e:
            log.warning("list bloqueado: %s", e)
            raise
        except Exception as e:
            log.debug("list fallo %s -> %s", spec["url"][:80], e)
    for spec in eps.get("deep", [])[:6]:
        if "{betofferId}" in spec["url"]:
            good_deep.append(spec)
            break
    return good_list, good_deep


async def run(args) -> None:
    pool = ProxyPool(Path("proxies.txt"))
    identity = Identity()
    store = CookieStore(STATE / "cookies.json")
    proxy = pool.current()
    await pool.geo(proxy, identity)
    if pool.empty:
        log.warning("sin proxies.txt -> conexion directa")

    eps = load_endpoints()
    if eps:
        log.info("endpoints cargados de %s (%s)", ENDPOINTS_FILE, eps.get("generated_at"))
    else:
        log.info("sin endpoints verificados; ejecutando warmup")
        warmed = await warmup(identity, store, proxy, headless=not args.show_browser)
        eps = endpoints_from_warm(warmed)

    fetcher = Fetcher(identity, store, proxy)
    fetcher.token = eps.get("token")
    await fetcher.start()
    live_list, live_deep = await probe(fetcher, eps)
    if not live_list:
        log.warning("ningun endpoint vivo. Ejecuta: python discover.py --show-browser")

    state: dict[str, dict] = {}
    changes = ChangeLog(DATA / "changes.jsonl")
    blocks = cycle = deep_cursor = 0
    last_warm = time.time()
    last_reset = time.time()

    log.info("polling %.1fs (+/-%.2f) | list=%d deep=%d | proxy=%s | geo=%s",
             INTERVAL, JITTER, len(live_list), len(live_deep),
             proxy.server if proxy else "directo", identity.country)

    while True:
        t0 = time.monotonic()
        cycle += 1
        try:
            for spec in live_list:
                parse_payload(await fetcher.request(spec), state, "list")

            if live_deep and DEEP_PER_CYCLE:
                ids = sorted({m["id"] for ev in state.values() for m in ev["markets"].values()})
                if ids:
                    batch = [ids[(deep_cursor + i) % len(ids)]
                             for i in range(min(DEEP_PER_CYCLE, len(ids)))]
                    deep_cursor = (deep_cursor + len(batch)) % len(ids)
                    for bid in batch:
                        try:
                            parse_payload(await fetcher.request(live_deep[0],
                                                                betofferId=bid), state, "deep")
                        except Blocked:
                            raise

            prune(state)
            n = changes.diff(state)
            meta = {"source": "daznbet.es", "cycle": cycle, "interval_s": INTERVAL,
                    "impersonate": identity.imp,
                    "proxy": proxy.server if proxy else None,
                    "proxy_country": identity.country,
                    "latency_ms": int((time.monotonic() - t0) * 1000), "blocked": False}
            doc = build_document(state, meta)
            write_json(DATA / "odds_latest.json", doc)
            if cycle % max(1, int(60 / INTERVAL)) == 0:
                write_json(SNAP / f"odds_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", doc)
            if n or cycle % 12 == 0:
                log.info("ciclo %d | %d eventos | %d cuotas | %d cambios | %dms",
                         cycle, doc["meta"]["events"], doc["meta"]["odds"], n,
                         meta["latency_ms"])

            blocks = 0

            if time.time() - last_warm > WARM_EVERY:
                log.info("recalibrado proactivo")
                await fetcher.close()
                warmed = await warmup(identity, store, proxy, headless=not args.show_browser)
                eps = endpoints_from_warm(warmed)
                fetcher = Fetcher(identity, store, proxy)
                fetcher.token = eps.get("token")
                await fetcher.start()
                live_list, live_deep = await probe(fetcher, eps)
                last_warm = last_reset = time.time()
            elif time.time() - last_reset > SESSION_RESET:
                log.info("reseteo de sesion TLS")
                await fetcher.close()
                await fetcher.start()
                last_reset = time.time()

            if cycle % PAUSE_EVERY == 0:
                await asyncio.sleep(random.uniform(1.5, 4.0))

            if args.once or (args.max_cycles and cycle >= args.max_cycles):
                return

        except Blocked as e:
            blocks += 1
            log.warning("bloqueo (%s) x%d", e, blocks)
            pool.penalize()
            if blocks >= 2:
                identity.rotate()
                if not pool.empty:
                    proxy = pool.rotate()
                    await pool.geo(proxy, identity)
                    log.info("rota identidad+proxy -> %s", identity.imp)
            await asyncio.sleep(min(2 ** blocks, 45) + random.uniform(0, 2))
            try:
                await fetcher.close()
                warmed = await warmup(identity, store, proxy, headless=not args.show_browser)
                eps = endpoints_from_warm(warmed)
                fetcher = Fetcher(identity, store, proxy)
                fetcher.token = eps.get("token")
                await fetcher.start()
                live_list, live_deep = await probe(fetcher, eps)
                last_warm = last_reset = time.time()
            except Exception as ex:
                log.error("recalibrado fallido: %s", ex)

        except (cerrors.RequestsError, asyncio.TimeoutError) as e:
            log.warning("red: %s", e)
        except Exception as e:
            log.exception("error: %s", e)

        if not args.once:
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.05, INTERVAL + random.uniform(-JITTER, JITTER) - elapsed))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--interval", type=float)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    global INTERVAL
    if args.interval:
        INTERVAL = args.interval
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("parado")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
'@

Write-Host "parche 2 aplicado." -ForegroundColor Green
Write-Host "Siguiente:  python discover.py --show-browser" -ForegroundColor Cyan