from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_str(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


@dataclass
class PermitRecord:
    permit_number: str

    status: str = ""
    permit_type: str = ""
    subtype: str = ""
    short_description: str = ""

    address: str = ""
    city_state_zip: str = ""
    apn: str = ""
    property_type: str = ""
    lot_size_sf: str = ""

    applied_date: str = ""
    approved_date: str = ""
    issued_date: str = ""
    finaled_date: str = ""
    expiration_date: str = ""

    source_url: str = ""

    first_seen_at: str = ""
    last_seen_at: str = ""
    last_changed_at: str = ""
    data_hash: str = ""

    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_scraped_fields(cls, fields: Dict[str, Any]) -> "PermitRecord":
        permit_number = normalize_str(fields.get("permit_number"))
        if not permit_number:
            raise ValueError("permit_number is required")

        record = cls(
            permit_number=permit_number,
            status=normalize_str(fields.get("status")),
            permit_type=normalize_str(fields.get("permit_type")),
            subtype=normalize_str(fields.get("subtype")),
            short_description=normalize_str(fields.get("short_description")),
            address=normalize_str(fields.get("address")),
            city_state_zip=normalize_str(fields.get("city_state_zip")),
            apn=normalize_str(fields.get("apn")),
            property_type=normalize_str(fields.get("property_type")),
            lot_size_sf=normalize_str(fields.get("lot_size_sf")),
            applied_date=normalize_str(fields.get("applied_date")),
            approved_date=normalize_str(fields.get("approved_date")),
            issued_date=normalize_str(fields.get("issued_date")),
            finaled_date=normalize_str(fields.get("finaled_date")),
            expiration_date=normalize_str(fields.get("expiration_date")),
            source_url=normalize_str(fields.get("source_url")),
            extra=fields.get("extra", {}) or {},
        )

        now = utc_now_iso()
        record.first_seen_at = normalize_str(fields.get("first_seen_at")) or now
        record.last_seen_at = normalize_str(fields.get("last_seen_at")) or now
        record.last_changed_at = normalize_str(fields.get("last_changed_at")) or now
        record.data_hash = record.compute_data_hash()
        return record

    def compute_data_hash(self) -> str:
        meaningful = {
            "permit_number": self.permit_number,
            "status": self.status,
            "permit_type": self.permit_type,
            "subtype": self.subtype,
            "short_description": self.short_description,
            "address": self.address,
            "city_state_zip": self.city_state_zip,
            "apn": self.apn,
            "property_type": self.property_type,
            "lot_size_sf": self.lot_size_sf,
            "applied_date": self.applied_date,
            "approved_date": self.approved_date,
            "issued_date": self.issued_date,
            "finaled_date": self.finaled_date,
            "expiration_date": self.expiration_date,
            "source_url": self.source_url,
            "extra": self.extra,
        }
        return sha256(repr(sorted(meaningful.items())).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PermitRecord":
        record = cls(**payload)
        return record

    def with_seen_timestamp(self, when_iso: Optional[str] = None) -> "PermitRecord":
        when_iso = when_iso or utc_now_iso()
        self.last_seen_at = when_iso
        return self

    def apply_new_scrape(self, new_record: "PermitRecord", when_iso: Optional[str] = None) -> tuple[bool, "PermitRecord"]:
        when_iso = when_iso or utc_now_iso()
        old_hash = self.data_hash
        new_hash = new_record.compute_data_hash()

        if old_hash == new_hash:
            self.last_seen_at = when_iso
            return False, self

        new_record.first_seen_at = self.first_seen_at or when_iso
        new_record.last_seen_at = when_iso
        new_record.last_changed_at = when_iso
        new_record.data_hash = new_hash
        return True, new_record
