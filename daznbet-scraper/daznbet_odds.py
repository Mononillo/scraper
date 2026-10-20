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
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
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
        d = json.loads(ENDPOINTS_FILE.read_text(encoding="utf-8"))
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