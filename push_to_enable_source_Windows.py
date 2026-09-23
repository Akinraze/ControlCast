import obspython as obs
import ntpath
import json
import os
import base64
import subprocess

NUM_BUTTONS = 30
VERSION = "Push_To_Enable_v2_0_0_PROFILE_SYSTEM"

# Per-button configuration
button_sources = [None] * NUM_BUTTONS
button_inverts = [False] * NUM_BUTTONS
hotkey_ids = [obs.OBS_INVALID_HOTKEY_ID] * NUM_BUTTONS
hotkey_names = [""] * NUM_BUTTONS

# Global state / reference counting
active_counts = {}
target_visibility = {}
rest_visibility = {}

# Package/profile controls
master_enabled = True
profile_name = "Default 30-Button Profile"
profile_path = ""
startup_self_check = True
script_settings_ref = None


def log(message):
    print("[Push to Enable] " + str(message))


def field_id(index):
    return "[%02d]" % index


def default_profile_path():
    try:
        folder = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        folder = os.getcwd()
    return os.path.join(folder, "push_to_enable_30_profile.json")


def normalize_profile_path(path_value):
    value = (path_value or "").strip()
    return value if value else default_profile_path()


def normalize_source_name(value):
    if not value or value == "(none)":
        return None
    return value


def script_description():
    return (
        "<b>%s</b><br><br>"
        "30-button Push-to-Enable overlay with group-aware source control, "
        "stable field IDs, automatic source naming, Master Enable, automatic "
        "JSON backup/restore, and startup self-check.<br><br>"
        "<b>Stable IDs:</b> [01] through [30].<br>"
        "The selected OBS Source name is automatically used in OBS Hotkeys, "
        "diagnostics, and profile data.<br><br>"
        "Profile JSON is exported automatically when settings are saved and "
        "imported automatically at startup when present.<br>"
        "Startup self-check stays silent on PASS and only opens a window for "
        "warnings/errors.<br><br>"
        "Sources may be placed inside OBS groups or nested groups."
    ) % VERSION


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


def add_source_list(props, key, label):
    prop = obs.obs_properties_add_list(
        props,
        key,
        label,
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING
    )
    obs.obs_property_list_add_string(prop, "(none)", "")
    for name in get_source_names():
        obs.obs_property_list_add_string(prop, name, name)
    return prop


def source_exists(name):
    if not name:
        return False
    src = obs.obs_get_source_by_name(name)
    if src is None:
        return False
    obs.obs_source_release(src)
    return True


def hotkey_bindings_to_plain(hotkey_id):
    if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
        return []

    arr = obs.obs_hotkey_save(hotkey_id)
    if arr is None:
        return []

    result = []
    try:
        count = obs.obs_data_array_count(arr)
        for index in range(count):
            item = obs.obs_data_array_item(arr, index)
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
    finally:
        obs.obs_data_array_release(arr)

    return result


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


def show_native_message(title, message, error=False, warning=False):
    """Open a detached, non-blocking Windows result dialog."""
    try:
        icon = "Information"
        if error:
            icon = "Error"
        elif warning:
            icon = "Warning"

        title_b64 = base64.b64encode(str(title).encode("utf-8")).decode("ascii")
        message_b64 = base64.b64encode(str(message).encode("utf-8")).decode("ascii")

        ps_script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Add-Type -AssemblyName PresentationFramework\n"
            "$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__TITLE__'))\n"
            "$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__MESSAGE__'))\n"
            "[System.Windows.MessageBox]::Show($message,$title,"
            "[System.Windows.MessageBoxButton]::OK,"
            "[System.Windows.MessageBoxImage]::__ICON__) | Out-Null\n"
        )
        ps_script = ps_script.replace("__TITLE__", title_b64)
        ps_script = ps_script.replace("__MESSAGE__", message_b64)
        ps_script = ps_script.replace("__ICON__", icon)

        encoded_command = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")

        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS

        subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded_command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        return True
    except Exception as exc:
        log("Could not open diagnostic window: %s" % exc)
        return False


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_text(
        props, "profile_name", "Profile Name", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_bool(
        props, "master_enabled", "Master Enable 30-Button Overlay"
    )
    obs.obs_properties_add_text(
        props, "profile_path", "Profile JSON Path", obs.OBS_TEXT_DEFAULT
    )

    for i in range(NUM_BUTTONS):
        add_source_list(
            props,
            "source_%d" % i,
            "%s OBS Source / Name" % field_id(i + 1)
        )
        obs.obs_properties_add_bool(
            props,
            "invert_%d" % i,
            "%s Invert (hide while pressed)" % field_id(i + 1)
        )

    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "profile_name", "Default 30-Button Profile")
    obs.obs_data_set_default_bool(settings, "master_enabled", True)
    obs.obs_data_set_default_string(settings, "profile_path", default_profile_path())

    for i in range(NUM_BUTTONS):
        obs.obs_data_set_default_string(settings, "source_%d" % i, "")
        obs.obs_data_set_default_bool(settings, "invert_%d" % i, False)


