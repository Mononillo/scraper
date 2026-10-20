"""Paquete stealth: identidad, cookies, proxies, parches y warmup."""
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
