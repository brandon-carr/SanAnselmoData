from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
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
    username: str = os.getenv("ETRAKIT_USERNAME", "")
    password: str = os.getenv("ETRAKIT_PASSWORD", "")

    # These can be adjusted after the first live authenticated inspection if needed.
    username_field: str = os.getenv("ETRAKIT_USERNAME_FIELD", "ctl00$MainContent$txtUserName")
    password_field: str = os.getenv("ETRAKIT_PASSWORD_FIELD", "ctl00$MainContent$txtPassword")
    login_button_field: str = os.getenv("ETRAKIT_LOGIN_BUTTON_FIELD", "ctl00$MainContent$btnLogin")

    # Search form fields; these may need one live adjustment.
    search_by_field: str = os.getenv("ETRAKIT_SEARCH_BY_FIELD", "ctl00$MainContent$ddlSearchBy")
    issued_start_field: str = os.getenv("ETRAKIT_ISSUED_START_FIELD", "ctl00$MainContent$txtStartDate")
    issued_end_field: str = os.getenv("ETRAKIT_ISSUED_END_FIELD", "ctl00$MainContent$txtEndDate")
    search_button_field: str = os.getenv("ETRAKIT_SEARCH_BUTTON_FIELD", "ctl00$MainContent$btnSearch")
    issued_search_value: str = os.getenv("ETRAKIT_ISSUED_SEARCH_VALUE", "ISSUED")


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

    def login(self) -> None:
        if not self.config.username or not self.config.password:
            raise ETrakitError("Missing ETRAKIT_USERNAME or ETRAKIT_PASSWORD")

        login_page = self._get(self.config.login_url)
        payload = self._parse_hidden_inputs(login_page.text)
        payload[self.config.username_field] = self.config.username
        payload[self.config.password_field] = self.config.password
        payload[self.config.login_button_field] = "Log In"

        response = self._post(self.config.login_url, payload)
        body = response.text.lower()

        # This is intentionally broad. It catches the common post-login cases.
        if "log out" not in body and "logout" not in body and "welcome" not in body:
            raise ETrakitError(
                "Login may have failed. Check login field env vars or inspect the live login form."
            )

    def search_permits_by_issued_date(self, issued_date_iso: str) -> List[str]:
        """
        Returns permit detail URLs for permits issued on the supplied YYYY-MM-DD date.
        """
        try:
            dt = datetime.strptime(issued_date_iso, "%Y-%m-%d")
        except ValueError as exc:
            raise ETrakitError(f"Invalid TARGET_ISSUED_DATE: {issued_date_iso}") from exc

        search_page = self._get(self.config.permit_search_url)
        payload = self._parse_hidden_inputs(search_page.text)
        date_text = dt.strftime("%m/%d/%Y")

        payload[self.config.search_by_field] = self.config.issued_search_value
        payload[self.config.issued_start_field] = date_text
        payload[self.config.issued_end_field] = date_text
        payload[self.config.search_button_field] = "Search"

        response = self._post(self.config.permit_search_url, payload)
        return self._extract_permit_links_from_search_results(response.text)

    def _extract_permit_links_from_search_results(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: List[str] = []

        # Best case: detail links contain ActivityNo in href.
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "ActivityNo=" in href or "Permit.aspx?" in href:
                full = urljoin(self.config.base_url, href)
                links.append(full)

        # De-duplicate while preserving order.
        deduped: List[str] = []
        seen = set()
        for link in links:
            if link not in seen:
                seen.add(link)
                deduped.append(link)
        return deduped

    def fetch_permit_details(self, detail_url: str) -> Dict[str, str]:
        response = self._get(detail_url)
        html = response.text
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
