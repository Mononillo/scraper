#!/usr/bin/env python3
"""patch3: fuerza UTF-8 en todas las escrituras/lecturas de fichero.

Windows usa cp1252 por defecto en open(), y los nombres de equipo traen
caracteres fuera de ese juego (chino, cirilico, flechas...).
"""
from __future__ import annotations

import py_compile
from pathlib import Path

EDITS = {
    "stealth/waf.py": [
        ('ensure_ascii=False))',
         'ensure_ascii=False), encoding="utf-8")'),
    ],
    "stealth/cookies.py": [
        ('tmp.write_text(json.dumps(list(self.jar.values()), ensure_ascii=False, indent=1))',
         'tmp.write_text(json.dumps(list(self.jar.values()), ensure_ascii=False, indent=1), '
         'encoding="utf-8")'),
        ('json.loads(self.path.read_text())',
         'json.loads(self.path.read_text(encoding="utf-8"))'),
    ],
    "stealth/proxies.py": [
        ('for line in Path(path).read_text().splitlines():',
         'for line in Path(path).read_text(encoding="utf-8").splitlines():'),
    ],
    "discover.py": [
        ('ensure_ascii=False))',
         'ensure_ascii=False), encoding="utf-8")'),
    ],
    "daznbet_odds.py": [
        ('ensure_ascii=False, separators=(",", ":")))',
         'ensure_ascii=False, separators=(",", ":")), encoding="utf-8")'),
        ('d = json.loads(ENDPOINTS_FILE.read_text())',
         'd = json.loads(ENDPOINTS_FILE.read_text(encoding="utf-8"))'),
    ],
}

INIT = '''"""Paquete stealth: identidad, cookies, proxies, parches y warmup."""
import sys

# consola en UTF-8: en Windows la consola tambien es cp1252 por defecto
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .identity import Identity, PROFILES
from .cookies import CookieStore
from .patches import stealth_script, launch_args, context_options
from .proxies import Proxy, ProxyPool
from .waf import Blocked, is_blocked, warmup, templateize

__all__ = ["Identity", "PROFILES", "CookieStore", "stealth_script",
           "launch_args", "context_options", "Proxy", "ProxyPool",
           "Blocked", "is_blocked", "warmup", "templateize"]
'''

fallos = 0
for fichero, pares in EDITS.items():
    p = Path(fichero)
    if not p.exists():
        print("falta el fichero:", fichero)
        fallos += 1
        continue
    txt = p.read_text(encoding="utf-8")
    for viejo, nuevo in pares:
        if viejo not in txt:
            print(f"NO ENCONTRADO en {fichero}: {viejo[:60]}")
            fallos += 1
            continue
        if nuevo in txt:
            print(f"ya aplicado en {fichero}: {viejo[:50]}")
            continue
        txt = txt.replace(viejo, nuevo, 1)
        print(f"aplicado en {fichero}: {viejo[:50]}")
    p.write_text(txt, encoding="utf-8")

Path("stealth/__init__.py").write_text(INIT, encoding="utf-8")
print("reescrito: stealth/__init__.py")

print()
for f in ("stealth/__init__.py", "stealth/waf.py", "stealth/cookies.py",
          "stealth/proxies.py", "discover.py", "daznbet_odds.py"):
    try:
        py_compile.compile(f, doraise=True)
        print("sintaxis OK :", f)
    except py_compile.PyCompileError as e:
        print("SINTAXIS MAL:", f)
        print(e)

print()
print("fallos:", fallos)
print("Siguiente:  python discover.py --show-browser")