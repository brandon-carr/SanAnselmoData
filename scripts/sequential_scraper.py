from __future__ import annotations

import configparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

from etrakit_client import ETrakitClient
from geocoder import JsonGeocoder
from permit_model import AddressRecord, PermitRecord, build_address_id, utc_now_iso
from storage import JsonPermitStore, hydrate_permits_from_addresses


CONFIG_PATH = Path(__file__).with_name("permit_scrapers.ini")
_CACHE_GEOCODER: JsonGeocoder | None = None


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


def load_site_config() -> dict:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    inactive_raw = parser.get("web", "inactive_statuses", fallback="")
    inactive_statuses = [item.strip() for item in inactive_raw.split(",") if item.strip()]
    return {
        "inactive_statuses": inactive_statuses,
        "show_left_cards": parser.getboolean("web", "show_left_cards", fallback=True),
    }


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
        "year_streams": {},
        "last_run_started_at": "",
        "last_run_finished_at": "",
        "last_run_status": "",
        "last_summary": {},
        "errors": [],
    }


def normalize_dataset_state(state: dict, *, year: int, batch_size: int) -> dict:
    if not isinstance(state, dict):
        return default_dataset_state(year, batch_size)

    normalized = default_dataset_state(
        int(state.get("year") or year),
        int(state.get("batch_size") or batch_size),
    )
    normalized.update(state)

    raw_year_streams = normalized.get("year_streams")
    year_streams = raw_year_streams if isinstance(raw_year_streams, dict) else {}

    raw_streams = normalized.get("streams")
    streams = raw_streams if isinstance(raw_streams, dict) else {}
    legacy_year = int(normalized.get("year") or year)
    legacy_year_key = str(legacy_year)

    if streams and legacy_year_key not in year_streams:
        year_streams[legacy_year_key] = streams

    normalized["year_streams"] = year_streams
    normalized["streams"] = year_streams.get(str(year), {})
    normalized["year"] = year
    normalized["batch_size"] = batch_size
    return normalized


def parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def should_skip_exhausted_stream(stream_state: dict, *, now: datetime, cooldown_hours: int = 24) -> bool:
    if not bool(stream_state.get("exhausted")):
        return False

    finished_at = parse_iso_datetime(stream_state.get("last_run_finished_at", ""))
    if finished_at is None:
        return False

    return (now - finished_at) < timedelta(hours=cooldown_hours)


def get_cache_geocoder() -> JsonGeocoder:
    global _CACHE_GEOCODER
    if _CACHE_GEOCODER is None:
        _CACHE_GEOCODER = JsonGeocoder()
    return _CACHE_GEOCODER


def print_prefix_summaries(dataset_name: str, prefixes: List[str], prefix_summaries: Dict[str, dict]) -> None:
    print(f"FINAL PREFIX SUMMARY dataset={dataset_name}")
    for prefix in prefixes:
        summary = prefix_summaries.get(prefix)
        if not summary:
            print(f"- prefix={prefix} status=not_run")
            continue

        if summary.get("skipped_recently_exhausted"):
            print(
                f"- prefix={prefix} skipped_recently_exhausted=true "
                f"next={build_activity_number(prefix, int(summary.get('year') or 0), int(summary.get('next_sequence') or 1))}"
            )
            continue

        print(
            f"- prefix={prefix} year={summary.get('year')} found={summary.get('found', 0)} "
            f"new={summary.get('new', 0)} changed={summary.get('changed', 0)} "
            f"unchanged={summary.get('unchanged', 0)} next={summary.get('next_sequence', 1)} "
            f"stopped_at_missing={summary.get('stopped_at_missing', '') or 'none'}"
        )


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
        if existing_address.has_meaningful_history_change(address_record):
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

    if not (address_record.latitude and address_record.longitude):
        cache_geocoder = get_cache_geocoder()
        if cache_geocoder.apply_cached_result(address_record):
            address_record.geocode_status = "success"
            address_record.geocode_error = ""
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
    store.save_site_config(load_site_config())
    state = normalize_dataset_state(
        store.load_scrape_state(dataset_name),
        year=config.year,
        batch_size=config.batch_size,
    )
    permit_records = store.load_permits(dataset_name)
    address_records = store.load_addresses()

    current_year_key = str(config.year)
    year_streams = state.get("year_streams")
    if not isinstance(year_streams, dict):
        year_streams = {}
        state["year_streams"] = year_streams
    current_streams = year_streams.get(current_year_key)
    if not isinstance(current_streams, dict):
        current_streams = {}
        year_streams[current_year_key] = current_streams
    state["streams"] = current_streams

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
    now_utc = datetime.now(timezone.utc)

    for prefix in config.prefixes:
        stream_state = current_streams.get(prefix, default_stream_state())
        if should_skip_exhausted_stream(stream_state, now=now_utc):
            prefix_summaries[prefix] = {
                "prefix": prefix,
                "year": config.year,
                "start_sequence": int(stream_state.get("next_sequence") or 1),
                "requested_batch_size": config.batch_size,
                "found": 0,
                "new": 0,
                "changed": 0,
                "unchanged": 0,
                "next_sequence": int(stream_state.get("next_sequence") or 1),
                "stopped_at_missing": (
                    stream_state.get("last_summary", {}) or {}
                ).get("stopped_at_missing", ""),
                "skipped_recently_exhausted": True,
            }
            print(
                f"SKIPPED dataset={dataset_name} prefix={prefix} "
                f"reason=recently_exhausted next={build_activity_number(prefix, config.year, int(stream_state.get('next_sequence') or 1))}"
            )
            current_streams[prefix] = stream_state
            continue

        next_sequence = int(stream_state.get("next_sequence") or 1)
        activity_numbers = [
            build_activity_number(prefix, config.year, next_sequence + offset)
            for offset in range(config.batch_size)
        ]

        print(f"TARGETS {dataset_name}:{prefix} " + ", ".join(activity_numbers))

        stream_state["last_run_started_at"] = utc_now_iso()
        stream_state["last_run_status"] = "running"
        stream_state["errors"] = []
        current_streams[prefix] = stream_state
        store.save_scrape_state(dataset_name, state)

        found_count = 0
        new_count = 0
        changed_count = 0
        unchanged_count = 0
        stream_errors: List[str] = []
        stop_at_sequence: int | None = None

        for sequence, activity_number in enumerate(activity_numbers, start=next_sequence):
            if activity_number in permit_records:
                unchanged_count += 1
                continue

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
                fields["record_type"] = dataset_name
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
        current_streams[prefix] = stream_state
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
    print_prefix_summaries(dataset_name, config.prefixes, prefix_summaries)

    return 0
