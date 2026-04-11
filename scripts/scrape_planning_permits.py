from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from etrakit_client import ETrakitClient
from permit_model import AddressRecord, PermitRecord, build_address_id, utc_now_iso
from storage import JsonPermitStore, finalize_run_state, hydrate_permits_from_addresses, init_run_state


DATASET_NAME = "planning"


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
    today_utc = datetime.now(timezone.utc).date()
    yesterday = today_utc - timedelta(days=1)

    if manual_date:
        try:
            manual_dt = datetime.strptime(manual_date, "%Y-%m-%d").date()
        except ValueError:
            return []
        if manual_dt > today_utc:
            return []
        return [manual_dt.isoformat()]

    completed = set(state.get("completed_issued_dates", []))
    completed_dates = sorted(
        datetime.strptime(d, "%Y-%m-%d").date()
        for d in completed
        if d
    )
    backfill_anchor = completed_dates[0] if completed_dates else yesterday

    targets: List[str] = []
    seen = set()

    def add_target(d) -> None:
        if d > today_utc:
            return
        d_str = d.isoformat()
        if d_str in seen:
            return
        if not force_rerun and d_str in completed:
            return
        seen.add(d_str)
        targets.append(d_str)

    add_target(yesterday)

    for offset in range(1, days_back + 1):
        add_target(backfill_anchor - timedelta(days=offset))

    return targets


def normalize_mmddyyyy_to_iso(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def scrape_one_date(
    client: ETrakitClient,
    permit_records: Dict[str, PermitRecord],
    address_records: Dict[str, AddressRecord],
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
                fields["permit_source"] = DATASET_NAME
                record = PermitRecord.from_scraped_fields(fields)
                issued_date_iso = normalize_mmddyyyy_to_iso(record.issued_date)
                if issued_date_iso != target_issued_date:
                    continue
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
            new_record.address_id = build_address_id(new_record.address, new_record.city_state_zip)

            if permit_number not in permit_records:
                address_record = AddressRecord.from_permit(new_record, when_iso=now_iso)
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
                    _, address_record = existing_address.apply_new_source(address_record, when_iso=now_iso)
                else:
                    address_record.first_seen_at = now_iso
                    address_record.last_seen_at = now_iso
                    address_record.last_changed_at = now_iso
                    address_record.data_hash = address_record.compute_data_hash()
                address_records[address_record.address_id] = address_record
                new_record.latitude = address_record.latitude
                new_record.longitude = address_record.longitude
                new_record.geocoded_address = address_record.geocoded_address
                new_record.geocode_source = address_record.geocode_source
                new_record.geocode_status = address_record.geocode_status
                new_record.geocode_error = address_record.geocode_error
                new_record.geocode_attempts = address_record.geocode_attempts
                new_record.geocode_last_attempt_at = address_record.geocode_last_attempt_at
                new_record.first_seen_at = now_iso
                new_record.last_seen_at = now_iso
                new_record.last_changed_at = now_iso
                new_record.data_hash = new_record.compute_data_hash()
                permit_records[permit_number] = new_record
                permits_new += 1
                continue

            existing = permit_records[permit_number]
            address_changed = (
                existing.address != new_record.address
                or existing.city_state_zip != new_record.city_state_zip
            )
            previous_address = address_records.get(existing.address_id)
            if address_changed:
                new_record.reset_geocode_fields()
            else:
                if previous_address:
                    new_record.latitude = previous_address.latitude
                    new_record.longitude = previous_address.longitude
                    new_record.geocoded_address = previous_address.geocoded_address
                    new_record.geocode_source = previous_address.geocode_source
                    new_record.geocode_status = previous_address.geocode_status
                    new_record.geocode_error = previous_address.geocode_error
                    new_record.geocode_attempts = previous_address.geocode_attempts
                    new_record.geocode_last_attempt_at = previous_address.geocode_last_attempt_at
                else:
                    new_record.copy_geocode_fields_from(existing)

            next_address = AddressRecord.from_permit(new_record, when_iso=now_iso)
            current_address = address_records.get(next_address.address_id)
            if current_address:
                if current_address.data_hash != next_address.compute_data_hash():
                    store.append_address_history(next_address.address_id, current_address)
                    _, next_address = current_address.apply_new_source(next_address, when_iso=now_iso)
                else:
                    current_address.last_seen_at = now_iso
                    next_address = current_address
            else:
                next_address.first_seen_at = now_iso
                next_address.last_seen_at = now_iso
                next_address.last_changed_at = now_iso
                next_address.data_hash = next_address.compute_data_hash()
            address_records[next_address.address_id] = next_address

            new_record.latitude = next_address.latitude
            new_record.longitude = next_address.longitude
            new_record.geocoded_address = next_address.geocoded_address
            new_record.geocode_source = next_address.geocode_source
            new_record.geocode_status = next_address.geocode_status
            new_record.geocode_error = next_address.geocode_error
            new_record.geocode_attempts = next_address.geocode_attempts
            new_record.geocode_last_attempt_at = next_address.geocode_last_attempt_at
            changed, merged = existing.apply_new_scrape(new_record, when_iso=now_iso)
            if changed:
                store.append_permit_history(DATASET_NAME, permit_number, existing)
                permit_records[permit_number] = merged
                permits_changed += 1
            else:
                permit_records[permit_number] = merged
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


def main() -> int:
    store = JsonPermitStore()
    state = store.load_scrape_state(DATASET_NAME)
    permit_records = store.load_permits(DATASET_NAME)
    address_records = store.load_addresses()

    target_dates = resolve_target_dates(state)
    if not target_dates:
        print("SKIPPED no target dates to process")
        return 0

    print(f"TARGETS {', '.join(target_dates)}")

    client = ETrakitClient()

    try:
        client.login()
    except Exception as exc:
        print("FAILED")
        print(f"- login failed: {exc}")
        return 1

    overall_success = True

    for target_issued_date in target_dates:
        run_state = init_run_state(state, target_issued_date)
        store.save_scrape_state(DATASET_NAME, run_state)

        success, summary, errors = scrape_one_date(
            client=client,
            permit_records=permit_records,
            address_records=address_records,
            target_issued_date=target_issued_date,
            store=store,
        )

        hydrate_permits_from_addresses(permit_records, address_records)
        store.save_addresses(address_records)
        store.save_permits(DATASET_NAME, permit_records)
        store.save_all_permits_view(store.load_all_permits())

        geocoded_changed = 0
        combined_errors = errors

        state = finalize_run_state(
            run_state,
            success=success,
            target_issued_date=target_issued_date,
            permits_found=summary["permits_found"],
            permits_new=summary["permits_new"],
            permits_changed=summary["permits_changed"],
            permits_unchanged=summary["permits_unchanged"],
            errors=combined_errors,
        )

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
        store.save_scrape_state(DATASET_NAME, state)

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
