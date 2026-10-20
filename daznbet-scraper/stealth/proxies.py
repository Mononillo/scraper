"""Pool de proxies sticky con deteccion de pais de salida."""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from curl_cffi.requests import AsyncSession

log = logging.getLogger("stealth.proxies")

_RE = re.compile(r"^(?P<scheme>\w+)://(?:(?P<user>[^:@]+):(?P<pwd>[^@]+)@)?"
                 r"(?P<host>[^:/]+):(?P<port>\d+)$")


@dataclass
class Proxy:
    raw: str
    server: str
    username: str | None = None
    password: str | None = None
    country: str | None = None
    fails: int = 0

    def curl(self) -> dict:
        return {"http": self.raw, "https": self.raw}

    def playwright(self) -> dict:
        d = {"server": self.server}
        if self.username:
            d["username"] = self.username
            d["password"] = self.password or ""
        return d


class ProxyPool:
    def __init__(self, path: Path | None):
        self.items: list[Proxy] = []
        self._current: Proxy | None = None
        if path and Path(path).exists():
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _RE.match(line)
                if m:
                    self.items.append(Proxy(raw=line,
                                            server=f"{m['scheme']}://{m['host']}:{m['port']}",
                                            username=m["user"], password=m["pwd"]))
                else:
                    self.items.append(Proxy(raw=line, server=line))

    @property
    def empty(self) -> bool:
        return not self.items

    def current(self) -> Proxy | None:
        if self._current is None and self.items:
            self._current = random.choice(self.items)
        return self._current

    def rotate(self) -> Proxy | None:
        if not self.items:
            return None
        pool = [p for p in self.items if p is not self._current] or self.items
        self._current = random.choice(pool)
        return self._current

    def penalize(self) -> None:
        if self._current:
            self._current.fails += 1

    async def geo(self, proxy: Proxy | None, identity) -> str | None:
        """Alinea locale/timezone con el pais real de la IP de salida."""
        kwargs = {"impersonate": "chrome124", "timeout": 12}
        if proxy:
            kwargs["proxies"] = proxy.curl()
        for url in ("https://ipwho.is/", "https://ipapi.co/json/"):
            try:
                async with AsyncSession(**kwargs) as s:
                    r = await s.get(url)
                    d = r.json()
                cc = (d.get("country_code") or d.get("country") or "").upper()[:2]
                if len(cc) == 2:
                    if proxy:
                        proxy.country = cc
                    identity.apply_geo(cc)
                    log.info("geo salida = %s -> locale %s / %s",
                             cc, identity.locale, identity.timezone_id)
                    return cc
            except Exception as e:
                log.debug("geo fallo con %s: %s", url, e)
        log.warning("no se pudo determinar el pais de salida; uso identidad ES")
        identity.apply_geo("ES")
        return None