def rebuild_visibility_maps():
    target_visibility.clear()
    rest_visibility.clear()

    for i in range(NUM_BUTTONS):
        src = button_sources[i]
        if src is None:
            continue

        invert = button_inverts[i]
        target_visibility[src] = not invert
        rest_visibility[src] = invert

        if src not in active_counts:
            active_counts[src] = 0


def refresh_hotkey_descriptions():
    setter = getattr(obs, "obs_hotkey_set_description", None)
    if not callable(setter):
        return

    for i, hotkey_id in enumerate(hotkey_ids):
        if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
            continue

        source_name = button_sources[i]
        label = source_name if source_name else ("Button %d" % (i + 1))
        desc = "Push to Enable %s %s" % (field_id(i + 1), label)

        try:
            setter(hotkey_id, desc)
        except Exception as exc:
            log("Could not update hotkey description %s: %s" % (field_id(i + 1), exc))


def script_update(settings):
    global button_sources, button_inverts
    global master_enabled, profile_name, profile_path

    old_sources = set(active_counts.keys())

    profile_name = obs.obs_data_get_string(settings, "profile_name").strip()
    if not profile_name:
        profile_name = "Default 30-Button Profile"

    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    profile_path = normalize_profile_path(obs.obs_data_get_string(settings, "profile_path"))

    new_sources = []
    new_inverts = []

    for i in range(NUM_BUTTONS):
        src_name = normalize_source_name(obs.obs_data_get_string(settings, "source_%d" % i))
        new_sources.append(src_name)
        new_inverts.append(obs.obs_data_get_bool(settings, "invert_%d" % i))

    button_sources = new_sources
    button_inverts = new_inverts

    rebuild_visibility_maps()

    current_sources = set(src for src in button_sources if src)

    removed_sources = old_sources - current_sources
    for src in removed_sources:
        if active_counts.get(src, 0) == 0:
            set_source_visibility(src, False)
        active_counts.pop(src, None)

    refresh_hotkey_descriptions()

    if not master_enabled:
        hide_all_assigned_sources()


def hotkey_callback(pressed, idx):
    if idx < 0 or idx >= NUM_BUTTONS:
        return

    src_name = button_sources[idx]
    if src_name is None:
        return

    if not master_enabled:
        if not pressed and active_counts.get(src_name, 0) > 0:
            active_counts[src_name] = max(0, active_counts[src_name] - 1)
        return

    if src_name not in active_counts:
        active_counts[src_name] = 0
        target_visibility[src_name] = not button_inverts[idx]
        rest_visibility[src_name] = button_inverts[idx]

    if pressed:
        prev_count = active_counts[src_name]
        active_counts[src_name] += 1
        if prev_count == 0:
            set_source_visibility(src_name, target_visibility.get(src_name, False))
    else:
        if active_counts[src_name] > 0:
            active_counts[src_name] -= 1
        if active_counts[src_name] == 0:
            set_source_visibility(src_name, rest_visibility.get(src_name, False))


def set_items_visibility_recursive(scene, source_name, visible):
    """Show/hide every matching source found in a scene, including groups."""
    if scene is None or source_name is None:
        return

    items = obs.obs_scene_enum_items(scene)
    if not items:
        return

    try:
        for item in items:
            item_src = obs.obs_sceneitem_get_source(item)

            if item_src is not None:
                try:
                    if obs.obs_source_get_name(item_src) == source_name:
                        if obs.obs_sceneitem_visible(item) != visible:
                            obs.obs_sceneitem_set_visible(item, visible)
                except Exception as exc:
                    log("Visibility error for '%s': %s" % (source_name, exc))

            try:
                if obs.obs_sceneitem_is_group(item):
                    group_scene = obs.obs_sceneitem_group_get_scene(item)
                    if group_scene is not None:
                        set_items_visibility_recursive(group_scene, source_name, visible)
            except Exception as exc:
                log("Group traversal error for '%s': %s" % (source_name, exc))
    finally:
        obs.sceneitem_list_release(items)


