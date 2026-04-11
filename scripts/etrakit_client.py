from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from permit_model import normalize_str

load_dotenv(override=True)


@dataclass
class ETrakitConfig:
    base_url: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_BASE_URL", "https://sanan-trk.aspgov.com/eTRAKiT/")
    )
    login_url: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_LOGIN_URL", "https://sanan-trk.aspgov.com/eTRAKiT/")
    )
    permit_search_url: str = field(
        default_factory=lambda: os.getenv(
            "ETRAKIT_PERMIT_SEARCH_URL",
            "https://sanan-trk.aspgov.com/eTRAKiT/Search/Permit.aspx",
        )
    )
    issue_report_url: str = field(
        default_factory=lambda: os.getenv(
            "ETRAKIT_ISSUE_REPORT_URL",
            "https://sanan-trk.aspgov.com/eTRAKiT/PermitApplication/issue_permit_report.aspx",
        )
    )
    dashboard_url: str = field(
        default_factory=lambda: os.getenv(
            "ETRAKIT_DASHBOARD_URL",
            "https://sanan-trk.aspgov.com/eTRAKiT/dashboard.aspx",
        )
    )

    username: str = field(default_factory=lambda: os.getenv("ETRAKIT_USERNAME", ""))
    password: str = field(default_factory=lambda: os.getenv("ETRAKIT_PASSWORD", ""))
    login_mode: str = field(default_factory=lambda: os.getenv("ETRAKIT_LOGIN_MODE", "auto").strip().lower())

    username_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_USERNAME_FIELD", "ctl00$MainContent$txtUserName")
    )
    password_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_PASSWORD_FIELD", "ctl00$MainContent$txtPassword")
    )
    login_button_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_LOGIN_BUTTON_FIELD", "ctl00$MainContent$btnLogin")
    )

    report_start_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_REPORT_START_FIELD", "ctl00$cplMain$calFromDate")
    )
    report_end_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_REPORT_END_FIELD", "ctl00$cplMain$calToDate")
    )
    report_run_button_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_REPORT_RUN_BUTTON_FIELD", "ctl00$cplMain$btnViewReport")
    )
    report_date_type_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_REPORT_DATE_TYPE_FIELD", "ctl00$cplMain$ddlDateTypes")
    )

    search_by_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_SEARCH_BY_FIELD", "ctl00$cplMain$ddSearchBy")
    )
    search_operator_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_SEARCH_OPERATOR_FIELD", "ctl00$cplMain$ddSearchOper")
    )
    search_text_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_SEARCH_TEXT_FIELD", "ctl00$cplMain$txtSearchString")
    )
    issued_start_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_ISSUED_START_FIELD", "ctl00$MainContent$txtStartDate")
    )
    issued_end_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_ISSUED_END_FIELD", "ctl00$MainContent$txtEndDate")
    )
    search_button_field: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_SEARCH_BUTTON_FIELD", "ctl00$MainContent$btnSearch")
    )
    issued_search_value: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_ISSUED_SEARCH_VALUE", "ISSUED")
    )
    permit_number_search_value: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_PERMIT_NUMBER_SEARCH_VALUE", "Permit Number")
    )
    search_equals_value: str = field(
        default_factory=lambda: os.getenv("ETRAKIT_SEARCH_EQUALS_VALUE", "Equals")
    )

    debug_enabled: bool = field(
        default_factory=lambda: os.getenv("ETRAKIT_DEBUG", "false").lower() == "true"
    )
    debug_dir: str = field(default_factory=lambda: os.getenv("ETRAKIT_DEBUG_DIR", "data/debug"))


class ETrakitError(RuntimeError):
    pass


