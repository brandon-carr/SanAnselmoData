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


def build_address_id(address: Any, city_state_zip: Any) -> str:
    canonical = {
        "address": normalize_str(address).lower(),
        "city_state_zip": normalize_str(city_state_zip).lower(),
    }
    return sha256(repr(sorted(canonical.items())).encode("utf-8")).hexdigest()[:16]


@dataclass
class PermitRecord:
    permit_number: str
    permit_source: str = ""

    status: str = ""
    permit_type: str = ""
    subtype: str = ""
    short_description: str = ""

    address: str = ""
    city_state_zip: str = ""
    address_id: str = ""
    apn: str = ""
    property_type: str = ""
    lot_size_sf: str = ""

    applied_date: str = ""
    approved_date: str = ""
    issued_date: str = ""
    finaled_date: str = ""
    expiration_date: str = ""

    source_url: str = ""
    latitude: str = ""
    longitude: str = ""
    geocoded_address: str = ""
    geocode_source: str = ""
    geocode_status: str = ""
    geocode_error: str = ""
    geocode_attempts: int = 0
    geocode_last_attempt_at: str = ""

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
            permit_source=normalize_str(fields.get("permit_source")),
            status=normalize_str(fields.get("status")),
            permit_type=normalize_str(fields.get("permit_type")),
            subtype=normalize_str(fields.get("subtype")),
            short_description=normalize_str(fields.get("short_description")),
            address=normalize_str(fields.get("address")),
            city_state_zip=normalize_str(fields.get("city_state_zip")),
            address_id=normalize_str(fields.get("address_id")),
            apn=normalize_str(fields.get("apn")),
            property_type=normalize_str(fields.get("property_type")),
            lot_size_sf=normalize_str(fields.get("lot_size_sf")),
            applied_date=normalize_str(fields.get("applied_date")),
            approved_date=normalize_str(fields.get("approved_date")),
            issued_date=normalize_str(fields.get("issued_date")),
            finaled_date=normalize_str(fields.get("finaled_date")),
            expiration_date=normalize_str(fields.get("expiration_date")),
            source_url=normalize_str(fields.get("source_url")),
            latitude=normalize_str(fields.get("latitude")),
            longitude=normalize_str(fields.get("longitude")),
            geocoded_address=normalize_str(fields.get("geocoded_address")),
            geocode_source=normalize_str(fields.get("geocode_source")),
            geocode_status=normalize_str(fields.get("geocode_status")),
            geocode_error=normalize_str(fields.get("geocode_error")),
            geocode_attempts=int(fields.get("geocode_attempts", 0) or 0),
            geocode_last_attempt_at=normalize_str(fields.get("geocode_last_attempt_at")),
            extra=fields.get("extra", {}) or {},
        )

        now = utc_now_iso()
        if not record.address_id:
            record.address_id = build_address_id(record.address, record.city_state_zip)
        record.first_seen_at = normalize_str(fields.get("first_seen_at")) or now
        record.last_seen_at = normalize_str(fields.get("last_seen_at")) or now
        record.last_changed_at = normalize_str(fields.get("last_changed_at")) or now
        record.data_hash = record.compute_data_hash()
        return record

    def compute_data_hash(self) -> str:
        meaningful = {
            "permit_number": self.permit_number,
            "permit_source": self.permit_source,
            "status": self.status,
            "permit_type": self.permit_type,
            "subtype": self.subtype,
            "short_description": self.short_description,
            "address": self.address,
            "city_state_zip": self.city_state_zip,
            "address_id": self.address_id,
            "apn": self.apn,
            "property_type": self.property_type,
            "lot_size_sf": self.lot_size_sf,
            "applied_date": self.applied_date,
            "approved_date": self.approved_date,
            "issued_date": self.issued_date,
            "finaled_date": self.finaled_date,
            "expiration_date": self.expiration_date,
            "source_url": self.source_url,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "geocoded_address": self.geocoded_address,
            "geocode_source": self.geocode_source,
            "geocode_status": self.geocode_status,
            "geocode_error": self.geocode_error,
            "geocode_attempts": self.geocode_attempts,
            "geocode_last_attempt_at": self.geocode_last_attempt_at,
            "extra": self.extra,
        }
        return sha256(repr(sorted(meaningful.items())).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PermitRecord":
        if not payload.get("address_id"):
            payload = dict(payload)
            payload["address_id"] = build_address_id(payload.get("address"), payload.get("city_state_zip"))
        record = cls(**payload)
        return record

    def with_seen_timestamp(self, when_iso: Optional[str] = None) -> "PermitRecord":
        when_iso = when_iso or utc_now_iso()
        self.last_seen_at = when_iso
        return self

    def reset_geocode_fields(self) -> None:
        self.latitude = ""
        self.longitude = ""
        self.geocoded_address = ""
        self.geocode_source = ""
        self.geocode_status = ""
        self.geocode_error = ""
        self.geocode_attempts = 0
        self.geocode_last_attempt_at = ""

    def copy_geocode_fields_from(self, other: "PermitRecord") -> None:
        self.latitude = other.latitude
        self.longitude = other.longitude
        self.geocoded_address = other.geocoded_address
        self.geocode_source = other.geocode_source
        self.geocode_status = other.geocode_status
        self.geocode_error = other.geocode_error
        self.geocode_attempts = other.geocode_attempts
        self.geocode_last_attempt_at = other.geocode_last_attempt_at

    def apply_new_scrape(self, new_record: "PermitRecord", when_iso: Optional[str] = None) -> tuple[bool, "PermitRecord"]:
        when_iso = when_iso or utc_now_iso()
        old_hash = self.data_hash
        if not new_record.address_id:
            new_record.address_id = build_address_id(new_record.address, new_record.city_state_zip)
        new_hash = new_record.compute_data_hash()

        if old_hash == new_hash:
            self.last_seen_at = when_iso
            return False, self

        new_record.first_seen_at = self.first_seen_at or when_iso
        new_record.last_seen_at = when_iso
        new_record.last_changed_at = when_iso
        new_record.data_hash = new_hash
        return True, new_record


@dataclass
class AddressRecord:
    address_id: str
    address: str = ""
    city_state_zip: str = ""
    latitude: str = ""
    longitude: str = ""
    geocoded_address: str = ""
    geocode_source: str = ""
    geocode_status: str = ""
    geocode_error: str = ""
    geocode_attempts: int = 0
    geocode_last_attempt_at: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    last_changed_at: str = ""
    data_hash: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_fields(cls, fields: Dict[str, Any]) -> "AddressRecord":
        record = cls(
            address_id=normalize_str(fields.get("address_id")) or build_address_id(fields.get("address"), fields.get("city_state_zip")),
            address=normalize_str(fields.get("address")),
            city_state_zip=normalize_str(fields.get("city_state_zip")),
            latitude=normalize_str(fields.get("latitude")),
            longitude=normalize_str(fields.get("longitude")),
            geocoded_address=normalize_str(fields.get("geocoded_address")),
            geocode_source=normalize_str(fields.get("geocode_source")),
            geocode_status=normalize_str(fields.get("geocode_status")),
            geocode_error=normalize_str(fields.get("geocode_error")),
            geocode_attempts=int(fields.get("geocode_attempts", 0) or 0),
            geocode_last_attempt_at=normalize_str(fields.get("geocode_last_attempt_at")),
            extra=fields.get("extra", {}) or {},
        )
        now = utc_now_iso()
        record.first_seen_at = normalize_str(fields.get("first_seen_at")) or now
        record.last_seen_at = normalize_str(fields.get("last_seen_at")) or now
        record.last_changed_at = normalize_str(fields.get("last_changed_at")) or now
        record.data_hash = record.compute_data_hash()
        return record

    @classmethod
    def from_permit(cls, permit: PermitRecord, when_iso: Optional[str] = None) -> "AddressRecord":
        when_iso = when_iso or utc_now_iso()
        record = cls(
            address_id=permit.address_id or build_address_id(permit.address, permit.city_state_zip),
            address=permit.address,
            city_state_zip=permit.city_state_zip,
            latitude=permit.latitude,
            longitude=permit.longitude,
            geocoded_address=permit.geocoded_address,
            geocode_source=permit.geocode_source,
            geocode_status=permit.geocode_status,
            geocode_error=permit.geocode_error,
            geocode_attempts=permit.geocode_attempts,
            geocode_last_attempt_at=permit.geocode_last_attempt_at,
            first_seen_at=permit.first_seen_at or when_iso,
            last_seen_at=permit.last_seen_at or when_iso,
            last_changed_at=permit.last_changed_at or when_iso,
        )
        record.data_hash = record.compute_data_hash()
        return record

    def compute_data_hash(self) -> str:
        meaningful = {
            "address_id": self.address_id,
            "address": self.address,
            "city_state_zip": self.city_state_zip,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "geocoded_address": self.geocoded_address,
            "geocode_source": self.geocode_source,
            "geocode_status": self.geocode_status,
            "geocode_error": self.geocode_error,
            "geocode_attempts": self.geocode_attempts,
            "geocode_last_attempt_at": self.geocode_last_attempt_at,
            "extra": self.extra,
        }
        return sha256(repr(sorted(meaningful.items())).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AddressRecord":
        if not payload.get("address_id"):
            payload = dict(payload)
            payload["address_id"] = build_address_id(payload.get("address"), payload.get("city_state_zip"))
        return cls(**payload)

    def with_seen_timestamp(self, when_iso: Optional[str] = None) -> "AddressRecord":
        self.last_seen_at = when_iso or utc_now_iso()
        return self

    def reset_geocode_fields(self) -> None:
        self.latitude = ""
        self.longitude = ""
        self.geocoded_address = ""
        self.geocode_source = ""
        self.geocode_status = ""
        self.geocode_error = ""
        self.geocode_attempts = 0
        self.geocode_last_attempt_at = ""

    def apply_new_source(self, new_record: "AddressRecord", when_iso: Optional[str] = None) -> tuple[bool, "AddressRecord"]:
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


def apply_address_to_permit(permit: PermitRecord, address: AddressRecord) -> None:
    permit.address_id = address.address_id
    permit.latitude = address.latitude
    permit.longitude = address.longitude
    permit.geocoded_address = address.geocoded_address
    permit.geocode_source = address.geocode_source
    permit.geocode_status = address.geocode_status
    permit.geocode_error = address.geocode_error
    permit.geocode_attempts = address.geocode_attempts
    permit.geocode_last_attempt_at = address.geocode_last_attempt_at
