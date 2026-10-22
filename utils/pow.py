import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Sequence
from urllib.parse import urljoin

import pybase64

DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
SENTINEL_LOADER_PATH = "/backend-api/sentinel/sdk.js"
from utils.helper import new_uuid
from utils.locale_env import LocaleEnv


CORES = [8, 16, 24, 32]
SCREEN_RESOLUTIONS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160]]
# 64 位 Chrome 的 performance.memory.jsHeapSizeLimit：V8 按物理内存的 1/4 定堆上限，
# 开启指针压缩后封顶 4GB。16GB 及以上内存为 4294705152，8GB 内存为 2172649472。
JS_HEAP_SIZE_LIMIT_HIGH_MEMORY = 4294705152
JS_HEAP_SIZE_LIMIT_8GB = 2172649472
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


# SDK 第 10 位：navigator 原型上随机一个属性，格式为 `名称−值`（U+2212）。按 Chrome 桌面版取值。
NAVIGATOR_PROBES = (
    "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
    "storage−[object StorageManager]",
    "locks−[object LockManager]",
    "appCodeName−Mozilla",
    "permissions−[object Permissions]",
    "share−function share() { [native code] }",
    "webdriver−false",
    "managed−[object NavigatorManagedData]",
    "canShare−function canShare() { [native code] }",
    "vendor−Google Inc.",
    "mediaDevices−[object MediaDevices]",
    "vibrate−function vibrate() { [native code] }",
    "storageBuckets−[object StorageBucketManager]",
    "mediaCapabilities−[object MediaCapabilities]",
    "cookieEnabled−true",
    "virtualKeyboard−[object VirtualKeyboard]",
    "product−Gecko",
    "presentation−[object Presentation]",
    "onLine−true",
    "mimeTypes−[object MimeTypeArray]",
    "credentials−[object CredentialsContainer]",
    "serviceWorker−[object ServiceWorkerContainer]",
    "keyboard−[object Keyboard]",
    "gpu−[object GPU]",
    # navigator.doNotTrack 为 null，SDK 的 toString() 抛错后只返回属性名。
    "doNotTrack",
    "serial−[object Serial]",
    "pdfViewerEnabled−true",
    "geolocation−[object Geolocation]",
    "userAgentData−[object NavigatorUAData]",
    "getUserMedia−function getUserMedia() { [native code] }",
    "sendBeacon−function sendBeacon() { [native code] }",
    "windowControlsOverlay−[object WindowControlsOverlay]",
)

# SDK 第 12 位：Object.keys(window) 中随机一个。以下为 Chrome 窗口自身的可枚举属性；
# 页面脚本定义的全局变量由调用方按页面补充。
WINDOW_OWN_KEYS = (
    "window", "self", "document", "name", "location", "customElements", "history", "navigation",
    "innerWidth", "innerHeight", "scrollX", "scrollY", "visualViewport", "screenX", "screenY",
    "outerWidth", "outerHeight", "devicePixelRatio", "screen", "chrome", "navigator", "onresize",
    "performance", "crypto", "indexedDB", "sessionStorage", "localStorage", "scheduler", "alert",
    "atob", "btoa", "fetch", "matchMedia", "postMessage", "queueMicrotask", "requestAnimationFrame",
    "setInterval", "setTimeout", "caches",
)
# chatgpt.com 页面：有子 frame（"0"）和 Next.js 注入的全局变量。
CHATGPT_PAGE_WINDOW_KEYS = ("0", "__NEXT_DATA__", "__BUILD_MANIFEST", "__NEXT_PRELOADREADY")

# SDK 第 18～24 位：Number("ai" / "createPRNG" / "cache" / "data" / "solana" / "dump" / "InstallTrigger" in window)。
# Chrome 页面上都不存在（InstallTrigger 仅 Firefox 有）。
WINDOW_PRESENCE_FLAGS = (0, 0, 0, 0, 0, 0, 0)


def navigator_probe(cores: int, language: str) -> str:
    """navigator 探测项；核数和语言与配置里对应字段保持一致。"""
    return random.choice((*NAVIGATOR_PROBES, f"language−{language}", f"hardwareConcurrency−{cores}"))


