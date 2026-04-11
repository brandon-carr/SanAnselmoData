from __future__ import annotations

import configparser
import sys
from pathlib import Path
from typing import Dict, List

from etrakit_client import ETrakitClient
from permit_model import AddressRecord, PermitRecord, build_address_id, utc_now_iso
from storage import JsonPermitStore, hydrate_permits_from_addresses


DATASET_NAME = "building"
CONFIG_PATH = Path(__file__).with_name("permit_scrapers.ini")


def load_building_config() -> tuple[int, int]:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")

    year = parser.getint("building", "year", fallback=2026)
    batch_size = parser.getint("building", "batch_size", fallback=10)
    if year < 1900:
        raise ValueError("building year must be a four-digit year")
    if batch_size < 1:
        raise ValueError("building batch_size must be at least 1")
    return year, batch_size


def build_permit_number(year: int, sequence: int) -> str:
    return f"B{year}-{sequence:04d}"


def sync_address_from_permit(
    permit: PermitRecord,
    permit_records: Dict[str, PermitRecord],
    address_records: Dict[str, AddressRecord],
    store: JsonPermitStore,
    when_iso: str,
) -> tuple[bool, PermitRecord]:
    permit.address_id = build_address_id(permit.address, permit.city_state_zip)
    address_record = AddressRecord.from_permit(permit, when_iso=when_iso)
    existing_address = address_records.get(address_record.address_id)

    if existing_address:
        address_record.latitude = existing_address.latitude
        address_record.longitude = existing_address.longitude
        address_record.geocoded_address = existing_address.geocoded_address
        address_record.geocode_source = existing_address.geocode_source
        address_record.geocode_status = existing_address.geocode_status
        address_record.geocode_error = existing_address.geocode_error
        address_record.geocode_attempts = existing_address.geocode_attempts
        address_record.geocode_last_attempt_at = existing_address.geocode_last_attempt_at
        if existing_address.data_hash != address_record.compute_data_hash():
            store.append_address_history(address_record.address_id, existing_address)
            _, address_record = existing_address.apply_new_source(address_record, when_iso=when_iso)
        else:
            existing_address.last_seen_at = when_iso
            address_record = existing_address
    else:
        address_record.first_seen_at = when_iso
        address_record.last_seen_at = when_iso
        address_record.last_changed_at = when_iso
        address_record.data_hash = address_record.compute_data_hash()

    address_records[address_record.address_id] = address_record
    permit.latitude = address_record.latitude
    permit.longitude = address_record.longitude
    permit.geocoded_address = address_record.geocoded_address
    permit.geocode_source = address_record.geocode_source
    permit.geocode_status = address_record.geocode_status
    permit.geocode_error = address_record.geocode_error
    permit.geocode_attempts = address_record.geocode_attempts
    permit.geocode_last_attempt_at = address_record.geocode_last_attempt_at

    existing = permit_records.get(permit.permit_number)
    if existing is None:
        permit.first_seen_at = when_iso
        permit.last_seen_at = when_iso
        permit.last_changed_at = when_iso
        permit.data_hash = permit.compute_data_hash()
        return True, permit

    changed, merged = existing.apply_new_scrape(permit, when_iso=when_iso)
    if not changed:
        return False, merged

    store.append_permit_history(DATASET_NAME, permit.permit_number, existing)
    return True, merged


def main() -> int:
    year, batch_size = load_building_config()
    store = JsonPermitStore()
    state = store.load_scrape_state(DATASET_NAME)
    permit_records = store.load_permits(DATASET_NAME)
    address_records = store.load_addresses()

    state_year = int(state.get("year") or 0)
    if state_year != year:
        state = {
            "year": year,
            "next_sequence": 1,
            "exhausted": False,
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "",
            "last_summary": {},
            "errors": [],
        }

    next_sequence = int(state.get("next_sequence") or 1)
    permit_numbers = [build_permit_number(year, next_sequence + offset) for offset in range(batch_size)]
    print("TARGETS " + ", ".join(permit_numbers))

    state["last_run_started_at"] = utc_now_iso()
    state["last_run_status"] = "running"
    state["errors"] = []
    store.save_scrape_state(DATASET_NAME, state)

    client = ETrakitClient()
    try:
        client.login()
    except Exception as exc:
        print("FAILED")
        print(f"- login failed: {exc}")
        return 1

    found_count = 0
    new_count = 0
    changed_count = 0
    unchanged_count = 0
    errors: List[str] = []
    stop_at_sequence: int | None = None

    for sequence, permit_number in enumerate(permit_numbers, start=next_sequence):
        try:
            links = client.search_permit_by_number(permit_number)
        except Exception as exc:
            errors.append(f"{permit_number}: {exc}")
            continue

        if not links:
            stop_at_sequence = sequence
            break

        detail_url = links[0]
        try:
            fields = client.fetch_permit_details(detail_url)
            fields["permit_source"] = DATASET_NAME
            record = PermitRecord.from_scraped_fields(fields)
        except Exception as exc:
            errors.append(f"{permit_number}: {exc}")
            continue

        found_count += 1
        when_iso = utc_now_iso()
        is_new = record.permit_number not in permit_records
        changed, merged = sync_address_from_permit(record, permit_records, address_records, store, when_iso)
        permit_records[record.permit_number] = merged

        if is_new:
            new_count += 1
        elif changed:
            changed_count += 1
        else:
            unchanged_count += 1

    hydrate_permits_from_addresses(permit_records, address_records)
    store.save_addresses(address_records)
    store.save_permits(DATASET_NAME, permit_records)
    store.save_all_permits_view(store.load_all_permits())

    if stop_at_sequence is not None:
        state["next_sequence"] = stop_at_sequence
        state["exhausted"] = True
    else:
        state["next_sequence"] = next_sequence + batch_size
        state["exhausted"] = False

    state["year"] = year
    state["last_run_finished_at"] = utc_now_iso()
    state["last_run_status"] = "success" if not errors else "partial"
    state["last_summary"] = {
        "year": year,
        "start_sequence": next_sequence,
        "requested_batch_size": batch_size,
        "found": found_count,
        "new": new_count,
        "changed": changed_count,
        "unchanged": unchanged_count,
        "next_sequence": state["next_sequence"],
        "stopped_at_missing": build_permit_number(year, stop_at_sequence) if stop_at_sequence is not None else "",
    }
    state["errors"] = errors
    store.save_scrape_state(DATASET_NAME, state)

    print(
        f"SUMMARY year={year} start={build_permit_number(year, next_sequence)} "
        f"found={found_count} new={new_count} changed={changed_count} unchanged={unchanged_count} "
        f"next={build_permit_number(year, state['next_sequence'])} exhausted={state['exhausted']}"
    )
    if errors:
        print("WARNINGS")
        for error in errors:
            print(f"- {error}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
