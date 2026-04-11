from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Protocol

from permit_model import AddressRecord, PermitRecord, apply_address_to_permit, utc_now_iso


class PermitStore(Protocol):
    def load_current(self) -> Dict[str, PermitRecord]:
        ...

    def save_current(self, records: Dict[str, PermitRecord]) -> None:
        ...

    def append_history(self, permit_number: str, old_record: PermitRecord) -> None:
        ...

    def load_state(self) -> dict:
        ...

    def save_state(self, state: dict) -> None:
        ...

    def load_addresses(self) -> Dict[str, AddressRecord]:
        ...

    def save_addresses(self, records: Dict[str, AddressRecord]) -> None:
        ...

    def append_address_history(self, address_id: str, old_record: AddressRecord) -> None:
        ...


class JsonPermitStore:
    def __init__(
        self,
        current_path: str = "data/current_permits.json",
        history_path: str = "data/permit_history.json",
        state_path: str = "data/sync_state.json",
        addresses_path: str = "data/addresses.json",
        address_history_path: str = "data/address_history.json",
    ) -> None:
        self.current_path = Path(current_path)
        self.history_path = Path(history_path)
        self.state_path = Path(state_path)
        self.addresses_path = Path(addresses_path)
        self.address_history_path = Path(address_history_path)

        self.current_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.addresses_path.parent.mkdir(parents=True, exist_ok=True)
        self.address_history_path.parent.mkdir(parents=True, exist_ok=True)

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

    def load_current(self) -> Dict[str, PermitRecord]:
        payload = self._read_json(self.current_path, {})
        return {k: PermitRecord.from_dict(v) for k, v in payload.items()}

    def save_current(self, records: Dict[str, PermitRecord]) -> None:
        payload = {k: v.to_dict() for k, v in records.items()}
        self._write_json(self.current_path, payload)

    def load_addresses(self) -> Dict[str, AddressRecord]:
        payload = self._read_json(self.addresses_path, {})
        return {k: AddressRecord.from_dict(v) for k, v in payload.items()}

    def save_addresses(self, records: Dict[str, AddressRecord]) -> None:
        payload = {k: v.to_dict() for k, v in records.items()}
        self._write_json(self.addresses_path, payload)

    def append_history(self, permit_number: str, old_record: PermitRecord) -> None:
        history = self._read_json(self.history_path, {})
        history.setdefault(permit_number, [])
        history[permit_number].append(old_record.to_dict())
        self._write_json(self.history_path, history)

    def append_address_history(self, address_id: str, old_record: AddressRecord) -> None:
        history = self._read_json(self.address_history_path, {})
        history.setdefault(address_id, [])
        history[address_id].append(old_record.to_dict())
        self._write_json(self.address_history_path, history)

    def load_state(self) -> dict:
        default = {
            "completed_issued_dates": [],
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "",
            "last_target_issued_date": "",
            "last_summary": {},
            "errors": [],
        }
        return self._read_json(self.state_path, default)

    def save_state(self, state: dict) -> None:
        self._write_json(self.state_path, state)


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
    if success and target_issued_date not in state["completed_issued_dates"]:
        state["completed_issued_dates"].append(target_issued_date)
        state["completed_issued_dates"] = sorted(set(state["completed_issued_dates"]))
    return state