def set_source_visibility(source_name, visible):
    if source_name is None:
        return

    scene_sources = obs.obs_frontend_get_scenes()
    if scene_sources is None:
        return

    try:
        for scn_src in scene_sources:
            scene = obs.obs_scene_from_source(scn_src)
            if scene is not None:
                set_items_visibility_recursive(scene, source_name, visible)
    finally:
        obs.source_list_release(scene_sources)


def hide_all_assigned_sources():
    seen = set()
    for src in button_sources:
        if src and src not in seen:
            seen.add(src)
            set_source_visibility(src, False)


def collect_self_check():
    errors = []
    warnings = []
    passes = []

    passes.append("Profile: %s" % profile_name)

    assigned_source_fields = {}

    for i in range(NUM_BUTTONS):
        fid = field_id(i + 1)
        src = button_sources[i]

        if not src:
            continue

        if source_exists(src):
            passes.append("%s source found: %s" % (fid, src))
        else:
            errors.append("%s source missing: %s" % (fid, src))

        assigned_source_fields.setdefault(src, []).append(fid)

        bindings = hotkey_bindings_to_plain(hotkey_ids[i])
        if bindings:
            passes.append("%s bound input: %s" % (fid, describe_bindings(bindings)))
        else:
            warnings.append("%s %s has a source but no OBS hotkey binding" % (fid, src))

    for source_name, fields in assigned_source_fields.items():
        if len(fields) > 1:
            warnings.append(
                "Duplicate OBS source '%s' assigned to %s"
                % (source_name, ", ".join(fields))
            )

    configured = sum(1 for src in button_sources if src)
    if configured == 0:
        warnings.append("No button sources are configured")
    else:
        passes.append("%d button source(s) configured" % configured)

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
        "30-Button Overlay Self-Check: %s" % status,
        "Profile: %s" % profile_name,
        "",
    ]

    if status == "PASS":
        body.append("All configured features passed self-check.")
        body.append("")

    body.extend(lines)
    return "\n".join(body)


def run_startup_self_check_once():
    try:
        obs.timer_remove(run_startup_self_check_once)
    except Exception:
        pass

    status, lines = collect_self_check()
    message = format_self_check_text(status, lines)
    log(message.replace("\n", " | "))

    if status != "PASS":
        show_native_message(
            "30-Button Overlay Self-Check - %s" % status,
            message,
            error=(status == "ERROR"),
            warning=(status == "WARNING"),
        )


def build_profile_payload():
    buttons = []

    for i in range(NUM_BUTTONS):
        buttons.append({
            "field": i + 1,
            "field_id": field_id(i + 1),
            "source": button_sources[i] or "",
            "invert": bool(button_inverts[i]),
            "hotkey_bindings": hotkey_bindings_to_plain(hotkey_ids[i]),
        })

    return {
        "schema": "akinraze_push_to_enable_30_profile",
        "schema_version": 1,
        "script_version": VERSION,
        "profile_name": profile_name,
        "master_enabled": bool(master_enabled),
        "buttons": buttons,
        "diagnostics": {
            "startup_self_check": True,
            "popup_on_pass": False,
            "popup_on_warning_or_error": True,
        },
    }


def export_profile_json():
    path = normalize_profile_path(profile_path)

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(build_profile_payload(), handle, indent=2, ensure_ascii=False)

        if not os.path.isfile(path):
            raise RuntimeError("Profile file was not created")

        log("Automatic profile export completed: %s" % path)
        return True

    except Exception as exc:
        log("Automatic profile export FAILED: %s" % exc)
        show_native_message(
            "30-Button Overlay - Profile Export Failed",
            "Automatic profile backup failed.\n\nFile: %s\n\n%s" % (path, exc),
            error=True,
        )
        return False