class ETrakitClient:
    def __init__(self, config: Optional[ETrakitConfig] = None) -> None:
        self.config = config or ETrakitConfig()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.debug_dir = Path(self.config.debug_dir)
        if self.config.debug_enabled:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    def _debug_write_text(self, filename: str, content: str) -> None:
        if not self.config.debug_enabled:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        (self.debug_dir / filename).write_text(content, encoding="utf-8")

    def _debug_write_json(self, filename: str, payload: dict) -> None:
        if not self.config.debug_enabled:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        (self.debug_dir / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _telerik_client_state(self, value: str) -> str:
        return json.dumps(
            {
                "enabled": True,
                "emptyMessage": "",
                "validationText": value,
                "valueAsString": value,
                "lastSetTextBoxValue": value,
            },
            separators=(",", ":"),
        )

    def _get(self, url: str) -> requests.Response:
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: Dict[str, str]) -> requests.Response:
        resp = self.session.post(url, data=data, timeout=60)
        resp.raise_for_status()
        return resp

    def _browser_headers(self, url: str, referer: str = "") -> Dict[str, str]:
        parsed = urlparse(url)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _set_telerik_date_payload(self, payload: Dict[str, str], base_field: str, dt: datetime) -> None:
        field_prefix = base_field.replace("$", "_")
        iso_date = dt.strftime("%Y-%m-%d")
        text_date = dt.strftime("%m/%d/%Y")
        value_as_string = f"{iso_date}-00-00-00"

        payload[base_field] = iso_date

        date_input_field = f"{base_field}$dateInput"
        # Telerik date controls submit both the ISO field and the visible text input.
        # If we omit the text input, the server can fall back to a broader/default range.
        payload[date_input_field] = text_date

        date_input_client_state_field = f"{field_prefix}_dateInput_ClientState"
        if date_input_client_state_field in payload:
            payload[date_input_client_state_field] = json.dumps(
                {
                    "enabled": True,
                    "emptyMessage": "",
                    "validationText": value_as_string,
                    "valueAsString": value_as_string,
                    "minDateStr": "1801-01-01-00-00-00",
                    "maxDateStr": "2500-01-01-00-00-00",
                    "lastSetTextBoxValue": text_date,
                },
                separators=(",", ":"),
            )

        client_state_field = f"{field_prefix}_ClientState"
        if client_state_field in payload:
            payload[client_state_field] = json.dumps(
                {
                    "minDateStr": "1801-01-01-00-00-00",
                    "maxDateStr": "2500-01-01-00-00-00",
                },
                separators=(",", ":"),
            )

        calendar_sd_field = f"{field_prefix}_calendar_SD"
        if calendar_sd_field in payload:
            payload[calendar_sd_field] = f"[[{dt.year},{dt.month - 1},{dt.day}]]"

        calendar_ad_field = f"{field_prefix}_calendar_AD"
        if calendar_ad_field in payload:
            payload[calendar_ad_field] = f"[[1801,1,1],[2500,1,1],[{dt.year},{dt.month},{dt.day}]]"

    def _parse_hidden_inputs(self, html: str) -> Dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        payload: Dict[str, str] = {}
        for inp in soup.select("input[type='hidden'][name]"):
            payload[inp.get("name")] = inp.get("value", "")
        # Some report controls are posted as plain text inputs, not hidden fields.
        # Include them so later payload mutation has the same shape as a browser form submit.
        for inp in soup.select("input[type='text'][name]"):
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

    def _is_login_page(self, html: str, url: str = "") -> bool:
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id="form1")
        action = (form.get("action", "") if form else "").lower()
        text_samples = list(soup.stripped_strings)[:50]

        has_login_controls = any(
            soup.select_one(selector)
            for selector in (
                "input[name$='$txtPassword']",
                "input[name$='$txtPublicPassword']",
                "input[name$='$btnLogin']",
                "input[name$='$btnPublicLogin']",
            )
        )

        title_text = " ".join(text_samples).lower()
        return (
            "login.aspx" in url.lower()
            or "login.aspx" in action
            or "<h1 style=\"display:none\">login</h1>" in html.lower()
            or ("please log in" in title_text and has_login_controls)
        )

    def _find_first_field_name(self, html: str, suffixes: Iterable[str]) -> str:
        soup = BeautifulSoup(html, "html.parser")
        tags = list(soup.select("input[name], select[name], textarea[name], button[name]"))
        for suffix in suffixes:
            for tag in tags:
                name = tag.get("name", "")
                if name.endswith(suffix):
                    return name
        return ""

    def _selected_select_value(self, html: str, field_name: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        select = soup.find("select", attrs={"name": field_name})
        if not select:
            return ""

        selected = select.find("option", selected=True)
        if selected:
            return selected.get("value", "")

        first = select.find("option")
        return first.get("value", "") if first else ""

    def _active_login_mode(self, html: str) -> str:
        login_type_field = self._find_first_field_name(html, ("$ddlSelLogin",))
        if not login_type_field:
            return ""
        return self._selected_select_value(html, login_type_field).strip().lower()

    def _switch_login_mode(self, page_url: str, html: str, target_mode: str) -> tuple[str, str]:
        login_type_field = self._find_first_field_name(html, ("$ddlSelLogin",))
        current_login_type = self._active_login_mode(html)
        if not login_type_field or not current_login_type or current_login_type == target_mode:
            return html, page_url

        mode_payload = self._parse_hidden_inputs(html)
        mode_payload[login_type_field] = target_mode.capitalize()
        mode_payload["__EVENTTARGET"] = login_type_field
        mode_payload["__EVENTARGUMENT"] = ""
        mode_response = self.session.post(
            page_url,
            data=mode_payload,
            headers=self._browser_headers(page_url, referer=page_url),
            timeout=60,
        )
        self._debug_write_text("01b_login_mode_switch_response.txt", mode_response.text)
        self._debug_write_json(
            "01b_login_mode_switch_meta.json",
            {
                "status_code": mode_response.status_code,
                "url": mode_response.url,
                "headers": dict(mode_response.headers),
                "target_mode": target_mode,
            },
        )
        return mode_response.text, mode_response.url or page_url

    def _match_select_option_value(self, html: str, field_name: str, desired_value: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        select = soup.find("select", attrs={"name": field_name})
        if not select:
            return ""

        desired_norm = normalize_str(desired_value).lower()
        for option in select.find_all("option"):
            option_value = option.get("value", "").strip()
            option_text = normalize_str(option.get_text(" ", strip=True)).lower()
            if desired_value == option_value:
                return option_value
            if desired_norm and desired_norm == option_text:
                return option_value
            if desired_norm and desired_norm in option_text:
                return option_value
        return ""

    def _find_login_field_name_for_mode(
        self,
        html: str,
        mode: str,
        public_suffixes: Iterable[str],
        contractor_suffixes: Iterable[str],
        fallback_suffixes: Iterable[str],
        configured_field: str = "",
    ) -> str:
        if mode == "public":
            suffix_groups = [public_suffixes, contractor_suffixes, fallback_suffixes]
        elif mode == "contractor":
            suffix_groups = [contractor_suffixes, public_suffixes, fallback_suffixes]
        else:
            suffix_groups = [public_suffixes, contractor_suffixes, fallback_suffixes]

        for suffixes in suffix_groups:
            field_name = self._find_first_field_name(html, suffixes)
            if field_name:
                return field_name

        if configured_field and configured_field in html:
            return configured_field
        return ""

    def _login_field_names_for_html(self, html: str) -> tuple[str, str, str]:
        login_mode = self._active_login_mode(html)

        username_field = self._find_login_field_name_for_mode(
            html,
            login_mode,
            public_suffixes=("$txtPublicUserName",),
            contractor_suffixes=("$txtLoginId", "$txtUserName"),
            fallback_suffixes=("$txtPublicUserName", "$txtLoginId", "$txtUserName"),
            configured_field=self.config.username_field,
        )

        password_field = self._find_login_field_name_for_mode(
            html,
            login_mode,
            public_suffixes=("$txtPublicPassword",),
            contractor_suffixes=("$txtPassword",),
            fallback_suffixes=("$txtPublicPassword", "$txtPassword"),
            configured_field=self.config.password_field,
        )

        login_button_field = self._find_login_field_name_for_mode(
            html,
            login_mode,
            public_suffixes=("$btnPublicLogin",),
            contractor_suffixes=("$btnContractorLogin", "$btnLogin"),
            fallback_suffixes=("$btnPublicLogin", "$btnContractorLogin", "$btnLogin"),
            configured_field=self.config.login_button_field,
        )

        return username_field, password_field, login_button_field

    def _report_field_names_for_html(self, html: str) -> tuple[str, str, str, str]:
        date_type_field = self._find_first_field_name(html, ("$ddlDateTypes",))
        start_field = self._find_first_field_name(html, ("$calFromDate", "$txtFromDate"))
        end_field = self._find_first_field_name(html, ("$calToDate", "$txtToDate"))
        run_button_field = self._find_first_field_name(html, ("$btnViewReport", "$btnReport"))

        if not date_type_field and self.config.report_date_type_field in html:
            date_type_field = self.config.report_date_type_field
        if not start_field and self.config.report_start_field in html:
            start_field = self.config.report_start_field
        if not end_field and self.config.report_end_field in html:
            end_field = self.config.report_end_field
        if not run_button_field and self.config.report_run_button_field in html:
            run_button_field = self.config.report_run_button_field

        return date_type_field, start_field, end_field, run_button_field

    def _permit_search_field_names_for_html(self, html: str) -> tuple[str, str, str, str]:
        search_by_field = self._find_first_field_name(html, ("$ddSearchBy", "$ddlSearchBy"))
        search_operator_field = self._find_first_field_name(html, ("$ddSearchOper", "$ddlSearchOper"))
        search_text_field = self._find_first_field_name(html, ("$txtSearchString", "$txtSearch"))
        search_button_field = self._find_first_field_name(html, ("$btnSearch",))

        if not search_by_field and self.config.search_by_field in html:
            search_by_field = self.config.search_by_field
        if not search_operator_field and self.config.search_operator_field in html:
            search_operator_field = self.config.search_operator_field
        if not search_text_field and self.config.search_text_field in html:
            search_text_field = self.config.search_text_field
        if not search_button_field and self.config.search_button_field in html:
            search_button_field = self.config.search_button_field

        return search_by_field, search_operator_field, search_text_field, search_button_field

    def _submit_login_form(self, page_url: str, html: str, preferred_mode: str = "public") -> requests.Response:
        if preferred_mode in {"public", "contractor"}:
            html, page_url = self._switch_login_mode(page_url, html, preferred_mode)

        active_mode = self._active_login_mode(html)
        username_field, password_field, login_button_field = self._login_field_names_for_html(html)
        contractor_field = self._find_first_field_name(html, ("$ddlSelContractor",))

        if active_mode == "contractor":
            if not contractor_field or not password_field or not login_button_field:
                raise ETrakitError("Could not locate contractor login form fields in login page HTML.")
        elif not username_field or not password_field or not login_button_field:
            raise ETrakitError("Could not locate login form fields in login page HTML.")

        payload = self._parse_hidden_inputs(html)
        if active_mode == "contractor" and contractor_field:
            contractor_value = self._match_select_option_value(html, contractor_field, self.config.username)
            if not contractor_value:
                raise ETrakitError(
                    "Could not match ETRAKIT_USERNAME to any contractor option in the login dropdown."
                )
            payload[contractor_field] = contractor_value
        else:
            payload[username_field] = self.config.username
            username_client_state_field = self._find_first_field_name(html, ("txtLoginId_ClientState", "txtUserName_ClientState", "txtPublicUserName_ClientState"))
            if username_client_state_field:
                payload[username_client_state_field] = self._telerik_client_state(self.config.username)
        payload[password_field] = self.config.password
        masked_password_field = self._find_first_field_name(html, ("$RadTextBox2",))
        password_client_state_field = self._find_first_field_name(
            html,
            ("txtPassword_ClientState", "txtPublicPassword_ClientState"),
        )
        if password_client_state_field:
            payload[password_client_state_field] = self._telerik_client_state(self.config.password)
        masked_password_client_state_field = self._find_first_field_name(html, ("RadTextBox2_ClientState",))
        if masked_password_field:
            payload[masked_password_field] = ""
        if masked_password_client_state_field:
            payload[masked_password_client_state_field] = self._telerik_client_state("")
        payload["__EVENTTARGET"] = login_button_field
        payload["__EVENTARGUMENT"] = ""

        if login_button_field.lower().endswith(("btnpubliclogin", "btncontractorlogin")):
            payload[login_button_field] = "Login"
        else:
            payload[login_button_field] = "Login"

        login_type_field = self._find_first_field_name(html, ("$ddlSelLogin",))
        if login_type_field and login_type_field not in payload and active_mode:
            payload[login_type_field] = active_mode.capitalize()

        self._debug_write_json(
            "02_login_post_payload.json",
            {
                "login_url": page_url,
                "payload_keys": sorted(payload.keys()),
                "username_field": username_field,
                "password_field": password_field,
                "password_client_state_field": password_client_state_field,
                "masked_password_field": masked_password_field,
                "masked_password_client_state_field": masked_password_client_state_field,
                "login_button_field": login_button_field,
                "detected_login_type_field": login_type_field,
                "active_mode": active_mode,
                "contractor_field": contractor_field,
            },
        )

        response = self.session.post(
            page_url,
            data=payload,
            headers=self._browser_headers(page_url, referer=page_url),
            timeout=60,
        )
        self._debug_write_text("03_login_response.html", response.text)
        self._debug_write_json("03_login_response_fields.json", self._extract_form_fields(response.text))
        return response

    def _ensure_logged_in_for_url(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=60)
        if not self._is_login_page(response.text, response.url):
            return response

        self._debug_write_text("04_issue_report_page_raw.html", response.text)
        self._debug_write_json(
            "04_issue_report_page_meta.json",
            {
                "status_code": response.status_code,
                "url": response.url,
                "headers": dict(response.headers),
            },
        )
        self._debug_write_json(
            "04_issue_report_page_fields.json",
            self._extract_form_fields(response.text),
        )

        login_response = self._submit_login_form(response.url, response.text)
        if self._is_login_page(login_response.text, login_response.url):
            raise ETrakitError("Login form submission did not clear the login page.")

        retry = self.session.get(url, timeout=60)
        return retry

    def login(self) -> None:
        if not self.config.username or not self.config.password:
            raise ETrakitError("Missing ETRAKIT_USERNAME or ETRAKIT_PASSWORD")

        login_page = self._get(self.config.login_url)
        self._debug_write_text("01_login_page.html", login_page.text)
        self._debug_write_json("01_login_page_fields.json", self._extract_form_fields(login_page.text))

        preferred_mode = self.config.login_mode if self.config.login_mode in {"public", "contractor"} else "public"
        login_response = self._submit_login_form(login_page.url, login_page.text, preferred_mode=preferred_mode)
        invalid_match = re.search(
            r"Invalid (?:Public )?Login|Invalid Contractor Login",
            login_response.text,
            flags=re.IGNORECASE,
        )
        if self._is_login_page(login_response.text, login_response.url):
            if (
                invalid_match
                and invalid_match.group(0).lower() == "invalid public login"
                and self.config.login_mode == "auto"
            ):
                login_response = self._submit_login_form(
                    login_page.url,
                    login_page.text,
                    preferred_mode="contractor",
                )
                if not self._is_login_page(login_response.text, login_response.url):
                    invalid_match = None
            if invalid_match:
                raise ETrakitError(f"Login rejected by site: {invalid_match.group(0)}")
            raise ETrakitError("Login form submission did not clear the login page.")
        if invalid_match:
            raise ETrakitError(f"Login rejected by site: {invalid_match.group(0)}")

        dashboard_page = self.session.get(self.config.dashboard_url, timeout=60)
        self._debug_write_text("03_dashboard_after_login.html", dashboard_page.text)
        self._debug_write_json(
            "03_dashboard_after_login_meta.json",
            {
                "status_code": dashboard_page.status_code,
                "url": dashboard_page.url,
                "headers": dict(dashboard_page.headers),
            },
        )

        if dashboard_page.status_code >= 400:
            raise ETrakitError(
                f"Dashboard failed after login: {dashboard_page.status_code} for {dashboard_page.url}"
            )

        if self._is_login_page(dashboard_page.text, dashboard_page.url):
            raise ETrakitError(
                "Login may have failed. Dashboard still returns the login form."
            )

    def search_permits_by_issued_date(self, issued_date_iso: str) -> List[str]:
        links = self.fetch_issued_report_for_date(issued_date_iso)
        if links:
            return links

        self._debug_write_json(
            "07_report_fallback.json",
            {"reason": "issue report returned zero links; using fallback permit search"},
        )
        return self._search_permits_by_issued_date_fallback(issued_date_iso)

    def search_permit_by_number(self, permit_number: str) -> List[str]:
        search_page = self._ensure_logged_in_for_url(self.config.permit_search_url)
        self._debug_write_text("11_building_search_page.html", search_page.text)
        self._debug_write_json("11_building_search_page_fields.json", self._extract_form_fields(search_page.text))

        search_by_field, search_operator_field, search_text_field, search_button_field = (
            self._permit_search_field_names_for_html(search_page.text)
        )
        if not search_by_field or not search_operator_field or not search_text_field or not search_button_field:
            raise ETrakitError("Could not locate permit-number search fields in search page HTML.")

        payload = self._parse_hidden_inputs(search_page.text)
        search_by_value = self._match_select_option_value(
            search_page.text,
            search_by_field,
            self.config.permit_number_search_value,
        )
        search_operator_value = self._match_select_option_value(
            search_page.text,
            search_operator_field,
            self.config.search_equals_value,
        )
        if not search_by_value:
            raise ETrakitError("Could not match permit-number search option in search page.")
        if not search_operator_value:
            raise ETrakitError("Could not match search operator option for exact permit-number lookup.")

        payload[search_by_field] = search_by_value
        payload[search_operator_field] = search_operator_value
        payload[search_text_field] = permit_number
        payload["__EVENTTARGET"] = search_button_field
        payload["__EVENTARGUMENT"] = ""
        payload[search_button_field] = "Search"

        self._debug_write_json(
            "12_building_search_post_payload.json",
            {
                "permit_number": permit_number,
                "payload_keys": sorted(payload.keys()),
                "search_by_field": search_by_field,
                "search_by_value": search_by_value,
                "search_operator_field": search_operator_field,
                "search_operator_value": search_operator_value,
                "search_text_field": search_text_field,
                "search_button_field": search_button_field,
            },
        )

        response = self.session.post(
            self.config.permit_search_url,
            data=payload,
            headers=self._browser_headers(self.config.permit_search_url, referer=search_page.url),
            timeout=60,
        )
        self._debug_write_text("13_building_search_results.html", response.text)
        self._debug_write_json(
            "13_building_search_results_meta.json",
            {
                "status_code": response.status_code,
                "url": response.url,
                "headers": dict(response.headers),
            },
        )

        if response.status_code >= 400:
            raise ETrakitError(
                f"Permit-number search failed: {response.status_code} for {response.url}"
            )

        links = self._extract_permit_links_from_search_results(response.text)
        if not links and "ActivityNo=" in response.url:
            links = [response.url]
        permit_token = f"ACTIVITYNO={permit_number.upper()}"
        matched_links = [link for link in links if permit_token in link.upper()]
        if matched_links:
            return matched_links

        direct_url = urljoin(
            self.config.base_url,
            f"Search/Permit.aspx?ActivityNo={permit_number.upper()}",
        )
        direct_response = self._ensure_logged_in_for_url(direct_url)
        if direct_response.status_code >= 400:
            return []

        extracted = self._extract_permit_number(direct_url, direct_response.text, [])
        if extracted.upper() == permit_number.upper():
            self._debug_write_json(
                "13b_building_search_direct_fallback.json",
                {"permit_number": permit_number, "matched_url": direct_url},
            )
            return [direct_url]

        return []

    def fetch_issued_report_for_date(self, issued_date_iso: str) -> List[str]:
        try:
            dt = datetime.strptime(issued_date_iso, "%Y-%m-%d")
        except ValueError as exc:
            raise ETrakitError(f"Invalid issued date: {issued_date_iso}") from exc

        # eTRAKiT does not reliably accept a same-day from/to range.
        # To ask for one logical target day, we submit a two-date window:
        # from = target day, to = target day + 1.
        report_end_dt = dt + timedelta(days=1)
        start_date_text = dt.strftime("%m/%d/%Y")
        end_date_text = report_end_dt.strftime("%m/%d/%Y")

        report_page = self._ensure_logged_in_for_url(self.config.issue_report_url)

        if report_page.status_code >= 400:
            raise ETrakitError(
                f"Failed to load issue report page: {report_page.status_code} for {report_page.url}"
            )

        self._debug_write_text("04_issue_report_page_raw.html", report_page.text)
        self._debug_write_json(
            "04_issue_report_page_meta.json",
            {
                "status_code": report_page.status_code,
                "url": report_page.url,
                "headers": dict(report_page.headers),
            },
        )
        self._debug_write_json(
            "04_issue_report_page_fields.json",
            self._extract_form_fields(report_page.text),
        )

        report_date_type_field, report_start_field, report_end_field, report_run_button_field = (
            self._report_field_names_for_html(report_page.text)
        )
        if not report_start_field or not report_end_field or not report_run_button_field:
            raise ETrakitError("Could not locate issue report form fields in report page HTML.")

        payload = self._parse_hidden_inputs(report_page.text)
        if report_date_type_field:
            payload[report_date_type_field] = "ISSUED"
        self._set_telerik_date_payload(payload, report_start_field, dt)
        self._set_telerik_date_payload(payload, report_end_field, report_end_dt)
        payload[report_run_button_field] = "View Report"

        self._debug_write_json(
            "05_issue_report_post_payload.json",
            {
                "issue_report_url": self.config.issue_report_url,
                "issued_date_iso": issued_date_iso,
                "issued_start_formatted": start_date_text,
                "issued_end_formatted": end_date_text,
                "payload_keys": sorted(payload.keys()),
                "report_date_type_field": report_date_type_field,
                "report_start_field": report_start_field,
                "report_end_field": report_end_field,
                "report_run_button_field": report_run_button_field,
            },
        )

        response = self.session.post(
            self.config.issue_report_url,
            data=payload,
            headers=self._browser_headers(self.config.issue_report_url, referer=report_page.url),
            timeout=60,
        )

        self._debug_write_text("06_issue_report_results_raw.html", response.text)
        self._debug_write_json(
            "06_issue_report_results_meta.json",
            {
                "status_code": response.status_code,
                "url": response.url,
                "headers": dict(response.headers),
            },
        )

        if response.status_code >= 400:
            raise ETrakitError(
                f"Issue report POST failed: {response.status_code} for {response.url}"
            )

        links = self._fetch_all_issue_report_links(
            initial_html=response.text,
            report_url=self.config.issue_report_url,
            referer=report_page.url,
        )
        self._debug_write_json(
            "06_issue_report_links.json",
            {"count": len(links), "links": links},
        )
        return links

    def _fetch_all_issue_report_links(
        self,
        initial_html: str,
        report_url: str,
        referer: str,
    ) -> List[str]:
        links = self._extract_permit_links_from_report(initial_html)
        pending_pages = self._extract_report_page_postbacks(initial_html)
        seen_pages = set()
        page_fetches = []
        current_html = initial_html

        while pending_pages:
            event_target, event_argument = pending_pages.pop(0)
            page_key = (event_target, event_argument)
            if page_key in seen_pages:
                continue

            seen_pages.add(page_key)
            payload = self._parse_hidden_inputs(current_html)
            payload["__EVENTTARGET"] = event_target
            payload["__EVENTARGUMENT"] = event_argument

            response = self.session.post(
                report_url,
                data=payload,
                headers=self._browser_headers(report_url, referer=referer),
                timeout=60,
            )

            if response.status_code >= 400:
                raise ETrakitError(
                    f"Issue report paging failed: {response.status_code} for {response.url}"
                )

            current_html = response.text
            links.extend(self._extract_permit_links_from_report(current_html))
            page_fetches.append(
                {
                    "event_target": event_target,
                    "event_argument": event_argument,
                    "status_code": response.status_code,
                    "url": response.url,
                }
            )

            for next_page in self._extract_report_page_postbacks(current_html):
                if next_page not in seen_pages and next_page not in pending_pages:
                    pending_pages.append(next_page)

        links = self._dedupe(links)
        self._debug_write_json(
            "06_issue_report_paging.json",
            {
                "pages_fetched": page_fetches,
                "total_pages_followed": len(page_fetches) + 1,
                "total_links": len(links),
            },
        )
        return links

    def _extract_permit_links_from_report(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: List[str] = []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "ActivityNo=" in href or "Permit.aspx?" in href:
                links.append(urljoin(self.config.base_url, href))

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

    def _extract_report_page_postbacks(self, html: str) -> List[tuple[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        page_postbacks: List[tuple[str, str]] = []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            match = re.search(
                r"__doPostBack\('([^']+)','(Page\$(\d+))'\)",
                href,
                flags=re.IGNORECASE,
            )
            if match and int(match.group(3)) > 1:
                page_postbacks.append((match.group(1), match.group(2)))

        return list(dict.fromkeys(page_postbacks))

    def _search_permits_by_issued_date_fallback(self, issued_date_iso: str) -> List[str]:
        dt = datetime.strptime(issued_date_iso, "%Y-%m-%d")
        search_page = self._ensure_logged_in_for_url(self.config.permit_search_url)
        self._debug_write_text("08_search_page.html", search_page.text)
        self._debug_write_json("08_search_page_fields.json", self._extract_form_fields(search_page.text))

        payload = self._parse_hidden_inputs(search_page.text)
        start_date_text = dt.strftime("%m/%d/%Y")
        end_date_text = (dt + timedelta(days=1)).strftime("%m/%d/%Y")

        payload[self.config.search_by_field] = self.config.issued_search_value
        payload[self.config.issued_start_field] = start_date_text
        payload[self.config.issued_end_field] = end_date_text
        payload[self.config.search_button_field] = "Search"

        self._debug_write_json(
            "09_search_post_payload.json",
            {
                "permit_search_url": self.config.permit_search_url,
                "issued_date_iso": issued_date_iso,
                "issued_start_formatted": start_date_text,
                "issued_end_formatted": end_date_text,
                "payload_keys": sorted(payload.keys()),
                "search_by_field": self.config.search_by_field,
                "issued_start_field": self.config.issued_start_field,
                "issued_end_field": self.config.issued_end_field,
                "search_button_field": self.config.search_button_field,
                "issued_search_value": self.config.issued_search_value,
            },
        )

        response = self.session.post(
            self.config.permit_search_url,
            data=payload,
            headers=self._browser_headers(self.config.permit_search_url, referer=search_page.url),
            timeout=60,
        )
        self._debug_write_text("10_search_results.html", response.text)
        self._debug_write_json(
            "10_search_results_meta.json",
            {
                "status_code": response.status_code,
                "url": response.url,
                "headers": dict(response.headers),
            },
        )

        if response.status_code >= 400:
            raise ETrakitError(
                f"Fallback permit search failed: {response.status_code} for {response.url}"
            )

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
        response = self.session.get(detail_url, timeout=60)
        html = response.text

        permit_number_hint = self._extract_permit_number(detail_url, html, [])
        safe_name = permit_number_hint or "unknown_permit"
        self._debug_write_text(f"permit_{safe_name}.html", html)

        if response.status_code >= 400:
            raise ETrakitError(
                f"Permit detail page failed: {response.status_code} for {response.url}"
            )

        soup = BeautifulSoup(html, "html.parser")
        text_nodes = [normalize_str(t) for t in soup.stripped_strings if normalize_str(t)]
        row_map: Dict[str, str] = {}

        for row in soup.select("div.row"):
            columns = row.select(":scope > div.column")
            if len(columns) < 2:
                continue

            label = normalize_str(columns[0].get_text(" ", strip=True)).rstrip(":")
            value = normalize_str(columns[1].get_text(" ", strip=True))
            if label:
                row_map[label.lower()] = value

        def find_after(label_variants: Iterable[str]) -> str:
            lookup = [normalize_str(v).rstrip(":").lower() for v in label_variants]
            for key in lookup:
                if key in row_map:
                    return row_map[key]

            lookup = {v.lower() for v in label_variants}
            for idx, token in enumerate(text_nodes):
                if token.lower() in lookup:
                    for nxt in text_nodes[idx + 1 : idx + 8]:
                        normalized_nxt = normalize_str(nxt).rstrip(":").lower()
                        if nxt and normalized_nxt not in row_map and nxt.lower() not in lookup:
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
