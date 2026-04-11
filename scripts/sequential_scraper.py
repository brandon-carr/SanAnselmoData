from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from etrakit_client import ETrakitClient
from permit_model import AddressRecord, PermitRecord, build_address_id, utc_now_iso
from storage import JsonPermitStore, hydrate_permits_from_addresses


CONFIG_PATH = Path(__file__).with_name("permit_scrapers.ini")


@dataclass(frozen=True)
class ScrapeStreamConfig:
    dataset_name: str
    detail_base_url: str
    record_number_label: str
    type_label: str
    address_label: str
    status_label: str
    prefixes: List[str]
    year: int
    batch_size: int


def load_stream_config(dataset_name: str) -> ScrapeStreamConfig:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")

    if not parser.has_section("global"):
        raise ValueError("permit_scrapers.ini is missing a [global] section")
    if not parser.has_section(dataset_name):
        raise ValueError(f"permit_scrapers.ini is missing a [{dataset_name}] section")

    year = parser.getint("global", "year", fallback=2026)
    batch_size = parser.getint("global", "batch_size", fallback=10)
    if year < 1900:
        raise ValueError("global year must be a four-digit year")
    if batch_size < 1:
        raise ValueError("global batch_size must be at least 1")

    detail_base_url = parser.get(dataset_name, "detail_base_url", fallback="").strip()
    if not detail_base_url:
        raise ValueError(f"{dataset_name} detail_base_url is required")

    prefixes_raw = parser.get(dataset_name, "prefixes", fallback="")
    prefixes = [item.strip().upper() for item in prefixes_raw.split(",") if item.strip()]
    if not prefixes:
        raise ValueError(f"{dataset_name} must define at least one prefix")

    return ScrapeStreamConfig(
        dataset_name=dataset_name,
        detail_base_url=detail_base_url,
        record_number_label=parser.get(dataset_name, "record_number_label", fallback="Permit Number").strip(),
        type_label=parser.get(dataset_name, "type_label", fallback="Type").strip(),
        address_label=parser.get(dataset_name, "address_label", fallback="Address").strip(),
        status_label=parser.get(dataset_name, "status_label", fallback="Status").strip(),
        prefixes=prefixes,
        year=year,
        batch_size=batch_size,
    )


def build_activity_number(prefix: str, year: int, sequence: int) -> str:
    return f"{prefix}{year}-{sequence:04d}"


def default_stream_state() -> dict:
    return {
        "next_sequence": 1,
        "exhausted": False,
        "last_run_started_at": "",
        "last_run_finished_at": "",
        "last_run_status": "",
        "last_summary": {},
        "errors": [],
    }


def default_dataset_state(year: int, batch_size: int) -> dict:
    return {
        "year": year,
        "batch_size": batch_size,
        "streams": {},
        "last_run_started_at": "",
        "last_run_finished_at": "",
        "last_run_status": "",
        "last_summary": {},
        "errors": [],
    }


def sync_address_from_record(
    dataset_name: str,
    record: PermitRecord,
    permit_records: Dict[str, PermitRecord],
    address_records: Dict[str, AddressRecord],
    store: JsonPermitStore,
    when_iso: str,
) -> tuple[bool, PermitRecord]:
    record.address_id = build_address_id(record.address, record.city_state_zip)
    address_record = AddressRecord.from_permit(record, when_iso=when_iso)
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
    record.latitude = address_record.latitude
    record.longitude = address_record.longitude
    record.geocoded_address = address_record.geocoded_address
    record.geocode_source = address_record.geocode_source
    record.geocode_status = address_record.geocode_status
    record.geocode_error = address_record.geocode_error
    record.geocode_attempts = address_record.geocode_attempts
    record.geocode_last_attempt_at = address_record.geocode_last_attempt_at

    existing = permit_records.get(record.permit_number)
    if existing is None:
        record.first_seen_at = when_iso
        record.last_seen_at = when_iso
        record.last_changed_at = when_iso
        record.data_hash = record.compute_data_hash()
        return True, record

    changed, merged = existing.apply_new_scrape(record, when_iso=when_iso)
    if not changed:
        return False, merged

    store.append_permit_history(dataset_name, record.permit_number, existing)
    return True, merged


