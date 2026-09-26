# OBS Studio Mouse Click Overlay
# macOS left/right indicators, twenty programmable extra inputs, named profiles, and bounded mouse movement.
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
import time
import json
import os
import re
import base64
import subprocess

VERSION = "MouseCast_macOS_v4_0_RC1"
POLL_MS = 16  # ~60 Hz; more than enough for OBS animation and much safer than 10 ms
MOVEMENT_STARTUP_DELAY = 1.5  # seconds; lets OBS finish restoring scenes before movement begins

# macOS CoreGraphics mouse input constants.
CG_EVENT_SOURCE_STATE_COMBINED_SESSION = 0
CG_MOUSE_BUTTON_LEFT = 0
CG_MOUSE_BUTTON_RIGHT = 1

left_source = None
right_source = None
left_down = False
right_down = False
loaded = False

master_enabled = True
setup_preview_enabled = False
current_profile = "Default"
profile_operation_guard = False
startup_self_check = True
script_settings_ref = None

PROFILE_DIRNAME = "ControlCast_mouse_profiles"
LEGACY_PROFILE_FILENAME = "mouse_overlay_profile.json"

left_function = "Left Click"
right_function = "Right Click"
extra_functions = ["Extra Mouse Button %d" % (i + 1) for i in range(20)]


movement_enabled = False
movement_fault = False
movement_start_after = 0.0
movement_scene = ""
movement_source = ""

range_x = 60.0
range_y = 40.0
sensitivity_x = 0.5
sensitivity_y = 0.5
smoothing = 12.0
return_speed = 4.0
invert_x = False
invert_y = False

movement_origin_x = 0.0
movement_origin_y = 0.0
movement_origin_valid = False

# Permanent rest position saved in the script settings.
# This prevents OBS from making a temporary animated position the new center
# when the scene collection is saved during shutdown.
saved_rest_x = 0.0
saved_rest_y = 0.0
saved_rest_valid = False
startup_restored = False

last_cursor = None
last_time = None
# Movement safety arm: after any settings/profile change, cursor samples are
# absorbed briefly before MouseCast is allowed to move the OBS group.
# This prevents a stale/large first delta from throwing the graphic across the canvas.
movement_arm_until = 0.0
MOVEMENT_ARM_SECONDS = 0.30
offset_x = 0.0
offset_y = 0.0
target_x = 0.0
target_y = 0.0

EXTRA_BUTTONS = 20  # hard maximum retained for profile/backward compatibility
extra_button_count = 20  # user-selected number of extra inputs shown/active (0-20)
extra_sources = [None] * EXTRA_BUTTONS
extra_down = [False] * EXTRA_BUTTONS
extra_hotkeys = []
extra_callbacks = []
extra_keys = ["OBS_HOTKEYS"] * EXTRA_BUTTONS
extra_modifiers = ["NONE"] * EXTRA_BUTTONS
modifier_only_down = [False] * EXTRA_BUTTONS

# macOS virtual key codes (left/right variants where applicable).
MAC_KEY_SHIFT_LEFT = 56
MAC_KEY_SHIFT_RIGHT = 60
MAC_KEY_CONTROL_LEFT = 59
MAC_KEY_CONTROL_RIGHT = 62
MAC_KEY_OPTION_LEFT = 58
MAC_KEY_OPTION_RIGHT = 61
MAC_KEY_COMMAND_LEFT = 55
MAC_KEY_COMMAND_RIGHT = 54


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
    cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
    cg.CGEventSourceKeyState.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
except Exception:
    cg = None
    cf = None


def log(message):
    print("[MouseCast] " + str(message))


def script_description():
    return (
        "<b>%s</b><br><br>"
        "macOS Community Test build for <b>MouseCast</b> with named mouse profiles and live profile switching.<br>"
        "Switch profiles while OBS is running or recording; no OBS restart is required.<br><b>Module:</b> MouseCast — physical mouse clicks, programmable mouse inputs, and reactive mouse movement visuals.<br><br>"
        "<b>Mouse inputs:</b> physical Left/Right Click plus a user-selected 0-20 additional programmable inputs "
        "([01]-[20] additional inputs) for mice with large side-button layouts, keyboard shortcuts, modifiers, or OBS hotkeys.<br>"
        "Set <b>Extra Programmable Inputs to Show</b> at the very top. After changing the count, refresh the Scripts panel "
        "by clicking another script and back (or close/reopen Tools &gt; Scripts) so OBS rebuilds the visible input blocks.<br>"
        "<b>Profiles:</b> each profile stores Sources, extra-button shortcut assignments/hotkeys, movement settings, "
        "range/sensitivity, invert options, and the permanent rest position.<br>"
        "Use New Profile Name + Profile Action to create, duplicate, save, or delete profiles.<br>"
        "<b>OBS Scripts-panel note:</b> profile changes apply live immediately, but OBS does not rebuild "
        "the already-open Python properties panel. After creating, deleting, duplicating, or switching a profile, "
        "close and reopen Tools &gt; Scripts (or click another script and back) to refresh the displayed fields. "
        "OBS itself does not need to restart.<br><br>"
        "Use <b>Setup / Preview Mode</b> whenever you need assigned graphics to remain visible while arranging them. "
        "Preview changes visibility only; movement is paused while Preview is ON.<br>"
        "<b>Movement safety:</b> MouseCast will move only the selected OBS <b>Group</b>, never an individual button image. "
        "After profile/settings changes it re-arms from the current physical cursor position before movement resumes.<br>"
        "Turning movement OFF, arranging the mouse, then ON saves that position as the profile's "
        "permanent rest position.<br><br>"
        "Startup self-check logs PASS silently and opens a detached macOS dialog only for warnings/errors.<br>"
        "<b>Safety:</b> no Python property-button callbacks are registered."
    ) % VERSION

def field_id(index):
    return "[%02d]" % index


def script_folder():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def profiles_folder():
    return os.path.join(script_folder(), PROFILE_DIRNAME)


def legacy_profile_path():
    return os.path.join(script_folder(), LEGACY_PROFILE_FILENAME)


def safe_profile_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", (name or "").strip())
    cleaned = cleaned.rstrip(" .")
    return cleaned or "Default"


def profile_file_path(name):
    return os.path.join(profiles_folder(), safe_profile_filename(name) + ".json")


