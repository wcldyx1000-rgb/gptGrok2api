"""代理出口的地区环境（时区 + 语言）。

同一个出口发出的 pow、sentinel、请求头和 conversation 参数必须来自同一套地区环境，
否则时区、语言和出口 IP 互相矛盾。偏移量始终按 IANA 时区实时计算，夏令时自动跟随。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

# Chrome 的 Date.toString() 使用 CLDR 长名称；未收录的时区与 Chrome 一样回落为 GMT±hh:mm。
_LONG_NAMES: dict[str, tuple[str, str]] = {
    "UTC": ("Coordinated Universal Time", "Coordinated Universal Time"),
    "Etc/UTC": ("Coordinated Universal Time", "Coordinated Universal Time"),
    "Asia/Shanghai": ("China Standard Time", "China Daylight Time"),
    "Asia/Taipei": ("Taipei Standard Time", "Taipei Daylight Time"),
    "Asia/Hong_Kong": ("Hong Kong Standard Time", "Hong Kong Summer Time"),
    "Asia/Macau": ("China Standard Time", "China Daylight Time"),
    "Asia/Tokyo": ("Japan Standard Time", "Japan Daylight Time"),
    "Asia/Seoul": ("Korean Standard Time", "Korean Daylight Time"),
    "Asia/Singapore": ("Singapore Standard Time", "Singapore Standard Time"),
    "Asia/Kuala_Lumpur": ("Malaysia Time", "Malaysia Time"),
    "Asia/Bangkok": ("Indochina Time", "Indochina Time"),
    "Asia/Ho_Chi_Minh": ("Indochina Time", "Indochina Time"),
    "Asia/Manila": ("Philippine Standard Time", "Philippine Summer Time"),
    "Asia/Jakarta": ("Western Indonesia Time", "Western Indonesia Time"),
    "Asia/Kolkata": ("India Standard Time", "India Standard Time"),
    "Asia/Dubai": ("Gulf Standard Time", "Gulf Standard Time"),
    "Europe/London": ("Greenwich Mean Time", "British Summer Time"),
    "Europe/Dublin": ("Greenwich Mean Time", "Irish Standard Time"),
    "Europe/Lisbon": ("Western European Standard Time", "Western European Summer Time"),
    "Europe/Berlin": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Paris": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Amsterdam": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Brussels": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Madrid": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Rome": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Zurich": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Stockholm": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Warsaw": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Helsinki": ("Eastern European Standard Time", "Eastern European Summer Time"),
    "Europe/Moscow": ("Moscow Standard Time", "Moscow Summer Time"),
    "America/New_York": ("Eastern Standard Time", "Eastern Daylight Time"),
    "America/Toronto": ("Eastern Standard Time", "Eastern Daylight Time"),
    "America/Chicago": ("Central Standard Time", "Central Daylight Time"),
    "America/Denver": ("Mountain Standard Time", "Mountain Daylight Time"),
    "America/Phoenix": ("Mountain Standard Time", "Mountain Daylight Time"),
    "America/Los_Angeles": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Vancouver": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Anchorage": ("Alaska Standard Time", "Alaska Daylight Time"),
    "Pacific/Honolulu": ("Hawaii-Aleutian Standard Time", "Hawaii-Aleutian Daylight Time"),
    "America/Sao_Paulo": ("Brasilia Standard Time", "Brasilia Summer Time"),
    "America/Mexico_City": ("Central Standard Time", "Central Daylight Time"),
    "Australia/Sydney": ("Australian Eastern Standard Time", "Australian Eastern Daylight Time"),
    "Australia/Melbourne": ("Australian Eastern Standard Time", "Australian Eastern Daylight Time"),
    "Australia/Perth": ("Australian Western Standard Time", "Australian Western Daylight Time"),
    "Pacific/Auckland": ("New Zealand Standard Time", "New Zealand Daylight Time"),
}


def normalize_timezone(value: object) -> str:
    """返回规范的 IANA 时区名；空值返回空串，无效值抛 ValueError。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid timezone: {text}") from exc
    return text


def normalize_language(value: object) -> str:
    """返回规范的 BCP 47 语言标签（如 zh-TW、en-US）；空值返回空串，无效值抛 ValueError。"""
    text = str(value or "").strip().replace("_", "-")
    if not text:
        return ""
    if not _LANGUAGE_PATTERN.match(text):
        raise ValueError(f"invalid language: {text}")
    parts = text.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2:
            normalized.append(part.upper())
        elif len(part) == 4:
            normalized.append(part.title())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


