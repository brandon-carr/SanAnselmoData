from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from permit_model import normalize_str


@dataclass
class ETrakitConfig:
    base_url: str = os.getenv("ETRAKIT_BASE_URL", "https://sanan-trk.aspgov.com/eTRAKiT/")
    login_url: str = os.getenv("ETRAKIT_LOGIN_URL", "https://sanan-trk.aspgov.com/eTRAKiT/")
    permit_search_url: str = os.getenv(
        "ETRAKIT_PERMIT_SEARCH_URL",
        "https://sanan-trk.aspgov.com/eTRAKiT/Search/Permit.aspx",
    )
    issue_report_url: str = os.getenv(
        "ETRAKIT_ISSUE_REPORT_URL",
        "https://sanan-trk.aspgov.com/eTRAKiT/PermitApplication/issue_permit_report.aspx",
    )

    username: str = os.getenv("ETRAKIT_USERNAME", "")
    password: str = os.getenv("ETRAKIT_PASSWORD", "")

    username_field: str = os.getenv("ETRAKIT_USERNAME_FIELD", "ctl00$MainContent$txtUserName")
    password_field: str = os.getenv("ETRAKIT_PASSWORD_FIELD", "ctl00$MainContent$txtPassword")
    login_button_field: str = os.getenv("ETRAKIT_LOGIN_BUTTON_FIELD", "ctl00$MainContent$btnLogin")

    # Report page fields. These may need one live adjustment.
    report_start_field: str = os.getenv("ETRAKIT_REPORT_START_FIELD", "ctl00$MainContent$txtFromDate")
    report_end_field: str = os.getenv("ETRAKIT_REPORT_END_FIELD", "ctl00$MainContent$txtToDate")
    report_run_button_field: str = os.getenv("ETRAKIT_REPORT_RUN_BUTTON_FIELD", "ctl00$MainContent$btnReport")

    # Fallback search fields.
    search_by_field: str = os.getenv("ETRAKIT_SEARCH_BY_FIELD", "ctl00$MainContent$ddlSearchBy")
    issued_start_field: str = os.getenv("ETRAKIT_ISSUED_START_FIELD", "ctl00$MainContent$txtStartDate")
    issued_end_field: str = os.getenv("ETRAKIT_ISSUED_END_FIELD", "ctl00$MainContent$txtEndDate")
    search_button_field: str = os.getenv("ETRAKIT_SEARCH_BUTTON_FIELD", "ctl00$MainContent$btnSearch")
    issued_search_value: str = os.getenv("ETRAKIT_ISSUED_SEARCH_VALUE", "ISSUED")

    debug_enabled: bool = os.getenv("ETRAKIT_DEBUG", "false").lower() == "true"
    debug_dir: str = os.getenv("ETRAKIT_DEBUG_DIR", "data/debug")


class ETrakitError(RuntimeError):
    pass


