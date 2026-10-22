// Adapted from the MIT-licensed Sentinel VM adapter in the user-provided
// gpt-outlook-register workspace. This process never performs network I/O;
// Python owns the authenticated curl_cffi session and supplies the challenge.
const EXPOSE_PATCH = 'return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});';
const EXPOSE_REPLACEMENT =
  'return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t.__debug_n=_n,t.__debug_bindProof=D,t}({});';
const INSTANCE_PATCH = 'var P=new _;';
const INSTANCE_REPLACEMENT = 'var P=new _;globalThis.__debugP=P;';
const SDK_GLOBAL_PATCH = 'var SentinelSDK=';
const SDK_GLOBAL_REPLACEMENT = 'globalThis.SentinelSDK=';
const CURRENT_EXPOSE_PATCH = 't.token=Ie,t}({});';
const CURRENT_EXPOSE_REPLACEMENT = 't.token=Ie,t.__debug_n=Pn,t}({});';

function bytesToBase64(bytes) {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let output = '';
  let index = 0;
  while (index < bytes.length) {
    const first = bytes[index++] || 0;
    const second = bytes[index++] || 0;
    const third = bytes[index++] || 0;
    const value = (first << 16) | (second << 8) | third;
    output += chars[(value >> 18) & 63];
    output += chars[(value >> 12) & 63];
    output += index - 2 < bytes.length ? chars[(value >> 6) & 63] : '=';
    output += index - 1 < bytes.length ? chars[value & 63] : '=';
  }
  return output;
}

function base64ToBytes(value) {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const clean = String(value || '').replace(/[^A-Za-z0-9+/=]/g, '');
  const bytes = [];
  for (let index = 0; index < clean.length; index += 4) {
    const first = chars.indexOf(clean[index]);
    const second = chars.indexOf(clean[index + 1]);
    const third = chars.indexOf(clean[index + 2]);
    const fourth = chars.indexOf(clean[index + 3]);
    const decoded =
      ((first & 63) << 18) |
      ((second & 63) << 12) |
      (((third < 0 ? 0 : third) & 63) << 6) |
      ((fourth < 0 ? 0 : fourth) & 63);
    bytes.push((decoded >> 16) & 255);
    if (clean[index + 2] !== '=') bytes.push((decoded >> 8) & 255);
    if (clean[index + 3] !== '=') bytes.push(decoded & 255);
  }
  return bytes;
}

function createStorage() {
  const values = new Map();
  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key) {
      return values.has(String(key)) ? values.get(String(key)) : null;
    },
    setItem(key, value) {
      values.set(String(key), String(value));
    },
    removeItem(key) {
      values.delete(String(key));
    },
  };
}

function createElement(tagName) {
  const tag = String(tagName || 'div').toLowerCase();
  return {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    nodeName: tag.toUpperCase(),
    style: {},
    children: [],
    src: '',
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((item) => item !== child);
      return child;
    },
    setAttribute() {},
    getAttribute() {
      return null;
    },
    addEventListener() {},
    removeEventListener() {},
    getBoundingClientRect() {
      return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
    },
  };
}

function formatBrowserDate(date, offsetMin, longName) {
  // Chrome's Date#toString: `Wed Oct 21 2026 08:02:54 GMT+0800 (China Standard Time)`.
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const local = new Date(date.getTime() - offsetMin * 60000);
  const pad = (value) => String(value).padStart(2, '0');
  const total = -offsetMin;
  const sign = total >= 0 ? '+' : '-';
  const hours = Math.floor(Math.abs(total) / 60);
  const minutes = Math.abs(total) % 60;
  const zone = longName || `GMT${sign}${pad(hours)}:${pad(minutes)}`;
  return `${days[local.getUTCDay()]} ${months[local.getUTCMonth()]} ${pad(local.getUTCDate())} ${local.getUTCFullYear()} `
    + `${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}:${pad(local.getUTCSeconds())} `
    + `GMT${sign}${pad(hours)}${pad(minutes)} (${zone})`;
}

