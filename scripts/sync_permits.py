from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
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

        state = finalize_run_state(
            state,
            success=success,
            target_issued_date=target_issued_date,
            permits_found=summary["permits_found"],
            permits_new=summary["permits_new"],
            permits_changed=summary["permits_changed"],
            permits_unchanged=summary["permits_unchanged"],
            errors=errors,
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
            "errors": errors,
        }

        store.save_current(current_records)
        store.save_state(state)

        last_summary = summary
        all_errors.extend(errors)

        if success:
            print(
                f"SUCCESS target={target_issued_date} found={summary['permits_found']} "
                f"new={summary['permits_new']} changed={summary['permits_changed']} "
                f"unchanged={summary['permits_unchanged']}"
            )
        else:
            overall_success = False
            print(f"FAILED target={target_issued_date}")
            for err in errors:
                print(f"- {err}")

    return 0 if overall_success else 1


if __name__ == "__main__":
    sys.exit(main())
