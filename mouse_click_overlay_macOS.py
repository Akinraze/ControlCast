# OBS Studio Mouse Click Overlay
# Windows left/right indicators, ten extra OBS hotkeys, and bounded mouse movement.
# Requires OBS's obspython and a Python runtime compatible with the installed OBS.
# Uses Python's standard library; no web server, pynput or AutoHotkey required.
#
# v1.1.4 safety / persistence changes:
# - Never stores or manually releases an OBS scene-item pointer across timer ticks.
# - Does not call obs_sceneitem_removed (not exposed by the OBS Python module).
# - Resolves the movement target fresh only for the duration of each movement update.
# - Disables movement after the first movement exception instead of flooding OBS's log.
# - Uses a ~60 Hz movement/button timer instead of 100 Hz.
# - Removes OBS properties-button callbacks; OBS 32.2.2 can crash while registering them on startup.
# - Preserves the saved Enable Mouse Movement setting across OBS restarts.
# - Adds a 1.5 second startup grace period before movement begins.

import obspython as obs
import ctypes
import ctypes.util
import time
import json
import os
import re
import subprocess

VERSION = "Mouse_Click_Overlay_macOS_v1_0_0"
POLL_MS = 16  # ~60 Hz; more than enough for OBS animation and much safer than 10 ms
MOVEMENT_STARTUP_DELAY = 1.5  # seconds; lets OBS finish restoring scenes before movement begins

# macOS CoreGraphics mouse input
CG_EVENT_SOURCE_STATE_COMBINED_SESSION = 0
CG_MOUSE_BUTTON_LEFT = 0
CG_MOUSE_BUTTON_RIGHT = 1


class CGPoint(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
    ]


cg = None
cf = None

try:
    cg = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )

    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventCreate.restype = ctypes.c_void_p

    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint

    cg.CGEventSourceButtonState.argtypes = [ctypes.c_int32, ctypes.c_uint32]
    cg.CGEventSourceButtonState.restype = ctypes.c_bool

    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
except Exception:
    cg = None
    cf = None


def log(message):
    print("[Mouse Click Overlay] " + str(message))


def script_description():
    return (
        "<b>%s</b><br><br>"
        "Standalone mouse overlay package with named profiles, JSON import/export, "
        "master enable, field IDs, and self-contained startup diagnostics.<br><br>"
        "<b>Stable field IDs:</b> [01] Left Mouse, [02] Right Mouse, "
        "[03]-[12] Extra Mouse Buttons.<br>"
        "The same IDs are used in this panel, OBS Hotkeys, diagnostics, and profile files.<br>""Function/hotkey names are automatically derived from the selected OBS Source.<br>""Startup self-check runs automatically; PASS is logged silently, warnings/errors open a results window.<br><br>"
        "Turning movement OFF, arranging the mouse, then ON saves that position "
        "as the permanent rest position.<br><br>"
        
        
        
        
        
        "Profile JSON is exported automatically when settings are saved and imported automatically at startup when present.<br>""Diagnostics open in a separate non-blocking macOS results dialog; no OBS text/image source is required.<br><br>""<b>Safety:</b> no Python property-button callbacks are registered."
    ) % VERSION



def field_id(index):
    return "[%02d]" % index


def default_profile_path():
    try:
        folder = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        folder = os.getcwd()
    return os.path.join(folder, "mouse_overlay_profile.json")


def normalize_profile_path(path_value):
    value = (path_value or "").strip()
    return value if value else default_profile_path()


def sanitize_function_name(value, fallback):
    value = (value or "").strip()
    return value if value else fallback


def get_source_by_name_exists(name):
    if not name:
        return False
    src = obs.obs_get_source_by_name(name)
    if src is None:
        return False
    obs.obs_source_release(src)
    return True


def get_scene_by_name_exists(name):
    if not name:
        return False
    src = obs.obs_get_source_by_name(name)
    if src is None:
        return False
    try:
        return obs.obs_scene_from_source(src) is not None
    finally:
        obs.obs_source_release(src)


def hide_all_indicator_sources():
    names = set()
    if left_source:
        names.add(left_source)
    if right_source:
        names.add(right_source)
    for name in extra_sources:
        if name:
            names.add(name)
    for name in names:
        set_source_visibility(name, False)


def binding_array_to_plain_dicts(binding_array):
    result = []
    if binding_array is None:
        return result

    count = obs.obs_data_array_count(binding_array)
    for index in range(count):
        item = obs.obs_data_array_item(binding_array, index)
        if item is None:
            continue
        try:
            result.append({
                "key": obs.obs_data_get_string(item, "key"),
                "shift": bool(obs.obs_data_get_bool(item, "shift")),
                "control": bool(obs.obs_data_get_bool(item, "control")),
                "alt": bool(obs.obs_data_get_bool(item, "alt")),
                "command": bool(obs.obs_data_get_bool(item, "command")),
            })
        finally:
            obs.obs_data_release(item)
    return result


def hotkey_bindings_to_plain(hotkey_id):
    if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
        return []
    arr = obs.obs_hotkey_save(hotkey_id)
    if arr is None:
        return []
    try:
        return binding_array_to_plain_dicts(arr)
    finally:
        obs.obs_data_array_release(arr)


