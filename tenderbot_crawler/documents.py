"""Local, evidence-backed extraction of original public GeM bid PDFs.

This module never fetches a URL, scans directories, performs OCR, or uses the
old bootstrap sidecar files. The small memory cache is keyed by the PDF hash.
Callers download and validate size limits before calling :func:`parse_pdf`.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import datetime as dt
import hashlib
import io
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

import pdfplumber

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_CACHE_LIMIT = 4
_EXTRACTION_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.Lock()


class DocumentMismatchError(ValueError):
    """An original PDF names a different bid from the listing being ingested."""

    def __init__(self, expected, observed):
        self.expected = expected
        self.observed = observed
        super().__init__(f"GeM document bid number mismatch: expected {expected}, received {observed}")


def _compact(value):
    return " ".join((value or "").split())


def _english(value):
    # English values are readable even where GeM's legacy Hindi font maps fail.
    value = re.sub(r"\(cid:\d+\)", "", value or "")
    return _compact(re.sub(r"[^\x20-\x7e\n]", "", value)).lstrip(" /?")


def _source_text(value):
    """Keep actual units, mathematical symbols and readable source Unicode."""
    return _compact(re.sub(r"\(cid:\d+\)", "", value or ""))


def _number(value):
    value = _compact(value)
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?", value):
        return None
    number = float(value.replace(",", ""))
    return int(number) if number.is_integer() else number


def _local_date(value):
    try:
        return dt.datetime.strptime(value, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST).isoformat()
    except (TypeError, ValueError):
        return None


def _as_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _official_url(value):
    try:
        parsed = urlsplit(value or "")
        host = parsed.hostname or ""
        return bool(
            parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and (host == "gem.gov.in" or host.endswith(".gem.gov.in"))
        )
    except (TypeError, ValueError):
        return False


def _extract_bytes(content, digest):
    with _CACHE_LOCK:
        cached = _EXTRACTION_CACHE.get(digest)
        if cached is not None:
            _EXTRACTION_CACHE.move_to_end(digest)
            return deepcopy(cached)
    pages, tables, links = [], [], []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                pages.append(page.extract_text() or "")
                tables.append({"page": index, "tables": page.extract_tables()})
                for annotation in page.hyperlinks:
                    if not _official_url(annotation.get("uri")):
                        continue
                    try:
                        label = _english(page.crop((annotation["x0"], annotation["top"], annotation["x1"], annotation["bottom"])).extract_text())
                    except (KeyError, TypeError, ValueError):
                        label = ""
                    links.append({"url": annotation["uri"], "page": index, "label": label})
    except Exception as error:
        raise ValueError("Cannot read the supplied PDF: " + type(error).__name__) from error
    result = {"pages": pages, "tables": tables, "links": links}
    with _CACHE_LOCK:
        _EXTRACTION_CACHE[digest] = deepcopy(result)
        _EXTRACTION_CACHE.move_to_end(digest)
        while len(_EXTRACTION_CACHE) > _CACHE_LIMIT:
            _EXTRACTION_CACHE.popitem(last=False)
    return result


def _read_pdf(pdf_path):
    content = Path(pdf_path).read_bytes()
    if not content.startswith(b"%PDF-"):
        raise ValueError("The supplied document is not a PDF (%PDF- header missing)")
    digest = hashlib.sha256(content).hexdigest()
    return digest, _extract_bytes(content, digest)


def extract_pdf_links(pdf_path):
    """Return observed HTTPS GeM links with one-based pages and visible labels.

    No link is fetched. Returned objects are independent of the hash cache.
    """
    _, extracted = _read_pdf(pdf_path)
    return extracted["links"]


def _base_detail(record, fetched_at, source_url, digest, page_count):
    fields = dict.fromkeys((
        "tender_id", "opening_at", "closing_at", "ministry", "department",
        "organisation", "office", "buyer_address", "buyer_email", "quantity",
        "tender_value", "emd_fee", "emd_required", "emd_exemption",
        "bidder_turnover_lakhs", "oem_turnover_lakhs", "experience_years",
        "mse_relaxation", "startup_relaxation", "seller_documents", "tender_type",
        "epbg_percentage", "epbg_duration_months", "offer_validity",
        "estimated_bid_value", "description", "contract_period",
    ))
    fields.update({
        "id": str(record["id"]), "source_document_url": source_url,
        "source_document_fetched_at": fetched_at,
        "detail_status": "parsed", "detailLoaded": True,
        "document_sha256": digest, "document_pages": page_count,
        "eligibility": [], "consignees": [], "technical_specifications": [],
        "buyer_added_terms": [],
        "evidence": [], "documents": [],
        "detail_warning": "These fields were extracted from the recorded original GeM PDF. Check the current notice, linked specifications, ATC and corrigenda before bidding.",
    })
    if source_url:
        fields["documents"].append({"name": "Original GeM bid document", "url": source_url})
    return fields


def _parse_tables(fields, extracted):
    source_url = fields["source_document_url"]
    service_group = None

    def record(field, value, page, quote):
        if value is not None:
            fields[field] = value
            fields["evidence"].append({"field": field, "page": page, "quote": quote, "source_url": source_url})

    def eligible(text, page):
        fields["eligibility"].append(f"{text} (GeM bid PDF, p. {page})")

    for page_data in extracted["tables"]:
        page = page_data["page"]
        for table in page_data["tables"]:
            address_header = next((i for i, row in enumerate(table) if len(row) >= 5 and any("Address" in _compact(c) for c in row if c)), None)
            if address_header is not None:
                headings = [_english(c) for c in table[address_header]]
                for row in table[address_header + 1:]:
                    if len(row) < 5 or not row[2]:
                        continue
                    item = {
                        "page": page, "consignee": _compact(row[1]),
                        "address": _compact(row[2]), "quantity": _number(row[3]),
                        "quantity_label": headings[3].lstrip("/"),
                        "delivery_days": _number(row[4]) if "Delivery" in headings[4] else None,
                    }
                    if "Additional" in headings[4]:
                        item["additional_requirements"] = _compact(row[4])
                    fields["consignees"].append(item)
                    fields["evidence"].append({"field": "consignees", "page": page, "quote": " | ".join(headings) + ": " + " | ".join(_compact(v) for v in row), "source_url": source_url})
                continue
            # Services use Specification / Values rather than the goods'
            # three-column Specification / Specification Name / Bid Requirement.
            service_header = next((i for i, row in enumerate(table) if len(row) == 2 and _english(row[0]).casefold() == "specification" and _english(row[1]).casefold() == "values"), None)
            if service_header is not None:
                parameters = []
                for row in table[service_header + 1:]:
                    if len(row) != 2:
                        continue
                    if row[0] and not row[1]:
                        service_group = _english(row[0]) or service_group
                    elif row[0] and row[1]:
                        parameters.append({"group": service_group, "parameter": _source_text(row[0]), "value": _source_text(row[1])})
                if parameters:
                    fields["technical_specifications"].append({"page": page, "parameters": parameters})
                    fields["evidence"].append({"field": "technical_specifications", "page": page, "quote": " | ".join(p["parameter"] + ": " + p["value"] for p in parameters), "source_url": source_url})
                continue
            specification_header = next((i for i, row in enumerate(table) if len(row) == 3 and any("Bid Requirement" in _compact(c) for c in row if c)), None)
            if specification_header is not None:
                parameters, group = [], None
                for row in table[specification_header + 1:]:
                    if len(row) != 3:
                        continue
                    group = _source_text(row[0]) or group
                    if row[1] and row[2]:
                        parameters.append({"group": group, "parameter": _source_text(row[1]), "value": _source_text(row[2])})
                if parameters:
                    fields["technical_specifications"].append({"page": page, "parameters": parameters})
                    fields["evidence"].append({"field": "technical_specifications", "page": page, "quote": " | ".join(p["parameter"] + ": " + p["value"] for p in parameters), "source_url": source_url})
                continue

            for row in table:
                if len(row) != 2 or not row[1]:
                    continue
                key, value = _english(row[0]), _compact(row[1])
                quote = key + ": " + value
                if "Bid End Date/Time" in key:
                    record("closing_at", _local_date(value), page, quote)
                elif "Bid Opening Date/Time" in key:
                    record("opening_at", _local_date(value), page, quote)
                elif "Ministry/State Name" in key:
                    record("ministry", value, page, quote)
                elif "Department Name" in key:
                    record("department", value, page, quote)
                elif "Organisation Name" in key:
                    record("organisation", value, page, quote)
                elif "Office Name" in key:
                    record("office", None if re.fullmatch(r"\*+", value) else value, page, quote)
                elif "Contact details of Grievance" in key:
                    email = re.search(r"Buyer Email id:\s*([^\s]+)", value)
                    record("buyer_email", email[1] if email else None, page, quote)
                elif "Total Quantity" in key:
                    record("quantity", _number(value), page, quote)
                elif key.endswith("Item Category"):
                    record("description", value, page, quote)
                elif key.endswith("Contract Period"):
                    record("contract_period", value, page, quote)
                elif "Minimum Average Annual Turnover of the bidder" in key:
                    amount = re.search(r"([\d,.]+)\s*Lakh", value)
                    record("bidder_turnover_lakhs", _number(amount[1]) if amount else None, page, quote)
                    eligible("Minimum average annual bidder turnover (3 years): " + value, page)
                elif "OEM Average Turnover" in key:
                    amount = re.search(r"([\d,.]+)\s*Lakh", value)
                    record("oem_turnover_lakhs", _number(amount[1]) if amount else None, page, quote)
                    eligible("OEM average turnover (last 3 years): " + value, page)
                elif "Years of Past Experience Required" in key:
                    amount = re.search(r"([\d.]+)\s*Year", value)
                    record("experience_years", _number(amount[1]) if amount else None, page, quote)
                    eligible("Past experience required for same/similar service: " + value, page)
                elif "MSE Relaxation" in key:
                    label = key[key.index("MSE Relaxation"):]
                    record("mse_relaxation", label + ": " + value, page, quote)
                    eligible(label + ": " + value, page)
                elif "Startup Relaxation" in key:
                    label = key[key.index("Startup Relaxation"):]
                    record("startup_relaxation", label + ": " + value, page, quote)
                    eligible(label + ": " + value, page)
                elif "Document required from seller" in key:
                    record("seller_documents", value, page, quote)
                    eligible("Documents required from seller: " + value, page)
                elif "Type of Bid" in key:
                    record("tender_type", value, page, quote)
                elif "Bid Offer Validity" in key:
                    record("offer_validity", value, page, quote)
                elif "EMD Amount" in key:
                    amount = _number(value)
                    record("emd_fee", amount, page, quote)
                    record("emd_required", True if amount is not None else None, page, quote)
                elif "ePBG Percentage" in key:
                    record("epbg_percentage", _number(value), page, quote)
                elif "Duration of ePBG required" in key:
                    record("epbg_duration_months", _number(value), page, quote)
                elif "Estimated Bid Value" in key:
                    record("estimated_bid_value", _number(value), page, quote)
                elif "Turnover eligibility criteria" in key or key == "Past Performance":
                    label = "Turnover eligibility criteria" if "Turnover eligibility criteria" in key else "Past performance"
                    eligible(label + ": " + value, page)
                    fields["evidence"].append({"field": "eligibility", "page": page, "quote": quote, "source_url": source_url})

        # 'Required' also appears under ePBG; bind it to the actual EMD heading.
        raw = extracted["pages"][page - 1]
        emd = re.search(r"EMD Detail\s*[^\n]*Required\s+(No|Yes)\b", raw)
        if emd:
            record("emd_required", emd[1] == "Yes", page, "EMD Detail / Required: " + emd[1])
            if emd[1] == "No":
                record("emd_fee", 0, page, "EMD Detail / Required: No (no EMD required)")
        exemption = re.search(r"EMD EXEMPTION:\s*(.*?)(?=\n\(b\)|$)", raw, re.S)
        if exemption:
            value = _english(exemption[1])
            record("emd_exemption", value, page, "EMD EXEMPTION: " + value)
            eligible("EMD exemption: " + value, page)


def _add_buyer_terms(fields, pages):
    """Copy the explicit buyer-added section, stopping at its Disclaimer.

    These are recorded source clauses, not a judgment on their validity or on
    a bidder's eligibility. Unclear/missing section boundaries add no terms.
    """
    active, found_end, segments = False, False, []
    for page_number, raw in enumerate(pages, start=1):
        heading = re.search(r"Buyer Added Bid Specific Terms and Conditions", raw, re.I)
        if heading:
            active = True
            raw = raw[heading.end():]
            # In the bilingual template, the Hindi heading follows its English
            # counterpart. The first numbered clause gives a clear body start.
            clause = re.search(r"(?m)^\s*\d+\.\s+[A-Za-z]", raw)
            if clause:
                raw = raw[clause.start():]
        if not active:
            continue
        disclaimer = re.search(r"(?mi)^(?:[^\n]*?/)?Disclaimer\s*$", raw)
        if disclaimer:
            raw = raw[:disclaimer.start()]
        raw = re.sub(r"(?m)^\s*\d+\s*/\s*\d+\s*$", "", raw)
        text = _source_text(raw)
        if text:
            segments.append({"page": page_number, "text": text})
        if disclaimer:
            found_end = True
            break
    if found_end:
        fields["buyer_added_terms"].extend(segments)
        for segment in segments:
            fields["evidence"].append({"field": "buyer_added_terms", "page": segment["page"], "quote": segment["text"], "source_url": fields["source_document_url"]})


def _add_links(fields, links):
    seen = {fields["source_document_url"]}
    for link in links:
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        lowered = link["url"].lower()
        if "specification" in lowered:
            name = "Buyer technical specification"
        elif "sla_upload_path" in lowered or "/atc" in lowered:
            name = "Buyer additional terms and conditions"
        elif "gtc" in lowered:
            name = "GeM general terms and conditions"
        elif "downloadomppd" in lowered:
            name = "GeM procurement policy order"
        else:
            name = link["label"] if link["label"] and link["label"] != "Download" else "GeM linked source document"
        fields["documents"].append({"name": name, "url": link["url"], "page": link["page"]})


def parse_pdf(pdf_path, record, fetched_at, source_url=None):
    """Parse one original GeM PDF into the site's detail/evidence schema.

    ``record`` is a normalized listing, requiring ``id``. Its ``tender_id`` (or
    ``bid_number`` / ``gem_bid_number``) is checked when supplied. ``source_url``
    explicitly overrides ``record.source_document_url``; neither is synthesized.

    Status is ``parsed`` for an identified GeM bid, ``no_readable_text`` for a
    PDF without extracted text, or ``unrecognized_document`` for other text.
    No OCR is attempted. Unknown fields remain null. A current listing deadline
    is preserved if the original PDF has older/different dates. Corrupt input
    raises ValueError; a bid identity mismatch raises DocumentMismatchError.
    """
    if not isinstance(record, dict) or not record.get("id"):
        raise ValueError("record.id is required to associate PDF evidence")
    url = source_url if source_url is not None else record.get("source_document_url")
    digest, extracted = _read_pdf(pdf_path)
    fields = _base_detail(record, fetched_at, url, digest, len(extracted["pages"]))
    readable = any(re.search(r"[^\W_]", re.sub(r"\(cid:\d+\)", "", page), re.UNICODE) for page in extracted["pages"])
    if not readable:
        fields["detail_status"] = "no_readable_text"
        fields["detail_warning"] = "No readable text was extracted from this PDF. It may require OCR; no tender requirements were inferred."
        return fields

    observed, bid_page = None, None
    for index, page in enumerate(extracted["pages"], start=1):
        match = re.search(r"Bid\s+Number\s*:\s*(GEM/[\w/-]+)", page, re.I)
        if match:
            observed, bid_page = match[1], index
            break
    expected = record.get("tender_id") or record.get("bid_number") or record.get("gem_bid_number")
    if observed and expected and observed.upper() != str(expected).strip().upper():
        raise DocumentMismatchError(str(expected), observed)
    if not observed:
        fields["detail_status"] = "unrecognized_document"
        fields["detail_warning"] = "Readable text was extracted, but this PDF was not identified as an original GeM bid document. No tender requirements were inferred."
        _add_links(fields, extracted["links"])
        return fields

    fields["tender_id"] = observed
    fields["evidence"].append({"field": "tender_id", "page": bid_page, "quote": "Bid Number: " + observed, "source_url": url})
    _parse_tables(fields, extracted)
    _add_buyer_terms(fields, extracted["pages"])
    _add_links(fields, extracted["links"])

    # A known quantity in a real listing is usable even if a service PDF omits
    # its mixed-unit total. Its evidence points to the listing, not a PDF page.
    quantity = record.get("quantity")
    if fields["quantity"] is None and isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
        fields["quantity"] = quantity
        fields["evidence"].append({"field": "quantity", "page": None, "quote": "Public GeM listing quantity: " + str(quantity), "source_url": record.get("listing_url") or record.get("source_url")})

    listing_close, pdf_close = _as_datetime(record.get("closing_at")), _as_datetime(fields["closing_at"])
    if listing_close is not None and pdf_close is not None and listing_close != pdf_close:
        fields["document_closing_at"] = fields["closing_at"]
        fields["document_opening_at"] = fields["opening_at"]
        fields["closing_at"] = record["closing_at"]
        fields["opening_at"] = None
        fields["date_discrepancy"] = {"listing_closing_at": record["closing_at"], "document_closing_at": fields["document_closing_at"], "document_opening_at": fields["document_opening_at"]}
        for evidence in fields["evidence"]:
            if evidence["field"] in ("opening_at", "closing_at"):
                evidence["field"] = "document_" + evidence["field"]
        fields["detail_warning"] = "The original PDF dates differ from the current public GeM listing. The listing closing date is retained. Confirm the latest opening date and any corrigenda on GeM; the original PDF opening date is not presented as current."
    return fields
