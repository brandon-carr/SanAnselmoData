from __future__ import annotations

import json
import sys
from pathlib import Path

from etrakit_client import ETrakitClient
from permit_model import InspectionRecord, PermitRecord, utc_now_iso
from sequential_scraper import load_stream_config, sync_address_from_record
from storage import JsonPermitStore, hydrate_permits_from_addresses


BATCH_SIZE = 100
STATE_PATH = Path("data/building_inspection_backfill_state.json")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "processed_permit_numbers": [],
            "failed_permit_numbers": [],
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "",
            "last_batch": [],
            "errors": [],
        }

    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "processed_permit_numbers": [],
            "failed_permit_numbers": [],
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "",
            "last_batch": [],
            "errors": [],
        }

    payload.setdefault("processed_permit_numbers", [])
    payload.setdefault("failed_permit_numbers", [])
    payload.setdefault("last_run_started_at", "")
    payload.setdefault("last_run_finished_at", "")
    payload.setdefault("last_run_status", "")
    payload.setdefault("last_batch", [])
    payload.setdefault("errors", [])
    return payload


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(STATE_PATH)


def main() -> int:
    dataset_name = "building"
    config = load_stream_config(dataset_name)
    store = JsonPermitStore()
    permit_records = store.load_permits(dataset_name)
    address_records = store.load_addresses()
    state = load_state()

    if not permit_records:
        print("FAILED")
        print("- no building permits found in data/building_permits.json")
        return 1

    processed = {str(item).strip().upper() for item in state.get("processed_permit_numbers", []) if str(item).strip()}
    failed = {str(item).strip().upper() for item in state.get("failed_permit_numbers", []) if str(item).strip()}
    remaining_permit_numbers = [
        permit_number
        for permit_number in sorted(permit_records.keys())
        if permit_number.upper() not in processed and permit_number.upper() not in failed
    ]
    permit_numbers = remaining_permit_numbers[:BATCH_SIZE]

    if not permit_numbers:
        print("OK")
        print("- no unprocessed building permits remain for inspection backfill")
        return 0

    state["last_run_started_at"] = utc_now_iso()
    state["last_run_status"] = "running"
    state["last_batch"] = permit_numbers
    state["errors"] = []
    save_state(state)

    client = ETrakitClient()
    try:
        client.login()
    except Exception as exc:
        state["last_run_finished_at"] = utc_now_iso()
        state["last_run_status"] = "failed"
        state["errors"] = [f"login failed: {exc}"]
        save_state(state)
        print("FAILED")
        print(f"- login failed: {exc}")
        return 1

    new_count = 0
    changed_count = 0
    unchanged_count = 0
    error_count = 0
    run_errors = []

    for permit_number in permit_numbers:
        try:
            fields = client.fetch_activity_details_by_number(
                config.detail_base_url,
                permit_number,
                record_number_labels=[config.record_number_label],
                type_labels=[config.type_label],
                address_labels=[config.address_label],
                status_labels=[config.status_label],
            )
            if not fields:
                error_count += 1
                failed.add(permit_number.upper())
                run_errors.append(f"{permit_number}: permit detail not found")
                print(f"FAILED {permit_number}: permit detail not found")
                continue

            enriched_inspections = []
            for inspection in fields.get("inspections") or []:
                inspection_payload = dict(inspection)
                record_id = str(inspection_payload.get("record_id") or "").strip()
                if not record_id:
                    continue

                try:
                    detail_fields = client.fetch_inspection_detail(
                        permit_number=permit_number,
                        record_id=record_id,
                    )
                    inspection_payload.update({k: v for k, v in detail_fields.items() if v})
                except Exception as exc:
                    print(
                        f"WARN {permit_number} inspection {record_id}: "
                        f"detail fetch failed: {exc}"
                    )

                inspection_payload["parent_permit_number"] = permit_number
                enriched_inspections.append(
                    InspectionRecord.from_fields(inspection_payload).to_dict()
                )

            fields["inspections"] = enriched_inspections
            fields["record_type"] = dataset_name
            record = PermitRecord.from_scraped_fields(fields)
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
            processed.add(permit_number.upper())

            if is_new:
                new_count += 1
            elif changed:
                changed_count += 1
            else:
                unchanged_count += 1
        except Exception as exc:
            error_count += 1
            failed.add(permit_number.upper())
            run_errors.append(f"{permit_number}: {exc}")
            print(f"FAILED {permit_number}: {exc}")

    hydrate_permits_from_addresses(permit_records, address_records)
    store.save_addresses(address_records)
    store.save_permits(dataset_name, permit_records)
    store.save_all_permits_view(store.load_all_permits())

    state["processed_permit_numbers"] = sorted(processed)
    state["failed_permit_numbers"] = sorted(failed)
    state["last_run_finished_at"] = utc_now_iso()
    state["last_run_status"] = "success" if error_count == 0 else "partial"
    state["errors"] = run_errors
    save_state(state)

    print("OK")
    print(
        f"- processed {len(permit_numbers)} stored building permits for inspection backfill: "
        f"new={new_count} changed={changed_count} unchanged={unchanged_count} errors={error_count}"
    )
    print(
        f"- remaining unprocessed building permits: "
        f"{max(len(remaining_permit_numbers) - len(permit_numbers), 0)}"
    )
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
