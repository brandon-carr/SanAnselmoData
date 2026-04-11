from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from etrakit_client import ETrakitClient, ETrakitError
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
    completed_dates = sorted(
        datetime.strptime(d, "%Y-%m-%d").date()
        for d in completed
        if d
    )
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    backfill_anchor = completed_dates[0] if completed_dates else yesterday

    targets: List[str] = []
    seen = set()

    def add_target(d) -> None:
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
            address_changed = (
                existing.address != new_record.address
                or existing.city_state_zip != new_record.city_state_zip
            )
            if address_changed:
                new_record.reset_geocode_fields()
            else:
                new_record.copy_geocode_fields_from(existing)
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


def main() -> int:
    store = JsonPermitStore()
    state = store.load_state()
    current_records = store.load_current()

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
        combined_errors = errors

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