def apply_profile_payload(payload, settings):
    if payload.get("schema") != "akinraze_push_to_enable_30_profile":
        raise ValueError("Not a compatible 30-Button Overlay profile JSON")

    obs.obs_data_set_string(
        settings,
        "profile_name",
        str(payload.get("profile_name", "Imported 30-Button Profile"))
    )
    obs.obs_data_set_bool(
        settings,
        "master_enabled",
        bool(payload.get("master_enabled", True))
    )

    by_field = {}
    for item in payload.get("buttons", []):
        try:
            by_field[int(item.get("field"))] = item
        except Exception:
            continue

    for i in range(NUM_BUTTONS):
        item = by_field.get(i + 1, {})

        obs.obs_data_set_string(
            settings,
            "source_%d" % i,
            str(item.get("source", ""))
        )
        obs.obs_data_set_bool(
            settings,
            "invert_%d" % i,
            bool(item.get("invert", False))
        )

        arr = plain_dicts_to_binding_array(item.get("hotkey_bindings", []))
        try:
            obs.obs_hotkey_load(hotkey_ids[i], arr)
            obs.obs_data_set_array(settings, hotkey_names[i], arr)
        finally:
            obs.obs_data_array_release(arr)

    script_update(settings)
    refresh_hotkey_descriptions()


def import_profile_json(settings):
    path = normalize_profile_path(profile_path)

    try:
        if not os.path.isfile(path):
            return False

        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        apply_profile_payload(payload, settings)
        log("Automatic profile import completed: %s" % path)
        return True

    except Exception as exc:
        log("Automatic profile import FAILED: %s" % exc)
        show_native_message(
            "30-Button Overlay - Profile Import Failed",
            "Automatic profile restore failed.\n\nFile: %s\n\n%s" % (path, exc),
            error=True,
        )
        return False


def script_load(settings):
    global script_settings_ref
    global master_enabled, profile_name, profile_path

    print("--- %s loaded from %s ---" % (VERSION, __file__))

    script_settings_ref = settings
    try:
        obs.obs_data_addref(script_settings_ref)
    except Exception:
        pass

    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    profile_name = obs.obs_data_get_string(settings, "profile_name").strip()
    if not profile_name:
        profile_name = "Default 30-Button Profile"
    profile_path = normalize_profile_path(obs.obs_data_get_string(settings, "profile_path"))

    for i in range(NUM_BUTTONS):
        hk_name = "push_to_enable_button_%d" % (i + 1)
        hotkey_names[i] = hk_name

        src = normalize_source_name(obs.obs_data_get_string(settings, "source_%d" % i))
        label = src if src else ("Button %d" % (i + 1))

        hotkey_ids[i] = obs.obs_hotkey_register_frontend(
            hk_name,
            "Push to Enable %s %s" % (field_id(i + 1), label),
            lambda pressed, idx=i: hotkey_callback(pressed, idx)
        )

        saved_array = obs.obs_data_get_array(settings, hk_name)
        if saved_array is not None:
            try:
                obs.obs_hotkey_load(hotkey_ids[i], saved_array)
            finally:
                obs.obs_data_array_release(saved_array)

    # Load current OBS settings first.
    script_update(settings)

    # Then restore JSON if one already exists.
    import_profile_json(settings)

    refresh_hotkey_descriptions()

    try:
        obs.timer_add(run_startup_self_check_once, 2200)
    except Exception as exc:
        log("Could not schedule startup self-check: %s" % exc)


def script_save(settings):
    for i in range(NUM_BUTTONS):
        try:
            save_array = obs.obs_hotkey_save(hotkey_ids[i])
            obs.obs_data_set_array(settings, hotkey_names[i], save_array)
            obs.obs_data_array_release(save_array)
        except Exception as exc:
            log("Could not save hotkey %s: %s" % (field_id(i + 1), exc))

    obs.obs_data_set_string(settings, "profile_name", profile_name)
    obs.obs_data_set_bool(settings, "master_enabled", master_enabled)
    obs.obs_data_set_string(settings, "profile_path", normalize_profile_path(profile_path))

    export_profile_json()


def script_unload():
    global script_settings_ref

    hide_all_assigned_sources()

    if script_settings_ref is not None:
        try:
            obs.obs_data_release(script_settings_ref)
        except Exception:
            pass
        script_settings_ref = None
