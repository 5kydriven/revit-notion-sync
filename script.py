# -*- coding: UTF-8 -*-
import json
import os
from datetime import datetime
from pyrevit import script, revit, DB
from Autodesk.Revit.DB import (
    FilteredElementCollector,
    BuiltInCategory,
    BuiltInParameter,
    Wall, Floor, FamilyInstance
)

output = script.get_output()
output.close_others(True)
output.center()
output.set_title('BOM Extractor -> Notion')

doc = revit.doc

# -- Output path
OUTPUT_DIR  = os.path.join(os.path.expanduser("~"), "Documents", "revit_notion_sync")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "bom_output.json")
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

# -- Helper: safe parameter reader
def get_param(element, bip):
    try:
        p = element.get_Parameter(bip)
        if p and p.HasValue:
            return p.AsValueString() or p.AsString() or str(p.AsDouble())
    except:
        pass
    return ""

# -- Helper: get level name
def get_level(element):
    try:
        level_id = element.LevelId
        if level_id:
            level = doc.GetElement(level_id)
            if level:
                return level.Name
    except:
        pass
    return "Unknown"

# -- Collectors
bom_items = []

# Walls
output.print_md("### Collecting Walls...")
walls = FilteredElementCollector(doc)\
    .OfClass(Wall)\
    .WhereElementIsNotElementType()\
    .ToElements()

for w in walls:
    try:
        length = w.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
        qty    = round(length.AsDouble() * 0.3048, 2) if length else 0
        bom_items.append({
            "element_id":  w.UniqueId,
            "name":        w.Name,
            "category":    "Walls",
            "family_type": get_param(w, BuiltInParameter.ELEM_FAMILY_AND_TYPE_PARAM),
            "level":       get_level(w),
            "quantity":    qty,
            "unit":        "m",
            "status":      "Not Started",
            "last_sync":   datetime.now().strftime("%Y-%m-%d")
        })
    except:
        pass

output.print_md("Walls collected: **{0}**".format(len(walls)))

# Floors
output.print_md("### Collecting Floors...")
floors = FilteredElementCollector(doc)\
    .OfCategory(BuiltInCategory.OST_Floors)\
    .WhereElementIsNotElementType()\
    .ToElements()

for f in floors:
    try:
        area = f.get_Parameter(BuiltInParameter.HOST_AREA_COMPUTED)
        qty  = round(area.AsDouble() * 0.0929, 2) if area else 0
        bom_items.append({
            "element_id":  f.UniqueId,
            "name":        f.Name,
            "category":    "Floors",
            "family_type": get_param(f, BuiltInParameter.ELEM_FAMILY_AND_TYPE_PARAM),
            "level":       get_level(f),
            "quantity":    qty,
            "unit":        "m2",
            "status":      "Not Started",
            "last_sync":   datetime.now().strftime("%Y-%m-%d")
        })
    except:
        pass

output.print_md("Floors collected: **{0}**".format(len(floors)))

# Doors
output.print_md("### Collecting Doors...")
doors = FilteredElementCollector(doc)\
    .OfCategory(BuiltInCategory.OST_Doors)\
    .WhereElementIsNotElementType()\
    .ToElements()

for d in doors:
    try:
        bom_items.append({
            "element_id":  d.UniqueId,
            "name":        d.Name,
            "category":    "Doors",
            "family_type": get_param(d, BuiltInParameter.ELEM_FAMILY_AND_TYPE_PARAM),
            "level":       get_level(d),
            "quantity":    1,
            "unit":        "ea",
            "status":      "Not Started",
            "last_sync":   datetime.now().strftime("%Y-%m-%d")
        })
    except:
        pass

output.print_md("Doors collected: **{0}**".format(len(doors)))

# Windows
output.print_md("### Collecting Windows...")
windows = FilteredElementCollector(doc)\
    .OfCategory(BuiltInCategory.OST_Windows)\
    .WhereElementIsNotElementType()\
    .ToElements()

for w in windows:
    try:
        bom_items.append({
            "element_id":  w.UniqueId,
            "name":        w.Name,
            "category":    "Windows",
            "family_type": get_param(w, BuiltInParameter.ELEM_FAMILY_AND_TYPE_PARAM),
            "level":       get_level(w),
            "quantity":    1,
            "unit":        "ea",
            "status":      "Not Started",
            "last_sync":   datetime.now().strftime("%Y-%m-%d")
        })
    except:
        pass

output.print_md("Windows collected: **{0}**".format(len(windows)))

# Rooms
output.print_md("### Collecting Rooms...")
rooms = FilteredElementCollector(doc)\
    .OfCategory(BuiltInCategory.OST_Rooms)\
    .WhereElementIsNotElementType()\
    .ToElements()

for r in rooms:
    try:
        area = r.get_Parameter(BuiltInParameter.ROOM_AREA)
        qty  = round(area.AsDouble() * 0.0929, 2) if area else 0
        name_param = r.get_Parameter(BuiltInParameter.ROOM_NAME)
        rname = name_param.AsString() if name_param else r.Name
        bom_items.append({
            "element_id":  r.UniqueId,
            "name":        rname,
            "category":    "Rooms",
            "family_type": "Room",
            "level":       get_level(r),
            "quantity":    qty,
            "unit":        "m2",
            "status":      "Not Started",
            "last_sync":   datetime.now().strftime("%Y-%m-%d")
        })
    except:
        pass

output.print_md("Rooms collected: **{0}**".format(len(rooms)))

# -- Safe unicode encode helper
def safe_str(val):
    if isinstance(val, unicode):
        return val.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    elif isinstance(val, str):
        return val.decode("utf-8", errors="replace")
    return val

def clean_item(item):
    return {k: safe_str(v) if isinstance(v, (str, unicode)) else v
            for k, v in item.items()}

# -- Save JSON
# -- Save JSON using .NET writer (bypasses IronPython unicode bug)
import clr
clr.AddReference("System")
from System.IO import StreamWriter, File
from System.Text import Encoding

def val_to_json(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    # string -- sanitize
    try:
        if isinstance(v, unicode):
            s = v.encode("ascii", errors="replace").decode("ascii")
        else:
            s = v.decode("latin-1", errors="replace").encode("ascii", errors="replace").decode("ascii")
    except:
        s = "?"
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"{0}"'.format(s)

def item_to_json(item):
    pairs = []
    for k, v in item.items():
        pairs.append('    "{0}": {1}'.format(k, val_to_json(v)))
    return "  {\n" + ",\n".join(pairs) + "\n  }"

json_str = "[\n" + ",\n".join([item_to_json(i) for i in bom_items]) + "\n]"

writer = StreamWriter(OUTPUT_FILE, False, Encoding.UTF8)
writer.Write(json_str)
writer.Close()

# -- Summary
output.print_md("---")
output.print_md("## BOM Extraction Complete")
output.print_md("**Total elements:** {0}".format(len(bom_items)))
output.print_md("**Saved to:** {0}".format(OUTPUT_FILE))
output.print_md("---")
output.print_md("### Breakdown")
output.print_md("- Walls: {0}".format(len(walls)))
output.print_md("- Floors: {0}".format(len(floors)))
output.print_md("- Doors: {0}".format(len(doors)))
output.print_md("- Windows: {0}".format(len(windows)))
output.print_md("- Rooms: {0}".format(len(rooms)))