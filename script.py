# -*- coding: UTF-8 -*-
import hashlib
import json
import os
import random
import re
from datetime import datetime

import clr
clr.AddReference("System")

from Autodesk.Revit.DB import FilteredElementCollector, SectionType, ViewSchedule
from pyrevit import revit, script
from System import Convert
from System.IO import StreamReader, StreamWriter
from System.Net import SecurityProtocolType, ServicePointManager, WebException, WebRequest
from System.Text import Encoding
from System.Threading import Thread


try:
    text_type = unicode
except NameError:
    text_type = str


output = script.get_output()
output.close_others(True)
output.center()
output.set_title("Revit Schedules -> Notion Tables")

doc = revit.doc
config = script.get_config()

try:
    ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12
except:
    pass

OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "revit_notion_sync")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "schedule_sync_preview.json")
NOTION_BASE_URL = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2026-03-11"
MAX_RICH_TEXT_CHARS = 1900
MAX_ERRORS_TO_PRINT = 10
MAX_URLS_TO_PRINT = 5
SKIP_SCHEDULE_NAMES = ["VIEW LIST", "KEYNOTE LEGEND"]
SKIP_SCHEDULE_PREFIXES = ["<REVISION SCHEDULE>"]
TITLE_PROPERTY = "Element ID"
SYNC_KEY_PROPERTY = "Sync Key"
LAST_SYNC_PROPERTY = "Last Sync"
NOTES_PROPERTY = "Notes"
HELPER_PROPERTIES = [TITLE_PROPERTY, SYNC_KEY_PROPERTY, LAST_SYNC_PROPERTY, NOTES_PROPERTY]

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)


def to_text(value):
    if value is None:
        return ""
    try:
        if isinstance(value, text_type):
            return value
    except:
        pass
    try:
        return text_type(value)
    except:
        return str(value)


