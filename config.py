# -*- coding: utf-8 -*-
import re
from pyrevit import forms, script


my_config = script.get_config()


def get_value(name, default_value=""):
    try:
        return getattr(my_config, name)
    except:
        return default_value


def normalize_notion_uuid(value):
    text = (value or "").strip()
    dashed_match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        text
    )
    if dashed_match:
        return dashed_match.group(1).lower()
    compact_matches = re.findall(r"[0-9a-fA-F]{32}", text)
    if not compact_matches:
        return text
    compact = compact_matches[-1].lower()
    return "{0}-{1}-{2}-{3}-{4}".format(
        compact[0:8],
        compact[8:12],
        compact[12:16],
        compact[16:20],
        compact[20:32]
    )


def configure_notion_sync():
    existing_token = get_value("notion_token", "")
    token = forms.ask_for_string(
        default="",
        prompt="Paste Notion token. Leave blank to keep the saved token.",
        title="Notion Sync Config"
    )
    if token:
        setattr(my_config, "notion_token", token.strip())
    elif not existing_token:
        setattr(my_config, "notion_token", "")

    parent_page_id = forms.ask_for_string(
        default=get_value("notion_parent_page_id", ""),
        prompt="Notion parent page URL or page ID. Schedule databases will be created under this page.",
        title="Notion Sync Config"
    )
    if parent_page_id is not None:
        setattr(my_config, "notion_parent_page_id", normalize_notion_uuid(parent_page_id))

    notion_version = forms.ask_for_string(
        default=get_value("notion_version", "2026-03-11"),
        prompt="Notion API version",
        title="Notion Sync Config"
    )
    if notion_version:
        setattr(my_config, "notion_version", notion_version.strip())

    sync_mode = forms.SelectFromList.show(
        ["dry_run", "upsert"],
        title="Sync mode",
        width=300,
        height=160,
        multiselect=False
    )
    if sync_mode:
        setattr(my_config, "sync_mode", sync_mode)

    auto_create = forms.SelectFromList.show(
        ["enabled", "disabled"],
        title="Auto-create missing schedule databases",
        width=420,
        height=160,
        multiselect=False
    )
    if auto_create:
        setattr(my_config, "auto_create_databases", "true" if auto_create == "enabled" else "false")

    script.save_config()
    forms.alert(
        "Notion sync configuration saved. Token is stored in pyRevit config and will not be printed.",
        title="Notion Sync"
    )


if __name__ == "__main__":
    configure_notion_sync()