def window_own_key(page_keys: Sequence[str] = ()) -> str:
    return random.choice((*WINDOW_OWN_KEYS, *page_keys))


def js_heap_size_limit(cores: int, device_rng: random.Random | Any) -> int:
    """按设备给出稳定的堆上限：8 核及以下的机器可能只有 8GB 内存，更多核视为大内存。"""
    if cores <= 8 and device_rng.random() < 0.5:
        return JS_HEAP_SIZE_LIMIT_8GB
    return JS_HEAP_SIZE_LIMIT_HIGH_MEMORY


def react_random_key() -> str:
    """React 每次加载生成一次的内部属性后缀：Math.random().toString(36).slice(2)。

    该写法通常得到 10～11 位 base36 字符，且末位不会是 0（toString 会去掉尾随零）。
    """
    length = random.choice((10, 11))
    body = "".join(random.choice(_BASE36) for _ in range(length - 1))
    return body + random.choice(_BASE36[1:])


@dataclass(frozen=True)
class PowFingerprint:
    """同一页面会话内不变的浏览器环境。

    屏幕、核数、堆上限按 device id 确定性生成，同一设备每次会话都一致；sid、timeOrigin
    和 React 内部属性后缀对应一次页面加载，每个会话生成一次。每次生成 token 时只有
    计数器、耗时、performance.now() 和随机探测键变化。
    """

    screen_sum: int
    cores: int
    sid: str
    time_origin_ms: float
    heap_size_limit: int = JS_HEAP_SIZE_LIMIT_HIGH_MEMORY
    react_key: str = ""
    screen_width: int = 0
    screen_height: int = 0

    @property
    def document_keys(self) -> tuple[str, ...]:
        # React 的容器标记和事件监听标记共用同一个后缀；监听标记没有 $ 分隔符。
        suffix = self.react_key or react_random_key()
        return (f"__reactContainer${suffix}", f"_reactListening{suffix}", "location")

    @classmethod
    def create(cls, device_id: str = "") -> "PowFingerprint":
        device_rng = random.Random(f"pow-device:{device_id}") if device_id else random
        width, height = device_rng.choice(SCREEN_RESOLUTIONS)
        cores = device_rng.choice(CORES)
        return cls(
            screen_sum=width + height,
            cores=cores,
            sid=new_uuid(),
            # 页面加载发生在首次生成 token 之前的几秒。
            time_origin_ms=time.time() * 1000 - random.uniform(1500, 8000),
            heap_size_limit=js_heap_size_limit(cores, device_rng),
            react_key=react_random_key(),
            screen_width=width,
            screen_height=height,
        )

    def performance_now(self) -> float:
        return time.time() * 1000 - self.time_origin_ms


class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.data_build = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src")
        if not src:
            return
        self.script_sources.append(src)
        # 与 SDK 一致：取第一个匹配 c/[^/]*/_ 的 script，找不到才回落到 <html data-build>。
        match = re.search(r"c/[^/]*/_", src)
        if match and not self.data_build:
            self.data_build = match.group(0)


def parse_pow_resources(html_content: str, base_url: str = "https://chatgpt.com/") -> tuple[list[str], str]:
    """解析首页里的 script。浏览器的 script.src 返回绝对地址，相对路径按页面地址补全。"""
    parser = ScriptSrcParser()
    parser.feed(html_content)
    script_sources = [urljoin(base_url, src) for src in parser.script_sources] or [DEFAULT_POW_SCRIPT]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html_content)
        if match:
            data_build = match.group(1)
    return script_sources, data_build


def parse_sentinel_loader(loader_js: str, base_url: str = "https://chatgpt.com/") -> str:
    """从 sentinel loader 中取出它注入页面的 SDK 地址（版本号随上游变化，不写死）。"""
    match = re.search(r"script\.src\s*=\s*['\"]([^'\"]+)['\"]", loader_js or "")
    if match:
        return urljoin(base_url, match.group(1))
    match = re.search(r"https://chatgpt\.com/sentinel/[A-Za-z0-9_-]+/sdk\.js", loader_js or "")
    return match.group(0) if match else ""


