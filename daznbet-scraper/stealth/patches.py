"""Parches JS anti-deteccion y opciones de lanzamiento del navegador."""
from __future__ import annotations

import json

_JS = r"""(() => {
  const CFG = __CFG__;
  const def = (o, p, v) => Object.defineProperty(o, p, { get: () => v, configurable: true });

  // 1. webdriver
  def(navigator, 'webdriver', undefined);
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}

  // 2. chrome.*
  window.chrome = window.chrome || {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  window.chrome.app = window.chrome.app || {
    isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
  };
  window.chrome.csi = window.chrome.csi || function () { return {}; };
  window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };

  // 3. plugins
  const mk = (name, filename, desc) => {
    const p = Object.create(window.Plugin ? Plugin.prototype : Object.prototype);
    def(p, 'name', name); def(p, 'filename', filename);
    def(p, 'description', desc); def(p, 'length', 1);
    return p;
  };
  def(navigator, 'plugins', [
    mk('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
  ]);

  // 4. idiomas y hardware
  def(navigator, 'languages', CFG.languages);
  def(navigator, 'hardwareConcurrency', CFG.cores);
  def(navigator, 'deviceMemory', CFG.mem);
  def(navigator, 'maxTouchPoints', 0);
  def(navigator, 'pdfViewerEnabled', true);
  def(navigator, 'vendor', 'Google Inc.');
  def(navigator, 'productSub', '20030107');

  // 5. userAgentData coherente con el UA
  const brands = CFG.brands;
  def(navigator, 'userAgentData', {
    brands: brands,
    mobile: false,
    platform: CFG.platformName,
    getHighEntropyValues: async (hints) => {
      const o = { brands: brands, mobile: false, platform: CFG.platformName };
      if (hints.includes('platformVersion')) o.platformVersion = CFG.platformVersion;
      if (hints.includes('architecture')) o.architecture = 'x86';
      if (hints.includes('bitness')) o.bitness = '64';
      if (hints.includes('model')) o.model = '';
      if (hints.includes('uaFullVersion')) o.uaFullVersion = CFG.fullVersion;
      if (hints.includes('fullVersionList')) o.fullVersionList = CFG.fullVersionList;
      if (hints.includes('wow64')) o.wow64 = false;
      return o;
    },
    toJSON: () => ({ brands: brands, mobile: false, platform: CFG.platformName })
  });

  // 6. permissions
  const oq = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission, onchange: null })
      : oq(p);

  // 7. WebGL vendor/renderer
  const patchGL = (proto) => {
    if (!proto) return;
    const gp = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return CFG.glVendor;
      if (p === 37446) return CFG.glRenderer;
      return gp.apply(this, arguments);
    };
    const ge = proto.getExtension;
    proto.getExtension = function (name) {
      if (name === 'WEBGL_debug_renderer_info') {
        return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
      }
      return ge.apply(this, arguments);
    };
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // 8. geometria de ventana coherente
  def(window, 'outerWidth', window.innerWidth);
  def(window, 'outerHeight', window.innerHeight + CFG.chromeHeight);
  def(screen, 'colorDepth', 24);
  def(screen, 'pixelDepth', 24);
  def(screen, 'availWidth', window.innerWidth);
  def(screen, 'availHeight', window.innerHeight + CFG.chromeHeight - CFG.taskbar);

  // 9. connection
  def(navigator, 'connection', {
    downlink: 10, effectiveType: '4g', rtt: 50, saveData: false, onchange: null
  });

  // 10. iframes
  const dCW = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
  if (dCW) {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
      get: function () {
        const w = dCW.get.call(this);
        try { def(w.navigator, 'webdriver', undefined); } catch (e) {}
        return w;
      }
    });
  }

  def(Notification, 'permission', 'default');
})();"""


def stealth_script(identity) -> str:
    return _JS.replace("__CFG__", json.dumps(identity.script_config()))


def launch_args() -> list:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,Translate,BackForwardCache",
        "--disable-dev-shm-usage",
        "--no-first-run", "--no-default-browser-check", "--disable-infobars",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
        "--password-store=basic", "--use-mock-keychain",
        "--metrics-recording-only", "--no-service-autorun",
    ]


def context_options(identity) -> dict:
    return dict(
        user_agent=identity.ua,
        locale=identity.locale,
        timezone_id=identity.timezone_id,
        viewport={"width": identity.viewport[0], "height": identity.viewport[1]},
        device_scale_factor=1,
        is_mobile=False,
        has_touch=False,
        color_scheme="dark",
        extra_http_headers={"accept-language": identity.accept_language},
    )