def ensure_profiles_folder():
    os.makedirs(profiles_folder(), exist_ok=True)


def list_profiles():
    ensure_profiles_folder()
    names = []
    try:
        for filename in os.listdir(profiles_folder()):
            if filename.lower().endswith(".json"):
                names.append(os.path.splitext(filename)[0])
    except Exception as exc:
        log("Could not enumerate mouse profiles: %s" % exc)
    if "Default" not in names:
        names.append("Default")
    return sorted(set(names), key=lambda value: value.lower())


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
    # Hide every configured extra source, including inputs currently outside
    # the selected visible count, so reducing the count cannot leave an old
    # indicator stuck on-screen.
    for name in extra_sources:
        if name:
            names.add(name)
    for name in names:
        set_source_visibility(name, False)


def show_all_indicator_sources():
    """Force all currently assigned indicator graphics visible for setup/positioning."""
    names = set()
    if left_source:
        names.add(left_source)
    if right_source:
        names.add(right_source)
    for name in extra_sources[:extra_button_count]:
        if name:
            names.add(name)
    for name in names:
        set_source_visibility(name, True)


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



def binding_from_ui(key_name, modifier_code):
    if not key_name or key_name in ("OBS_HOTKEYS", "UNASSIGNED", "MODIFIER_ONLY"):
        return []
    mods = set((modifier_code or "NONE").split("+"))
    return [{
        "key": key_name,
        "shift": "SHIFT" in mods,
        "control": "CTRL" in mods,
        "alt": "ALT" in mods,
        "command": "COMMAND" in mods,
    }]


def load_extra_hotkey_binding(index, bindings):
    if not (0 <= index < EXTRA_BUTTONS):
        return
    if index >= len(extra_hotkeys):
        return
    hotkey_id = extra_hotkeys[index]
    if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
        return
    arr = plain_dicts_to_binding_array(bindings)
    try:
        obs.obs_hotkey_load(hotkey_id, arr)
        if script_settings_ref is not None:
            obs.obs_data_set_array(script_settings_ref, "mouse_overlay_extra_%d" % index, arr)
    finally:
        obs.obs_data_array_release(arr)


def apply_extra_ui_binding(index):
    key_name = extra_keys[index]
    if key_name == "OBS_HOTKEYS":
        return
    load_extra_hotkey_binding(index, binding_from_ui(key_name, extra_modifiers[index]))


def is_mac_key_down(key_code):
    if cg is None:
        return False
    try:
        return bool(cg.CGEventSourceKeyState(
            CG_EVENT_SOURCE_STATE_COMBINED_SESSION, int(key_code)
        ))
    except Exception:
        return False


def modifier_combo_is_down(modifier_code):
    mods = set((modifier_code or "NONE").split("+"))
    mods.discard("NONE")
    if not mods:
        return False

    checks = []
    if "SHIFT" in mods:
        checks.append(is_mac_key_down(MAC_KEY_SHIFT_LEFT) or is_mac_key_down(MAC_KEY_SHIFT_RIGHT))
    if "CTRL" in mods:
        checks.append(is_mac_key_down(MAC_KEY_CONTROL_LEFT) or is_mac_key_down(MAC_KEY_CONTROL_RIGHT))
    if "ALT" in mods:
        checks.append(is_mac_key_down(MAC_KEY_OPTION_LEFT) or is_mac_key_down(MAC_KEY_OPTION_RIGHT))
    if "COMMAND" in mods:
        checks.append(is_mac_key_down(MAC_KEY_COMMAND_LEFT) or is_mac_key_down(MAC_KEY_COMMAND_RIGHT))
    return bool(checks) and all(checks)


def poll_extra_modifier_only_inputs():
    changed = False
    for i in range(EXTRA_BUTTONS):
        should_be_down = (
            i < extra_button_count
            and master_enabled
            and not setup_preview_enabled
            and extra_keys[i] == "MODIFIER_ONLY"
            and modifier_combo_is_down(extra_modifiers[i])
        )
        if should_be_down != modifier_only_down[i]:
            modifier_only_down[i] = should_be_down
            extra_down[i] = should_be_down
            changed = True
    if changed:
        update_click_visibility()

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
            setter(hotkey_id, "MouseCast %s %s" % (field_id(i + 1), label))
        except Exception as exc:
            log("Could not update hotkey description %s: %s" % (field_id(i + 1), exc))


def collect_self_check():
    errors = []
    warnings = []
    passes = []

    if cg is None or cf is None:
        errors.append("macOS CoreGraphics input API unavailable")
    else:
        passes.append("macOS CoreGraphics mouse/input API")

    passes.append("Profile: %s" % (current_profile or "Default"))

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
    passes.append("Active extra programmable inputs: %d" % extra_button_count)
    for i in range(extra_button_count):
        fid = field_id(i + 1)
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
            if movement_enabled and not movement_group_is_valid():
                warnings.append("Movement target is not an OBS Group. Movement is blocked to protect individual button-image positions.")
        else:
            errors.append("Movement enabled but Mouse Graphic / Group is missing/invalid")

        if saved_rest_valid:
            passes.append("Permanent rest position saved")
        else:
            warnings.append("Movement enabled but no permanent rest position has been saved")


    folder = profiles_folder()
    if os.path.isdir(folder):
        passes.append("Named profile folder exists")
    else:
        warnings.append("Named profile folder does not exist: %s" % folder)

    if errors:
        return "ERROR", errors + warnings + passes
    if warnings:
        return "WARNING", warnings + passes
    return "PASS", passes


def format_self_check_text(status, lines):
    body = [
        "Mouse Overlay Self-Check: %s" % status,
        "Profile: %s" % (current_profile or "Default"),
        "",
    ]
    if status == "PASS":
        body.append("All configured features passed self-check.")
        body.append("")
    body.extend(lines)
    return "\n".join(body)


