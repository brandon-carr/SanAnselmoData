from __future__ import annotations

import configparser
import os
import sys
from typing import Dict, List
from pathlib import Path

import requests

from geocoder import JsonGeocoder
from permit_model import AddressRecord, PermitRecord, normalize_str, utc_now_iso
from storage import JsonPermitStore, hydrate_permit_groups_from_addresses

CONFIG_PATH = Path(__file__).with_name("permit_scrapers.ini")


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_geocoding_settings() -> dict:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")

    def get_int(option: str, env_name: str, default: int) -> int:
        if parser.has_section("geocoding"):
            raw = parser.get("geocoding", option, fallback="").strip()
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass
        return getenv_int(env_name, default)

    return {
        "batch_size": get_int("batch_size", "GEOCODE_BATCH_SIZE", 10),
        "max_attempts": get_int("max_attempts", "GEOCODE_MAX_ATTEMPTS", 3),
    }


def geocode_sort_key(record: PermitRecord) -> tuple[str, str]:
    return (
        normalize_str(record.first_seen_at) or normalize_str(record.last_seen_at),
        record.permit_number,
    )


def should_attempt_geocode(record: AddressRecord, max_attempts: int) -> bool:
    if record.latitude and record.longitude:
        return False
    if record.geocode_status == "success":
        return False
    if record.geocode_attempts >= max_attempts:
        return False
    return True


def ensure_address_records(
    permits: Dict[str, PermitRecord],
    addresses: Dict[str, AddressRecord],
) -> None:
    for permit in permits.values():
        if not permit.address_id:
            continue
        if permit.address_id in addresses:
            continue
        addresses[permit.address_id] = AddressRecord.from_permit(permit)


def select_pending_addresses(
    permit_groups: Dict[str, Dict[str, PermitRecord]],
    addresses: Dict[str, AddressRecord],
    batch_size: int,
    max_attempts: int,
) -> List[AddressRecord]:
    newest_permit_by_address: Dict[str, PermitRecord] = {}
    for permits in permit_groups.values():
        for permit in permits.values():
            if not permit.address_id:
                continue
            current = newest_permit_by_address.get(permit.address_id)
            if current is None or geocode_sort_key(permit) > geocode_sort_key(current):
                newest_permit_by_address[permit.address_id] = permit

    pending = []
    for address_id, permit in newest_permit_by_address.items():
        address = addresses.get(address_id)
        if not address:
            continue
        if should_attempt_geocode(address, max_attempts):
            pending.append((geocode_sort_key(permit), address))

    pending.sort(key=lambda item: item[0], reverse=True)
    return [address for _, address in pending[:batch_size]]


def attempt_geocode_address(
    record: AddressRecord,
    geocoder: JsonGeocoder,
    when_iso: str,
) -> tuple[bool, str, bool]:
    record.geocode_attempts += 1
    record.geocode_last_attempt_at = when_iso

    try:
        geocoded = geocoder.geocode_address(record)
    except Exception as exc:
        record.geocode_source = geocoder.config.provider
        record.geocode_status = "error"
        record.geocode_error = normalize_str(exc) or "Geocode request failed"
        return False, record.geocode_error, is_fatal_geocode_error(exc)

    if geocoded.latitude and geocoded.longitude:
        geocoded.geocode_status = "success"
        geocoded.geocode_error = ""
        geocoded.last_seen_at = when_iso
        return True, "", False

    geocoded.geocode_status = "error"
    geocoded.geocode_error = "No geocode match returned"
    return False, geocoded.geocode_error, False


def is_fatal_geocode_error(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {401, 403, 404, 408, 409, 429, 500, 502, 503, 504}:
            return True

    message = normalize_str(exc).lower()
    fatal_markers = (
        "429",
        "403",
        "401",
        "too many requests",
        "forbidden",
        "unauthorized",
        "connection refused",
        "name resolution",
        "temporarily unavailable",
        "service unavailable",
        "timed out",
        "timeout",
    )
    return any(marker in message for marker in fatal_markers)


def main() -> int:
    geocoding_settings = load_geocoding_settings()
    batch_size = geocoding_settings["batch_size"]
    max_attempts = geocoding_settings["max_attempts"]

    store = JsonPermitStore()
    permit_groups = store.load_all_permits()
    address_records = store.load_addresses()
    geocoder = JsonGeocoder()
    for permits in permit_groups.values():
        ensure_address_records(permits, address_records)

    pending = select_pending_addresses(
        permit_groups,
        address_records,
        batch_size=batch_size,
        max_attempts=max_attempts,
    )
    if not pending:
        print("SKIPPED no addresses pending geocoding")
        return 0

    print("TARGETS " + ", ".join(record.address_id for record in pending))

    successes = 0
    errors: List[str] = []

    for record in pending:
        previous = AddressRecord.from_dict(record.to_dict())
        when_iso = utc_now_iso()
        success, error_message, fatal_error = attempt_geocode_address(record, geocoder, when_iso)
        record.last_seen_at = when_iso
        record.data_hash = record.compute_data_hash()

        if previous.has_meaningful_history_change(record) and not record.is_first_geocode_fill_from(previous):
            store.append_address_history(record.address_id, previous)

        if success:
            record.last_changed_at = when_iso
            successes += 1
            print(
                f"SUCCESS address={record.address_id} attempts={record.geocode_attempts} "
                f"status={record.geocode_status}"
            )
        else:
            record.last_changed_at = when_iso
            errors.append(f"{record.address_id}: {error_message}")
            print(
                f"ERROR address={record.address_id} attempts={record.geocode_attempts} "
                f"status={record.geocode_status} error={record.geocode_error}"
            )
            if fatal_error:
                print(
                    "FATAL geocode provider error encountered; stopping remaining geocode attempts "
                    "for this run so the failure can be reviewed."
                )
                break

    hydrate_permit_groups_from_addresses(permit_groups, address_records)
    store.save_addresses(address_records)
    store.save_all_permits(permit_groups)
    store.save_all_permits_view(permit_groups)
    print(
        f"SUMMARY selected={len(pending)} success={successes} "
        f"errors={len(errors)} max_attempts={max_attempts}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
