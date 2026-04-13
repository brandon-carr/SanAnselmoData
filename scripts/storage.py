from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from permit_model import AddressRecord, PermitRecord, apply_address_to_permit


@dataclass(frozen=True)
class PermitDatasetConfig:
    name: str
    current_path: str
    history_path: str
    published_history_path: str
    state_path: str


DEFAULT_PERMIT_DATASETS: Dict[str, PermitDatasetConfig] = {
    "planning": PermitDatasetConfig(
        name="planning",
        current_path="data/planning_permits.json",
        history_path="data/planning_permit_history.json",
        published_history_path="web/data/planning_permit_history.json",
        state_path="data/planning_scrape_state.json",
    ),
    "building": PermitDatasetConfig(
        name="building",
        current_path="data/building_permits.json",
        history_path="data/building_permit_history.json",
        published_history_path="web/data/building_permit_history.json",
        state_path="data/building_scrape_state.json",
    ),
    "violations": PermitDatasetConfig(
        name="violations",
        current_path="data/violations_permits.json",
        history_path="data/violations_permit_history.json",
        published_history_path="web/data/violations_permit_history.json",
        state_path="data/violations_scrape_state.json",
    ),
}


class JsonPermitStore:
    def __init__(
        self,
        *,
        dataset_configs: Dict[str, PermitDatasetConfig] | None = None,
        addresses_path: str = "data/addresses.json",
        address_history_path: str = "data/address_history.json",
        all_permits_view_path: str = "data/all_permits_view.json",
        permits_view_path: str = "data/permits_view.json",
        violations_view_path: str = "data/violations_view.json",
        inspections_view_path: str = "data/inspections_view.json",
        published_addresses_path: str = "web/data/addresses.json",
        published_all_permits_view_path: str = "web/data/all_permits_view.json",
        published_permits_view_path: str = "web/data/permits_view.json",
        published_violations_view_path: str = "web/data/violations_view.json",
        published_inspections_view_path: str = "web/data/inspections_view.json",
        published_site_config_path: str = "web/data/site_config.json",
    ) -> None:
        self.dataset_configs = dataset_configs or DEFAULT_PERMIT_DATASETS
        self.addresses_path = Path(addresses_path)
        self.address_history_path = Path(address_history_path)
        self.all_permits_view_path = Path(all_permits_view_path)
        self.permits_view_path = Path(permits_view_path)
        self.violations_view_path = Path(violations_view_path)
        self.inspections_view_path = Path(inspections_view_path)
        self.published_addresses_path = Path(published_addresses_path)
        self.published_all_permits_view_path = Path(published_all_permits_view_path)
        self.published_permits_view_path = Path(published_permits_view_path)
        self.published_violations_view_path = Path(published_violations_view_path)
        self.published_inspections_view_path = Path(published_inspections_view_path)
        self.published_site_config_path = Path(published_site_config_path)

        self.addresses_path.parent.mkdir(parents=True, exist_ok=True)
        self.address_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.all_permits_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.permits_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.violations_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.inspections_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_addresses_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_all_permits_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_permits_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_violations_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_inspections_view_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_site_config_path.parent.mkdir(parents=True, exist_ok=True)

        for config in self.dataset_configs.values():
            Path(config.current_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.history_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.published_history_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.state_path).parent.mkdir(parents=True, exist_ok=True)

    def _config(self, dataset_name: str) -> PermitDatasetConfig:
        if dataset_name not in self.dataset_configs:
            raise KeyError(f"Unknown permit dataset: {dataset_name}")
        return self.dataset_configs[dataset_name]

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default

        try:
            with path.open("r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return default
                return json.loads(content)
        except Exception:
            return default

    def _write_json(self, path: Path, payload) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        tmp.replace(path)

    def dataset_names(self) -> List[str]:
        return list(self.dataset_configs.keys())

    def load_permits(self, dataset_name: str) -> Dict[str, PermitRecord]:
        config = self._config(dataset_name)
        payload = self._read_json(Path(config.current_path), {})
        return {k: PermitRecord.from_dict(v) for k, v in payload.items()}

    def save_permits(self, dataset_name: str, records: Dict[str, PermitRecord]) -> None:
        payload = {k: v.to_dict() for k, v in records.items()}
        self._write_json(Path(self._config(dataset_name).current_path), payload)

    def append_permit_history(self, dataset_name: str, permit_number: str, old_record: PermitRecord) -> None:
        config = self._config(dataset_name)
        history_path = Path(config.history_path)
        history = self._read_json(history_path, {})
        history.setdefault(permit_number, [])
        history[permit_number].append(old_record.to_dict())
        self._write_json(history_path, history)
        self._write_json(Path(config.published_history_path), history)

    def load_scrape_state(self, dataset_name: str) -> dict:
        config = self._config(dataset_name)
        default = {
            "year": 0,
            "batch_size": 0,
            "streams": {},
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "",
            "last_summary": {},
            "errors": [],
        }
        return self._read_json(Path(config.state_path), default)

    def save_scrape_state(self, dataset_name: str, state: dict) -> None:
        self._write_json(Path(self._config(dataset_name).state_path), state)

    def load_all_permits(self, dataset_names: Iterable[str] | None = None) -> Dict[str, Dict[str, PermitRecord]]:
        names = list(dataset_names) if dataset_names is not None else self.dataset_names()
        return {name: self.load_permits(name) for name in names}

    def save_all_permits(self, permit_groups: Dict[str, Dict[str, PermitRecord]]) -> None:
        for dataset_name, records in permit_groups.items():
            self.save_permits(dataset_name, records)

    def _flatten_permit_groups(self, permit_groups: Dict[str, Dict[str, PermitRecord]]) -> List[dict]:
        payload: List[dict] = []
        for dataset_name in self.dataset_names():
            records = permit_groups.get(dataset_name, {})
            for permit in records.values():
                item = permit.to_dict()
                if not item.get("record_type"):
                    item["record_type"] = dataset_name
                item["record_id"] = f"{item['record_type']}:{item.get('permit_number', '')}"
                payload.append(item)

                if dataset_name != "building":
                    continue

                for inspection in permit.inspections:
                    inspection_item = {
                        "record_id": f"inspection:{inspection.record_id}",
                        "record_type": "inspection",
                        "parent_record_type": dataset_name,
                        "parent_permit_number": permit.permit_number,
                        "permit_number": permit.permit_number,
                        "status": inspection.result or permit.status,
                        "permit_type": inspection.inspection_type,
                        "subtype": "",
                        "short_description": inspection.notes or inspection.remarks or permit.short_description,
                        "address": permit.address,
                        "city_state_zip": permit.city_state_zip,
                        "address_id": permit.address_id,
                        "apn": permit.apn,
                        "property_type": permit.property_type,
                        "lot_size_sf": permit.lot_size_sf,
                        "applied_date": permit.applied_date,
                        "approved_date": permit.approved_date,
                        "issued_date": permit.issued_date,
                        "finaled_date": permit.finaled_date,
                        "expiration_date": permit.expiration_date,
                        "source_url": permit.source_url,
                        "latitude": permit.latitude,
                        "longitude": permit.longitude,
                        "geocoded_address": permit.geocoded_address,
                        "geocode_source": permit.geocode_source,
                        "geocode_status": permit.geocode_status,
                        "geocode_error": permit.geocode_error,
                        "geocode_attempts": permit.geocode_attempts,
                        "geocode_last_attempt_at": permit.geocode_last_attempt_at,
                        "first_seen_at": permit.first_seen_at,
                        "last_seen_at": permit.last_seen_at,
                        "last_changed_at": permit.last_changed_at,
                        "inspection_record_id": inspection.record_id,
                        "inspection_type": inspection.inspection_type,
                        "result": inspection.result,
                        "scheduled_date": inspection.scheduled_date,
                        "scheduled_time": inspection.scheduled_time,
                        "completed_date": inspection.completed_date,
                        "completed_time": inspection.completed_time,
                        "inspector": inspection.inspector,
                        "remarks": inspection.remarks,
                        "notes": inspection.notes,
                        "extra": dict(inspection.extra or {}),
                    }
                    payload.append(inspection_item)

        payload.sort(
            key=lambda item: (
                str(item.get("scheduled_date") or item.get("completed_date") or item.get("issued_date") or ""),
                str(item.get("record_type") or ""),
                str(item.get("permit_number") or ""),
                str(item.get("inspection_record_id") or ""),
            ),
            reverse=True,
        )
        return payload

    def save_all_permits_view(self, permit_groups: Dict[str, Dict[str, PermitRecord]]) -> None:
        payload = self._flatten_permit_groups(permit_groups)

        self._write_json(self.all_permits_view_path, payload)
        self._write_json(self.published_all_permits_view_path, payload)
        self._write_json(
            self.permits_view_path,
            [item for item in payload if item.get("record_type") in {"building", "planning"}],
        )
        self._write_json(
            self.published_permits_view_path,
            [item for item in payload if item.get("record_type") in {"building", "planning"}],
        )
        self._write_json(
            self.violations_view_path,
            [item for item in payload if item.get("record_type") == "violations"],
        )
        self._write_json(
            self.published_violations_view_path,
            [item for item in payload if item.get("record_type") == "violations"],
        )
        self._write_json(
            self.inspections_view_path,
            [item for item in payload if item.get("record_type") == "inspection"],
        )
        self._write_json(
            self.published_inspections_view_path,
            [item for item in payload if item.get("record_type") == "inspection"],
        )

    def load_addresses(self) -> Dict[str, AddressRecord]:
        payload = self._read_json(self.addresses_path, {})
        return {k: AddressRecord.from_dict(v) for k, v in payload.items()}

    def save_addresses(self, records: Dict[str, AddressRecord]) -> None:
        payload = {k: v.to_dict() for k, v in records.items()}
        self._write_json(self.addresses_path, payload)
        self._write_json(self.published_addresses_path, payload)

    def save_site_config(self, payload: dict) -> None:
        self._write_json(self.published_site_config_path, payload)

    def append_address_history(self, address_id: str, old_record: AddressRecord) -> None:
        history = self._read_json(self.address_history_path, {})
        history.setdefault(address_id, [])
        history[address_id].append(old_record.to_dict())
        self._write_json(self.address_history_path, history)


def hydrate_permits_from_addresses(
    permits: Dict[str, PermitRecord],
    addresses: Dict[str, AddressRecord],
) -> None:
    for permit in permits.values():
        address_id = permit.address_id
        if not address_id:
            continue
        address = addresses.get(address_id)
        if not address:
            continue
        apply_address_to_permit(permit, address)


def hydrate_permit_groups_from_addresses(
    permit_groups: Dict[str, Dict[str, PermitRecord]],
    addresses: Dict[str, AddressRecord],
) -> None:
    for permits in permit_groups.values():
        hydrate_permits_from_addresses(permits, addresses)