def with_injected_scripts(script_sources: Sequence[str], base_url: str, injected_sdk: str) -> list[str]:
    """SDK 取第 5 位时页面里已有运行时注入的 sentinel loader 和 SDK，把它们并入候选池（去重、保序）。"""
    pool: list[str] = []
    for source in [*script_sources, urljoin(base_url, SENTINEL_LOADER_PATH), injected_sdk]:
        if source and source not in pool:
            pool.append(source)
    return pool


def _legacy_parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def build_pow_config(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    locale: LocaleEnv | None = None,
    fingerprint: PowFingerprint | None = None,
) -> list[Any]:
    """字段顺序与 Sentinel SDK 的 getConfig 一致；locale 为当前出口的地区环境，未配置时沿用原有固定值。"""
    fp = fingerprint or PowFingerprint.create()
    # navigator.language / languages 必须与第 7、8 位一致；未配置地区环境时沿用原默认 en-US。
    language = locale.language if locale else "en-US"
    languages = ",".join(locale.languages) if locale else "en-US,es-US,en,es"
    script_source = random.choice(list(script_sources)) if script_sources else DEFAULT_POW_SCRIPT
    return [
        fp.screen_sum,
        locale.date_string() if locale else _legacy_parse_time(),
        fp.heap_size_limit,
        1,
        user_agent,
        script_source,
        data_build,
        language,
        languages,
        random.random(),
        navigator_probe(fp.cores, language),
        random.choice(fp.document_keys),
        window_own_key(CHATGPT_PAGE_WINDOW_KEYS),
        fp.performance_now(),
        fp.sid,
        "",
        fp.cores,
        fp.time_origin_ms,
        *WINDOW_PRESENCE_FLAGS,
    ]


def _pow_generate(
    seed: str,
    difficulty: str,
    config: list[Any],
    limit: int = 500000,
    started_at: float | None = None,
) -> tuple[str, bool]:
    """第 3 位为尝试次数，第 9 位为从开始求解起的耗时（毫秒取整），与 SDK 的 _runCheck 一致。"""
    started = time.perf_counter() if started_at is None else started_at
    target = bytes.fromhex(difficulty)
    diff_len = len(difficulty) // 2
    seed_bytes = seed.encode()
    static_1 = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode()
    static_2 = ("," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ",").encode()
    static_3 = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode()
    for i in range(limit):
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        final_json = static_1 + str(i).encode() + static_2 + str(elapsed_ms).encode() + static_3
        encoded = pybase64.b64encode(final_json)
        digest = hashlib.sha3_512(seed_bytes + encoded).digest()
        if digest[:diff_len] <= target:
            return encoded.decode(), True
    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + pybase64.b64encode(f'"{seed}"'.encode()).decode()
    return fallback, False


def build_legacy_requirements_token(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    locale: LocaleEnv | None = None,
    fingerprint: PowFingerprint | None = None,
) -> str:
    started = time.perf_counter()
    config = build_pow_config(
        user_agent,
        script_sources=script_sources,
        data_build=data_build,
        locale=locale,
        fingerprint=fingerprint,
    )
    # SDK 的阻塞式 requirements token：第 3 位为 1，第 9 位为生成配置的耗时（毫秒，不取整）。
    config[3] = 1
    config[9] = (time.perf_counter() - started) * 1000
    return "gAAAAAC" + pybase64.b64encode(
        json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()


def build_proof_token(
    seed: str,
    difficulty: str,
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    locale: LocaleEnv | None = None,
    fingerprint: PowFingerprint | None = None,
) -> str:
    started = time.perf_counter()
    config = build_pow_config(
        user_agent,
        script_sources=script_sources,
        data_build=data_build,
        locale=locale,
        fingerprint=fingerprint,
    )
    answer, solved = _pow_generate(seed, difficulty, config, started_at=started)
    if not solved:
        raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
    return "gAAAAAB" + answer
