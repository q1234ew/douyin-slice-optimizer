from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


XLSX_SUFFIXES = {".xlsx", ".xslx"}


def read_table_rows(path: str | Path, *, preferred_sheets: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Read the first useful tabular sheet from an xlsx workbook."""
    workbook_path = Path(path).expanduser().resolve()
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = _shared_strings(archive)
        sheet_path = _sheet_path(archive, preferred_sheets=preferred_sheets)
        if not sheet_path:
            return []
        rows = _sheet_rows(archive.read(sheet_path), shared_strings)
    return _records_from_rows(rows)


def _sheet_rows(xml: bytes, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(xml)
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", ns):
        values: dict[int, str] = {}
        for cell in row.findall("x:c", ns):
            ref = cell.attrib.get("r", "")
            col_index = _column_index(ref)
            values[col_index] = _cell_value(cell, shared_strings, ns)
        if values:
            max_index = max(values)
            rows.append([values.get(index, "") for index in range(max_index + 1)])
    return rows


def _records_from_rows(rows: list[list[str]]) -> list[dict[str, str]]:
    while rows and not any(str(value).strip() for value in rows[0]):
        rows.pop(0)
    if not rows:
        return []
    header_index = _header_index(rows)
    headers = [str(value).strip() for value in rows[header_index]]
    records: list[dict[str, str]] = []
    for values in rows[header_index + 1 :]:
        if not any(str(value).strip() for value in values):
            continue
        record = {headers[index]: values[index] if index < len(values) else "" for index in range(len(headers)) if headers[index]}
        if record:
            records.append(record)
    return records


def _header_index(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows[:10]):
        non_empty = [str(value).strip() for value in row if str(value).strip()]
        if len(non_empty) >= 2:
            return index
    return 0


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        xml = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for item in root.findall("x:si", ns):
        strings.append("".join(text.text or "" for text in item.findall(".//x:t", ns)))
    return strings


def _sheet_path(archive: zipfile.ZipFile, *, preferred_sheets: tuple[str, ...]) -> str | None:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    workbook_ns = {
        "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rel_ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    rel_targets = {rel.attrib.get("Id"): rel.attrib.get("Target") for rel in rels.findall("rel:Relationship", rel_ns)}
    sheets = workbook.findall(".//x:sheets/x:sheet", workbook_ns)
    selected = None
    for name in preferred_sheets:
        selected = next((sheet for sheet in sheets if sheet.attrib.get("name") == name), None)
        if selected is not None:
            break
    selected = selected if selected is not None else (sheets[0] if sheets else None)
    if selected is None:
        return None
    target = rel_targets.get(selected.attrib.get(f"{{{workbook_ns['r']}}}id"))
    if not target:
        return None
    target = target.lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def _cell_value(cell: ET.Element, shared_strings: list[str], ns: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//x:t", ns))
    value = cell.find("x:v", ns)
    if value is None or value.text is None:
        return ""
    text = value.text
    if cell_type == "s":
        index = int(float(text))
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    return text


def _column_index(ref: str) -> int:
    letters = ""
    for char in ref.upper():
        if not ("A" <= char <= "Z"):
            break
        letters += char
    if not letters:
        return 0
    index = 0
    for char in letters:
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1