def show_native_message(title, message, error=False, warning=False):
    """Open a detached macOS dialog so OBS scripting/timers never block."""
    try:
        script = (
            'on run argv\\n'
            'set dialogTitle to item 1 of argv\\n'
            'set dialogMessage to item 2 of argv\\n'
            'display dialog dialogMessage with title dialogTitle buttons {"OK"} default button "OK"\\n'
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


def show_profile_refresh_notice(profile_name, action_text):
    """
    Explain the one OBS UI limitation without blocking OBS.

    The profile has already been applied to runtime/settings when this is called.
    Only the currently rendered Scripts properties widgets can still show stale
    values until OBS rebuilds that panel.
    """
    show_native_message(
        "MouseCast - Profile Updated",
        "%s\n\nActive profile: %s\n\n"
        "The profile change is already live. OBS does not automatically rebuild "
        "an already-open Python settings panel, so the visible fields may still "
        "show the previous profile until the panel is refreshed.\n\n"
        "To refresh only the display:\n"
        "1. Click another script and then click this script again, OR\n"
        "2. Close and reopen Tools > Scripts.\n\n"
        "You do NOT need to restart OBS, stop recording, or stop streaming."
        % (action_text, profile_name),
    )


def build_profile_payload(name=None):
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
            "input_type": "programmable_hotkey",
            "source": extra_sources[i] or "",
            "key_mode": extra_keys[i],
            "modifiers": extra_modifiers[i],
            "hotkey_bindings": hotkey_bindings_to_plain(extra_hotkeys[i]) if i < len(extra_hotkeys) else [],
        })

    return {
        "schema": "controlcast_mouse_profiles",
        "schema_version": 3,
        "script_version": VERSION,
        "profile_name": name or current_profile,
        "master_enabled": bool(master_enabled),
        "setup_preview_enabled": bool(setup_preview_enabled),
        "extra_button_count": int(extra_button_count),
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
    }


def save_profile(name=None, show_error=True):
    target = (name or current_profile or "Default").strip() or "Default"
    ensure_profiles_folder()
    path = profile_file_path(target)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(build_profile_payload(target), handle, indent=2, ensure_ascii=False)
        log("Saved mouse profile '%s' -> %s" % (target, path))
        return True
    except Exception as exc:
        log("Mouse profile save failed: %s" % exc)
        if show_error:
            show_native_message(
                "MouseCast - Profile Save Failed",
                "Could not save profile '%s'.\n\n%s" % (target, exc),
                error=True,
            )
        return False


def blank_profile_payload(name):
    controls = [
        {"field": 1, "field_id": field_id(1), "function": "Left Click", "input_type": "physical_left_mouse", "source": ""},
        {"field": 2, "field_id": field_id(2), "function": "Right Click", "input_type": "physical_right_mouse", "source": ""},
    ]
    for i in range(EXTRA_BUTTONS):
        controls.append({
            "field": i + 3,
            "field_id": field_id(i + 3),
            "function": "Extra Mouse Button %d" % (i + 1),
            "input_type": "programmable_hotkey",
            "source": "",
            "key_mode": "OBS_HOTKEYS",
            "modifiers": "NONE",
            "hotkey_bindings": [],
        })
    return {
        "schema": "controlcast_mouse_profiles",
        "schema_version": 3,
        "script_version": VERSION,
        "profile_name": name,
        "master_enabled": True,
        "setup_preview_enabled": False,
        "extra_button_count": 20,
        "controls": controls,
        "movement": {
            "enabled": False,
            "scene": "",
            "source": "",
            "range_x": 60.0,
            "sensitivity_x": 0.5,
            "range_y": 40.0,
            "sensitivity_y": 0.5,
            "smoothing": 12.0,
            "return_speed": 4.0,
            "invert_x": False,
            "invert_y": False,
            "saved_rest_valid": False,
            "saved_rest_x": 0.0,
            "saved_rest_y": 0.0,
        },
    }


def write_payload_to_profile(name, payload):
    ensure_profiles_folder()
    path = profile_file_path(name)
    copied = dict(payload)
    copied["schema"] = "controlcast_mouse_profiles"
    copied["schema_version"] = 2
    copied["profile_name"] = name
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(copied, handle, indent=2, ensure_ascii=False)