def run_dataset(dataset_name: str) -> int:
    config = load_stream_config(dataset_name)
    store = JsonPermitStore()
    state = store.load_scrape_state(dataset_name)
    permit_records = store.load_permits(dataset_name)
    address_records = store.load_addresses()

    if (
        int(state.get("year") or 0) != config.year
        or int(state.get("batch_size") or 0) != config.batch_size
        or not isinstance(state.get("streams"), dict)
    ):
        state = default_dataset_state(config.year, config.batch_size)

    state["last_run_started_at"] = utc_now_iso()
    state["last_run_status"] = "running"
    state["errors"] = []
    store.save_scrape_state(dataset_name, state)

    client = ETrakitClient()
    try:
        client.login()
    except Exception as exc:
        print("FAILED")
        print(f"- login failed: {exc}")
        return 1

    overall_errors: List[str] = []
    prefix_summaries: Dict[str, dict] = {}

    for prefix in config.prefixes:
        stream_state = state["streams"].get(prefix, default_stream_state())
        next_sequence = int(stream_state.get("next_sequence") or 1)
        activity_numbers = [
            build_activity_number(prefix, config.year, next_sequence + offset)
            for offset in range(config.batch_size)
        ]

        print(f"TARGETS {dataset_name}:{prefix} " + ", ".join(activity_numbers))

        stream_state["last_run_started_at"] = utc_now_iso()
        stream_state["last_run_status"] = "running"
        stream_state["errors"] = []
        state["streams"][prefix] = stream_state
        store.save_scrape_state(dataset_name, state)

        found_count = 0
        new_count = 0
        changed_count = 0
        unchanged_count = 0
        stream_errors: List[str] = []
        stop_at_sequence: int | None = None

        for sequence, activity_number in enumerate(activity_numbers, start=next_sequence):
            try:
                fields = client.fetch_activity_details_by_number(
                    config.detail_base_url,
                    activity_number,
                    record_number_labels=[config.record_number_label],
                    type_labels=[config.type_label],
                    address_labels=[config.address_label],
                    status_labels=[config.status_label],
                )
            except Exception as exc:
                stream_errors.append(f"{activity_number}: {exc}")
                continue

            if not fields:
                stop_at_sequence = sequence
                break

            try:
                fields["permit_source"] = dataset_name
                record = PermitRecord.from_scraped_fields(fields)
            except Exception as exc:
                stream_errors.append(f"{activity_number}: {exc}")
                continue

            found_count += 1
            when_iso = utc_now_iso()
            is_new = record.permit_number not in permit_records
            changed, merged = sync_address_from_record(
                dataset_name,
                record,
                permit_records,
                address_records,
                store,
                when_iso,
            )
            permit_records[record.permit_number] = merged

            if is_new:
                new_count += 1
            elif changed:
                changed_count += 1
            else:
                unchanged_count += 1

        hydrate_permits_from_addresses(permit_records, address_records)
        store.save_addresses(address_records)
        store.save_permits(dataset_name, permit_records)
        store.save_all_permits_view(store.load_all_permits())

        if stop_at_sequence is not None:
            stream_state["next_sequence"] = stop_at_sequence
            stream_state["exhausted"] = True
        else:
            stream_state["next_sequence"] = next_sequence + config.batch_size
            stream_state["exhausted"] = False

        stream_state["last_run_finished_at"] = utc_now_iso()
        stream_state["last_run_status"] = "success" if not stream_errors else "partial"
        stream_state["last_summary"] = {
            "prefix": prefix,
            "year": config.year,
            "start_sequence": next_sequence,
            "requested_batch_size": config.batch_size,
            "found": found_count,
            "new": new_count,
            "changed": changed_count,
            "unchanged": unchanged_count,
            "next_sequence": stream_state["next_sequence"],
            "stopped_at_missing": (
                build_activity_number(prefix, config.year, stop_at_sequence)
                if stop_at_sequence is not None
                else ""
            ),
        }
        stream_state["errors"] = stream_errors
        state["streams"][prefix] = stream_state
        prefix_summaries[prefix] = dict(stream_state["last_summary"])
        overall_errors.extend(stream_errors)
        store.save_scrape_state(dataset_name, state)

        print(
            f"SUMMARY dataset={dataset_name} prefix={prefix} "
            f"start={build_activity_number(prefix, config.year, next_sequence)} "
            f"found={found_count} new={new_count} changed={changed_count} unchanged={unchanged_count} "
            f"next={build_activity_number(prefix, config.year, stream_state['next_sequence'])} "
            f"exhausted={stream_state['exhausted']}"
        )
        if stream_errors:
            print(f"WARNINGS prefix={prefix}")
            for error in stream_errors:
                print(f"- {error}")

    state["year"] = config.year
    state["batch_size"] = config.batch_size
    state["last_run_finished_at"] = utc_now_iso()
    state["last_run_status"] = "success" if not overall_errors else "partial"
    state["last_summary"] = {"prefix_summaries": prefix_summaries}
    state["errors"] = overall_errors
    store.save_scrape_state(dataset_name, state)

    return 0