def plain_dicts_to_binding_array(bindings):
    arr = obs.obs_data_array_create()
    for binding in bindings or []:
        item = obs.obs_data_create()
        try:
            obs.obs_data_set_string(item, "key", str(binding.get("key", "")))
            obs.obs_data_set_bool(item, "shift", bool(binding.get("shift", False)))
            obs.obs_data_set_bool(item, "control", bool(binding.get("control", False)))
            obs.obs_data_set_bool(item, "alt", bool(binding.get("alt", False)))
            obs.obs_data_set_bool(item, "command", bool(binding.get("command", False)))
            obs.obs_data_array_push_back(arr, item)
        finally:
            obs.obs_data_release(item)
    return arr


def pretty_key_name(raw_key):
    raw = (raw_key or "").strip()
    if not raw:
        return "Unassigned"

    try:
        if callable(getattr(obs, "obs_key_from_name", None)) and callable(getattr(obs, "obs_key_to_name", None)):
            key_enum = obs.obs_key_from_name(raw)
            display = obs.obs_key_to_name(key_enum)
            if display:
                return str(display)
    except Exception:
        pass

    return raw.replace("OBS_KEY_", "").replace("_", " ").title()


def describe_bindings(bindings):
    if not bindings:
        return "UNASSIGNED"

    labels = []
    for binding in bindings:
        pieces = []
        if binding.get("control"):
            pieces.append("Ctrl")
        if binding.get("shift"):
            pieces.append("Shift")
        if binding.get("alt"):
            pieces.append("Alt")
        if binding.get("command"):
            pieces.append("Command")
        pieces.append(pretty_key_name(binding.get("key")))
        labels.append("+".join(pieces))
    return " / ".join(labels)


def refresh_extra_hotkey_descriptions():
    setter = getattr(obs, "obs_hotkey_set_description", None)
    if not callable(setter):
        return

    for i, hotkey_id in enumerate(extra_hotkeys):
        if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
            continue
        label = extra_functions[i] if i < len(extra_functions) else ("Extra Mouse Button %d" % (i + 1))
        try:
            setter(hotkey_id, "Mouse Overlay %s %s" % (field_id(i + 3), label))
        except Exception as exc:
            log("Could not update hotkey description %s: %s" % (field_id(i + 3), exc))


def collect_self_check():
    errors = []
    warnings = []
    passes = []

    if cg is None or cf is None:
        errors.append("macOS CoreGraphics mouse input API unavailable")
    else:
        passes.append("macOS mouse input API")

    passes.append("Profile: %s" % (profile_name or "Unnamed"))

    if left_source:
        if get_source_by_name_exists(left_source):
            passes.append("%s %s source found: %s" % (field_id(1), left_function, left_source))
        else:
            errors.append("%s source missing: %s" % (field_id(1), left_source))
    else:
        warnings.append("%s %s has no OBS source" % (field_id(1), left_function))

    if right_source:
        if get_source_by_name_exists(right_source):
            passes.append("%s %s source found: %s" % (field_id(2), right_function, right_source))
        else:
            errors.append("%s source missing: %s" % (field_id(2), right_source))
    else:
        warnings.append("%s %s has no OBS source" % (field_id(2), right_function))

    assigned_source_fields = {}
    for i in range(EXTRA_BUTTONS):
        fid = field_id(i + 3)
        name = extra_sources[i]
        func = extra_functions[i]

        if name:
            if get_source_by_name_exists(name):
                passes.append("%s %s source found: %s" % (fid, func, name))
            else:
                errors.append("%s source missing: %s" % (fid, name))

            assigned_source_fields.setdefault(name, []).append(fid)

            bindings = hotkey_bindings_to_plain(extra_hotkeys[i]) if i < len(extra_hotkeys) else []
            if bindings:
                passes.append("%s bound input: %s" % (fid, describe_bindings(bindings)))
            else:
                warnings.append("%s %s has a source but no OBS hotkey binding" % (fid, func))


    for source_name, fields in assigned_source_fields.items():
        if len(fields) > 1:
            warnings.append("Duplicate OBS source '%s' assigned to %s" %
                            (source_name, ", ".join(fields)))

    if movement_enabled:
        if movement_scene and get_scene_by_name_exists(movement_scene):
            passes.append("Movement scene found: %s" % movement_scene)
        else:
            errors.append("Movement enabled but Movement Scene is missing/invalid")

        if movement_source and get_source_by_name_exists(movement_source):
            passes.append("Movement source/group found: %s" % movement_source)
        else:
            errors.append("Movement enabled but Mouse Graphic / Group is missing/invalid")

        if saved_rest_valid:
            passes.append("Permanent rest position saved")
        else:
            warnings.append("Movement enabled but no permanent rest position has been saved")


    path = normalize_profile_path(profile_path)
    parent = os.path.dirname(path) or "."
    if os.path.isdir(parent):
        passes.append("Profile folder exists")
    else:
        warnings.append("Profile folder does not exist: %s" % parent)

    if errors:
        return "ERROR", errors + warnings + passes
    if warnings:
        return "WARNING", warnings + passes
    return "PASS", passes


def format_self_check_text(status, lines):
    body = [
        "Mouse Overlay Self-Check: %s" % status,
        "Profile: %s" % (profile_name or "Unnamed"),
        "",
    ]
    if status == "PASS":
        body.append("All configured features passed self-check.")
        body.append("")
    body.extend(lines)
    return "\n".join(body)


