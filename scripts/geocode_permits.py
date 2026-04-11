from __future__ import annotations

import os
import sys
from typing import Dict, List

from geocoder import JsonGeocoder
from permit_model import AddressRecord, PermitRecord, normalize_str, utc_now_iso
from storage import JsonPermitStore, hydrate_permit_groups_from_addresses


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
) -> tuple[bool, str]:
    record.geocode_attempts += 1
    record.geocode_last_attempt_at = when_iso

    try:
        geocoded = geocoder.geocode_address(record)
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
        success, error_message = attempt_geocode_address(record, geocoder, when_iso)
        record.last_seen_at = when_iso
        record.data_hash = record.compute_data_hash()

        if record.data_hash != previous.data_hash:
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
