from __future__ import annotations

import os
import sys
from typing import List

from geocoder import JsonGeocoder
from permit_model import PermitRecord, normalize_str, utc_now_iso
from storage import JsonPermitStore


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def geocode_sort_key(record: PermitRecord) -> tuple[str, str]:
    return (
        normalize_str(record.first_seen_at) or normalize_str(record.last_seen_at),
        record.permit_number,
    )


def should_attempt_geocode(record: PermitRecord, max_attempts: int) -> bool:
    if record.latitude and record.longitude:
        return False
    if record.geocode_status == "success":
        return False
    if record.geocode_attempts >= max_attempts:
        return False
    return True


def select_pending_records(records: dict[str, PermitRecord], batch_size: int, max_attempts: int) -> List[PermitRecord]:
    pending = [record for record in records.values() if should_attempt_geocode(record, max_attempts)]
    pending.sort(key=geocode_sort_key, reverse=True)
    return pending[:batch_size]


def attempt_geocode_record(
    record: PermitRecord,
    geocoder: JsonGeocoder,
    when_iso: str,
) -> tuple[bool, str]:
    record.geocode_attempts += 1
    record.geocode_last_attempt_at = when_iso

    try:
        geocoded = geocoder.geocode_record(record)
    except Exception as exc:
        record.geocode_source = geocoder.config.provider
        record.geocode_status = "error"
        record.geocode_error = normalize_str(exc) or "Geocode request failed"
        return False, record.geocode_error

    if geocoded.latitude and geocoded.longitude:
        geocoded.geocode_status = "success"
        geocoded.geocode_error = ""
        geocoded.last_seen_at = when_iso
        return True, ""

    geocoded.geocode_status = "error"
    geocoded.geocode_error = "No geocode match returned"
    return False, geocoded.geocode_error


def main() -> int:
    batch_size = getenv_int("GEOCODE_BATCH_SIZE", 10)
    max_attempts = getenv_int("GEOCODE_MAX_ATTEMPTS", 3)

    store = JsonPermitStore()
    current_records = store.load_current()
    geocoder = JsonGeocoder()

    pending = select_pending_records(current_records, batch_size=batch_size, max_attempts=max_attempts)
    if not pending:
        print("SKIPPED no permits pending geocoding")
        return 0

    print("TARGETS " + ", ".join(record.permit_number for record in pending))

    successes = 0
    errors: List[str] = []

    for record in pending:
        previous = PermitRecord.from_dict(record.to_dict())
        when_iso = utc_now_iso()
        success, error_message = attempt_geocode_record(record, geocoder, when_iso)
        record.last_seen_at = when_iso
        record.data_hash = record.compute_data_hash()

        if record.data_hash != previous.data_hash:
            store.append_history(record.permit_number, previous)

        if success:
            record.last_changed_at = when_iso
            successes += 1
            print(
                f"SUCCESS permit={record.permit_number} attempts={record.geocode_attempts} "
                f"status={record.geocode_status}"
            )
        else:
            record.last_changed_at = when_iso
            errors.append(f"{record.permit_number}: {error_message}")
            print(
                f"ERROR permit={record.permit_number} attempts={record.geocode_attempts} "
                f"status={record.geocode_status} error={record.geocode_error}"
            )

    store.save_current(current_records)
    print(
        f"SUMMARY selected={len(pending)} success={successes} "
        f"errors={len(errors)} max_attempts={max_attempts}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
