from __future__ import annotations

import configparser
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

import requests

from permit_model import AddressRecord, PermitRecord, normalize_str, utc_now_iso

CONFIG_PATH = Path(__file__).with_name("permit_scrapers.ini")


def load_geocoding_ini() -> configparser.SectionProxy | None:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    if not parser.has_section("geocoding"):
        return None
    return parser["geocoding"]


def ini_or_env(name: str, env_name: str, default: str) -> str:
    section = load_geocoding_ini()
    if section is not None:
        value = section.get(name, fallback="").strip()
        if value:
            return value
    return os.getenv(env_name, default).strip()


@dataclass
class GeocodeConfig:
    enabled: bool = field(default_factory=lambda: os.getenv("GEOCODER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"})
    provider: str = field(default_factory=lambda: os.getenv("GEOCODER_PROVIDER", "nominatim").strip().lower())
    cache_path: str = field(default_factory=lambda: os.getenv("GEOCODER_CACHE_PATH", "data/geocode_cache.json"))
    base_url: str = field(default_factory=lambda: os.getenv("GEOCODER_BASE_URL", "https://nominatim.openstreetmap.org/search"))
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "GEOCODER_USER_AGENT",
            "SanAnselmoPermitScrape/1.0 (local geocoding workflow)",
        )
    )
    email: str = field(default_factory=lambda: os.getenv("GEOCODER_EMAIL", "").strip())
    country_codes: str = field(default_factory=lambda: os.getenv("GEOCODER_COUNTRY_CODES", "us").strip())
    viewbox: str = field(default_factory=lambda: os.getenv("GEOCODER_VIEWBOX", "").strip())
    bounded: bool = field(default_factory=lambda: os.getenv("GEOCODER_BOUNDED", "false").strip().lower() in {"1", "true", "yes", "on"})
    timeout_seconds: int = field(default_factory=lambda: int(ini_or_env("timeout_seconds", "GEOCODER_TIMEOUT_SECONDS", "30") or "30"))
    min_interval_seconds: float = field(default_factory=lambda: float(ini_or_env("min_interval_seconds", "GEOCODER_MIN_INTERVAL_SECONDS", "1.0") or "1.0"))


class JsonGeocoder:
    def __init__(self, config: GeocodeConfig | None = None) -> None:
        self.config = config or GeocodeConfig()
        self.cache_path = Path(self.config.cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = self._load_cache()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.config.user_agent, "Accept": "application/json"})
        self._last_request_started = 0.0

    def _load_cache(self) -> Dict[str, dict]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self) -> None:
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _build_query(self, record: PermitRecord | AddressRecord) -> str:
        parts = [normalize_str(record.address), normalize_str(record.city_state_zip)]
        return ", ".join(part for part in parts if part)

    def _build_queries(self, record: PermitRecord | AddressRecord) -> list[str]:
        primary = self._build_query(record)
        if not primary:
            return []

        queries = [primary]
        address = normalize_str(record.address)
        city_state_zip = normalize_str(record.city_state_zip)

        stripped_address = re.sub(r"\s+unit\s+[\w-]+\s*$", "", address, flags=re.IGNORECASE).strip()
        if stripped_address and stripped_address.lower() != address.lower():
            fallback = ", ".join(part for part in [stripped_address, city_state_zip] if part)
            if fallback and fallback.lower() not in {query.lower() for query in queries}:
                queries.append(fallback)

        return queries

    def _cache_key(self, query: str) -> str:
        return query.lower()

    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request_started
        wait_for = self.config.min_interval_seconds - elapsed
        if wait_for > 0:
            time.sleep(wait_for)

    def geocode_record(self, record: PermitRecord) -> PermitRecord:
        return self._geocode(record)

    def geocode_address(self, record: AddressRecord) -> AddressRecord:
        return self._geocode(record)

    def _geocode(self, record: PermitRecord | AddressRecord):
        if not self.config.enabled:
            return record

        queries = self._build_queries(record)
        if not queries:
            return record

        if self.config.provider != "nominatim":
            raise RuntimeError(f"Unsupported geocoder provider: {self.config.provider}")

        last_result = None
        for query in queries:
            cache_key = self._cache_key(query)
            cached = self.cache.get(cache_key)
            if cached:
                last_result = cached
                if normalize_str(cached.get("latitude")) and normalize_str(cached.get("longitude")):
                    return self._apply_result(record, cached)
                continue

            self._sleep_if_needed()
            self._last_request_started = time.monotonic()

            params = {
                "q": query,
                "format": "jsonv2",
                "limit": 1,
            }
            if self.config.country_codes:
                params["countrycodes"] = self.config.country_codes
            if self.config.email:
                params["email"] = self.config.email
            if self.config.viewbox:
                params["viewbox"] = self.config.viewbox
                params["bounded"] = "1" if self.config.bounded else "0"

            response = self.session.get(
                self.config.base_url,
                params=params,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()

            result = {
                "query": query,
                "latitude": "",
                "longitude": "",
                "geocoded_address": "",
                "geocode_source": self.config.provider,
                "queried_at": utc_now_iso(),
            }
            if payload:
                best = payload[0]
                result["latitude"] = normalize_str(best.get("lat"))
                result["longitude"] = normalize_str(best.get("lon"))
                result["geocoded_address"] = normalize_str(best.get("display_name"))

            self.cache[cache_key] = result
            self._save_cache()
            last_result = result
            if result["latitude"] and result["longitude"]:
                return self._apply_result(record, result)

        if last_result:
            return self._apply_result(record, last_result)
        return record

    def needs_geocoding(self, record: PermitRecord | AddressRecord) -> bool:
        if not self.config.enabled:
            return False

        queries = self._build_queries(record)
        if not queries:
            return False

        if not record.latitude or not record.longitude:
            return True

        for query in queries:
            cached = self.cache.get(self._cache_key(query))
            if not cached:
                return True
            cached_lat = normalize_str(cached.get("latitude"))
            cached_lon = normalize_str(cached.get("longitude"))
            if cached_lat == normalize_str(record.latitude) and cached_lon == normalize_str(record.longitude):
                return False

        return True

    def _apply_result(self, record: PermitRecord | AddressRecord, result: dict):
        record.latitude = normalize_str(result.get("latitude"))
        record.longitude = normalize_str(result.get("longitude"))
        record.geocoded_address = normalize_str(result.get("geocoded_address"))
        record.geocode_source = normalize_str(result.get("geocode_source"))
        return record
