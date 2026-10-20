"""Jar persistente: conserva aws-waf-token y sesion entre reinicios."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class CookieStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.jar: dict[tuple, dict] = {}
        self.loaded_at = time.time()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for c in json.loads(self.path.read_text(encoding="utf-8")):
                self.jar[(c.get("name"), c.get("domain", ""), c.get("path", "/"))] = c
        except Exception:
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.jar.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def ingest_playwright(self, cookies: list) -> None:
        for c in cookies:
            c = dict(c)
            c.pop("sameSite", None)
            self.jar[(c.get("name"), c.get("domain", ""), c.get("path", "/"))] = c
        self.save()

    def ingest_response(self, r) -> None:
        try:
            for c in r.cookies.jar:
                self.jar[(c.name, c.domain, c.path)] = {
                    "name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path, "secure": bool(c.secure), "expires": c.expires,
                }
        except Exception:
            pass

    def apply(self, session) -> None:
        for c in self.jar.values():
            try:
                session.cookies.set(c["name"], c["value"],
                                    domain=(c.get("domain") or "").lstrip("."),
                                    path=c.get("path") or "/")
            except Exception:
                pass

    def as_playwright(self) -> list:
        out = []
        for c in self.jar.values():
            if c.get("domain") and c.get("path") and c.get("name"):
                out.append({"name": c["name"], "value": c["value"],
                            "domain": c["domain"], "path": c["path"]})
        return out

    def has(self, needle: str) -> bool:
        return any(needle in (k[0] or "") for k in self.jar)

    def names(self) -> list:
        return sorted({k[0] for k in self.jar})