function installDateLocale(payload) {
  if (payload.timezone_offset_min === undefined || payload.timezone_offset_min === null) return;
  const offsetMin = Number(payload.timezone_offset_min) || 0;
  const longName = String(payload.timezone_long_name || '');
  Object.defineProperty(Date.prototype, 'toString', {
    configurable: true,
    writable: true,
    value: function toString() {
      return formatBrowserDate(this, offsetMin, longName);
    },
  });
  Object.defineProperty(Date.prototype, 'getTimezoneOffset', {
    configurable: true,
    writable: true,
    value: function getTimezoneOffset() {
      return offsetMin;
    },
  });
}

function createNavigator(payload) {
  // Sentinel reads Object.keys(Object.getPrototypeOf(navigator)), so the probes
  // must live on an enumerable prototype the way they do in Chrome.
  const proto = {};
  const probes = Array.isArray(payload.navigator_probes) ? payload.navigator_probes : [];
  for (const probe of probes) {
    if (!Array.isArray(probe) || probe.length < 2) continue;
    const name = String(probe[0]);
    const text = String(probe[1]);
    Object.defineProperty(proto, name, {
      configurable: true,
      enumerable: true,
      get() {
        if (text === 'true' || text === 'false') return text === 'true';
        if (text === 'null') return null;
        return { toString: () => text };
      },
    });
  }
  const userAgent = String(payload.user_agent || 'Mozilla/5.0');
  const own = {
    userAgent,
    appCodeName: 'Mozilla',
    appName: 'Netscape',
    product: 'Gecko',
    productSub: '20030107',
    language: String(payload.language || 'pt-BR'),
    languages: Array.isArray(payload.languages) ? payload.languages : ['pt-BR', 'pt'],
    hardwareConcurrency: Number(payload.hardware_concurrency || 12),
    platform: /Macintosh|Mac OS X/i.test(userAgent) ? 'MacIntel' : 'Win32',
    vendor: 'Google Inc.',
    webdriver: false,
  };
  if (!probes.length) return own;
  for (const name of ['userAgent', 'language', 'languages', 'hardwareConcurrency', 'platform', 'vendor', 'webdriver']) {
    Object.defineProperty(proto, name, { configurable: true, enumerable: true, get: () => own[name] });
  }
  return Object.create(proto);
}

function exposeOnlyKeys(target, keys) {
  // Object.keys(window/document) must look like a Chrome page, not like Node
  // or this stub: hide every own enumerable key, then expose the given ones.
  if (!Array.isArray(keys) || !keys.length) return;
  for (const key of Object.keys(target)) {
    try {
      Object.defineProperty(target, key, { enumerable: false });
    } catch {}
  }
  for (const key of keys) {
    const name = String(key);
    try {
      const descriptor = Object.getOwnPropertyDescriptor(target, name);
      if (descriptor) {
        Object.defineProperty(target, name, { ...descriptor, enumerable: true });
      } else {
        Object.defineProperty(target, name, { configurable: true, enumerable: true, writable: true, value: {} });
      }
    } catch {}
  }
}

