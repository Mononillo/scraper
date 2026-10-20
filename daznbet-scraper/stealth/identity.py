"""Identidades de navegador coherentes: TLS + UA + client hints + geo.

La clave del sigilo es que UA, sec-ch-ua, navigator.userAgentData,
Accept-Language, locale y timezone apunten TODOS a la misma version de Chrome
y al mismo pais que la IP de salida. Cualquier mezcla es un flag de bot.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

PROFILES = [
    dict(imp="chrome124", major=124, platform='"Windows"', platform_name="Windows",
         ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
         brands=[("Chromium", "124"), ("Google Chrome", "124"), ("Not-A.Brand", "99")],
         platform_version="10.0.0", cores=8, mem=8, viewport=(1440, 900)),
    dict(imp="chrome120", major=120, platform='"Windows"', platform_name="Windows",
         ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
         brands=[("Not_A Brand", "8"), ("Chromium", "120"), ("Google Chrome", "120")],
         platform_version="10.0.0", cores=8, mem=8, viewport=(1536, 864)),
    dict(imp="chrome116", major=116, platform='"Windows"', platform_name="Windows",
         ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"),
         brands=[("Chromium", "116"), ("Not)A;Brand", "24"), ("Google Chrome", "116")],
         platform_version="10.0.0", cores=12, mem=8, viewport=(1366, 768)),
    dict(imp="chrome110", major=110, platform='"Windows"', platform_name="Windows",
         ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"),
         brands=[("Chromium", "110"), ("Not A(Brand", "24"), ("Google Chrome", "110")],
         platform_version="10.0.0", cores=4, mem=4, viewport=(1600, 900)),
]

GEO = {
    "ES": ("es-ES", "Europe/Madrid", "es-ES,es;q=0.9,en;q=0.8"),
    "IT": ("it-IT", "Europe/Rome", "it-IT,it;q=0.9,en;q=0.8"),
    "DE": ("de-DE", "Europe/Berlin", "de-DE,de;q=0.9,en;q=0.8"),
    "GB": ("en-GB", "Europe/London", "en-GB,en;q=0.9"),
    "PT": ("pt-PT", "Europe/Lisbon", "pt-PT,pt;q=0.9,en;q=0.8"),
    "FR": ("fr-FR", "Europe/Paris", "fr-FR,fr;q=0.9,en;q=0.8"),
    "NL": ("nl-NL", "Europe/Amsterdam", "nl-NL,nl;q=0.9,en;q=0.8"),
}
DEFAULT_GEO = ("es-ES", "Europe/Madrid", "es-ES,es;q=0.9,en;q=0.8")


@dataclass
class Identity:
    profile: dict = field(default_factory=lambda: dict(random.choice(PROFILES)))
    country: str | None = None
    locale: str = DEFAULT_GEO[0]
    timezone_id: str = DEFAULT_GEO[1]
    accept_language: str = DEFAULT_GEO[2]

    def rotate(self) -> None:
        others = [p for p in PROFILES if p["imp"] != self.profile["imp"]] or PROFILES
        self.profile = dict(random.choice(others))

    def apply_geo(self, country: str | None) -> None:
        if not country:
            return
        self.country = country.upper()
        loc, tz, al = GEO.get(self.country, DEFAULT_GEO)
        self.locale, self.timezone_id, self.accept_language = loc, tz, al

    @property
    def ua(self) -> str:
        return self.profile["ua"]

    @property
    def imp(self) -> str:
        return self.profile["imp"]

    @property
    def platform(self) -> str:
        return self.profile["platform"]

    @property
    def viewport(self) -> tuple:
        return self.profile["viewport"]

    def ch_ua(self) -> str:
        return ", ".join(f'"{n}";v="{v}"' for n, v in self.profile["brands"])

    def brands_json(self) -> list:
        return [{"brand": n, "version": v} for n, v in self.profile["brands"]]

    def headers(self, referer: str,
                accept: str = "application/json, text/plain, */*") -> dict:
        """Mismo orden que Chrome manda en un XHR cross-site."""
        return {
            "accept": accept,
            "accept-language": self.accept_language,
            "origin": "https://www.daznbet.es",
            "referer": referer,
            "sec-ch-ua": self.ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": self.ua,
        }

    def script_config(self) -> dict:
        return {
            "languages": [self.locale, self.locale.split("-")[0], "en"],
            "cores": self.profile["cores"],
            "mem": self.profile["mem"],
            "brands": self.brands_json(),
            "platformName": self.profile["platform_name"],
            "platformVersion": self.profile["platform_version"],
            "fullVersion": f'{self.profile["major"]}.0.0.0',
            "fullVersionList": [
                {"brand": "Chromium", "version": f'{self.profile["major"]}.0.0.0'},
                {"brand": "Google Chrome", "version": f'{self.profile["major"]}.0.0.0'},
                {"brand": "Not-A.Brand", "version": "99.0.0.0"},
            ],
            "glVendor": "Google Inc. (Intel)",
            "glRenderer": ("ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 "
                           "ps_5_0, D3D11)"),
            "chromeHeight": 85,
            "taskbar": 40,
        }