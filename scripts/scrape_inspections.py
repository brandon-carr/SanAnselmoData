from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

from etrakit_client import ETrakitClient
from permit_model import InspectionRecord, PermitRecord, utc_now_iso
from sequential_scraper import load_stream_config, sync_address_from_record
from storage import JsonPermitStore, hydrate_permits_from_addresses


CONFIG_PATH = Path(__file__).with_name("permit_scrapers.ini")


def load_existing_inspections_url() -> str:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    return parser.get(
        "inspections",
        "existing_inspections_url",
        fallback="https://sanan-trk.aspgov.com/eTRAKiT/ExistingInspections.aspx",
    ).strip()


def activity_prefix(activity_number: str) -> str:
    match = re.match(r"^([A-Z]+)", str(activity_number or "").upper())
    return match.group(1) if match else ""


def main() -> int:
    dataset_name = "building"
    config = load_stream_config(dataset_name)
    inspections_url = load_existing_inspections_url()
    if not inspections_url:
        print("FAILED")
        print("- inspections existing_inspections_url is missing")
        return 1

    store = JsonPermitStore()
    permit_records = store.load_permits(dataset_name)
    address_records = store.load_addresses()

    client = ETrakitClient()
    try:
        client.login()
    except Exception as exc:
        print("FAILED")
        print(f"- login failed: {exc}")
        return 1

    try:
        activity_numbers = client.fetch_existing_inspection_activity_numbers(inspections_url)
    except Exception as exc:
        print("FAILED")
        print(f"- existing inspections fetch failed: {exc}")
        return 1

    allowed_prefixes = {prefix.upper() for prefix in config.prefixes}
    target_numbers = [
        activity_number
        for activity_number in activity_numbers
        if activity_prefix(activity_number) in allowed_prefixes
    ]

    if not target_numbers:
        print("OK")
        print("- no building permits found on today's inspection page")
        return 0

    new_count = 0
    changed_count = 0
    unchanged_count = 0
    error_count = 0

    for activity_number in target_numbers:
        try:
            fields = client.fetch_activity_details_by_number(
                config.detail_base_url,
                activity_number,
                record_number_labels=[config.record_number_label],
                type_labels=[config.type_label],
                address_labels=[config.address_label],
                status_labels=[config.status_label],
            )
            if not fields:
                error_count += 1
                print(f"FAILED {activity_number}: permit detail not found")
                continue

            enriched_inspections = []
            for inspection in fields.get("inspections") or []:
                inspection_payload = dict(inspection)
                record_id = str(inspection_payload.get("record_id") or "").strip()
                if not record_id:
                    continue

                try:
                    detail_fields = client.fetch_inspection_detail(
                        permit_number=activity_number,
                        record_id=record_id,
                    )
                    inspection_payload.update({k: v for k, v in detail_fields.items() if v})
                except Exception as exc:
                    print(f"WARN {activity_number} inspection {record_id}: detail fetch failed: {exc}")

                inspection_payload["parent_permit_number"] = activity_number
                enriched_inspections.append(InspectionRecord.from_fields(inspection_payload).to_dict())

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

            if is_new:
                new_count += 1
            elif changed:
                changed_count += 1
            else:
                unchanged_count += 1
        except Exception as exc:
            error_count += 1
            print(f"FAILED {activity_number}: {exc}")

    hydrate_permits_from_addresses(permit_records, address_records)
    store.save_addresses(address_records)
    store.save_permits(dataset_name, permit_records)
    store.save_all_permits_view(store.load_all_permits())

    print("OK")
    print(
        f"- processed {len(target_numbers)} building permits from today's inspections: "
        f"new={new_count} changed={changed_count} unchanged={unchanged_count} errors={error_count}"
    )
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