class ETrakitClient:
    def __init__(self, config: Optional[ETrakitConfig] = None) -> None:
        self.config = config or ETrakitConfig()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; SanAnselmoPermitSync/1.0)",
            }
        )
        self.debug_dir = Path(self.config.debug_dir)
        if self.config.debug_enabled:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    def _debug_write_text(self, filename: str, content: str) -> None:
        if not self.config.debug_enabled:
            return
        (self.debug_dir / filename).write_text(content, encoding="utf-8")

    def _debug_write_json(self, filename: str, payload: dict) -> None:
        if not self.config.debug_enabled:
            return
        (self.debug_dir / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _get(self, url: str) -> requests.Response:
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: Dict[str, str]) -> requests.Response:
        resp = self.session.post(url, data=data, timeout=60)
        resp.raise_for_status()
        return resp

    def _parse_hidden_inputs(self, html: str) -> Dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        payload: Dict[str, str] = {}
        for inp in soup.select("input[type='hidden'][name]"):
            payload[inp.get("name")] = inp.get("value", "")
        return payload

    def _extract_form_fields(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        fields = []
        for tag in soup.select("input[name], select[name], textarea[name], button[name]"):
            fields.append(
                {
                    "tag": tag.name,
                    "name": tag.get("name", ""),
                    "id": tag.get("id", ""),
                    "type": tag.get("type", ""),
                    "value": tag.get("value", ""),
                }
            )
        return {"fields": fields}

    def login(self) -> None:
        if not self.config.username or not self.config.password:
            raise ETrakitError("Missing ETRAKIT_USERNAME or ETRAKIT_PASSWORD")

        login_page = self._get(self.config.login_url)
        self._debug_write_text("01_login_page.html", login_page.text)
        self._debug_write_json("01_login_page_fields.json", self._extract_form_fields(login_page.text))

        payload = self._parse_hidden_inputs(login_page.text)
        payload[self.config.username_field] = self.config.username
        payload[self.config.password_field] = self.config.password
        payload[self.config.login_button_field] = "Log In"

        self._debug_write_json(
            "02_login_post_payload.json",
            {
                "login_url": self.config.login_url,
                "payload_keys": sorted(payload.keys()),
                "username_field": self.config.username_field,
                "password_field": self.config.password_field,
                "login_button_field": self.config.login_button_field,
            },
        )

        response = self._post(self.config.login_url, payload)
        self._debug_write_text("03_login_response.html", response.text)
        self._debug_write_json("03_login_response_fields.json", self._extract_form_fields(response.text))

        body = response.text.lower()
        if "log out" not in body and "logout" not in body and "welcome" not in body:
            raise ETrakitError(
                "Login may have failed. Check data/debug for the real login field names and response HTML."
            )

    def search_permits_by_issued_date(self, issued_date_iso: str) -> List[str]:
        links = self.fetch_issued_report_for_date(issued_date_iso)
        if links:
            return links

        # Fallback only if the report returned nothing.
        self._debug_write_json(
            "07_report_fallback.json",
            {"reason": "issue report returned zero links; using fallback permit search"},
        )
        return self._search_permits_by_issued_date_fallback(issued_date_iso)

    def fetch_issued_report_for_date(self, issued_date_iso: str) -> List[str]:
        try:
            dt = datetime.strptime(issued_date_iso, "%Y-%m-%d")
        except ValueError as exc:
            raise ETrakitError(f"Invalid issued date: {issued_date_iso}") from exc

        try:
            report_page = self._get(self.config.issue_report_url)
        except requests.HTTPError as exc:
            response = exc.response
            if response is not None:
                self._debug_write_text("04_issue_report_page_error.html", response.text)
                self._debug_write_json(
                    "04_issue_report_page_error_meta.json",
                    {
                        "status_code": response.status_code,
                        "url": response.url,
                    },
                )
            raise ETrakitError(
                f"Failed to load issue report page: {exc}"
            ) from exc

        self._debug_write_text("04_issue_report_page.html", report_page.text)
        self._debug_write_json(
            "04_issue_report_page_fields.json",
            self._extract_form_fields(report_page.text),
        )

        payload = self._parse_hidden_inputs(report_page.text)
        date_text = dt.strftime("%m/%d/%Y")

        payload[self.config.report_start_field] = date_text
        payload[self.config.report_end_field] = date_text
        payload[self.config.report_run_button_field] = "Run Report"

        self._debug_write_json(
            "05_issue_report_post_payload.json",
            {
                "issue_report_url": self.config.issue_report_url,
                "issued_date_iso": issued_date_iso,
                "issued_date_formatted": date_text,
                "payload_keys": sorted(payload.keys()),
                "report_start_field": self.config.report_start_field,
                "report_end_field": self.config.report_end_field,
                "report_run_button_field": self.config.report_run_button_field,
            },
        )

        response = self._post(self.config.issue_report_url, payload)
        self._debug_write_text("06_issue_report_results.html", response.text)

        links = self._extract_permit_links_from_report(response.text)
        self._debug_write_json(
            "06_issue_report_links.json",
            {"count": len(links), "links": links},
        )
        return links

    def _extract_permit_links_from_report(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: List[str] = []

        # First choice: actual permit detail links.
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "ActivityNo=" in href or "Permit.aspx?" in href:
                links.append(urljoin(self.config.base_url, href))

        # Backup: sometimes the report may contain plain permit numbers but not direct links.
        # If so, synthesize detail URLs from permit numbers found in text.
        if not links:
            text = " ".join(soup.stripped_strings)
            permit_numbers = re.findall(r"\b([A-Z]\d{4}-\d{4,})\b", text, flags=re.IGNORECASE)
            for permit_number in permit_numbers:
                links.append(
                    urljoin(
                        self.config.base_url,
                        f"Search/Permit.aspx?ActivityNo={permit_number.upper()}",
                    )
                )

        return self._dedupe(links)

    def _search_permits_by_issued_date_fallback(self, issued_date_iso: str) -> List[str]:
        dt = datetime.strptime(issued_date_iso, "%Y-%m-%d")
        search_page = self._get(self.config.permit_search_url)
        self._debug_write_text("08_search_page.html", search_page.text)
        self._debug_write_json("08_search_page_fields.json", self._extract_form_fields(search_page.text))

        payload = self._parse_hidden_inputs(search_page.text)
        date_text = dt.strftime("%m/%d/%Y")

        payload[self.config.search_by_field] = self.config.issued_search_value
        payload[self.config.issued_start_field] = date_text
        payload[self.config.issued_end_field] = date_text
        payload[self.config.search_button_field] = "Search"

        self._debug_write_json(
            "09_search_post_payload.json",
            {
                "permit_search_url": self.config.permit_search_url,
                "issued_date_iso": issued_date_iso,
                "issued_date_formatted": date_text,
                "payload_keys": sorted(payload.keys()),
                "search_by_field": self.config.search_by_field,
                "issued_start_field": self.config.issued_start_field,
                "issued_end_field": self.config.issued_end_field,
                "search_button_field": self.config.search_button_field,
                "issued_search_value": self.config.issued_search_value,
            },
        )

        response = self._post(self.config.permit_search_url, payload)
        self._debug_write_text("10_search_results.html", response.text)

        links = self._extract_permit_links_from_search_results(response.text)
        self._debug_write_json("10_search_results_links.json", {"count": len(links), "links": links})
        return links

    def _extract_permit_links_from_search_results(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "ActivityNo=" in href or "Permit.aspx?" in href:
                links.append(urljoin(self.config.base_url, href))
        return self._dedupe(links)

    def _dedupe(self, links: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for link in links:
            if link not in seen:
                seen.add(link)
                out.append(link)
        return out

    def fetch_permit_details(self, detail_url: str) -> Dict[str, str]:
        response = self._get(detail_url)
        html = response.text

        permit_number_hint = self._extract_permit_number(detail_url, html, [])
        safe_name = permit_number_hint or "unknown_permit"
        self._debug_write_text(f"permit_{safe_name}.html", html)

        soup = BeautifulSoup(html, "html.parser")
        text_nodes = [normalize_str(t) for t in soup.stripped_strings if normalize_str(t)]

        def find_after(label_variants: Iterable[str]) -> str:
            lookup = {v.lower() for v in label_variants}
            for idx, token in enumerate(text_nodes):
                if token.lower() in lookup:
                    for nxt in text_nodes[idx + 1 : idx + 8]:
                        if nxt and nxt.lower() not in lookup:
                            return nxt
            return ""

        permit_number = self._extract_permit_number(detail_url, html, text_nodes)

        fields = {
            "permit_number": permit_number,
            "status": find_after(["Status:", "Status"]),
            "permit_type": find_after(["Type:", "Type"]),
            "subtype": find_after(["Subtype:", "Subtype"]),
            "short_description": find_after(["Short Description:", "Short Description"]),
            "address": find_after(["Address:", "Address"]),
            "city_state_zip": find_after(["City/State/Zip:", "City/State/Zip"]),
            "apn": find_after(["APN:", "APN"]),
            "property_type": find_after(["Property Type:", "Property Type"]),
            "lot_size_sf": find_after(["Lot Size (SF):", "Lot Size (SF)"]),
            "applied_date": find_after(["Applied Date:", "Applied Date"]),
            "approved_date": find_after(["Approved Date:", "Approved Date"]),
            "issued_date": find_after(["Issued Date:", "Issued Date"]),
            "finaled_date": find_after(["Finaled Date:", "Finaled Date"]),
            "expiration_date": find_after(["Expiration Date:", "Expiration Date"]),
            "source_url": detail_url,
            "extra": {},
        }

        self._debug_write_json(f"permit_{safe_name}_parsed.json", fields)

        if not fields["permit_number"]:
            raise ETrakitError(f"Could not extract permit number from {detail_url}")

        return fields

    def _extract_permit_number(self, detail_url: str, html: str, text_nodes: List[str]) -> str:
        match = re.search(r"ActivityNo=([A-Z]\d{4}-\d{4,})", detail_url, re.IGNORECASE)
        if match:
            return match.group(1).upper()

        html_match = re.search(r"Permit\s*#\s*([A-Z]\d{4}-\d{4,})", html, re.IGNORECASE)
        if html_match:
            return html_match.group(1).upper()

        for token in text_nodes:
            token_match = re.search(r"\b([A-Z]\d{4}-\d{4,})\b", token, re.IGNORECASE)
            if token_match:
                return token_match.group(1).upper()

        return ""
