from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from permit_model import AddressRecord, PermitRecord, apply_address_to_permit, utc_now_iso


@dataclass(frozen=True)
class PermitDatasetConfig:
    name: str
    current_path: str
    history_path: str
    state_path: str


DEFAULT_PERMIT_DATASETS: Dict[str, PermitDatasetConfig] = {
    "planning": PermitDatasetConfig(
        name="planning",
        current_path="data/planning_permits.json",
        history_path="data/planning_permit_history.json",
        state_path="data/planning_scrape_state.json",
    ),
    "building": PermitDatasetConfig(
        name="building",
        current_path="data/building_permits.json",
        history_path="data/building_permit_history.json",
        state_path="data/building_scrape_state.json",
    ),
    "violations": PermitDatasetConfig(
        name="violations",
        current_path="data/violations_permits.json",
        history_path="data/violations_permit_history.json",
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
    ) -> None:
        self.dataset_configs = dataset_configs or DEFAULT_PERMIT_DATASETS
        self.addresses_path = Path(addresses_path)
        self.address_history_path = Path(address_history_path)
        self.all_permits_view_path = Path(all_permits_view_path)

        self.addresses_path.parent.mkdir(parents=True, exist_ok=True)
        self.address_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.all_permits_view_path.parent.mkdir(parents=True, exist_ok=True)

        for config in self.dataset_configs.values():
            Path(config.current_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.history_path).parent.mkdir(parents=True, exist_ok=True)
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
        history_path = Path(self._config(dataset_name).history_path)
        history = self._read_json(history_path, {})
        history.setdefault(permit_number, [])
        history[permit_number].append(old_record.to_dict())
        self._write_json(history_path, history)

    def load_scrape_state(self, dataset_name: str) -> dict:
        config = self._config(dataset_name)
        default = {
            "completed_issued_dates": [],
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "",
            "last_target_issued_date": "",
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

    def save_all_permits_view(self, permit_groups: Dict[str, Dict[str, PermitRecord]]) -> None:
        payload = []
        for dataset_name in self.dataset_names():
            records = permit_groups.get(dataset_name, {})
            for permit in records.values():
                item = permit.to_dict()
                if not item.get("permit_source"):
                    item["permit_source"] = dataset_name
                payload.append(item)

        payload.sort(
            key=lambda item: (
                str(item.get("issued_date") or ""),
                str(item.get("permit_source") or ""),
                str(item.get("permit_number") or ""),
            ),
            reverse=True,
        )
        self._write_json(self.all_permits_view_path, payload)

    def load_addresses(self) -> Dict[str, AddressRecord]:
        payload = self._read_json(self.addresses_path, {})
        return {k: AddressRecord.from_dict(v) for k, v in payload.items()}

    def save_addresses(self, records: Dict[str, AddressRecord]) -> None:
        payload = {k: v.to_dict() for k, v in records.items()}
        self._write_json(self.addresses_path, payload)

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


def init_run_state(state: dict, target_issued_date: str) -> dict:
    state["last_run_started_at"] = utc_now_iso()
    state["last_run_finished_at"] = ""
    state["last_run_status"] = "running"
    state["last_target_issued_date"] = target_issued_date
    state["last_summary"] = {
        "target_issued_date": target_issued_date,
        "permits_found": 0,
        "permits_new": 0,
        "permits_changed": 0,
        "permits_unchanged": 0,
    }
    state["errors"] = []
    return state


def finalize_run_state(
    state: dict,
    *,
    success: bool,
    target_issued_date: str,
    permits_found: int,
    permits_new: int,
    permits_changed: int,
    permits_unchanged: int,
    errors: List[str],
) -> dict:
    state["last_run_finished_at"] = utc_now_iso()
    state["last_run_status"] = "success" if success else "failed"
    state["last_target_issued_date"] = target_issued_date
    state["last_summary"] = {
        "target_issued_date": target_issued_date,
        "permits_found": permits_found,
        "permits_new": permits_new,
        "permits_changed": permits_changed,
        "permits_unchanged": permits_unchanged,
    }
    state["errors"] = errors
    if success and target_issued_date not in state.get("completed_issued_dates", []):
        state.setdefault("completed_issued_dates", [])
        state["completed_issued_dates"].append(target_issued_date)
        state["completed_issued_dates"] = sorted(set(state["completed_issued_dates"]))
    return state