def clean_text(value):
    text = to_text(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def trim_text(value, limit):
    text = clean_text(value)
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def md_text(value):
    text = clean_text(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize_key(value):
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "column"


def make_unique_name(base_name, used):
    name = trim_text(base_name, 90) or "Column"
    counter = 2
    while name in used:
        suffix = " {0}".format(counter)
        name = trim_text(base_name, 90 - len(suffix)) + suffix
        counter += 1
    used[name] = True
    return name


def sha1_text(value):
    text = to_text(value)
    try:
        raw = text.encode("utf-8")
    except:
        raw = str(text)
    return hashlib.sha1(raw).hexdigest()


def parse_number(value):
    text = clean_text(value)
    if not text:
        return None
    text = text.replace(",", "")
    text = re.sub(r"[^0-9\.\-]", "", text)
    if not text or text in ["-", ".", "-."]:
        return None
    try:
        return float(text)
    except:
        return None


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def today_date():
    return datetime.now().strftime("%Y-%m-%d")


def get_config_value(name, default_value=""):
    try:
        return clean_text(getattr(config, name))
    except:
        return default_value


def get_bool_config(name, default_value):
    value = get_config_value(name, "")
    if not value:
        return default_value
    return value.lower() in ["1", "true", "yes", "enabled", "on"]


def normalize_notion_uuid(value):
    text = clean_text(value)
    if not text:
        return ""

    dashed_match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        text
    )
    if dashed_match:
        return dashed_match.group(1).lower()

    compact_matches = re.findall(r"[0-9a-fA-F]{32}", text)
    if not compact_matches:
        return ""
    compact = compact_matches[-1].lower()
    return "{0}-{1}-{2}-{3}-{4}".format(
        compact[0:8],
        compact[8:12],
        compact[12:16],
        compact[16:20],
        compact[20:32]
    )


def get_notion_settings():
    sync_mode = get_config_value("sync_mode", "dry_run").lower()
    if sync_mode not in ["dry_run", "upsert"]:
        sync_mode = "dry_run"
    raw_parent_page_id = get_config_value("notion_parent_page_id", "")
    return {
        "token": get_config_value("notion_token", ""),
        "parent_page_id": normalize_notion_uuid(raw_parent_page_id),
        "raw_parent_page_id": raw_parent_page_id,
        "version": get_config_value("notion_version", DEFAULT_NOTION_VERSION) or DEFAULT_NOTION_VERSION,
        "sync_mode": sync_mode,
        "auto_create_databases": get_bool_config("auto_create_databases", True),
        "timeout_seconds": 30,
        "max_retries": 4,
    }


def load_schedule_data_source_map():
    raw_map = get_config_value("schedule_data_source_map", "{}")
    try:
        parsed = json.loads(raw_map)
        if isinstance(parsed, dict):
            return parsed
    except:
        pass
    return {}


def save_schedule_data_source_map(schedule_map):
    try:
        setattr(config, "schedule_data_source_map", json_dumps(schedule_map))
        script.save_config()
    except:
        pass


def json_dumps(data):
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except:
        return json.dumps(data, indent=2)


def get_cell_text(schedule, section_type, row_index, column_index):
    try:
        return clean_text(schedule.GetCellText(section_type, row_index, column_index))
    except:
        return ""


def get_schedule_section(schedule, section_type):
    try:
        return schedule.GetTableData().GetSectionData(section_type)
    except:
        return None


def is_skipped_schedule_name(schedule_name):
    normalized_name = clean_text(schedule_name).upper()
    if normalized_name in SKIP_SCHEDULE_NAMES:
        return True
    for prefix in SKIP_SCHEDULE_PREFIXES:
        if normalized_name.startswith(prefix):
            return True
    return False


def is_skipped_schedule(schedule):
    return is_skipped_schedule_name(schedule.Name)


def get_field_names(schedule):
    names = []
    try:
        definition = schedule.Definition
        field_ids = list(definition.GetFieldOrder())
        for field_id in field_ids:
            try:
                field = definition.GetField(field_id)
                if hasattr(field, "IsHidden") and field.IsHidden:
                    continue
                names.append(clean_text(field.GetName()))
            except:
                pass
    except:
        pass
    return names


def get_header_name(schedule, column_index):
    header = get_schedule_section(schedule, SectionType.Header)
    if not header:
        return ""
    last_value = ""
    try:
        for row_index in range(header.NumberOfRows):
            value = get_cell_text(schedule, SectionType.Header, row_index, column_index)
            if value:
                last_value = value
    except:
        pass
    return last_value


def get_schedule_columns(schedule, body):
    field_names = get_field_names(schedule)
    used_names = {}
    columns = []

    for col_index in range(body.NumberOfColumns):
        source_name = ""
        if col_index < len(field_names):
            source_name = field_names[col_index]
        if not source_name:
            source_name = get_header_name(schedule, col_index)
        if not source_name:
            source_name = "Column {0}".format(col_index + 1)

        display_name = make_unique_name(source_name, used_names)
        columns.append({
            "key": normalize_key(display_name),
            "name": display_name,
            "source_name": source_name,
            "column_index": col_index,
        })
    return columns


def detect_row_type(cell_values):
    joined = " ".join(cell_values).upper()
    non_empty = [value for value in cell_values if value]
    if not non_empty:
        return "blank"
    if "GRANDTOTAL" in joined or "GRAND TOTAL" in joined:
        return "total"
    if "SUBTOTAL" in joined or joined.strip() == "TOTAL":
        return "total"
    return "data"


def find_preferred_row_identity(row_cells):
    preferred_names = [
        "notion_sync_key",
        "notion sync key",
        "uniqueid",
        "unique id",
        "element id",
        "element_id",
        "id",
    ]
    for cell in row_cells:
        candidates = [
            normalize_key(cell["column_name"]),
            clean_text(cell["column_name"]).lower(),
        ]
        for preferred_name in preferred_names:
            if preferred_name in candidates and cell["text"]:
                return clean_text(cell["text"])
    return ""


def first_meaningful_value(row_cells):
    for cell in row_cells:
        if cell["text"]:
            return cell["text"]
    return ""


def build_schedule_identity(schedule):
    unique_id = clean_text(getattr(schedule, "UniqueId", ""))
    if unique_id:
        return unique_id
    return clean_text(schedule.Id.IntegerValue)


def build_legacy_row_key(schedule, row_index, row_cells):
    values = []
    for cell in row_cells:
        values.append(cell["text"])
    fingerprint = sha1_text("|".join(values))
    return "{0}::{1}::{2}".format(
        schedule.Id.IntegerValue,
        row_index + 1,
        fingerprint
    )


def build_row_key(schedule, row_index, row_cells):
    schedule_identity = build_schedule_identity(schedule)
    preferred_identity = find_preferred_row_identity(row_cells)
    if preferred_identity:
        return "{0}::identity::{1}".format(
            schedule_identity,
            trim_text(preferred_identity, MAX_RICH_TEXT_CHARS - len(schedule_identity) - 12)
        )
    return "{0}::row::{1}".format(schedule_identity, row_index + 1)


def build_legacy_row_key_prefix(schedule, row_index):
    return "{0}::{1}::".format(schedule.Id.IntegerValue, row_index + 1)


def build_title_value(schedule, row_index, row_cells, sync_key):
    return sha1_text(sync_key)[:12]


def extract_schedule(schedule):
    body = get_schedule_section(schedule, SectionType.Body)
    if not body:
        return None

    columns = get_schedule_columns(schedule, body)
    rows = []

    for row_index in range(body.NumberOfRows):
        row_cells = []
        cell_values = []
        for column in columns:
            text = get_cell_text(schedule, SectionType.Body, row_index, column["column_index"])
            cell_values.append(text)
            row_cells.append({
                "column_name": column["name"],
                "text": text,
            })

        row_type = detect_row_type(cell_values)
        if row_type in ("blank", "total"):
            continue

        column_names = [col["source_name"].strip().lower() for col in columns]
        cell_texts = [c.strip().lower() for c in cell_values]
        if column_names == cell_texts:
            continue

        sync_key = build_row_key(schedule, row_index, row_cells)
        legacy_sync_key = build_legacy_row_key(schedule, row_index, row_cells)
        rows.append({
            "element_id": build_title_value(schedule, row_index, row_cells, sync_key),
            "sync_key": sync_key,
            "legacy_sync_key": legacy_sync_key,
            "legacy_sync_key_prefix": build_legacy_row_key_prefix(schedule, row_index),
            "row_index": row_index + 1,
            "row_type": row_type,
            "cells": row_cells,
        })

    return {
        "schedule_id": schedule.Id.IntegerValue,
        "schedule_unique_id": clean_text(getattr(schedule, "UniqueId", "")),
        "name": clean_text(schedule.Name),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "body_rows": body.NumberOfRows,
        "body_columns": body.NumberOfColumns,
    }


def extract_schedules():
    detected = []
    skipped = []
    ignored_sync_names = []
    extracted = []

    schedules = FilteredElementCollector(doc).OfClass(ViewSchedule).ToElements()
    for schedule in schedules:
        try:
            if schedule.IsTemplate:
                continue
            schedule_name = clean_text(schedule.Name)
            if not schedule_name:
                skipped.append("<blank schedule name>")
                continue
            detected.append(schedule_name)
            if is_skipped_schedule(schedule):
                skipped.append(schedule_name)
                ignored_sync_names.append(schedule_name)
                continue
            schedule_data = extract_schedule(schedule)
            if schedule_data:
                if schedule_data["row_count"] <= 0:
                    skipped.append("{0} (no data rows)".format(schedule_name))
                    ignored_sync_names.append(schedule_name)
                    continue
                extracted.append(schedule_data)
        except Exception as ex:
            skipped.append("{0} (error: {1})".format(clean_text(schedule.Name), clean_text(ex)))

    extracted = sorted(extracted, key=lambda item: item["name"].upper())
    detected = sorted(detected, key=lambda item: item.upper())
    skipped = sorted(skipped, key=lambda item: item.upper())
    ignored_sync_names = sorted(ignored_sync_names, key=lambda item: item.upper())
    return detected, skipped, extracted, ignored_sync_names


def infer_column_property_type(schedule_data, column_name):
    # values = []
    # for row in schedule_data["rows"]:
    #     for cell in row["cells"]:
    #         if cell["column_name"] == column_name and clean_text(cell["text"]):
    #             values.append(cell["text"])
    # if not values:
    return "rich_text"
    # for value in values:
    #     if parse_number(value) is None:
    #         return "rich_text"
    # return "number"


def schedule_schema_properties(schedule_data):
    # Data columns first, then Notes and Last Sync at the end
    properties = {
        TITLE_PROPERTY: {"title": {}},
        SYNC_KEY_PROPERTY: {"rich_text": {}},
    }

    for column in schedule_data["columns"]:
        prop_name = column["name"]
        if prop_name in [TITLE_PROPERTY, SYNC_KEY_PROPERTY, LAST_SYNC_PROPERTY, NOTES_PROPERTY]:
            continue
        prop_type = infer_column_property_type(schedule_data, prop_name)
        properties[prop_name] = {"number": {}} if prop_type == "number" else {"rich_text": {}}

    # Notes and Last Sync always last
    properties[NOTES_PROPERTY] = {"rich_text": {}}
    properties[LAST_SYNC_PROPERTY] = {"date": {}}
    return properties


def write_debug_json(schedules, detected, skipped):
    payload = {
        "schema_version": 2,
        "exported_at": now_iso(),
        "revit": {
            "document_title": clean_text(doc.Title),
            "document_path": clean_text(getattr(doc, "PathName", "")),
        },
        "detected_schedules": detected,
        "skipped_schedules": skipped,
        "schedules": schedules,
    }
    writer = StreamWriter(OUTPUT_FILE, False, Encoding.UTF8)
    writer.Write(json_dumps(payload))
    writer.Close()


class NotionResult(object):
    def __init__(self, status, text, data, headers):
        self.status = status
        self.text = text
        self.data = data
        self.headers = headers


class NotionClient(object):
    def __init__(self, token, version, timeout_seconds, max_retries):
        self.token = token
        self.version = version
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_count = 0

    def request(self, method, path, body=None):
        retry_statuses = [429, 409, 500, 502, 503, 504]
        last_result = None
        for attempt in range(self.max_retries + 1):
            result = self._request_once(method, path, body)
            if result.status not in retry_statuses:
                return result
            last_result = result
            if attempt >= self.max_retries:
                break
            self.retry_count += 1
            Thread.Sleep(int(self._retry_seconds(result, attempt) * 1000))
        return last_result

    def _retry_seconds(self, result, attempt):
        retry_after = ""
        try:
            retry_after = result.headers["Retry-After"]
        except:
            pass
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except:
                pass
        return min(2 ** attempt, 12) + random.random()

    def _request_once(self, method, path, body=None):
        request = WebRequest.Create(NOTION_BASE_URL + path)
        request.Method = method
        try:
            request.KeepAlive = False
        except:
            pass
        request.Accept = "application/json"
        request.ContentType = "application/json"
        request.Timeout = int(self.timeout_seconds * 1000)
        request.Headers.Add("Authorization", "Bearer " + self.token)
        request.Headers.Add("Notion-Version", self.version)

        if body is not None:
            body_text = json_dumps(body)
            body_bytes = Encoding.UTF8.GetBytes(body_text)
            request.ContentLength = body_bytes.Length
            stream = request.GetRequestStream()
            stream.Write(body_bytes, 0, body_bytes.Length)
            stream.Close()

        try:
            response = request.GetResponse()
            status = Convert.ToInt32(response.StatusCode)
            text = self._read_response_text(response)
            headers = response.Headers
            response.Close()
            return NotionResult(status, text, self._parse_json(text), headers)
        except WebException as ex:
            response = ex.Response
            if response:
                status = Convert.ToInt32(response.StatusCode)
                text = self._read_response_text(response)
                headers = response.Headers
                response.Close()
                return NotionResult(status, text, self._parse_json(text), headers)
            return NotionResult(0, clean_text(ex), {}, {})

    def _read_response_text(self, response):
        reader = StreamReader(response.GetResponseStream(), Encoding.UTF8)
        text = reader.ReadToEnd()
        reader.Close()
        return text

    def _parse_json(self, text):
        try:
            return json.loads(text)
        except:
            return {}

    def search_data_sources(self, query):
        return self.request("POST", "/search", {
            "query": query,
            "filter": {
                "property": "object",
                "value": "data_source"
            },
            "page_size": 20
        })

    def retrieve_database(self, database_id):
        return self.request("GET", "/databases/{0}".format(database_id))

    def create_database(self, parent_page_id, title, properties):
        return self.request("POST", "/databases", {
            "parent": {
                "type": "page_id",
                "page_id": parent_page_id
            },
            "title": [
                {
                    "type": "text",
                    "text": {
                        "content": title
                    }
                }
            ],
            "is_inline": True,
            "initial_data_source": {
                "title": [
                    {
                        "type": "text",
                        "text": {
                            "content": title
                        }
                    }
                ],
                "properties": properties
            }
        })

    def retrieve_data_source(self, data_source_id):
        return self.request("GET", "/data_sources/{0}".format(data_source_id))

    def update_data_source_properties(self, data_source_id, properties):
        return self.request("PATCH", "/data_sources/{0}".format(data_source_id), {
            "properties": properties
        })

    def query_by_sync_key(self, data_source_id, sync_key):
        return self.request("POST", "/data_sources/{0}/query".format(data_source_id), {
            "filter": {
                "property": SYNC_KEY_PROPERTY,
                "rich_text": {
                    "equals": sync_key
                }
            },
            "page_size": 3,
            "result_type": "page"
        })

    def query_by_sync_key_prefix(self, data_source_id, sync_key_prefix):
        return self.request("POST", "/data_sources/{0}/query".format(data_source_id), {
            "filter": {
                "property": SYNC_KEY_PROPERTY,
                "rich_text": {
                    "starts_with": sync_key_prefix
                }
            },
            "page_size": 10,
            "result_type": "page"
        })

    def query_data_source_access(self, data_source_id):
        return self.request("POST", "/data_sources/{0}/query".format(data_source_id), {
            "page_size": 1,
            "result_type": "page"
        })

    def create_page(self, data_source_id, properties):
        return self.request("POST", "/pages", {
            "parent": {
                "type": "data_source_id",
                "data_source_id": data_source_id
            },
            "properties": properties
        })

    def update_page(self, page_id, properties):
        return self.request("PATCH", "/pages/{0}".format(page_id), {
            "properties": properties
        })


def plain_title(title_array):
    parts = []
    for item in title_array or []:
        try:
            parts.append(item.get("plain_text", ""))
        except:
            pass
    return clean_text("".join(parts))


def data_source_title(data_source):
    title_value = data_source.get("title", [])
    if isinstance(title_value, list):
        return plain_title(title_value)
    return clean_text(title_value)


def database_parent_page_id(database):
    try:
        parent = database.get("parent", {})
        if parent.get("type") == "page_id":
            return clean_text(parent.get("page_id", ""))
    except:
        pass
    return ""


def get_first_data_source_id(database):
    for source in database.get("data_sources", []) or []:
        try:
            source_id = source.get("id", "")
            if source_id:
                return source_id
        except:
            pass
    return ""


def get_property_type(prop_schema):
    try:
        return prop_schema.get("type", "")
    except:
        return ""


def get_title_property_name(properties):
    for prop_name, prop_schema in properties.items():
        if get_property_type(prop_schema) == "title":
            return prop_name
    return TITLE_PROPERTY


def property_schema(prop_type):
    if prop_type == "number":
        return {"number": {}}
    if prop_type == "date":
        return {"date": {}}
    if prop_type == "title":
        return {"title": {}}
    return {"rich_text": {}}


def find_data_source_for_schedule(client, settings, schedule_name):
    result = client.search_data_sources(schedule_name)
    if result.status != 200:
        return None, "Search failed HTTP {0} {1}".format(result.status, trim_text(result.text, 250))

    matches = []
    for item in result.data.get("results", []) or []:
        title = data_source_title(item)
        if title == schedule_name:
            data_source_id = item.get("id", "")
            if not data_source_id:
                continue
            access = client.query_data_source_access(data_source_id)
            if access.status == 200:
                matches.append(item)

    if len(matches) > 1:
        return None, "Multiple Notion data sources named '{0}' were found. Rename one or clear duplicates before syncing.".format(schedule_name)
    if len(matches) == 1:
        return matches[0], ""
    return None, ""


def ensure_schedule_database(client, settings, schedule_data, schedule_map):
    schema = schedule_schema_properties(schedule_data)

    mapped_id = clean_text(schedule_map.get(schedule_data["name"], ""))
    if mapped_id:
        mapped = client.retrieve_data_source(mapped_id)
        if mapped.status == 200:
            access = client.query_data_source_access(mapped_id)
            if access.status == 200:
                return mapped.data, mapped_id, False, ""
            try:
                del schedule_map[schedule_data["name"]]
                save_schedule_data_source_map(schedule_map)
            except:
                pass
        else:
            try:
                del schedule_map[schedule_data["name"]]
                save_schedule_data_source_map(schedule_map)
            except:
                pass

    existing, error = find_data_source_for_schedule(client, settings, schedule_data["name"])
    if error:
        return None, None, False, error

    if existing:
        data_source_id = existing.get("id", "")
        if not data_source_id:
            return None, None, False, "Data source '{0}' has no API-visible id.".format(schedule_data["name"])
        schedule_map[schedule_data["name"]] = data_source_id
        save_schedule_data_source_map(schedule_map)
        return existing, data_source_id, False, ""

    if settings["sync_mode"] == "dry_run":
        return None, None, True, ""
    if not settings["auto_create_databases"]:
        return None, None, False, "Database '{0}' is missing and auto-create is disabled.".format(schedule_data["name"])

    created = client.create_database(settings["parent_page_id"], schedule_data["name"], schema)
    if created.status != 200:
        return None, None, False, "Create database '{0}' failed HTTP {1} {2}".format(
            schedule_data["name"],
            created.status,
            trim_text(created.text, 250)
        )
    database = created.data
    data_source_id = get_first_data_source_id(database)
    if not data_source_id:
        retrieved = client.retrieve_database(database.get("id", ""))
        if retrieved.status == 200:
            database = retrieved.data
            data_source_id = get_first_data_source_id(database)
    if not data_source_id:
        return database, None, False, "Created database '{0}' but could not find its data source id.".format(schedule_data["name"])
    schedule_map[schedule_data["name"]] = data_source_id
    save_schedule_data_source_map(schedule_map)
    return database, data_source_id, True, ""


def ensure_data_source_schema(client, settings, data_source_id, schedule_data):
    result = client.retrieve_data_source(data_source_id)
    if result.status != 200:
        return None, [], "Could not retrieve data source for '{0}': HTTP {1} {2}".format(
            schedule_data["name"],
            result.status,
            trim_text(result.text, 250)
        )

    data_source = result.data
    existing_properties = data_source.get("properties", {})
    desired = schedule_schema_properties(schedule_data)
    title_property = get_title_property_name(existing_properties)
    if title_property != TITLE_PROPERTY:
        if TITLE_PROPERTY in desired:
            desired[TITLE_PROPERTY] = {"rich_text": {}}
        if title_property not in desired:
            desired[title_property] = {"title": {}}
    missing = []
    for prop_name, prop_schema in desired.items():
        if prop_name not in existing_properties:
            prop_type = "rich_text"
            try:
                for key in prop_schema.keys():
                    prop_type = key
                    break
            except:
                pass
            missing.append((prop_name, prop_type))

    if missing and settings["sync_mode"] == "upsert":
        patch_properties = {}
        for prop_name, prop_type in missing:
            patch_properties[prop_name] = property_schema(prop_type)
        update = client.update_data_source_properties(data_source_id, patch_properties)
        if update.status != 200:
            return None, missing, "Could not create properties for '{0}': HTTP {1} {2}".format(
                schedule_data["name"],
                update.status,
                trim_text(update.text, 250)
            )
        data_source = update.data

    return data_source, missing, ""


def rich_text_value(value):
    text = trim_text(value, MAX_RICH_TEXT_CHARS)
    if not text:
        return []
    return [{"text": {"content": text}}]


def notion_value(prop_type, value):
    if prop_type == "title":
        title = trim_text(value, 180) or "Untitled Revit Row"
        return {"title": [{"text": {"content": title}}]}
    if prop_type == "number":
        return {"number": parse_number(value)}
    if prop_type == "date":
        text = clean_text(value)
        return {"date": {"start": text}} if text else {"date": None}
    if prop_type == "select":
        text = trim_text(value, 90)
        return {"select": {"name": text}} if text else {"select": None}
    return {"rich_text": rich_text_value(value)}


def build_row_properties(row, data_source_schema):
    properties = {}
    schema_props = data_source_schema.get("properties", {})
    title_property = get_title_property_name(schema_props)

    if title_property in schema_props:
        properties[title_property] = notion_value(get_property_type(schema_props[title_property]), row["element_id"])
    if TITLE_PROPERTY in schema_props and TITLE_PROPERTY != title_property:
        properties[TITLE_PROPERTY] = notion_value(get_property_type(schema_props[TITLE_PROPERTY]), row["element_id"])
    if SYNC_KEY_PROPERTY in schema_props:
        properties[SYNC_KEY_PROPERTY] = notion_value(get_property_type(schema_props[SYNC_KEY_PROPERTY]), row["sync_key"])
    if LAST_SYNC_PROPERTY in schema_props:
        properties[LAST_SYNC_PROPERTY] = notion_value(get_property_type(schema_props[LAST_SYNC_PROPERTY]), today_date())

    for cell in row["cells"]:
        prop_name = cell["column_name"]
        if prop_name in [NOTES_PROPERTY, SYNC_KEY_PROPERTY, LAST_SYNC_PROPERTY]:
            continue
        if prop_name == TITLE_PROPERTY:
            continue
        if prop_name in schema_props:
            prop_type = get_property_type(schema_props[prop_name])
            properties[prop_name] = notion_value(prop_type, cell["text"])

    return properties


def page_reference(page):
    try:
        return page.get("url", "") or page.get("id", "")
    except:
        return ""


def page_id(page):
    try:
        return page.get("id", "")
    except:
        return ""


def append_duplicate_candidate(summary, schedule_name, row, matches, reason):
    summary["duplicates"] += 1
    refs = []
    for match in matches:
        ref = page_reference(match)
        if ref:
            refs.append(ref)
    if refs:
        summary["duplicate_candidates"].append("{0} row {1}: {2} ({3})".format(
            schedule_name,
            row["row_index"],
            reason,
            ", ".join(refs[:MAX_URLS_TO_PRINT])
        ))
    else:
        summary["duplicate_candidates"].append("{0} row {1}: {2}".format(
            schedule_name,
            row["row_index"],
            reason
        ))


def report_legacy_prefix_candidates(client, data_source_id, schedule_data, row, summary, current_match):
    legacy_prefix = clean_text(row.get("legacy_sync_key_prefix", ""))
    if not legacy_prefix:
        return

    prefix_query = client.query_by_sync_key_prefix(data_source_id, legacy_prefix)
    if prefix_query.status != 200:
        return

    current_id = page_id(current_match)
    matches = []
    for match in prefix_query.data.get("results", []) or []:
        if page_id(match) != current_id:
            matches.append(match)
    if matches:
        append_duplicate_candidate(summary, schedule_data["name"], row, matches, "legacy row-position duplicates still exist")


def find_existing_row_match(client, data_source_id, schedule_data, row, summary):
    query = client.query_by_sync_key(data_source_id, row["sync_key"])
    if query.status != 200:
        return None, "query failed HTTP {0} {1}".format(query.status, trim_text(query.text, 250))

    matches = query.data.get("results", []) or []
    if len(matches) > 1:
        append_duplicate_candidate(summary, schedule_data["name"], row, matches, "duplicate stable Sync Key")
        return None, "duplicate stable Sync Key skipped"
    if len(matches) == 1:
        report_legacy_prefix_candidates(client, data_source_id, schedule_data, row, summary, matches[0])
        return matches[0], ""

    legacy_key = clean_text(row.get("legacy_sync_key", ""))
    if legacy_key and legacy_key != row["sync_key"]:
        legacy_query = client.query_by_sync_key(data_source_id, legacy_key)
        if legacy_query.status != 200:
            return None, "legacy query failed HTTP {0} {1}".format(
                legacy_query.status,
                trim_text(legacy_query.text, 250)
            )
        legacy_matches = legacy_query.data.get("results", []) or []
        if len(legacy_matches) > 1:
            append_duplicate_candidate(summary, schedule_data["name"], row, legacy_matches, "duplicate exact legacy Sync Key")
            return None, "duplicate exact legacy Sync Key skipped"
        if len(legacy_matches) == 1:
            report_legacy_prefix_candidates(client, data_source_id, schedule_data, row, summary, legacy_matches[0])
            return legacy_matches[0], ""

    legacy_prefix = clean_text(row.get("legacy_sync_key_prefix", ""))
    if legacy_prefix:
        prefix_query = client.query_by_sync_key_prefix(data_source_id, legacy_prefix)
        if prefix_query.status != 200:
            return None, "legacy prefix query failed HTTP {0} {1}".format(
                prefix_query.status,
                trim_text(prefix_query.text, 250)
            )
        prefix_matches = prefix_query.data.get("results", []) or []
        if len(prefix_matches) > 1:
            append_duplicate_candidate(summary, schedule_data["name"], row, prefix_matches, "multiple legacy row-position matches")
            return None, "multiple legacy row-position matches skipped"
        if len(prefix_matches) == 1:
            return prefix_matches[0], ""

    return None, ""


def sync_schedule_rows(client, settings, data_source_id, data_source_schema, schedule_data, summary):
    for row in schedule_data["rows"]:
        summary["queried"] += 1
        match, query_error = find_existing_row_match(client, data_source_id, schedule_data, row, summary)
        if query_error:
            if "duplicate" in query_error or "multiple legacy" in query_error:
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
            summary["errors"].append("{0}: {1}: {2}".format(
                schedule_data["name"],
                row["sync_key"],
                query_error
            ))
            continue

        props = build_row_properties(row, data_source_schema)
        if not match:
            if settings["sync_mode"] == "dry_run":
                summary["would_create"] += 1
                continue
            created = client.create_page(data_source_id, props)
            if created.status == 200:
                summary["created"] += 1
                ref = page_reference(created.data)
                if ref and len(summary["refs"]) < MAX_URLS_TO_PRINT:
                    summary["refs"].append(ref)
            else:
                summary["failed"] += 1
                summary["errors"].append("{0}: create row failed HTTP {1} {2}".format(
                    row["sync_key"],
                    created.status,
                    trim_text(created.text, 250)
                ))
        else:
            if settings["sync_mode"] == "dry_run":
                summary["would_update"] += 1
                continue
            updated = client.update_page(page_id(match), props)
            if updated.status == 200:
                summary["updated"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].append("{0}: update row failed HTTP {1} {2}".format(
                    row["sync_key"],
                    updated.status,
                    trim_text(updated.text, 250)
                ))


def prune_ignored_schedule_map(schedule_map, ignored_schedule_names, summary):
    if not ignored_schedule_names:
        return

    changed = False
    seen = {}
    for schedule_name in ignored_schedule_names:
        clean_name = clean_text(schedule_name)
        if not clean_name or clean_name in seen:
            continue
        seen[clean_name] = True
        mapped_id = clean_text(schedule_map.get(clean_name, ""))
        if mapped_id:
            summary["ignored_mapped_schedules"].append((clean_name, mapped_id))
            try:
                del schedule_map[clean_name]
                changed = True
            except:
                pass

    if changed:
        save_schedule_data_source_map(schedule_map)


def sync_to_notion(schedules, settings, ignored_schedule_names=None):
    summary = {
        "databases_found": 0,
        "databases_created": 0,
        "would_create_databases": 0,
        "missing_properties": [],
        "queried": 0,
        "created": 0,
        "updated": 0,
        "would_create": 0,
        "would_update": 0,
        "skipped": 0,
        "failed": 0,
        "duplicates": 0,
        "refs": [],
        "duplicate_candidates": [],
        "ignored_mapped_schedules": [],
        "errors": [],
        "retry_count": 0,
    }
    schedule_map = load_schedule_data_source_map()
    prune_ignored_schedule_map(schedule_map, ignored_schedule_names, summary)

    if not settings["token"] or not settings["parent_page_id"]:
        summary["skipped"] = sum([len(item["rows"]) for item in schedules])
        if settings.get("raw_parent_page_id", "") and not settings["parent_page_id"]:
            summary["errors"].append(
                "Notion parent page ID is invalid. Paste a Notion page URL or the 32-character/UUID page ID, not only the page title."
            )
        else:
            summary["errors"].append("Notion token or parent page ID is missing. Configure with shift-click.")
        return summary

    client = NotionClient(
        settings["token"],
        settings["version"],
        settings["timeout_seconds"],
        settings["max_retries"]
    )

    for schedule_data in schedules:
        database, data_source_id, was_created_or_would_create, db_error = ensure_schedule_database(client, settings, schedule_data, schedule_map)
        if db_error:
            summary["failed"] += len(schedule_data["rows"])
            summary["errors"].append(db_error)
            continue
        if was_created_or_would_create and settings["sync_mode"] == "dry_run":
            summary["would_create_databases"] += 1
            summary["would_create"] += len(schedule_data["rows"])
            summary["missing_properties"].extend([(schedule_data["name"], key) for key in schedule_schema_properties(schedule_data).keys()])
            continue
        if was_created_or_would_create:
            summary["databases_created"] += 1
        else:
            summary["databases_found"] += 1

        data_source_schema, missing, schema_error = ensure_data_source_schema(client, settings, data_source_id, schedule_data)
        for item in missing:
            summary["missing_properties"].append((schedule_data["name"], item[0]))
        if schema_error:
            summary["failed"] += len(schedule_data["rows"])
            summary["errors"].append(schema_error)
            continue
        if settings["sync_mode"] == "dry_run" and missing:
            summary["would_create"] += len(schedule_data["rows"])
            summary["errors"].append(
                "{0}: dry-run cannot query/update until missing Sync Key/schema fields exist; rows counted as would-create.".format(schedule_data["name"])
            )
            continue

        sync_schedule_rows(client, settings, data_source_id, data_source_schema, schedule_data, summary)

    summary["retry_count"] = client.retry_count
    return summary


def print_extraction_summary(detected, skipped, schedules):
    output.print_md("## Revit Schedule Extraction")
    output.print_md("Detected schedules: **{0}**".format(len(detected)))
    for schedule_name in detected:
        output.print_md("- {0}".format(md_text(schedule_name)))
    output.print_md("Skipped schedules: **{0}**".format(len(skipped)))
    for schedule_name in skipped:
        output.print_md("- {0}".format(md_text(schedule_name)))
    output.print_md("Extracted schedules: **{0}**".format(len(schedules)))
    for schedule_data in schedules:
        output.print_md("- {0}: {1} rows x {2} columns".format(
            md_text(schedule_data["name"]),
            schedule_data["row_count"],
            schedule_data["body_columns"]
        ))


def print_notion_summary(settings, summary):
    output.print_md("---")
    output.print_md("## Notion Per-Schedule Table Sync")
    output.print_md("Mode: **{0}**".format(settings["sync_mode"]))
    output.print_md("Token configured: **{0}**".format("yes" if settings["token"] else "no"))
    output.print_md("Parent page ID configured: **{0}**".format("yes" if settings["parent_page_id"] else "no"))
    output.print_md("Auto-create schedule databases: **{0}**".format("yes" if settings["auto_create_databases"] else "no"))
    output.print_md("Databases found: **{0}**".format(summary["databases_found"]))
    output.print_md("Databases created: **{0}**".format(summary["databases_created"]))
    output.print_md("Would create databases: **{0}**".format(summary["would_create_databases"]))
    output.print_md("Queried rows: **{0}**".format(summary["queried"]))
    output.print_md("Would create rows: **{0}**".format(summary["would_create"]))
    output.print_md("Would update rows: **{0}**".format(summary["would_update"]))
    output.print_md("Created rows: **{0}**".format(summary["created"]))
    output.print_md("Updated rows: **{0}**".format(summary["updated"]))
    output.print_md("Skipped rows: **{0}**".format(summary["skipped"]))
    output.print_md("Failed rows: **{0}**".format(summary["failed"]))
    output.print_md("Duplicate keys: **{0}**".format(summary["duplicates"]))
    output.print_md("Rate-limit/server retries: **{0}**".format(summary["retry_count"]))

    if summary["missing_properties"]:
        output.print_md("### Missing/created properties")
        for schedule_name, prop_name in summary["missing_properties"][:30]:
            output.print_md("- {0}: {1}".format(md_text(schedule_name), md_text(prop_name)))
    if summary["refs"]:
        output.print_md("### First created pages")
        for ref in summary["refs"]:
            output.print_md("- {0}".format(ref))
    if summary["ignored_mapped_schedules"]:
        output.print_md("### Ignored mapped non-quantity schedules")
        for schedule_name, mapped_id in summary["ignored_mapped_schedules"][:MAX_ERRORS_TO_PRINT]:
            output.print_md("- {0}: {1}".format(md_text(schedule_name), md_text(mapped_id)))
    if summary["duplicate_candidates"]:
        output.print_md("### Duplicate candidates")
        for duplicate in summary["duplicate_candidates"][:MAX_ERRORS_TO_PRINT]:
            output.print_md("- {0}".format(duplicate))
    if summary["errors"]:
        output.print_md("### Errors")
        for error in summary["errors"][:MAX_ERRORS_TO_PRINT]:
            output.print_md("- {0}".format(error))


def main():
    detected, skipped, schedules, ignored_sync_names = extract_schedules()
    write_debug_json(schedules, detected, skipped)
    print_extraction_summary(detected, skipped, schedules)
    output.print_md("---")
    output.print_md("Debug JSON saved to: `{0}`".format(OUTPUT_FILE))

    settings = get_notion_settings()
    summary = sync_to_notion(schedules, settings, ignored_sync_names)
    print_notion_summary(settings, summary)


main()
