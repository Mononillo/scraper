# DAZN Bet odds scraper â€” todas las cuotas en JSON, cada 5 s, sin ser detectado

Pipeline anti-deteccion en 4 capas:

1. **Identidad coherente** â€” UA + client hints + timezone + locale + Accept-Language
   alineados entre si y con el pais de la IP de salida.
2. **Navegador real para el WAF** â€” Chrome real (no Chromium), parches JS anti-leak,
   resuelve el challenge de AWS WAF y captura cookies + endpoints + headers reales.
3. **Cliente HTTP con fingerprint de Chrome** â€” curl_cffi impersonate: TLS/JA3/HTTP2
   identicos a un navegador. Polling ligero a los endpoints JSON descubiertos.
4. **Rotacion proactiva** â€” recalibrado programado, reseteo de sesion TLS,
   rotacion de proxy/identidad y backoff adaptativo ante bloqueos.

## Instalacion (Windows)

    py -3 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    python -m playwright install chromium

## Uso

    Copy-Item proxies.txt.example proxies.txt   # edita con tus proxies
    python audit_stealth.py --show-browser --warm   # 1) audita el sigilo
    python daznbet_odds.py --once -v                # 2) un ciclo de prueba
    python daznbet_odds.py                          # 3) produccion

## Salida

    data/odds_latest.json        todas las cuotas, JSON limpio (escritura atomica)
    data/snapshots/odds_*.json   historico
    data/changes.jsonl           cada movimiento de cuota (steam / arbitraje)
    state/cookies.json           jar persistente (incl. aws-waf-token)
    state/endpoints.json         endpoints descubiertos del trafico real

## Variables de entorno (PowerShell)

    $env:INTERVAL=5          segundos entre ciclos
    $env:JITTER=0.35         variacion aleatoria del intervalo
    $env:DEEP_PER_CYCLE=10   mercados profundizados por ciclo (round-robin)
    $env:WARM_EVERY=1200     recalibrado proactivo (segundos)
    $env:SESSION_RESET=1800  reseteo de sesion TLS (segundos)
    $env:USE_PATCHRIGHT=1    usa patchright en vez de playwright
    $env:MODE=http           http (rapido) | browser (maximo sigilo)