@dataclass(frozen=True)
class LocaleEnv:
    timezone: str
    language: str

    @classmethod
    def from_config(cls, raw: object) -> "LocaleEnv | None":
        """从存储的 {timezone, language} 构造；两项都必须有效，否则视为未配置。"""
        if not isinstance(raw, dict):
            return None
        try:
            tz = normalize_timezone(raw.get("timezone"))
            language = normalize_language(raw.get("language"))
        except ValueError:
            return None
        if not tz or not language:
            return None
        return cls(timezone=tz, language=language)

    def _now(self, now: datetime | None = None) -> datetime:
        zone = ZoneInfo(self.timezone)
        if now is None:
            return datetime.now(zone)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(zone)

    def offset_min(self, now: datetime | None = None) -> int:
        """与 JS Date.getTimezoneOffset() 同号：东八区为 -480。"""
        offset = self._now(now).utcoffset()
        return -int(offset.total_seconds() // 60) if offset is not None else 0

    def date_string(self, now: datetime | None = None) -> str:
        """浏览器 Date.toString() 格式，例如 `Thu Oct 16 2026 01:25:00 GMT+0800 (Taipei Standard Time)`。"""
        local = self._now(now)
        total = -self.offset_min(now)
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total), 60)
        gmt = f"GMT{sign}{hours:02d}{minutes:02d}"
        names = _LONG_NAMES.get(self.timezone)
        if names is not None:
            dst = local.dst()
            long_name = names[1] if dst and dst.total_seconds() else names[0]
        else:
            long_name = f"GMT{sign}{hours:02d}:{minutes:02d}"
        return f"{local.strftime('%a %b %d %Y %H:%M:%S')} {gmt} ({long_name})"

    @property
    def languages(self) -> list[str]:
        """navigator.languages：首选语言、其基础语言，非英语再补 en-US/en。"""
        result = [self.language]
        base = self.language.split("-", 1)[0]
        if base != self.language:
            result.append(base)
        if base != "en":
            result.extend(["en-US", "en"])
        return result

    @property
    def accept_language(self) -> str:
        parts: list[str] = []
        for index, language in enumerate(self.languages):
            parts.append(language if index == 0 else f"{language};q={max(0.1, 1 - index / 10):.1f}")
        return ",".join(parts)


def normalize_locale_fields(timezone_value: object, language_value: object) -> tuple[str, str]:
    """校验代理配置里的时区 / 语言：两项都填或都留空；返回规范化后的值，无效时抛 ValueError。"""
    tz = normalize_timezone(timezone_value)
    language = normalize_language(language_value)
    if bool(tz) != bool(language):
        raise ValueError("timezone and language must be set together")
    return tz, language


def locale_from_fields(source: object) -> LocaleEnv | None:
    """从代理节点 / 代理配置 / 运行时设置读取地区环境：支持顶层 timezone/language 或嵌套 locale。"""
    if not isinstance(source, dict):
        return None
    nested = source.get("locale")
    if isinstance(nested, dict):
        found = LocaleEnv.from_config(nested)
        if found is not None:
            return found
    return LocaleEnv.from_config({"timezone": source.get("timezone"), "language": source.get("language")})


# Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
# 出口国家 → 建议的时区和语言。一个国家多个时区的（美国、加拿大、澳洲）只给语言和候选时区，时区要手动选。
COUNTRY_LOCALES: dict[str, tuple[str, str]] = {
    "TW": ("Asia/Taipei", "zh-TW"), "HK": ("Asia/Hong_Kong", "zh-HK"), "MO": ("Asia/Macau", "zh-MO"),
    "CN": ("Asia/Shanghai", "zh-CN"), "JP": ("Asia/Tokyo", "ja-JP"), "KR": ("Asia/Seoul", "ko-KR"),
    "SG": ("Asia/Singapore", "en-SG"), "MY": ("Asia/Kuala_Lumpur", "ms-MY"), "TH": ("Asia/Bangkok", "th-TH"),
    "VN": ("Asia/Ho_Chi_Minh", "vi-VN"), "PH": ("Asia/Manila", "en-PH"), "ID": ("Asia/Jakarta", "id-ID"),
    "IN": ("Asia/Kolkata", "en-IN"), "AE": ("Asia/Dubai", "ar-AE"), "GB": ("Europe/London", "en-GB"),
    "IE": ("Europe/Dublin", "en-IE"), "PT": ("Europe/Lisbon", "pt-PT"), "DE": ("Europe/Berlin", "de-DE"),
    "FR": ("Europe/Paris", "fr-FR"), "NL": ("Europe/Amsterdam", "nl-NL"), "BE": ("Europe/Brussels", "nl-BE"),
    "ES": ("Europe/Madrid", "es-ES"), "IT": ("Europe/Rome", "it-IT"), "CH": ("Europe/Zurich", "de-CH"),
    "SE": ("Europe/Stockholm", "sv-SE"), "PL": ("Europe/Warsaw", "pl-PL"), "FI": ("Europe/Helsinki", "fi-FI"),
    "BR": ("America/Sao_Paulo", "pt-BR"), "MX": ("America/Mexico_City", "es-MX"), "NZ": ("Pacific/Auckland", "en-NZ"),
}
MULTI_ZONE_COUNTRIES: dict[str, tuple[str, list[str]]] = {
    "US": ("en-US", ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
                     "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"]),
    "CA": ("en-CA", ["America/Toronto", "America/Vancouver"]),
    "AU": ("en-AU", ["Australia/Sydney", "Australia/Melbourne", "Australia/Perth"]),
}


def timezone_country(tz: str) -> str:
    for country, (zone, _language) in COUNTRY_LOCALES.items():
        if zone == tz:
            return country
    for country, (_language, zones) in MULTI_ZONE_COUNTRIES.items():
        if tz in zones:
            return country
    return ""


def suggest_locale(country: object) -> dict[str, object] | None:
    code = str(country or "").strip().upper()
    if code in COUNTRY_LOCALES:
        zone, language = COUNTRY_LOCALES[code]
        return {"country": code, "timezone": zone, "language": language, "timezone_options": [zone]}
    if code in MULTI_ZONE_COUNTRIES:
        language, zones = MULTI_ZONE_COUNTRIES[code]
        return {"country": code, "timezone": "", "language": language, "timezone_options": zones}
    return None