def load_profile_payload(name):
    path = profile_file_path(name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def reset_mouse_states_for_profile_switch():
    global left_down, right_down
    left_down = False
    right_down = False
    for i in range(EXTRA_BUTTONS):
        extra_down[i] = False
        modifier_only_down[i] = False
    try:
        hide_all_indicator_sources()
    except Exception:
        pass
    try:
        restore_movement_to_origin()
    except Exception:
        pass
    clear_movement_state(keep_origin=False)


def apply_profile_payload(payload, settings):
    global current_profile, master_enabled, setup_preview_enabled, profile_operation_guard
    global extra_button_count
    global saved_rest_x, saved_rest_y, saved_rest_valid

    if payload.get("schema") not in ("controlcast_mouse_profiles", "akinraze_mouse_overlay_profile"):
        raise ValueError("Unsupported MouseCast profile format")

    current_profile = str(payload.get("profile_name", current_profile or "Default")) or "Default"
    master_enabled = bool(payload.get("master_enabled", True))
    setup_preview_enabled = bool(payload.get("setup_preview_enabled", False))
    try:
        extra_button_count = max(0, min(EXTRA_BUTTONS, int(payload.get("extra_button_count", 20))))
    except Exception:
        extra_button_count = 20
    obs.obs_data_set_string(settings, "selected_profile", current_profile)
    obs.obs_data_set_bool(settings, "master_enabled", master_enabled)
    obs.obs_data_set_bool(settings, "setup_preview_enabled", setup_preview_enabled)
    obs.obs_data_set_int(settings, "extra_button_count", extra_button_count)

    by_field = {}
    for control in payload.get("controls", []):
        try:
            by_field[int(control.get("field"))] = control
        except Exception:
            continue

    obs.obs_data_set_string(settings, "left_source", str(by_field.get(1, {}).get("source", "")))
    obs.obs_data_set_string(settings, "right_source", str(by_field.get(2, {}).get("source", "")))

    for i in range(EXTRA_BUTTONS):
        control = by_field.get(i + 3, {})
        key_mode = str(control.get("key_mode", "OBS_HOTKEYS")) or "OBS_HOTKEYS"
        modifiers = str(control.get("modifiers", "NONE")) or "NONE"
        obs.obs_data_set_string(settings, "extra_source_%d" % i, str(control.get("source", "")))
        obs.obs_data_set_string(settings, "extra_key_%d" % i, key_mode)
        obs.obs_data_set_string(settings, "extra_mod_%d" % i, modifiers)
        bindings = control.get("hotkey_bindings", [])
        if key_mode not in ("OBS_HOTKEYS", "UNASSIGNED", "MODIFIER_ONLY"):
            bindings = binding_from_ui(key_mode, modifiers)
        elif key_mode in ("UNASSIGNED", "MODIFIER_ONLY"):
            bindings = []
        load_extra_hotkey_binding(i, bindings)

    movement = payload.get("movement", {})
    obs.obs_data_set_bool(settings, "movement_enabled", bool(movement.get("enabled", False)))
    obs.obs_data_set_string(settings, "movement_scene", str(movement.get("scene", "")))
    obs.obs_data_set_string(settings, "movement_source", str(movement.get("source", "")))
    obs.obs_data_set_double(settings, "range_x", float(movement.get("range_x", 60)))
    obs.obs_data_set_double(settings, "sensitivity_x", float(movement.get("sensitivity_x", 0.5)))
    obs.obs_data_set_double(settings, "range_y", float(movement.get("range_y", 40)))
    obs.obs_data_set_double(settings, "sensitivity_y", float(movement.get("sensitivity_y", 0.5)))
    obs.obs_data_set_double(settings, "smoothing", float(movement.get("smoothing", 12)))
    obs.obs_data_set_double(settings, "return_speed", float(movement.get("return_speed", 4)))
    obs.obs_data_set_bool(settings, "invert_x", bool(movement.get("invert_x", False)))
    obs.obs_data_set_bool(settings, "invert_y", bool(movement.get("invert_y", False)))
    saved_rest_valid = bool(movement.get("saved_rest_valid", False))
    saved_rest_x = float(movement.get("saved_rest_x", 0.0))
    saved_rest_y = float(movement.get("saved_rest_y", 0.0))
    obs.obs_data_set_bool(settings, "saved_rest_valid", saved_rest_valid)
    obs.obs_data_set_double(settings, "saved_rest_x", saved_rest_x)
    obs.obs_data_set_double(settings, "saved_rest_y", saved_rest_y)

    # Apply the profile's settings to runtime state immediately.  During a live
    # switch the outer switch routine uses the guard to prevent recursive profile
    # switching, so temporarily lower only that guard while script_update reads
    # the already-selected profile values.
    previous_guard = profile_operation_guard
    profile_operation_guard = False
    try:
        script_update(settings)
    finally:
        profile_operation_guard = previous_guard
    refresh_extra_hotkey_descriptions()


def switch_profile(name, settings):
    global current_profile, profile_operation_guard
    target = (name or "Default").strip() or "Default"
    if target == current_profile:
        return True

    save_profile(current_profile, show_error=False)
    payload = load_profile_payload(target)
    if payload is None:
        payload = blank_profile_payload(target)
        write_payload_to_profile(target, payload)

    try:
        profile_operation_guard = True
        reset_mouse_states_for_profile_switch()
        apply_profile_payload(payload, settings)
        current_profile = target
        obs.obs_data_set_string(settings, "selected_profile", target)
        log("Live mouse profile switch -> %s" % target)
        return True
    except Exception as exc:
        log("Mouse profile switch failed: %s" % exc)
        show_native_message(
            "MouseCast - Profile Switch Failed",
            "Could not switch to profile '%s'.\n\n%s" % (target, exc),
            error=True,
        )
        return False
    finally:
        profile_operation_guard = False


def migrate_legacy_profile_if_needed():
    ensure_profiles_folder()
    existing_real = [p for p in list_profiles() if os.path.isfile(profile_file_path(p))]
    if existing_real:
        return
    old_path = legacy_profile_path()
    if not os.path.isfile(old_path):
        return
    try:
        with open(old_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        old_name = str(payload.get("profile_name", "Imported Mouse Profile")) or "Imported Mouse Profile"
        write_payload_to_profile(old_name, payload)
        log("Migrated legacy mouse profile to named profiles folder: %s" % old_name)
    except Exception as exc:
        log("Legacy mouse profile migration skipped: %s" % exc)


def execute_profile_action(action, settings):
    global current_profile, profile_operation_guard
    if not action:
        return
    new_name = obs.obs_data_get_string(settings, "new_profile_name").strip()
    try:
        profile_operation_guard = True
        if action == "SAVE":
            save_profile(current_profile)
        elif action == "CREATE_BLANK":
            if not new_name:
                show_native_message("MouseCast", "Enter a New Profile Name first.", warning=True)
            else:
                name = safe_profile_filename(new_name)
                write_payload_to_profile(name, blank_profile_payload(name))
                obs.obs_data_set_string(settings, "selected_profile", name)
                profile_operation_guard = False
                if switch_profile(name, settings):
                    show_profile_refresh_notice(
                        name,
                        "Blank profile '%s' was created and activated successfully." % name,
                    )
                profile_operation_guard = True
        elif action == "DUPLICATE":
            if not new_name:
                show_native_message("MouseCast", "Enter a New Profile Name first.", warning=True)
            else:
                name = safe_profile_filename(new_name)
                save_profile(current_profile, show_error=False)
                write_payload_to_profile(name, build_profile_payload(name))
                current_profile = name
                obs.obs_data_set_string(settings, "selected_profile", name)
                log("Duplicated mouse profile -> %s" % name)
                show_profile_refresh_notice(
                    name,
                    "Profile '%s' was created as a duplicate and is now active." % name,
                )
        elif action == "RESET_BLANK":
            name = current_profile or "Default"
            payload = blank_profile_payload(name)
            write_payload_to_profile(name, payload)
            apply_profile_payload(payload, settings)
            log("Reset mouse profile '%s' to blank" % name)
            show_profile_refresh_notice(
                name,
                "Profile '%s' was reset to a completely blank configuration." % name,
            )

        elif action == "DELETE":
            doomed = current_profile or "Default"
            path = profile_file_path(doomed)
            if os.path.isfile(path):
                os.remove(path)

            if doomed == "Default":
                # Default is the permanent fallback name. Deleting it means:
                # recreate Default as a truly blank profile, rather than
                # resurrecting stale OBS script settings.
                payload = blank_profile_payload("Default")
                write_payload_to_profile("Default", payload)
                current_profile = "Default"
                obs.obs_data_set_string(settings, "selected_profile", "Default")
                apply_profile_payload(payload, settings)
                log("Deleted/reset Default mouse profile to blank")
                show_profile_refresh_notice(
                    "Default",
                    "Default was cleared and recreated as a blank profile.",
                )
            else:
                current_profile = "Default"
                obs.obs_data_set_string(settings, "selected_profile", "Default")
                payload = load_profile_payload("Default")
                if payload is None:
                    payload = blank_profile_payload("Default")
                    write_payload_to_profile("Default", payload)
                apply_profile_payload(payload, settings)
                log("Deleted mouse profile '%s'" % doomed)
                show_profile_refresh_notice(
                    "Default",
                    "Profile '%s' was deleted. Default is now active." % doomed,
                )
    except Exception as exc:
        log("Mouse profile action failed: %s" % exc)
        show_native_message("MouseCast - Profile Action Failed", str(exc), error=True)
    finally:
        # Keep the selected Profile Action in place so repeated operations (for example,
        # deleting several profiles one after another) do not require re-selecting it.
        # Only the confirmation control is reset after each execution.
        obs.obs_data_set_bool(settings, "profile_action_confirm", False)
        if action in ("CREATE_BLANK", "DUPLICATE"):
            obs.obs_data_set_string(settings, "new_profile_name", "")
        profile_operation_guard = False


def add_profile_list(props):
    prop = obs.obs_properties_add_list(
        props, "selected_profile", "Active Mouse Profile",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    for name in list_profiles():
        obs.obs_property_list_add_string(prop, name, name)
    return prop


def add_profile_action_list(props):
    prop = obs.obs_properties_add_list(
        props, "profile_action", "Profile Action",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    choices = [
        ("No action", ""),
        ("Save current profile", "SAVE"),
        ("Create blank profile using New Profile Name", "CREATE_BLANK"),
        ("Duplicate current profile using New Profile Name", "DUPLICATE"),
        ("Reset selected profile to blank", "RESET_BLANK"),
        ("Delete selected profile (permanent)", "DELETE"),
    ]
    for display, value in choices:
        obs.obs_property_list_add_string(prop, display, value)
    return prop


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



def get_group_names():
    """Return only OBS Group sources; ordinary image/browser/etc. sources are excluded."""
    names = []
    sources = None
    try:
        sources = obs.obs_enum_sources()
        if sources:
            for src in sources:
                try:
                    source_id = ""
                    getter = getattr(obs, "obs_source_get_unversioned_id", None)
                    if callable(getter):
                        source_id = getter(src) or ""
                    if not source_id:
                        getter = getattr(obs, "obs_source_get_id", None)
                        if callable(getter):
                            source_id = getter(src) or ""
                    if str(source_id).lower() == "group":
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


def common_key_choices():
    choices = [
        ("Assign / keep binding in OBS Hotkeys", "OBS_HOTKEYS"),
        ("Modifier only (use Modifiers below)", "MODIFIER_ONLY"),
        ("Unassigned", "UNASSIGNED"),
    ]
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        choices.append((ch, "OBS_KEY_%s" % ch))
    for digit in "0123456789":
        choices.append((digit, "OBS_KEY_%s" % digit))
    for number in range(1, 25):
        choices.append(("F%d" % number, "OBS_KEY_F%d" % number))
    choices.extend([
        ("Space", "OBS_KEY_SPACE"),
        ("Enter / Return", "OBS_KEY_RETURN"),
        ("Tab", "OBS_KEY_TAB"),
        ("Escape", "OBS_KEY_ESCAPE"),
        ("Backspace", "OBS_KEY_BACKSPACE"),
        ("Delete", "OBS_KEY_DELETE"),
        ("Insert", "OBS_KEY_INSERT"),
        ("Home", "OBS_KEY_HOME"),
        ("End", "OBS_KEY_END"),
        ("Page Up", "OBS_KEY_PAGEUP"),
        ("Page Down", "OBS_KEY_PAGEDOWN"),
        ("Arrow Up", "OBS_KEY_UP"),
        ("Arrow Down", "OBS_KEY_DOWN"),
        ("Arrow Left", "OBS_KEY_LEFT"),
        ("Arrow Right", "OBS_KEY_RIGHT"),
        ("Minus -", "OBS_KEY_MINUS"),
        ("Equals =", "OBS_KEY_EQUAL"),
        ("Left Bracket [", "OBS_KEY_BRACKETLEFT"),
        ("Right Bracket ]", "OBS_KEY_BRACKETRIGHT"),
        ("Semicolon ;", "OBS_KEY_SEMICOLON"),
        ("Apostrophe '", "OBS_KEY_APOSTROPHE"),
        ("Comma ,", "OBS_KEY_COMMA"),
        ("Period .", "OBS_KEY_PERIOD"),
        ("Slash /", "OBS_KEY_SLASH"),
        ("Backslash \\", "OBS_KEY_BACKSLASH"),
        ("Grave / Tilde `", "OBS_KEY_ASCIITILDE"),
        ("Numpad 0", "OBS_KEY_NUM0"),
        ("Numpad 1", "OBS_KEY_NUM1"),
        ("Numpad 2", "OBS_KEY_NUM2"),
        ("Numpad 3", "OBS_KEY_NUM3"),
        ("Numpad 4", "OBS_KEY_NUM4"),
        ("Numpad 5", "OBS_KEY_NUM5"),
        ("Numpad 6", "OBS_KEY_NUM6"),
        ("Numpad 7", "OBS_KEY_NUM7"),
        ("Numpad 8", "OBS_KEY_NUM8"),
        ("Numpad 9", "OBS_KEY_NUM9"),
        ("Numpad +", "OBS_KEY_NUMPLUS"),
        ("Numpad -", "OBS_KEY_NUMMINUS"),
        ("Numpad *", "OBS_KEY_NUMASTERISK"),
        ("Numpad /", "OBS_KEY_NUMSLASH"),
        ("Numpad .", "OBS_KEY_NUMPERIOD"),
        ("Mouse 1", "OBS_KEY_MOUSE1"),
        ("Mouse 2", "OBS_KEY_MOUSE2"),
        ("Mouse 3", "OBS_KEY_MOUSE3"),
        ("Mouse 4", "OBS_KEY_MOUSE4"),
        ("Mouse 5", "OBS_KEY_MOUSE5"),
    ])
    return choices


def add_key_list(props, key, label):
    prop = obs.obs_properties_add_list(
        props, key, label, obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    for display, value in common_key_choices():
        obs.obs_property_list_add_string(prop, display, value)
    return prop


def add_modifier_list(props, key, label):
    prop = obs.obs_properties_add_list(
        props, key, label, obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    choices = [
        ("None", "NONE"),
        ("Ctrl", "CTRL"),
        ("Shift", "SHIFT"),
        ("Option (Alt)", "ALT"),
        ("Ctrl + Shift", "CTRL+SHIFT"),
        ("Ctrl + Option", "CTRL+ALT"),
        ("Shift + Option", "SHIFT+ALT"),
        ("Ctrl + Shift + Option", "CTRL+SHIFT+ALT"),
        ("Command", "COMMAND"),
        ("Command + Ctrl", "COMMAND+CTRL"),
        ("Command + Shift", "COMMAND+SHIFT"),
        ("Command + Option", "COMMAND+ALT"),
    ]
    for display, value in choices:
        obs.obs_property_list_add_string(prop, display, value)
    return prop


def add_group_list(props, key, label):
    prop = obs.obs_properties_add_list(
        props, key, label, obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    obs.obs_property_list_add_string(prop, "(none)", "")
    for name in get_group_names():
        obs.obs_property_list_add_string(prop, name, name)
    return prop

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

    # OBS Python properties cannot safely use modified/button callbacks in this
    # project (OBS 32.2.2 native callback crashes were observed). The selected
    # count is therefore applied immediately, while the visible property blocks
    # rebuild the next time the Scripts panel is refreshed.

    add_profile_list(props)
    obs.obs_properties_add_text(props, "new_profile_name", "New Profile Name", obs.OBS_TEXT_DEFAULT)
    add_profile_action_list(props)
    # A native obs_properties_add_button callback is intentionally NOT used here.
    # That callback path previously caused native OBS 32.2.2 crashes in this project.
    # A normal boolean property still gives us an explicit, safe "confirm/execute" step.
    obs.obs_properties_add_bool(props, "profile_action_confirm", "Confirm / Execute Selected Profile Action")

    obs.obs_properties_add_bool(props, "master_enabled", "Master Enable Mouse Overlay")
    obs.obs_properties_add_bool(
        props,
        "setup_preview_enabled",
        "Setup / Preview Mode - Show All Assigned Graphics",
    )

    # Readability-only section labels. These are OBS info fields, not editable controls,
    # and do not use property callbacks.
    info_type = getattr(obs, "OBS_TEXT_INFO", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "ui_primary_header", "", info_type)

    add_source_list(props, "left_source", "Left Mouse Button Source")
    add_source_list(props, "right_source", "Right Mouse Button Source")

    # Standalone divider between primary mouse buttons and the extra-input controls.
    obs.obs_properties_add_text(props, "ui_extra_separator", "", info_type)

    # Keep the extra-input count next to the primary mouse-button assignments so
    # users naturally choose how many additional physical/programmable buttons
    # their mouse needs before configuring those inputs.
    count_prop = obs.obs_properties_add_list(
        props,
        "extra_button_count",
        "Extra Inputs to Show (0-20)",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_INT,
    )
    for count in range(0, EXTRA_BUTTONS + 1):
        obs.obs_property_list_add_int(count_prop, str(count), count)

    # Small visual hint directly beneath the count. Using the label keeps the
    # note out of the far-right info-text column in narrow OBS windows.
    obs.obs_properties_add_text(props, "ui_extra_count_note", "I.e. Side Buttons", info_type)

    obs.obs_properties_add_text(props, "ui_extra_header", "", info_type)

    for i in range(extra_button_count):
        fid = field_id(i + 1)
        obs.obs_properties_add_text(props, "ui_extra_divider_%d" % i, "", info_type)
        add_source_list(props, "extra_source_%d" % i, "%s OBS Source / Name" % fid)
        add_key_list(props, "extra_key_%d" % i, "%s Shortcut Key" % fid)
        add_modifier_list(props, "extra_mod_%d" % i, "%s Modifiers" % fid)

    obs.obs_properties_add_text(props, "ui_movement_header", "", info_type)
    obs.obs_properties_add_bool(props, "movement_enabled", "Enable Mouse Movement")
    add_scene_list(props, "movement_scene", "Select OBS Scene")
    add_group_list(props, "movement_source", "Mouse Group (groups only)")

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
    obs.obs_data_set_default_int(settings, "extra_button_count", 20)
    obs.obs_data_set_default_string(settings, "selected_profile", "Default")
    obs.obs_data_set_default_string(settings, "new_profile_name", "")
    obs.obs_data_set_default_string(settings, "profile_action", "")
    obs.obs_data_set_default_bool(settings, "profile_action_confirm", False)
    obs.obs_data_set_default_string(
        settings,
        "profile_refresh_note",
        "Changes apply live. Refresh the Scripts panel after profile or input-count changes. No OBS restart is needed.",
    )
    obs.obs_data_set_default_bool(settings, "master_enabled", True)
    obs.obs_data_set_default_bool(settings, "setup_preview_enabled", False)

    obs.obs_data_set_default_string(settings, "ui_primary_header", "[ PRIMARY BUTTONS ]")
    obs.obs_data_set_default_string(settings, "left_source", "")
    obs.obs_data_set_default_string(settings, "right_source", "")
    obs.obs_data_set_default_string(settings, "ui_extra_count_note", "")
    obs.obs_data_set_default_string(settings, "ui_extra_separator", "────────────────")

    obs.obs_data_set_default_string(settings, "ui_extra_header", "[ EXTRA INPUTS ]")
    for i in range(EXTRA_BUTTONS):
        slot = i + 3
        obs.obs_data_set_default_string(
            settings,
            "ui_extra_divider_%d" % i,
            "[ %02d ] INPUT" % slot,
        )
        obs.obs_data_set_default_string(settings, "extra_source_%d" % i, "")
        obs.obs_data_set_default_string(settings, "extra_key_%d" % i, "OBS_HOTKEYS")
        obs.obs_data_set_default_string(settings, "extra_mod_%d" % i, "NONE")

    obs.obs_data_set_default_string(settings, "ui_movement_header", "[ MOUSE MOVEMENT ]")
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
    global master_enabled, setup_preview_enabled, current_profile, profile_operation_guard, startup_self_check
    global extra_button_count
    global left_function, right_function, extra_functions, extra_keys, extra_modifiers
    global movement_enabled, movement_scene, movement_source
    global range_x, range_y, sensitivity_x, sensitivity_y, smoothing, return_speed
    global invert_x, invert_y, movement_fault
    global saved_rest_x, saved_rest_y, saved_rest_valid, startup_restored
    global movement_origin_x, movement_origin_y, movement_origin_valid

    previous_movement_enabled = movement_enabled
    previous_master_enabled = master_enabled

    if profile_operation_guard:
        return

    obs.obs_data_set_string(
        settings,
        "profile_refresh_note",
        "Changes apply live. Refresh the Scripts panel after profile or input-count changes. No OBS restart is needed.",
    )

    requested_profile = obs.obs_data_get_string(settings, "selected_profile").strip() or "Default"
    if loaded and requested_profile != current_profile:
        switch_profile(requested_profile, settings)
        return

    previous_extra_button_count = extra_button_count
    try:
        extra_button_count = max(0, min(EXTRA_BUTTONS, obs.obs_data_get_int(settings, "extra_button_count")))
    except Exception:
        extra_button_count = 20

    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    previous_preview_enabled = setup_preview_enabled
    setup_preview_enabled = obs.obs_data_get_bool(settings, "setup_preview_enabled")
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
        extra_keys[i] = obs.obs_data_get_string(settings, "extra_key_%d" % i) or "OBS_HOTKEYS"
        extra_modifiers[i] = obs.obs_data_get_string(settings, "extra_mod_%d" % i) or "NONE"
        apply_extra_ui_binding(i)

    # Inputs above the selected count remain stored in the profile so lowering
    # and later raising the count does not destroy assignments. They are simply
    # inactive and hidden while outside the selected range.
    for i in range(extra_button_count, EXTRA_BUTTONS):
        extra_down[i] = False
        modifier_only_down[i] = False
        if extra_sources[i]:
            set_source_visibility(extra_sources[i], False)

    if previous_extra_button_count != extra_button_count:
        log("Extra programmable inputs changed: %d -> %d. Refresh Tools > Scripts to rebuild visible input blocks." %
            (previous_extra_button_count, extra_button_count))

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

    # Preview is visibility-only. Entering Preview forces the movement group
    # home and pauses movement; leaving Preview starts from a fresh cursor baseline.
    if loaded and setup_preview_enabled and not previous_preview_enabled:
        if saved_rest_valid:
            restore_saved_rest_position()
        clear_movement_state(keep_origin=bool(saved_rest_valid))
    elif loaded and previous_preview_enabled and not setup_preview_enabled:
        clear_movement_state(keep_origin=bool(saved_rest_valid))

    # While OBS is already running, an OFF -> ON transition means:
    # "the mouse is arranged exactly where I want it; save this as rest."
    #
    # We intentionally do NOT do this during startup because OBS may have
    # restored a temporary animated position from the previous shutdown.
    if loaded and movement_enabled and not previous_movement_enabled and movement_group_is_valid():
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
            clear_movement_state(keep_origin=True)
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
    elif setup_preview_enabled:
        show_all_indicator_sources()
    else:
        update_click_visibility()

    action = obs.obs_data_get_string(settings, "profile_action")
    confirm_action = obs.obs_data_get_bool(settings, "profile_action_confirm")
    if loaded and action and confirm_action:
        execute_profile_action(action, settings)


def update_click_visibility():
    if not master_enabled:
        hide_all_indicator_sources()
        return

    if setup_preview_enabled:
        show_all_indicator_sources()
        return

    states = {}

    pairs = [(left_source, left_down), (right_source, right_down)]
    pairs.extend(zip(extra_sources[:extra_button_count], extra_down[:extra_button_count]))

    for name, down in pairs:
        if name:
            states[name] = states.get(name, False) or bool(down)

    for name, down in states.items():
        set_source_visibility(name, down)


def extra_button_callback(pressed, index):
    if 0 <= index < EXTRA_BUTTONS:
        if index >= extra_button_count:
            extra_down[index] = False
            return
        extra_down[index] = bool(pressed)
        if master_enabled:
            update_click_visibility()


def script_save(settings):
    obs.obs_data_set_int(settings, "extra_button_count", extra_button_count)
    obs.obs_data_set_string(settings, "selected_profile", current_profile)
    obs.obs_data_set_bool(settings, "master_enabled", master_enabled)
    obs.obs_data_set_bool(settings, "setup_preview_enabled", setup_preview_enabled)
    for i, hotkey_id in enumerate(extra_hotkeys):
        try:
            binding = obs.obs_hotkey_save(hotkey_id)
            obs.obs_data_set_array(settings, "mouse_overlay_extra_%d" % i, binding)
            obs.obs_data_array_release(binding)
        except Exception as exc:
            log("Could not save Extra Button %d hotkey: %s" % (i + 1, exc))

    obs.obs_data_set_bool(settings, "saved_rest_valid", saved_rest_valid)
    if saved_rest_valid:
        obs.obs_data_set_double(settings, "saved_rest_x", saved_rest_x)
        obs.obs_data_set_double(settings, "saved_rest_y", saved_rest_y)

    if loaded:
        save_profile(current_profile, show_error=False)

def clear_movement_state(keep_origin=False, rearm=True):
    global movement_origin_x, movement_origin_y, movement_origin_valid
    global last_cursor, last_time, movement_arm_until
    global offset_x, offset_y, target_x, target_y

    last_cursor = None
    last_time = None
    offset_x = 0.0
    offset_y = 0.0
    target_x = 0.0
    target_y = 0.0

    if rearm:
        movement_arm_until = time.monotonic() + MOVEMENT_ARM_SECONDS

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

        # CRITICAL LAYOUT SAFETY:
        # Movement is allowed ONLY on an OBS Group scene-item. Individual button
        # images/sources are visibility targets and must never have their position
        # changed by MouseCast. This protects the user's arranged button layout.
        is_group_fn = getattr(obs, "obs_sceneitem_is_group", None)
        if not callable(is_group_fn) or not is_group_fn(item):
            return False

        action(item)
        return True

    finally:
        obs.obs_source_release(scene_source)


def movement_group_is_valid():
    """Return True only when the configured movement target resolves to an OBS Group."""
    if not movement_scene or not movement_source:
        return False
    ok = [False]

    def mark_valid(_item):
        ok[0] = True

    try:
        return bool(with_movement_item(mark_valid) and ok[0])
    except Exception:
        return False


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

def get_cursor_position():
    """Return current macOS cursor position as (x, y), or None on failure."""
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


def poll_movement():
    global movement_origin_valid, startup_restored
    global last_cursor, last_time, movement_arm_until
    global offset_x, offset_y, target_x, target_y

    if movement_fault:
        return

    if time.monotonic() < movement_start_after:
        return

    if not master_enabled or setup_preview_enabled:
        return

    if not movement_enabled or not movement_scene or not movement_source:
        return

    # Never move a loose image/source. The configured movement target must be
    # an OBS Group so child button graphics retain their exact local positions.
    if not movement_group_is_valid():
        return

    # A permanent rest point is required before movement is permitted. The
    # OFF -> arrange -> ON workflow captures this safely in script_update().
    if not saved_rest_valid:
        return

    # Restore the saved group home before accepting cursor movement.
    if not startup_restored:
        if not restore_saved_rest_position():
            return
        startup_restored = True

    if not movement_origin_valid:
        movement_origin_x_local = saved_rest_x
        movement_origin_y_local = saved_rest_y
        # restore_saved_rest_position normally establishes these globals. If a
        # settings refresh cleared them, restore again rather than guessing.
        if not restore_saved_rest_position():
            return

    cursor = get_cursor_position()
    if cursor is None:
        last_cursor = None
        last_time = None
        return

    cursor_x, cursor_y = cursor
    now = time.monotonic()

    # First-frame / profile-switch guard. While armed, continuously absorb the
    # current physical cursor coordinates without applying any OBS movement.
    # When the guard expires, the next delta starts from this fresh baseline.
    if now < movement_arm_until:
        last_cursor = (cursor_x, cursor_y)
        last_time = now
        offset_x = 0.0
        offset_y = 0.0
        target_x = 0.0
        target_y = 0.0
        return

    if last_cursor is None:
        last_cursor = (cursor_x, cursor_y)
        last_time = now
        return

    dt = min(0.1, max(0.0, now - last_time))
    dx = cursor_x - last_cursor[0]
    dy = cursor_y - last_cursor[1]

    last_cursor = (cursor_x, cursor_y)
    last_time = now

    # Extra sanity guard: a single implausibly large OS cursor delta is treated
    # as a re-baseline event rather than movement. This can occur after focus,
    # display, RDP, DPI, or profile changes.
    if abs(dx) > 500 or abs(dy) > 500:
        target_x = 0.0
        target_y = 0.0
        offset_x = 0.0
        offset_y = 0.0
        movement_arm_until = now + MOVEMENT_ARM_SECONDS
        return

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

    if not with_movement_item(apply_position):
        clear_movement_state(keep_origin=False)


def is_button_down(mouse_button):
    if cg is None:
        return False
    try:
        return bool(cg.CGEventSourceButtonState(
            CG_EVENT_SOURCE_STATE_COMBINED_SESSION, int(mouse_button)
        ))
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

    if setup_preview_enabled:
        # Setup mode intentionally overrides reactive visibility so users can
        # arrange assigned images/groups without fighting the 60 Hz input poller.
        left_down = False
        right_down = False
        for i in range(EXTRA_BUTTONS):
            extra_down[i] = False
        show_all_indicator_sources()
        return

    try:
        poll_extra_modifier_only_inputs()
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
    global master_enabled, current_profile, startup_self_check
    global left_function, right_function, script_settings_ref

    print("--- %s loaded from %s ---" % (VERSION, __file__))
    ensure_profiles_folder()
    migrate_legacy_profile_if_needed()

    script_settings_ref = settings
    try:
        obs.obs_data_addref(script_settings_ref)
    except Exception:
        pass

    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    current_profile = obs.obs_data_get_string(settings, "selected_profile").strip() or "Default"
    try:
        extra_button_count = max(0, min(EXTRA_BUTTONS, obs.obs_data_get_int(settings, "extra_button_count")))
    except Exception:
        extra_button_count = 20
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
        log("ERROR: macOS CoreGraphics input API is unavailable.")

    left_down = False
    right_down = False
    for i in range(EXTRA_BUTTONS):
        extra_down[i] = False
        modifier_only_down[i] = False

    extra_hotkeys.clear()
    extra_callbacks.clear()

    for i in range(EXTRA_BUTTONS):
        callback = lambda pressed, index=i: extra_button_callback(pressed, index)
        extra_callbacks.append(callback)
        name = "mouse_overlay_extra_%d" % i
        desc = "MouseCast %s %s" % (field_id(i + 1), extra_functions[i])
        hotkey_id = obs.obs_hotkey_register_frontend(name, desc, callback)
        extra_hotkeys.append(hotkey_id)

        binding = obs.obs_data_get_array(settings, name)
        if binding is not None:
            try:
                obs.obs_hotkey_load(hotkey_id, binding)
            finally:
                obs.obs_data_array_release(binding)

    loaded = True

    payload = load_profile_payload(current_profile)
    if payload is not None:
        try:
            apply_profile_payload(payload, settings)
        except Exception as exc:
            log("Initial named mouse profile restore failed: %s" % exc)
            # Safety fallback: use a blank profile rather than stale OBS settings.
            payload = blank_profile_payload(current_profile)
            write_payload_to_profile(current_profile, payload)
            apply_profile_payload(payload, settings)
    else:
        # IMPORTANT:
        # OBS keeps Python script settings even after a profile JSON is deleted
        # (and can keep them after a script is removed/re-added). Never rebuild a
        # missing profile from those stale values. A missing named profile now
        # starts blank unless the legacy migration step created a real profile.
        payload = blank_profile_payload(current_profile)
        write_payload_to_profile(current_profile, payload)
        apply_profile_payload(payload, settings)
        log("No named profile file existed; created blank profile '%s'" % current_profile)

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
        save_profile(current_profile, show_error=False)

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
        modifier_only_down[i] = False

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