function installRuntime(payload) {
  const screen = {
    width: Number(payload.screen_width || 1366),
    height: Number(payload.screen_height || 768),
    availWidth: Number(payload.screen_width || 1366),
    availHeight: Number(payload.screen_height || 768),
    colorDepth: 24,
    pixelDepth: 24,
  };
  installDateLocale(payload);
  const scripts = [];
  const documentElement = createElement('html');
  documentElement.clientWidth = screen.width;
  documentElement.clientHeight = screen.height;
  const dataBuild = payload.data_build ? String(payload.data_build) : null;
  documentElement.getAttribute = (name) => (String(name) === 'data-build' ? dataBuild : null);
  const pageUrl = String(payload.page_url || payload.frame_url || 'https://chatgpt.com/backend-api/sentinel/frame.html');
  const document = {
    readyState: 'complete',
    hidden: false,
    visibilityState: 'visible',
    referrer: String(
      payload.referrer !== undefined ? payload.referrer : payload.checkout_url || 'https://chatgpt.com/checkout/',
    ),
    URL: pageUrl,
    cookie: `oai-did=${encodeURIComponent(payload.device_id || '')}`,
    scripts,
    currentScript: {
      src: String(payload.sdk_url || 'https://chatgpt.com/sentinel/sdk.js'),
      getAttribute() {
        return null;
      },
    },
    documentElement,
    body: createElement('body'),
    head: createElement('head'),
    createElement(tag) {
      const element = createElement(tag);
      if (String(tag).toLowerCase() === 'script') scripts.push(element);
      return element;
    },
    createElementNS(_namespace, tag) {
      return this.createElement(tag);
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    getElementById() {
      return null;
    },
    getElementsByTagName() {
      return [];
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return true;
    },
  };
  // A chat page lists its own <script> tags (plus the injected loader/SDK);
  // the bare frame only has the SDK itself.
  const pageScripts = Array.isArray(payload.page_scripts) ? payload.page_scripts : [];
  for (const src of pageScripts) {
    if (src && src !== document.currentScript.src) scripts.push({ src: String(src), getAttribute() { return null; } });
  }
  scripts.push(document.currentScript);

  // performance.now() advances in real time from a page load a few seconds ago,
  // so elapsed-time fields (e.g. the PoW solve time) are realistic.
  const startedAt = Date.now();
  const hrStart = process.hrtime.bigint();
  const timeOrigin = payload.time_origin
    ? Number(payload.time_origin)
    : startedAt - Number(payload.performance_now || 1500 + Math.random() * 6500);
  const performance = {
    now: () => startedAt - timeOrigin + Number(process.hrtime.bigint() - hrStart) / 1e6,
    timeOrigin,
    memory: { jsHeapSizeLimit: Number(payload.js_heap_size_limit || 4294705152) },
  };

  class TextEncoderPolyfill {
    encode(text) {
      const source = String(text || '');
      const bytes = new Uint8Array(source.length);
      for (let index = 0; index < source.length; index += 1) bytes[index] = source.charCodeAt(index) & 255;
      return bytes;
    }
  }

  class TextDecoderPolyfill {
    decode(input) {
      if (!input) return '';
      let output = '';
      for (let index = 0; index < input.length; index += 1) output += String.fromCharCode(input[index]);
      return output;
    }
  }

  class URLSearchParamsPolyfill {
    constructor(search) {
      this.pairs = [];
      const source = String(search || '').replace(/^\?/, '');
      if (!source) return;
      for (const part of source.split('&')) {
        if (!part) continue;
        const separator = part.indexOf('=');
        this.pairs.push(
          separator < 0
            ? [decodeURIComponent(part), '']
            : [decodeURIComponent(part.slice(0, separator)), decodeURIComponent(part.slice(separator + 1))],
        );
      }
    }
    keys() {
      return this.pairs.map((pair) => pair[0])[Symbol.iterator]();
    }
  }

  class URLPolyfill {
    constructor(input, base) {
      const raw = String(input || '');
      this.href = /^https?:\/\//i.test(raw)
        ? raw
        : `${String(base || 'https://auth.openai.com/').replace(/\/$/, '')}/${raw.replace(/^\//, '')}`;
      const match = this.href.match(/^(https?:)\/\/([^/]+)(\/[^?#]*)?(\?[^#]*)?(#.*)?$/i);
      this.protocol = match ? match[1] : 'https:';
      this.host = match ? match[2] : 'auth.openai.com';
      this.hostname = this.host;
      this.pathname = match && match[3] ? match[3] : '/';
      this.search = match && match[4] ? match[4] : '';
      this.hash = match && match[5] ? match[5] : '';
      this.origin = `${this.protocol}//${this.host}`;
    }
    toString() {
      return this.href;
    }
  }

  globalThis.window = globalThis;
  globalThis.self = globalThis;
  globalThis.top = globalThis;
  globalThis.parent = globalThis;
  globalThis.document = document;
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    enumerable: true,
    value: createNavigator(payload),
  });
  const pageMatch = pageUrl.match(/^(https?:\/\/[^/?#]+)([^?#]*)(\?[^#]*)?/i);
  globalThis.location = {
    href: pageUrl,
    origin: pageMatch ? pageMatch[1] : 'https://chatgpt.com',
    pathname: pageMatch && pageMatch[2] ? pageMatch[2] : '/',
    search: pageMatch && pageMatch[3] ? pageMatch[3] : '',
  };
  document.location = globalThis.location;
  globalThis.screen = screen;
  globalThis.performance = performance;
  globalThis.localStorage = createStorage();
  globalThis.sessionStorage = createStorage();
  globalThis.__sentinel_init_pending = [];
  globalThis.__sentinel_token_pending = [];
  globalThis.setTimeout = (callback) => {
    if (typeof callback === 'function') callback();
    return 1;
  };
  globalThis.clearTimeout = () => {};
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};
  globalThis.requestIdleCallback = (callback) => {
    if (typeof callback === 'function') callback({ didTimeout: false, timeRemaining: () => 50 });
    return 1;
  };
  globalThis.cancelIdleCallback = () => {};
  globalThis.addEventListener = () => {};
  globalThis.removeEventListener = () => {};
  globalThis.dispatchEvent = () => true;
  globalThis.postMessage = () => {};
  globalThis.atob = (input) => String.fromCharCode(...base64ToBytes(input));
  globalThis.btoa = (input) => {
    const source = String(input || '');
    return bytesToBase64(Array.from(source, (character) => character.charCodeAt(0) & 255));
  };
  globalThis.TextEncoder = globalThis.TextEncoder || TextEncoderPolyfill;
  globalThis.TextDecoder = globalThis.TextDecoder || TextDecoderPolyfill;
  globalThis.URL = globalThis.URL || URLPolyfill;
  globalThis.URLSearchParams = globalThis.URLSearchParams || URLSearchParamsPolyfill;
  globalThis.Event = globalThis.Event || class Event { constructor(type) { this.type = type; } };
  globalThis.CustomEvent = globalThis.CustomEvent || class CustomEvent extends globalThis.Event {
    constructor(type, init) {
      super(type);
      this.detail = init && Object.prototype.hasOwnProperty.call(init, 'detail') ? init.detail : null;
    }
  };
  globalThis.MessageChannel = globalThis.MessageChannel || class MessageChannel {
    constructor() {
      this.port1 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
      this.port2 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
    }
  };
  globalThis.matchMedia = globalThis.matchMedia || ((query) => ({
    media: String(query || ''), matches: false, onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, dispatchEvent() { return false; },
  }));
  globalThis.getComputedStyle = globalThis.getComputedStyle || (() => ({ getPropertyValue() { return ''; } }));
  globalThis.history = globalThis.history || { length: 1, state: null, back() {}, forward() {}, go() {}, pushState() {}, replaceState() {} };
  globalThis.chrome = globalThis.chrome || { runtime: {}, app: {} };
  globalThis.CSS = globalThis.CSS || { supports() { return true; } };
  globalThis.indexedDB = globalThis.indexedDB || { open() { return { onerror: null, onsuccess: null, onupgradeneeded: null, result: {}, error: null }; }, deleteDatabase() { return {}; } };
  globalThis.fetch = async () => { throw new Error('fetch should not be called'); };
  const nativeRandomUUID = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
    ? globalThis.crypto.randomUUID.bind(globalThis.crypto)
    : undefined;
  // The SDK's first UUID is its page-session sid; reuse the caller's sid so the
  // requirements and proof tokens of one page share it, as in a real tab.
  const presetUUIDs = payload.sid ? [String(payload.sid)] : [];
  const cryptoValue = {
    randomUUID: nativeRandomUUID
      ? () => (presetUUIDs.length ? presetUUIDs.shift() : nativeRandomUUID())
      : undefined,
    getRandomValues(array) {
      for (let index = 0; index < array.length; index += 1) array[index] = Math.floor(Math.random() * 256);
      return array;
    },
  };
  Object.defineProperty(globalThis, 'crypto', {
    configurable: true,
    enumerable: true,
    value: cryptoValue,
  });
}

function loadPatchedSdk(source) {
  let sdk = String(source || '');
  sdk = sdk.replace(SDK_GLOBAL_PATCH, SDK_GLOBAL_REPLACEMENT);
  sdk = sdk.replace(INSTANCE_PATCH, INSTANCE_REPLACEMENT);
  sdk = sdk.replace(
    /var ([A-Za-z_$][\w$]*)=new _;/,
    (match, name) => `${match}globalThis.__debugP=${name};`,
  );
  // Newer SDK builds rename the instance class (e.g. `var E=new O;`); collect
  // every top-level `var x=new Y;` and pick the one exposing the token API.
  sdk = sdk.replace(
    /var ([A-Za-z_$][\w$]*)=new ([A-Za-z_$][\w$]*);/g,
    (match, name) => `${match}(globalThis.__sentinelCandidates=globalThis.__sentinelCandidates||[]).push(${name});`,
  );
  sdk = sdk.replace(EXPOSE_PATCH, EXPOSE_REPLACEMENT);
  sdk = sdk.replace(CURRENT_EXPOSE_PATCH, CURRENT_EXPOSE_REPLACEMENT);
  eval(sdk);
  if (!globalThis.__debugP || typeof globalThis.__debugP.getRequirementsToken !== 'function') {
    const candidates = globalThis.__sentinelCandidates || [];
    globalThis.__debugP = candidates.find(
      (item) => item && typeof item.getRequirementsToken === 'function' && typeof item.getEnforcementToken === 'function',
    );
  }
  if (!globalThis.__debugP) throw new Error('sentinel_sdk_instance_not_found');
}

async function run(payload, sdkSource) {
  installRuntime(payload);
  loadPatchedSdk(sdkSource);
  exposeOnlyKeys(globalThis.document, payload.document_keys);
  exposeOnlyKeys(globalThis, payload.window_keys);
  if (payload.action === 'requirements') {
    return { request_p: await globalThis.__debugP.getRequirementsToken() };
  }
  if (payload.action === 'solve') {
    const challenge = payload.challenge || {};
    const requestProof = String(payload.request_p || '').trim();
    if (!requestProof) throw new Error('missing request_p');
    const finalProof = await globalThis.__debugP.getEnforcementToken(challenge);
    const dx = challenge.turnstile ? challenge.turnstile.dx : null;
    let turnstile = null;
    // Builds whose Turnstile solver cannot be exposed return t=null and the
    // Python side falls back to the protocol solver.
    const sdkGlobal = globalThis.SentinelSDK || {};
    if (dx && typeof sdkGlobal.__debug_n === 'function') {
      if (typeof sdkGlobal.__debug_bindProof === 'function') {
        sdkGlobal.__debug_bindProof(challenge, requestProof);
      }
      turnstile = await sdkGlobal.__debug_n(challenge, dx);
    }
    return { final_p: finalProof, t: turnstile };
  }
  throw new Error(`unsupported action: ${payload.action}`);
}

(async () => {
  try {
    const payload = JSON.parse(String(globalThis.__payload_json || '{}'));
    const sdkSource = String(globalThis.__sdk_source || '');
    globalThis.__vm_output_json = JSON.stringify(await run(payload, sdkSource));
  } catch (error) {
    const message = error && error.stack ? String(error.stack) : String(error);
    globalThis.__vm_error = message;
  } finally {
    globalThis.__vm_done = true;
  }
})();
