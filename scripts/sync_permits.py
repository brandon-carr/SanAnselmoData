from __future__ import annotations

import os
import sys
from typing import Dict, List

from etrakit_client import ETrakitClient, ETrakitError
from permit_model import PermitRecord, utc_now_iso
from storage import JsonPermitStore, finalize_run_state, init_run_state


def getenv_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    target_issued_date = getenv_required("TARGET_ISSUED_DATE")
    store = JsonPermitStore()
    state = store.load_state()
    state = init_run_state(state, target_issued_date)
    store.save_state(state)

    current_records = store.load_current()
    client = ETrakitClient()
    errors: List[str] = []

    permits_found = 0
    permits_new = 0
    permits_changed = 0
    permits_unchanged = 0

    try:
        client.login()
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
            raise RuntimeError(
                "One or more permit detail pages failed; date will not be marked complete."
            )

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

        store.save_current(current_records)

        state = finalize_run_state(
            state,
            success=True,
            target_issued_date=target_issued_date,
            permits_found=permits_found,
            permits_new=permits_new,
            permits_changed=permits_changed,
            permits_unchanged=permits_unchanged,
            errors=[],
        )
        store.save_state(state)

        print(
            f"SUCCESS target={target_issued_date} found={permits_found} "
            f"new={permits_new} changed={permits_changed} unchanged={permits_unchanged}"
        )
        return 0

    except (ETrakitError, RuntimeError, Exception) as exc:
        errors.append(str(exc))
        state = finalize_run_state(
            state,
            success=False,
            target_issued_date=target_issued_date,
            permits_found=permits_found,
            permits_new=permits_new,
            permits_changed=permits_changed,
            permits_unchanged=permits_unchanged,
            errors=errors,
        )
        store.save_state(state)
        print("FAILED")
        for err in errors:
            print(f"- {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