def show_native_message(title, message, error=False, warning=False):
    """Launch a non-blocking macOS dialog in a separate osascript process."""
    try:
        script = (
            'on run argv\n'
            'set dialogTitle to item 1 of argv\n'
            'set dialogMessage to item 2 of argv\n'
            'display dialog dialogMessage with title dialogTitle buttons {"OK"} default button "OK"\n'
            'end run'
        )

        subprocess.Popen(
            ["osascript", "-e", script, str(title), str(message)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception as exc:
        log("Could not open non-blocking macOS diagnostic window: %s" % exc)
        return False


def show_diagnostics(status=None, lines=None):
    if status is None or lines is None:
        status, lines = collect_self_check()

    message = format_self_check_text(status, lines)
    log(message.replace("\n", " | "))

    show_native_message(
        "Mouse Overlay Self-Check - %s" % status,
        message,
        error=(status == "ERROR"),
        warning=(status == "WARNING"),
    )
    return status


def build_profile_payload():
    controls = [
        {
            "field": 1,
            "field_id": field_id(1),
            "function": left_function,
            "input_type": "physical_left_mouse",
            "source": left_source or "",
        },
        {
            "field": 2,
            "field_id": field_id(2),
            "function": right_function,
            "input_type": "physical_right_mouse",
            "source": right_source or "",
        },
    ]

    for i in range(EXTRA_BUTTONS):
        controls.append({
            "field": i + 3,
            "field_id": field_id(i + 3),
            "function": extra_functions[i],
            "input_type": "obs_hotkey",
            "source": extra_sources[i] or "",
            "hotkey_bindings": hotkey_bindings_to_plain(extra_hotkeys[i]) if i < len(extra_hotkeys) else [],
        })

    return {
        "schema": "akinraze_mouse_overlay_profile",
        "schema_version": 1,
        "script_version": VERSION,
        "profile_name": profile_name,
        "master_enabled": bool(master_enabled),
        "controls": controls,
        "movement": {
            "enabled": bool(movement_enabled),
            "scene": movement_scene or "",
            "source": movement_source or "",
            "range_x": range_x,
            "sensitivity_x": sensitivity_x,
            "range_y": range_y,
            "sensitivity_y": sensitivity_y,
            "smoothing": smoothing,
            "return_speed": return_speed,
            "invert_x": bool(invert_x),
            "invert_y": bool(invert_y),
            "saved_rest_valid": bool(saved_rest_valid),
            "saved_rest_x": saved_rest_x,
            "saved_rest_y": saved_rest_y,
        },
        "diagnostics": {
            "startup_self_check": True,
            "popup_on_pass": False,
            "popup_on_warning_or_error": True,
        },
    }


def export_profile_json(show_confirmation=False):
    path = normalize_profile_path(profile_path)

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        payload = build_profile_payload()

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

        # Verify that the file genuinely exists before declaring success.
        if not os.path.isfile(path):
            raise RuntimeError("Profile file was not created")

        size = os.path.getsize(path)

        log("Profile exported successfully: %s (%d bytes)" % (path, size))

        if show_confirmation:
            show_native_message(
                "Mouse Overlay - Profile Exported",
                "Profile exported successfully.\n\n"
                "Profile: %s\n"
                "File: %s\n"
                "Size: %d bytes" % (profile_name, path, size),
            )
        return True

    except Exception as exc:
        log("Profile export FAILED: %s" % exc)
        show_native_message(
            "Mouse Overlay - Export Failed",
            "Profile export failed.\n\n"
            "File: %s\n\n"
            "%s" % (path, exc),
            error=True,
        )
        return False


def apply_profile_payload(payload):
    global script_settings_ref

    if script_settings_ref is None:
        raise RuntimeError("OBS script settings are not available")
    if payload.get("schema") != "akinraze_mouse_overlay_profile":
        raise ValueError("Not a compatible Mouse Overlay profile JSON")

    obs.obs_data_set_string(script_settings_ref, "profile_name",
                            str(payload.get("profile_name", "Imported Mouse Profile")))
    obs.obs_data_set_bool(script_settings_ref, "master_enabled",
                          bool(payload.get("master_enabled", True)))

    by_field = {}
    for control in payload.get("controls", []):
        try:
            by_field[int(control.get("field"))] = control
        except Exception:
            continue

    left = by_field.get(1, {})
    right = by_field.get(2, {})

    obs.obs_data_set_string(script_settings_ref, "left_source",
                            str(left.get("source", "")))
    obs.obs_data_set_string(script_settings_ref, "right_source",
                            str(right.get("source", "")))

    for i in range(EXTRA_BUTTONS):
        control = by_field.get(i + 3, {})
        obs.obs_data_set_string(script_settings_ref, "extra_source_%d" % i,
                                str(control.get("source", "")))

        if i < len(extra_hotkeys):
            arr = plain_dicts_to_binding_array(control.get("hotkey_bindings", []))
            try:
                obs.obs_hotkey_load(extra_hotkeys[i], arr)
                obs.obs_data_set_array(script_settings_ref, "mouse_overlay_extra_%d" % i, arr)
            finally:
                obs.obs_data_array_release(arr)

    movement = payload.get("movement", {})
    obs.obs_data_set_bool(script_settings_ref, "movement_enabled",
                          bool(movement.get("enabled", False)))
    obs.obs_data_set_string(script_settings_ref, "movement_scene",
                            str(movement.get("scene", "")))
    obs.obs_data_set_string(script_settings_ref, "movement_source",
                            str(movement.get("source", "")))
    obs.obs_data_set_double(script_settings_ref, "range_x", float(movement.get("range_x", 60)))
    obs.obs_data_set_double(script_settings_ref, "sensitivity_x", float(movement.get("sensitivity_x", 0.5)))
    obs.obs_data_set_double(script_settings_ref, "range_y", float(movement.get("range_y", 40)))
    obs.obs_data_set_double(script_settings_ref, "sensitivity_y", float(movement.get("sensitivity_y", 0.5)))
    obs.obs_data_set_double(script_settings_ref, "smoothing", float(movement.get("smoothing", 12)))
    obs.obs_data_set_double(script_settings_ref, "return_speed", float(movement.get("return_speed", 4)))
    obs.obs_data_set_bool(script_settings_ref, "invert_x", bool(movement.get("invert_x", False)))
    obs.obs_data_set_bool(script_settings_ref, "invert_y", bool(movement.get("invert_y", False)))
    obs.obs_data_set_bool(script_settings_ref, "saved_rest_valid",
                          bool(movement.get("saved_rest_valid", False)))
    obs.obs_data_set_double(script_settings_ref, "saved_rest_x",
                            float(movement.get("saved_rest_x", 0.0)))
    obs.obs_data_set_double(script_settings_ref, "saved_rest_y",
                            float(movement.get("saved_rest_y", 0.0)))

    script_update(script_settings_ref)
    refresh_extra_hotkey_descriptions()


def import_profile_json(show_confirmation=False):
    path = normalize_profile_path(profile_path)

    try:
        if not os.path.isfile(path):
            raise FileNotFoundError("Profile JSON does not exist")

        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        imported_name = str(payload.get("profile_name", "Imported Mouse Profile"))
        apply_profile_payload(payload)

        log("Profile imported successfully: %s" % path)

        if show_confirmation:
            show_native_message(
                "Mouse Overlay - Profile Imported",
                "Profile imported successfully.\n\n"
                "Profile: %s\n"
                "File: %s\n\n"
                "If the Scripts window is currently open, close and reopen it "
                "to refresh every displayed field." % (imported_name, path),
            )
        return True

    except Exception as exc:
        log("Profile import FAILED: %s" % exc)
        show_native_message(
            "Mouse Overlay - Import Failed",
            "Profile import failed.\n\n"
            "File: %s\n\n"
            "%s" % (path, exc),
            error=True,
        )
        return False


def get_source_names():
    names = []
    sources = None
    try:
        sources = obs.obs_enum_sources()
        if sources:
            for src in sources:
                try:
                    name = obs.obs_source_get_name(src)
                    if name:
                        names.append(name)
                except Exception:
                    pass
    finally:
        if sources is not None:
            try:
                obs.source_list_release(sources)
            except Exception:
                pass
    return sorted(set(names), key=lambda value: value.lower())


def get_scene_names():
    names = []
    scenes = None
    try:
        scenes = obs.obs_frontend_get_scenes()
        if scenes:
            for src in scenes:
                try:
                    name = obs.obs_source_get_name(src)
                    if name:
                        names.append(name)
                except Exception:
                    pass
    finally:
        if scenes is not None:
            try:
                obs.source_list_release(scenes)
            except Exception:
                pass
    return sorted(set(names), key=lambda value: value.lower())


def add_scene_list(props, key, label):
    prop = obs.obs_properties_add_list(
        props,
        key,
        label,
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(prop, "(none)", "")
    for name in get_scene_names():
        obs.obs_property_list_add_string(prop, name, name)
    return prop


def add_source_list(props, key, label):
    prop = obs.obs_properties_add_list(
        props,
        key,
        label,
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(prop, "(none)", "")
    for name in get_source_names():
        obs.obs_property_list_add_string(prop, name, name)
    return prop


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_text(props, "profile_name", "Profile Name", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_bool(props, "master_enabled", "Master Enable Mouse Overlay")
    obs.obs_properties_add_text(props, "profile_path", "Profile JSON Path", obs.OBS_TEXT_DEFAULT)

    add_source_list(props, "left_source", "Left Mouse Button Source")
    add_source_list(props, "right_source", "Right Mouse Button Source")

    for i in range(EXTRA_BUTTONS):
        fid = field_id(i + 3)
        add_source_list(props, "extra_source_%d" % i, "%s OBS Source / Name" % fid)

    obs.obs_properties_add_bool(props, "movement_enabled", "Enable Mouse Movement")
    add_scene_list(props, "movement_scene", "Select OBS Scene")
    add_source_list(props, "movement_source", "Mouse Graphic / Group")

    for key, label, low, high, step in [
        ("range_x", "Horizontal Range (scene units each way)", 0, 2000, 1),
        ("sensitivity_x", "Horizontal Sensitivity", 0.01, 10, 0.01),
        ("range_y", "Vertical Range (scene units each way)", 0, 2000, 1),
        ("sensitivity_y", "Vertical Sensitivity", 0.01, 10, 0.01),
        ("smoothing", "Follow Speed (higher = faster)", 1, 60, 1),
        ("return_speed", "Return to Center Speed (0 = stay)", 0, 20, 0.1),
    ]:
        obs.obs_properties_add_float(props, key, label, low, high, step)

    obs.obs_properties_add_bool(props, "invert_x", "Invert Movement X")
    obs.obs_properties_add_bool(props, "invert_y", "Invert Movement Y")
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "profile_name", "Default Mouse Profile")
    obs.obs_data_set_default_bool(settings, "master_enabled", True)
    obs.obs_data_set_default_string(settings, "profile_path", default_profile_path())

    obs.obs_data_set_default_string(settings, "left_source", "")
    obs.obs_data_set_default_string(settings, "right_source", "")

    for i in range(EXTRA_BUTTONS):
        obs.obs_data_set_default_string(settings, "extra_source_%d" % i, "")

    obs.obs_data_set_default_bool(settings, "movement_enabled", False)
    obs.obs_data_set_default_string(settings, "movement_scene", "")
    obs.obs_data_set_default_string(settings, "movement_source", "")

    for key, value in [
        ("range_x", 60),
        ("range_y", 40),
        ("sensitivity_x", 0.5),
        ("sensitivity_y", 0.5),
        ("smoothing", 12),
        ("return_speed", 4),
    ]:
        obs.obs_data_set_default_double(settings, key, value)

    obs.obs_data_set_default_bool(settings, "invert_x", False)
    obs.obs_data_set_default_bool(settings, "invert_y", False)
    obs.obs_data_set_default_bool(settings, "saved_rest_valid", False)
    obs.obs_data_set_default_double(settings, "saved_rest_x", 0.0)
    obs.obs_data_set_default_double(settings, "saved_rest_y", 0.0)


def normalize_source_name(value):
    if not value or value == "(none)":
        return None
    return value


def script_update(settings):
    global left_source, right_source
    global master_enabled, profile_name, profile_path, startup_self_check
    global left_function, right_function, extra_functions
    global movement_enabled, movement_scene, movement_source
    global range_x, range_y, sensitivity_x, sensitivity_y, smoothing, return_speed
    global invert_x, invert_y, movement_fault
    global saved_rest_x, saved_rest_y, saved_rest_valid, startup_restored
    global movement_origin_x, movement_origin_y, movement_origin_valid

    previous_movement_enabled = movement_enabled
    previous_master_enabled = master_enabled

    profile_name = sanitize_function_name(
        obs.obs_data_get_string(settings, "profile_name"), "Default Mouse Profile"
    )
    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    profile_path = normalize_profile_path(obs.obs_data_get_string(settings, "profile_path"))
    startup_self_check = True

    # If movement was active, first return the graphic to its known rest
    # position before applying any changed settings.
    restore_movement_to_origin()

    old_left = left_source
    old_right = right_source
    old_extra = set(name for name in extra_sources if name)

    new_left = normalize_source_name(obs.obs_data_get_string(settings, "left_source"))
    new_right = normalize_source_name(obs.obs_data_get_string(settings, "right_source"))

    for i in range(EXTRA_BUTTONS):
        extra_sources[i] = normalize_source_name(
            obs.obs_data_get_string(settings, "extra_source_%d" % i)
        )

    left_source = new_left
    right_source = new_right

    # Automatically derive the human-readable field/hotkey names from the
    # selected OBS Source names. This removes the separate manual naming step.
    left_function = left_source if left_source else "Left Click"
    right_function = right_source if right_source else "Right Click"
    for i in range(EXTRA_BUTTONS):
        extra_functions[i] = (
            extra_sources[i]
            if extra_sources[i]
            else "Extra Mouse Button %d" % (i + 1)
        )

    if old_left and old_left != new_left:
        set_source_visibility(old_left, False)
    if old_right and old_right != new_right:
        set_source_visibility(old_right, False)

    for name in old_extra - set(name for name in extra_sources if name):
        if name not in {left_source, right_source}:
            set_source_visibility(name, False)

    left_source_saved = normalize_source_name(obs.obs_data_get_string(settings, "left_source"))
    right_source_saved = normalize_source_name(obs.obs_data_get_string(settings, "right_source"))

    left_function = left_source_saved if left_source_saved else "Left Click"
    right_function = right_source_saved if right_source_saved else "Right Click"

    for i in range(EXTRA_BUTTONS):
        saved_name = normalize_source_name(obs.obs_data_get_string(settings, "extra_source_%d" % i))
        extra_functions[i] = saved_name if saved_name else "Extra Mouse Button %d" % (i + 1)

    movement_enabled = obs.obs_data_get_bool(settings, "movement_enabled")
    movement_scene = obs.obs_data_get_string(settings, "movement_scene")
    movement_source = obs.obs_data_get_string(settings, "movement_source")

    range_x = max(0.0, obs.obs_data_get_double(settings, "range_x"))
    range_y = max(0.0, obs.obs_data_get_double(settings, "range_y"))
    sensitivity_x = max(0.01, obs.obs_data_get_double(settings, "sensitivity_x"))
    sensitivity_y = max(0.01, obs.obs_data_get_double(settings, "sensitivity_y"))
    smoothing = max(1.0, obs.obs_data_get_double(settings, "smoothing"))
    return_speed = max(0.0, obs.obs_data_get_double(settings, "return_speed"))
    invert_x = obs.obs_data_get_bool(settings, "invert_x")
    invert_y = obs.obs_data_get_bool(settings, "invert_y")

    # Load the previously saved permanent rest position.
    saved_rest_valid = obs.obs_data_get_bool(settings, "saved_rest_valid")
    saved_rest_x = obs.obs_data_get_double(settings, "saved_rest_x")
    saved_rest_y = obs.obs_data_get_double(settings, "saved_rest_y")

    movement_fault = False
    clear_movement_state(keep_origin=False)

    # While OBS is already running, an OFF -> ON transition means:
    # "the mouse is arranged exactly where I want it; save this as rest."
    #
    # We intentionally do NOT do this during startup because OBS may have
    # restored a temporary animated position from the previous shutdown.
    if loaded and movement_enabled and not previous_movement_enabled:
        captured = [False]

        def capture_new_rest(item):
            global saved_rest_x, saved_rest_y, saved_rest_valid
            global movement_origin_x, movement_origin_y, movement_origin_valid

            pos = obs.vec2()
            obs.obs_sceneitem_get_pos(item, pos)

            saved_rest_x = float(pos.x)
            saved_rest_y = float(pos.y)
            saved_rest_valid = True

            movement_origin_x = saved_rest_x
            movement_origin_y = saved_rest_y
            movement_origin_valid = True

            obs.obs_data_set_double(settings, "saved_rest_x", saved_rest_x)
            obs.obs_data_set_double(settings, "saved_rest_y", saved_rest_y)
            obs.obs_data_set_bool(settings, "saved_rest_valid", True)

            captured[0] = True

        try:
            with_movement_item(capture_new_rest)
        except Exception as exc:
            log("Could not save new rest position: %s" % exc)

        if captured[0]:
            log("Saved new permanent rest position: %.2f, %.2f" %
                (saved_rest_x, saved_rest_y))

    # If movement is enabled and a permanent rest position already exists,
    # make that the logical origin immediately. The physical item is restored
    # after the startup grace period.
    if movement_enabled and saved_rest_valid:
        movement_origin_x = saved_rest_x
        movement_origin_y = saved_rest_y
        movement_origin_valid = True

    startup_restored = False
    refresh_extra_hotkey_descriptions()

    if not master_enabled:
        hide_all_indicator_sources()
        restore_movement_to_origin()
    else:
        update_click_visibility()


def update_click_visibility():
    if not master_enabled:
        hide_all_indicator_sources()
        return

    states = {}

    pairs = [(left_source, left_down), (right_source, right_down)]
    pairs.extend(zip(extra_sources, extra_down))

    for name, down in pairs:
        if name:
            states[name] = states.get(name, False) or bool(down)

    for name, down in states.items():
        set_source_visibility(name, down)


def extra_button_callback(pressed, index):
    if 0 <= index < EXTRA_BUTTONS:
        extra_down[index] = bool(pressed)
        if master_enabled:
            update_click_visibility()


def script_save(settings):
    obs.obs_data_set_string(settings, "profile_name", profile_name)
    obs.obs_data_set_bool(settings, "master_enabled", master_enabled)
    obs.obs_data_set_string(settings, "profile_path", normalize_profile_path(profile_path))
    for i, hotkey_id in enumerate(extra_hotkeys):
        try:
            binding = obs.obs_hotkey_save(hotkey_id)
            obs.obs_data_set_array(settings, "mouse_overlay_extra_%d" % i, binding)
            obs.obs_data_array_release(binding)
        except Exception as exc:
            log("Could not save Extra Button %d hotkey: %s" % (i + 1, exc))


    # Persist the permanent rest position independently of OBS's scene-item
    # transform saved during shutdown.
    obs.obs_data_set_bool(settings, "saved_rest_valid", saved_rest_valid)
    if saved_rest_valid:
        obs.obs_data_set_double(settings, "saved_rest_x", saved_rest_x)
        obs.obs_data_set_double(settings, "saved_rest_y", saved_rest_y)
    # Automatic backup: every OBS script-settings save refreshes the JSON profile.
    # No user hotkey is required.
    if loaded:
        export_profile_json(show_confirmation=False)


def clear_movement_state(keep_origin=False):
    global movement_origin_x, movement_origin_y, movement_origin_valid
    global last_cursor, last_time
    global offset_x, offset_y, target_x, target_y

    last_cursor = None
    last_time = None
    offset_x = 0.0
    offset_y = 0.0
    target_x = 0.0
    target_y = 0.0

    if not keep_origin:
        movement_origin_x = 0.0
        movement_origin_y = 0.0
        movement_origin_valid = False


def with_movement_item(action):
    """
    Resolve the configured movement scene and item, run action(item),
    then release only the scene SOURCE reference.

    IMPORTANT:
    The scene-item pointer is NEVER stored beyond this function.
    """
    if not movement_scene or not movement_source:
        return False

    required = (
        "obs_get_source_by_name",
        "obs_source_release",
        "obs_scene_from_source",
        "obs_scene_find_source_recursive",
    )
    missing = [name for name in required if not callable(getattr(obs, name, None))]
    if missing:
        raise RuntimeError(
            "This OBS Python module lacks: " + ", ".join(missing)
        )

    scene_source = obs.obs_get_source_by_name(movement_scene)
    if scene_source is None:
        return False

    try:
        scene = obs.obs_scene_from_source(scene_source)
        if scene is None:
            return False

        item = obs.obs_scene_find_source_recursive(scene, movement_source)
        if item is None:
            return False

        action(item)
        return True

    finally:
        obs.obs_source_release(scene_source)


def capture_movement_origin():
    global movement_origin_x, movement_origin_y, movement_origin_valid

    def capture(item):
        global movement_origin_x, movement_origin_y, movement_origin_valid
        pos = obs.vec2()
        obs.obs_sceneitem_get_pos(item, pos)
        movement_origin_x = float(pos.x)
        movement_origin_y = float(pos.y)
        movement_origin_valid = True

    return with_movement_item(capture)


def restore_movement_to_origin():
    global movement_origin_valid

    if not movement_origin_valid:
        clear_movement_state(keep_origin=False)
        return

    origin_x = movement_origin_x
    origin_y = movement_origin_y

    try:
        def restore(item):
            pos = obs.vec2()
            pos.x = origin_x
            pos.y = origin_y
            obs.obs_sceneitem_set_pos(item, pos)

        with_movement_item(restore)

    except Exception as exc:
        log("Could not restore rest position: %s" % exc)

    finally:
        clear_movement_state(keep_origin=False)



def restore_saved_rest_position():
    """Force the configured mouse graphic/group to the permanent saved rest position."""
    global movement_origin_x, movement_origin_y, movement_origin_valid

    if not saved_rest_valid:
        return False

    def restore(item):
        pos = obs.vec2()
        pos.x = saved_rest_x
        pos.y = saved_rest_y
        obs.obs_sceneitem_set_pos(item, pos)

    if not with_movement_item(restore):
        return False

    movement_origin_x = saved_rest_x
    movement_origin_y = saved_rest_y
    movement_origin_valid = True
    clear_movement_state(keep_origin=True)
    return True

def poll_movement():
    global movement_origin_valid, startup_restored
    global last_cursor, last_time
    global offset_x, offset_y, target_x, target_y

    if movement_fault:
        return

    if time.monotonic() < movement_start_after:
        return

    if not master_enabled:
        return

    if not movement_enabled or not movement_scene or not movement_source:
        return

    # On every OBS startup, correct any temporary animated position that OBS
    # may have saved during the previous shutdown BEFORE accepting mouse input.
    if not startup_restored:
        if saved_rest_valid:
            if not restore_saved_rest_position():
                return
        startup_restored = True

    # Upgrade/first-use fallback: if no permanent rest has ever been saved,
    # use the current position for this session. The recommended workflow is
    # Movement OFF -> arrange -> Movement ON once, which saves it permanently.
    if not movement_origin_valid:
        if not capture_movement_origin():
            return

    cursor = get_cursor_pos()
    if cursor is None:
        last_cursor = None
        last_time = None
        return

    point_x, point_y = cursor
    now = time.monotonic()

    if last_cursor is None:
        last_cursor = (point_x, point_y)
        last_time = now
        return

    dt = min(0.1, max(0.0, now - last_time))
    dx = point_x - last_cursor[0]
    dy = point_y - last_cursor[1]

    last_cursor = (point_x, point_y)
    last_time = now

    if dx == 0 and dy == 0 and return_speed > 0:
        decay = pow(2.718281828, -return_speed * dt)
        target_x *= decay
        target_y *= decay

    x_direction = -1 if invert_x else 1
    y_direction = -1 if invert_y else 1

    target_x = max(
        -range_x,
        min(range_x, target_x + dx * sensitivity_x * x_direction),
    )
    target_y = max(
        -range_y,
        min(range_y, target_y + dy * sensitivity_y * y_direction),
    )

    alpha = 1.0 - pow(2.718281828, -smoothing * dt)

    offset_x += (target_x - offset_x) * alpha
    offset_y += (target_y - offset_y) * alpha

    new_x = movement_origin_x + offset_x
    new_y = movement_origin_y + offset_y

    def apply_position(item):
        pos = obs.vec2()
        pos.x = new_x
        pos.y = new_y
        obs.obs_sceneitem_set_pos(item, pos)

    # Re-resolve the item only for this update.
    # No scene-item pointer survives into the next timer call.
    if not with_movement_item(apply_position):
        # Target disappeared or scene changed. Forget the origin and wait
        # until it becomes resolvable again.
        clear_movement_state(keep_origin=False)


def get_cursor_pos():
    if cg is None or cf is None:
        return None

    event = None
    try:
        event = cg.CGEventCreate(None)
        if not event:
            return None
        point = cg.CGEventGetLocation(event)
        return (float(point.x), float(point.y))
    except Exception:
        return None
    finally:
        if event:
            try:
                cf.CFRelease(event)
            except Exception:
                pass


def is_button_down(mouse_button):
    if cg is None:
        return False

    try:
        return bool(
            cg.CGEventSourceButtonState(
                CG_EVENT_SOURCE_STATE_COMBINED_SESSION,
                mouse_button,
            )
        )
    except Exception:
        return False


def set_items_visibility(scene, source_name, visible):
    items = obs.obs_scene_enum_items(scene)
    if items is None:
        return

    try:
        for item in items:
            src = obs.obs_sceneitem_get_source(item)

            if src and obs.obs_source_get_name(src) == source_name:
                if obs.obs_sceneitem_visible(item) != visible:
                    obs.obs_sceneitem_set_visible(item, visible)

            try:
                if obs.obs_sceneitem_is_group(item):
                    group_scene = obs.obs_sceneitem_group_get_scene(item)
                    if group_scene is not None:
                        set_items_visibility(group_scene, source_name, visible)
            except Exception:
                pass

    finally:
        obs.sceneitem_list_release(items)


def set_source_visibility(source_name, visible):
    if not source_name:
        return

    scenes = None

    try:
        scenes = obs.obs_frontend_get_scenes()
        if not scenes:
            return

        for scene_source in scenes:
            scene = obs.obs_scene_from_source(scene_source)
            if scene is not None:
                set_items_visibility(scene, source_name, visible)

    except Exception as exc:
        log("Visibility error for '%s': %s" % (source_name, exc))

    finally:
        if scenes is not None:
            try:
                obs.source_list_release(scenes)
            except Exception:
                pass


def poll_mouse_buttons():
    global left_down, right_down, movement_fault

    if not master_enabled:
        if left_down or right_down:
            left_down = False
            right_down = False
        hide_all_indicator_sources()
        return

    try:
        new_left = is_button_down(CG_MOUSE_BUTTON_LEFT)
        new_right = is_button_down(CG_MOUSE_BUTTON_RIGHT)

        if new_left != left_down or new_right != right_down:
            left_down = new_left
            right_down = new_right
            update_click_visibility()

    except Exception as exc:
        # Click handling should not be able to kill the movement timer.
        log("Mouse-button polling error: %s" % exc)

    if movement_fault:
        return

    try:
        poll_movement()

    except Exception as exc:
        # CRITICAL SAFETY BEHAVIOR:
        # One error = pause movement. Do NOT throw again every 16 ms.
        movement_fault = True
        clear_movement_state(keep_origin=False)
        log(
            "Movement PAUSED after one error: %s. "
            "Change a movement setting or reload the script to retry." % exc
        )


def run_startup_self_check_once():
    try:
        obs.timer_remove(run_startup_self_check_once)
    except Exception:
        pass

    status, lines = collect_self_check()
    message = format_self_check_text(status, lines)

    # Always keep a record in Script Log.
    log(message.replace("\n", " | "))

    # A healthy startup should not interrupt the user.
    # Only warnings/errors open a visible results window.
    if status != "PASS":
        show_native_message(
            "Mouse Overlay Self-Check - %s" % status,
            message,
            error=(status == "ERROR"),
            warning=(status == "WARNING"),
        )


def script_load(settings):
    global loaded, left_down, right_down
    global movement_enabled, movement_fault, movement_start_after
    global saved_rest_x, saved_rest_y, saved_rest_valid, startup_restored
    global movement_origin_x, movement_origin_y, movement_origin_valid
    global master_enabled, profile_name, profile_path, startup_self_check
    global left_function, right_function, script_settings_ref

    print("--- %s loaded from %s ---" % (VERSION, __file__))

    script_settings_ref = settings
    try:
        obs.obs_data_addref(script_settings_ref)
    except Exception:
        pass

    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    profile_name = sanitize_function_name(
        obs.obs_data_get_string(settings, "profile_name"), "Default Mouse Profile"
    )
    profile_path = normalize_profile_path(obs.obs_data_get_string(settings, "profile_path"))
    startup_self_check = True

    movement_enabled = obs.obs_data_get_bool(settings, "movement_enabled")
    saved_rest_valid = obs.obs_data_get_bool(settings, "saved_rest_valid")
    saved_rest_x = obs.obs_data_get_double(settings, "saved_rest_x")
    saved_rest_y = obs.obs_data_get_double(settings, "saved_rest_y")

    if movement_enabled and saved_rest_valid:
        movement_origin_x = saved_rest_x
        movement_origin_y = saved_rest_y
        movement_origin_valid = True

    startup_restored = False
    movement_fault = False
    movement_start_after = time.monotonic() + MOVEMENT_STARTUP_DELAY

    if cg is None or cf is None:
        log("ERROR: macOS CoreGraphics mouse API is unavailable.")

    left_down = False
    right_down = False

    for i in range(EXTRA_BUTTONS):
        extra_down[i] = False

    extra_hotkeys.clear()
    extra_callbacks.clear()

    for i in range(EXTRA_BUTTONS):
        callback = lambda pressed, index=i: extra_button_callback(pressed, index)
        extra_callbacks.append(callback)

        name = "mouse_overlay_extra_%d" % i
        desc = "Mouse Overlay %s %s" % (field_id(i + 3), extra_functions[i])

        hotkey_id = obs.obs_hotkey_register_frontend(name, desc, callback)
        extra_hotkeys.append(hotkey_id)

        binding = obs.obs_data_get_array(settings, name)
        if binding is not None:
            try:
                obs.obs_hotkey_load(hotkey_id, binding)
            finally:
                obs.obs_data_array_release(binding)

    loaded = True

    # Automatic restore: if the configured JSON profile already exists,
    # load it without requiring a management hotkey.
    auto_profile_path = normalize_profile_path(profile_path)
    if os.path.isfile(auto_profile_path):
        try:
            import_profile_json(show_confirmation=False)
            log("Automatic profile import completed.")
        except Exception as exc:
            log("Automatic profile import failed: %s" % exc)
    refresh_extra_hotkey_descriptions()
    update_click_visibility()

    if left_source:
        set_source_visibility(left_source, False)
    if right_source:
        set_source_visibility(right_source, False)

    obs.timer_add(poll_mouse_buttons, POLL_MS)

    try:
        obs.timer_add(run_startup_self_check_once, 2200)
    except Exception as exc:
        log("Could not schedule startup self-check: %s" % exc)


def script_unload():
    global loaded, left_down, right_down, script_settings_ref

    if loaded:
        try:
            obs.timer_remove(poll_mouse_buttons)
        except Exception:
            pass

    # Restore the graphic while the configured scene/source names still exist.
    restore_movement_to_origin()

    left_down = False
    right_down = False

    for i in range(EXTRA_BUTTONS):
        extra_down[i] = False

    try:
        update_click_visibility()
    except Exception:
        pass

    if left_source:
        set_source_visibility(left_source, False)
    if right_source:
        set_source_visibility(right_source, False)

    extra_hotkeys.clear()
    extra_callbacks.clear()

    if script_settings_ref is not None:
        try:
            obs.obs_data_release(script_settings_ref)
        except Exception:
            pass
        script_settings_ref = None

    loaded = False
