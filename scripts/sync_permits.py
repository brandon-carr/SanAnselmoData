from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List

from etrakit_client import ETrakitClient, ETrakitError
from geocoder import JsonGeocoder
from permit_model import PermitRecord, utc_now_iso
from storage import JsonPermitStore, finalize_run_state, init_run_state


def getenv_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def getenv_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def resolve_target_dates(state: dict) -> List[str]:
    manual_date = getenv_str("TARGET_ISSUED_DATE")
    days_back = getenv_int("DAYS_BACK", 1)
    force_rerun = getenv_bool("FORCE_RERUN", False)

    if manual_date:
        return [manual_date]

    completed = set(state.get("completed_issued_dates", []))
    today = datetime.utcnow().date()

    targets: List[str] = []
    for offset in range(1, days_back + 1):
        d = today - timedelta(days=offset)
        d_str = d.isoformat()

        if not force_rerun and d_str in completed:
            continue

        targets.append(d_str)

    return targets


def sync_one_date(
    client: ETrakitClient,
    current_records: Dict[str, PermitRecord],
    target_issued_date: str,
    store: JsonPermitStore,
) -> tuple[bool, dict, List[str]]:
    errors: List[str] = []
    permits_found = 0
    permits_new = 0
    permits_changed = 0
    permits_unchanged = 0

    try:
        detail_urls = client.search_permits_by_issued_date(target_issued_date)
        permits_found = len(detail_urls)

        scraped_records: Dict[str, PermitRecord] = {}
        for detail_url in detail_urls:
            try:
                fields = client.fetch_permit_details(detail_url)
                record = PermitRecord.from_scraped_fields(fields)
                scraped_records[record.permit_number] = record
            except Exception as exc:
                errors.append(f"{detail_url}: {exc}")

        if errors:
            return False, {
                "target_issued_date": target_issued_date,
                "permits_found": permits_found,
                "permits_new": permits_new,
                "permits_changed": permits_changed,
                "permits_unchanged": permits_unchanged,
            }, errors

        now_iso = utc_now_iso()

        for permit_number, new_record in scraped_records.items():
            if permit_number not in current_records:
                new_record.first_seen_at = now_iso
                new_record.last_seen_at = now_iso
                new_record.last_changed_at = now_iso
                new_record.data_hash = new_record.compute_data_hash()
                current_records[permit_number] = new_record
                permits_new += 1
                continue

            existing = current_records[permit_number]
            new_record.latitude = existing.latitude
            new_record.longitude = existing.longitude
            new_record.geocoded_address = existing.geocoded_address
            new_record.geocode_source = existing.geocode_source
            changed, merged = existing.apply_new_scrape(new_record, when_iso=now_iso)
            if changed:
                store.append_history(permit_number, existing)
                current_records[permit_number] = merged
                permits_changed += 1
            else:
                current_records[permit_number] = merged
                permits_unchanged += 1

        return True, {
            "target_issued_date": target_issued_date,
            "permits_found": permits_found,
            "permits_new": permits_new,
            "permits_changed": permits_changed,
            "permits_unchanged": permits_unchanged,
        }, []

    except Exception as exc:
        errors.append(str(exc))
        return False, {
            "target_issued_date": target_issued_date,
            "permits_found": permits_found,
            "permits_new": permits_new,
            "permits_changed": permits_changed,
            "permits_unchanged": permits_unchanged,
        }, errors


def enrich_records_with_geocoding(
    geocoder: JsonGeocoder,
    current_records: Dict[str, PermitRecord],
    store: JsonPermitStore,
) -> tuple[int, List[str]]:
    errors: List[str] = []
    geocoded_changed = 0
    now_iso = utc_now_iso()

    for permit_number, record in current_records.items():
        if not geocoder.needs_geocoding(record):
            continue

        previous = PermitRecord.from_dict(record.to_dict())
        try:
            geocoded = geocoder.geocode_record(record)
        except Exception as exc:
            errors.append(f"{permit_number}: {exc}")
            continue

        geocoded.last_seen_at = now_iso
        new_hash = geocoded.compute_data_hash()
        if new_hash != previous.data_hash:
            store.append_history(permit_number, previous)
            geocoded.last_changed_at = now_iso
            geocoded.data_hash = new_hash
            current_records[permit_number] = geocoded
            geocoded_changed += 1
        else:
            geocoded.data_hash = previous.data_hash
            current_records[permit_number] = geocoded

    return geocoded_changed, errors


def main() -> int:
    store = JsonPermitStore()
    state = store.load_state()
    current_records = store.load_current()

    target_dates = resolve_target_dates(state)
    if not target_dates:
        print("SKIPPED no target dates to process")
        return 0

    client = ETrakitClient()
    geocoder = JsonGeocoder()

    try:
        client.login()
    except Exception as exc:
        print("FAILED")
        print(f"- login failed: {exc}")
        return 1

    overall_success = True
    last_summary = {}
    all_errors: List[str] = []

    for target_issued_date in target_dates:
        run_state = init_run_state(state, target_issued_date)
        store.save_state(run_state)

        success, summary, errors = sync_one_date(
            client=client,
            current_records=current_records,
            target_issued_date=target_issued_date,
            store=store,
        )

        store.save_current(current_records)

        geocoded_changed = 0
        geocode_errors: List[str] = []
        if success:
            geocoded_changed, geocode_errors = enrich_records_with_geocoding(
                geocoder=geocoder,
                current_records=current_records,
                store=store,
            )
            store.save_current(current_records)

        combined_errors = errors + geocode_errors

        state = finalize_run_state(
            state,
            success=success,
            target_issued_date=target_issued_date,
            permits_found=summary["permits_found"],
            permits_new=summary["permits_new"],
            permits_changed=summary["permits_changed"],
            permits_unchanged=summary["permits_unchanged"],
            errors=combined_errors,
        )

        # optional per-date status tracking
        state.setdefault("date_status", {})
        state["date_status"][target_issued_date] = {
            "status": "success" if success else "failed",
            "completed_at": utc_now_iso() if success else "",
            "last_attempt_at": utc_now_iso(),
            "permits_found": summary["permits_found"],
            "permits_new": summary["permits_new"],
            "permits_changed": summary["permits_changed"],
            "permits_unchanged": summary["permits_unchanged"],
            "geocoded_changed": geocoded_changed,
            "errors": combined_errors,
        }
        store.save_state(state)

        last_summary = summary
        all_errors.extend(combined_errors)

        if success:
            print(
                f"SUCCESS target={target_issued_date} found={summary['permits_found']} "
                f"new={summary['permits_new']} changed={summary['permits_changed']} "
                f"unchanged={summary['permits_unchanged']} geocoded_changed={geocoded_changed}"
            )
        else:
            overall_success = False
            print(f"FAILED target={target_issued_date}")
            for err in combined_errors:
                print(f"- {err}")

    return 0 if overall_success else 1


if __name__ == "__main__":
    sys.exit(main())
