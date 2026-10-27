import obspython as obs
import json
import os
import re
import ctypes
import shutil
import subprocess
import time


MAX_BUTTONS = 65
INPUT_COUNT_CHOICES = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65]
VERSION = "InputCast_Linux_v4_0_3_RC1"
PROFILE_DIRNAME = "ControlCast_profiles"
LEGACY_PROFILE_FILENAME = "push_to_enable_30_profile.json"

# Runtime button configuration
button_sources = [None] * MAX_BUTTONS
button_inverts = [False] * MAX_BUTTONS
button_keys = ["OBS_HOTKEYS"] * MAX_BUTTONS
button_modifiers = ["NONE"] * MAX_BUTTONS
hotkey_ids = [obs.OBS_INVALID_HOTKEY_ID] * MAX_BUTTONS
hotkey_names = ["push_to_enable_button_%d" % (i + 1) for i in range(MAX_BUTTONS)]
cached_hotkey_bindings = [[] for _ in range(MAX_BUTTONS)]

# Visibility/reference-count state
active_counts = {}
target_visibility = {}
rest_visibility = {}

# Profile/runtime state
master_enabled = True
setup_preview_enabled = False
input_count = 30
current_profile = "Default"
profile_operation_guard = False
script_settings_ref = None
loaded = False

# Linux modifier-only polling state. OBS hotkey bindings require a primary key,
# so modifier-only shortcuts (Shift, Ctrl, Alt, Super, or combinations) are
# read directly from the X11 display server and then fed through the same
# visibility/reference-count logic as normal OBS hotkeys.
MODIFIER_POLL_MS = 16
modifier_only_down = [False] * MAX_BUTTONS


# ------------------------------
# Logging / paths / text helpers
# ------------------------------

def log(message):
    print("[InputCast] " + scrub_text(message))


# ------------------------------
# v4.0.2 safety helpers
# ------------------------------
# PRIVACY NOTE: ControlCast reads only the inputs you configure (mouse buttons,
# the specific keys / modifiers you assign, and mouse-wheel events). It does not
# record, store or transmit keystrokes, cursor paths or window contents, and it
# contains no network code.

_WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % n for n in range(1, 10)}
    | {"LPT%d" % n for n in range(1, 10)}
)


def scrub_text(text):
    """Replace your home folder path with ~ so logs and pop-ups are safe to share."""
    try:
        text = str(text)
        home = os.path.expanduser("~")
        if home and home != "~" and len(home) > 3:
            for variant in {home, home.replace("\\", "/")}:
                text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)
        return text
    except Exception:
        return str(text)


def clamp_finite(value, default, low, high):
    """float(value) limited to [low, high]; NaN, infinity and junk fall back to default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return max(float(low), min(float(high), number))


def _is_valid_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            json.load(handle)
        return True
    except Exception:
        return False


def atomic_write_json(path, payload):
    """Write JSON so a crash or power loss can never leave a half-written profile.

    Data goes to a temp file first, the previous good file is kept as .bak, and the
    temp file then replaces the real one in a single step.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass

    if os.path.isfile(path) and _is_valid_json_file(path):
        try:
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass

    last_error = None
    for _attempt in range(5):  # antivirus scanners can briefly lock files on Windows
        try:
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise last_error


def read_json_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json_with_backup(path, label):
    """Read a profile; if it is corrupt, fall back to the last good .bak copy."""
    try:
        return read_json_file(path)
    except (ValueError, OSError) as exc:
        log("Profile '%s' could not be read (%s)." % (label, exc))
        backup = path + ".bak"
        if os.path.isfile(backup):
            payload = read_json_file(backup)
            log("Recovered profile '%s' from its backup copy." % label)
            return payload
        raise


def remove_profile_files(path):
    """Delete a profile and its .bak / .tmp companions so deleted stays deleted."""
    for candidate in (path, path + ".bak", path + ".tmp"):
        if os.path.isfile(candidate):
            try:
                os.remove(candidate)
            except OSError as exc:
                log("Could not remove %s: %s" % (os.path.basename(candidate), exc))


def legacy_modifier_only_code(bindings):
    """Older profiles stored a modifier-only input as an OBS hotkey with no key.

    OBS can never fire those, so convert them to the polled MODIFIER_ONLY mode.
    Returns a modifier code such as "SHIFT" or "CTRL+ALT", or None.
    """
    try:
        for binding in bindings or []:
            key = str(binding.get("key", "") or "OBS_KEY_NONE")
            if key not in ("OBS_KEY_NONE", ""):
                continue
            mods = []
            if binding.get("command"):
                mods.append("COMMAND")
            if binding.get("control"):
                mods.append("CTRL")
            if binding.get("shift"):
                mods.append("SHIFT")
            if binding.get("alt"):
                mods.append("ALT")
            if mods:
                return "+".join(mods)
    except Exception:
        pass
    return None


def show_native_message(title, message, error=False, warning=False):
    """Show a desktop pop-up without blocking OBS.

    Uses zenity (GNOME and most desktops) or kdialog (KDE) when installed. The
    text is passed as arguments, never through a shell, so it cannot change what
    runs. When neither tool exists, the message is still in the Script Log.
    """
    safe_title = scrub_text(title)
    safe_message = scrub_text(message)
    kind = "error" if error else ("warning" if warning else "info")
    candidates = [
        ["zenity", "--%s" % kind, "--no-markup", "--title", safe_title, "--text", safe_message],
        ["kdialog", "--title", safe_title,
         {"error": "--error", "warning": "--sorry", "info": "--msgbox"}[kind], safe_message],
    ]
    for args in candidates:
        if shutil.which(args[0]) is None:
            continue
        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            return True
        except Exception as exc:
            log("Could not open %s pop-up: %s" % (args[0], exc))
    log("No pop-up tool (zenity or kdialog) found; see the message above in the Script Log.")
    return False


# ------------------------------
# Linux X11 input layer
# ------------------------------
# Reads the mouse buttons, cursor position and modifier keys from the X11 display
# server (libX11, part of every X11 desktop). Nothing is recorded, stored or sent
# anywhere. On Wayland desktops the X11 server only sees XWayland windows, so
# detection is limited there (the startup self-check says so).

X11_SHIFT_L = 0xFFE1
X11_SHIFT_R = 0xFFE2
X11_CONTROL_L = 0xFFE3
X11_CONTROL_R = 0xFFE4
X11_ALT_L = 0xFFE9
X11_ALT_R = 0xFFEA
X11_SUPER_L = 0xFFEB
X11_SUPER_R = 0xFFEC

X11_BUTTON1_MASK = 1 << 8   # left button
X11_BUTTON3_MASK = 1 << 10  # right button

x11 = None
x11_display = None
x11_root = 0
x11_failed = False
_x11_keycodes = {}
_x11_keymap = None
_x11_keymap_time = 0.0


def is_wayland_session():
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def x11_ready():
    """Open the X11 connection on first use. Returns True when input can be read."""
    global x11, x11_display, x11_root, x11_failed
    if x11_display:
        return True
    if x11_failed:
        return False
    try:
        if x11 is None:
            lib = None
            for name in ("libX11.so.6", "libX11.so"):
                try:
                    lib = ctypes.CDLL(name)
                    break
                except OSError:
                    continue
            if lib is None:
                raise OSError("libX11 not found")
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            lib.XDefaultRootWindow.restype = ctypes.c_ulong
            lib.XQueryPointer.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_uint),
            ]
            lib.XQueryPointer.restype = ctypes.c_int
            lib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char * 32]
            lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.XKeysymToKeycode.restype = ctypes.c_ubyte
            x11 = lib
        display = x11.XOpenDisplay(None)
        if not display:
            raise OSError("no X11 display (is DISPLAY set?)")
        x11_display = display
        x11_root = x11.XDefaultRootWindow(display)
        _x11_keycodes.clear()
        return True
    except Exception as exc:
        x11_failed = True
        log("X11 input unavailable: %s" % exc)
        return False


def x11_close():
    global x11_display, x11_failed
    if x11 is not None and x11_display:
        try:
            x11.XCloseDisplay(x11_display)
        except Exception:
            pass
    x11_display = None
    x11_failed = False


def x11_pointer_state():
    """Return (x, y, button_mask) for the pointer, or None."""
    if not x11_ready():
        return None
    try:
        root = ctypes.c_ulong()
        child = ctypes.c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        if not x11.XQueryPointer(
            x11_display, x11_root,
            ctypes.byref(root), ctypes.byref(child),
            ctypes.byref(rx), ctypes.byref(ry),
            ctypes.byref(wx), ctypes.byref(wy),
            ctypes.byref(mask),
        ):
            return None
        return (float(rx.value), float(ry.value), int(mask.value))
    except Exception:
        return None


def is_x11_key_down(keysym):
    """True while the key for this X11 keysym is physically held."""
    global _x11_keymap, _x11_keymap_time
    if not x11_ready():
        return False
    try:
        keycode = _x11_keycodes.get(keysym)
        if keycode is None:
            keycode = int(x11.XKeysymToKeycode(x11_display, keysym))
            _x11_keycodes[keysym] = keycode
        if not keycode:
            return False
        now = time.monotonic()
        if _x11_keymap is None or now - _x11_keymap_time > 0.005:
            buffer = (ctypes.c_char * 32)()
            x11.XQueryKeymap(x11_display, buffer)
            _x11_keymap = bytes(buffer.raw)
            _x11_keymap_time = now
        return bool(_x11_keymap[keycode // 8] & (1 << (keycode % 8)))
    except Exception:
        return False


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
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip())
    cleaned = cleaned.rstrip(" .")
    if cleaned.split(".")[0].strip().upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned  # CON, NUL, COM1 ... are reserved device names on Windows
    cleaned = cleaned[:100].rstrip(" .")
    return cleaned or "Default"


def profile_path(name):
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
        log("Could not enumerate profiles: %s" % exc)
    if "Default" not in names:
        names.append("Default")
    return sorted(set(names), key=lambda value: value.lower())


def normalize_source_name(value):
    if not value or value == "(none)":
        return None
    return value


def normalize_input_count_value(value):
    try:
        requested = int(value)
    except Exception:
        requested = 30
    return min(INPUT_COUNT_CHOICES, key=lambda c: (abs(c - requested), -c))


# ------------------------------
# UI list helpers
# ------------------------------

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


def add_source_list(props, key, label, source_names):
    prop = obs.obs_properties_add_list(
        props, key, label, obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    obs.obs_property_list_add_string(prop, "(none)", "")
    for name in source_names:
        obs.obs_property_list_add_string(prop, name, name)
    return prop


def add_profile_list(props):
    prop = obs.obs_properties_add_list(
        props, "selected_profile", "Active Profile",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    for name in list_profiles():
        obs.obs_property_list_add_string(prop, name, name)
    return prop


def add_input_count_list(props):
    prop = obs.obs_properties_add_list(
        props, "input_count", "Programmable Input Count",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_INT
    )
    for count in INPUT_COUNT_CHOICES:
        obs.obs_property_list_add_int(prop, str(count), count)
    return prop


def common_key_choices():
    choices = [
        ("Assign / keep binding in OBS Hotkeys", "OBS_HOTKEYS"),
        ("Modifier only (use Modifiers below)", "MODIFIER_ONLY"),
        ("Unassigned", "UNASSIGNED"),
    ]

    # Letters
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        choices.append((ch, "OBS_KEY_%s" % ch))

    # Number row
    for digit in "0123456789":
        choices.append((digit, "OBS_KEY_%s" % digit))

    # Function keys
    for number in range(1, 25):
        choices.append(("F%d" % number, "OBS_KEY_F%d" % number))

    # Common editing/navigation keys
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
        ("Alt", "ALT"),
        ("Ctrl + Shift", "CTRL+SHIFT"),
        ("Ctrl + Alt", "CTRL+ALT"),
        ("Shift + Alt", "SHIFT+ALT"),
        ("Ctrl + Shift + Alt", "CTRL+SHIFT+ALT"),
        ("Super (Win)", "COMMAND"),
        ("Super + Ctrl", "COMMAND+CTRL"),
        ("Super + Shift", "COMMAND+SHIFT"),
        ("Super + Alt", "COMMAND+ALT"),
    ]
    for display, value in choices:
        obs.obs_property_list_add_string(prop, display, value)
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


# ------------------------------
# OBS hotkey serialization
# ------------------------------

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



def slot_hotkey_bindings(index):
    if not (0 <= index < MAX_BUTTONS):
        return []
    hotkey_id = hotkey_ids[index]
    if hotkey_id != obs.OBS_INVALID_HOTKEY_ID:
        bindings = hotkey_bindings_to_plain(hotkey_id)
        cached_hotkey_bindings[index] = list(bindings)
        return bindings
    return list(cached_hotkey_bindings[index])


def register_hotkey_slot(index):
    if not (0 <= index < MAX_BUTTONS):
        return False
    if hotkey_ids[index] != obs.OBS_INVALID_HOTKEY_ID:
        return True

    source = button_sources[index]
    label = source if source else ("Input %d" % (index + 1))
    hotkey_id = obs.obs_hotkey_register_frontend(
        hotkey_names[index],
        "InputCast %s %s" % (field_id(index + 1), label),
        lambda pressed, idx=index: hotkey_callback(pressed, idx),
    )
    hotkey_ids[index] = hotkey_id

    bindings = list(cached_hotkey_bindings[index])
    if not bindings and script_settings_ref is not None:
        saved = obs.obs_data_get_array(script_settings_ref, hotkey_names[index])
        if saved is not None:
            try:
                count = obs.obs_data_array_count(saved)
                temp = []
                for j in range(count):
                    item = obs.obs_data_array_item(saved, j)
                    if item is None:
                        continue
                    try:
                        temp.append({
                            "key": obs.obs_data_get_string(item, "key"),
                            "shift": bool(obs.obs_data_get_bool(item, "shift")),
                            "control": bool(obs.obs_data_get_bool(item, "control")),
                            "alt": bool(obs.obs_data_get_bool(item, "alt")),
                            "command": bool(obs.obs_data_get_bool(item, "command")),
                        })
                    finally:
                        obs.obs_data_release(item)
                bindings = temp
                cached_hotkey_bindings[index] = list(temp)
            finally:
                obs.obs_data_array_release(saved)

    if bindings:
        load_hotkey_binding(index, bindings)
    return hotkey_id != obs.OBS_INVALID_HOTKEY_ID


def unregister_hotkey_slot(index):
    if not (0 <= index < MAX_BUTTONS):
        return
    hotkey_id = hotkey_ids[index]
    if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
        return

    try:
        cached_hotkey_bindings[index] = hotkey_bindings_to_plain(hotkey_id)
        if script_settings_ref is not None:
            arr = plain_dicts_to_binding_array(cached_hotkey_bindings[index])
            try:
                obs.obs_data_set_array(script_settings_ref, hotkey_names[index], arr)
            finally:
                obs.obs_data_array_release(arr)
    except Exception as exc:
        log("Could not cache hotkey %s before unregister: %s" % (field_id(index + 1), exc))

    unregister = getattr(obs, "obs_hotkey_unregister", None)
    if callable(unregister):
        try:
            unregister(hotkey_id)
        except Exception as exc:
            log("Could not unregister hotkey %s: %s" % (field_id(index + 1), exc))
    hotkey_ids[index] = obs.OBS_INVALID_HOTKEY_ID
    modifier_only_down[index] = False


def sync_registered_hotkeys(active_count):
    desired = normalize_input_count_value(active_count)
    for i in range(MAX_BUTTONS):
        if i < desired:
            register_hotkey_slot(i)
        else:
            unregister_hotkey_slot(i)

def binding_from_ui(key_name, modifier_code):
    if not key_name or key_name in ("OBS_HOTKEYS", "UNASSIGNED", "MODIFIER_ONLY"):
        return []
    mods = set((modifier_code or "NONE").split("+"))
    # OBS hotkey bindings have native modifier fields for Ctrl/Shift/Alt/Command,
    # but not Caps Lock. Any shortcut containing CAPS is polled directly below.
    if "CAPS" in mods:
        return []
    return [{
        "key": key_name,
        "shift": "SHIFT" in mods,
        "control": "CTRL" in mods,
        "alt": "ALT" in mods,
        "command": "COMMAND" in mods,
    }]


def load_hotkey_binding(index, bindings):
    if not (0 <= index < MAX_BUTTONS):
        return
    cached_hotkey_bindings[index] = list(bindings or [])
    if hotkey_ids[index] == obs.OBS_INVALID_HOTKEY_ID:
        return
    arr = plain_dicts_to_binding_array(bindings)
    try:
        obs.obs_hotkey_load(hotkey_ids[index], arr)
        if script_settings_ref is not None:
            obs.obs_data_set_array(script_settings_ref, hotkey_names[index], arr)
    finally:
        obs.obs_data_array_release(arr)


def apply_ui_binding(index):
    key_name = button_keys[index]
    if key_name == "OBS_HOTKEYS":
        return
    # UNASSIGNED and MODIFIER_ONLY intentionally clear any previous OBS binding.
    # Modifier-only input is read directly from X11 instead.
    bindings = binding_from_ui(key_name, button_modifiers[index])
    load_hotkey_binding(index, bindings)


def is_vk_down(vk_code):
    """Windows key codes are not used on Linux."""
    return False


def key_name_to_vk(key_name):
    """Caps Lock key combinations are Windows-only; nothing is mapped on Linux."""
    return None


def ui_key_is_down(key_name):
    return False


def modifier_combo_is_down(modifier_code):
    mods = set((modifier_code or "NONE").split("+"))
    mods.discard("NONE")
    if not mods or "CAPS" in mods:
        # Caps Lock combinations are a Windows-only feature (see self-check).
        return False
    checks = []
    if "SHIFT" in mods:
        checks.append(is_x11_key_down(X11_SHIFT_L) or is_x11_key_down(X11_SHIFT_R))
    if "CTRL" in mods:
        checks.append(is_x11_key_down(X11_CONTROL_L) or is_x11_key_down(X11_CONTROL_R))
    if "ALT" in mods:
        checks.append(is_x11_key_down(X11_ALT_L) or is_x11_key_down(X11_ALT_R))
    if "COMMAND" in mods:
        checks.append(is_x11_key_down(X11_SUPER_L) or is_x11_key_down(X11_SUPER_R))
    return bool(checks) and all(checks)


def poll_modifier_only_inputs():
    # Poll active Modifier Only slots plus any direct shortcut containing CAPS.
    # OBS does not expose Caps Lock as a native hotkey modifier, so CAPS combos
    # are handled here and routed through the normal InputCast callback path.
    for i in range(MAX_BUTTONS):
        should_be_down = False
        if i < input_count:
            key_mode = button_keys[i]
            modifier_code = button_modifiers[i]
            mods = set((modifier_code or "NONE").split("+"))

            if key_mode == "MODIFIER_ONLY":
                should_be_down = modifier_combo_is_down(modifier_code)
            elif "CAPS" in mods and key_mode not in ("OBS_HOTKEYS", "UNASSIGNED"):
                should_be_down = modifier_combo_is_down(modifier_code) and ui_key_is_down(key_mode)

        if should_be_down != modifier_only_down[i]:
            modifier_only_down[i] = should_be_down
            hotkey_callback(should_be_down, i)


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
            pieces.append("Win/Cmd")
        pieces.append(pretty_key_name(binding.get("key")))
        labels.append("+".join(pieces))
    return " / ".join(labels)


# ------------------------------
# Source visibility
# ------------------------------

def set_items_visibility_recursive(scene, source_name, visible):
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


def rebuild_visibility_maps():
    active_counts.clear()
    target_visibility.clear()
    rest_visibility.clear()
    for i in range(input_count):
        src = button_sources[i]
        if not src:
            continue
        active_counts[src] = 0
        target_visibility[src] = not button_inverts[i]
        rest_visibility[src] = button_inverts[i]


def reset_all_states(hide_old=True):
    old_sources = set(src for src in button_sources if src)
    for i in range(MAX_BUTTONS):
        modifier_only_down[i] = False
    active_counts.clear()
    target_visibility.clear()
    rest_visibility.clear()
    if hide_old:
        for src in old_sources:
            try:
                set_source_visibility(src, False)
            except Exception:
                pass


def apply_rest_states():
    seen = set()
    for i in range(input_count):
        src = button_sources[i]
        if not src or src in seen:
            continue
        seen.add(src)
        set_source_visibility(src, rest_visibility.get(src, False))


def apply_preview_visibility():
    seen = set()
    for i in range(input_count):
        src = button_sources[i]
        if not src or src in seen:
            continue
        seen.add(src)
        set_source_visibility(src, True)



def hotkey_callback(pressed, idx):
    if idx < 0 or idx >= input_count:
        return
    if setup_preview_enabled:
        return
    src_name = button_sources[idx]
    if not src_name:
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
        prev = active_counts[src_name]
        active_counts[src_name] += 1
        if prev == 0:
            set_source_visibility(src_name, target_visibility.get(src_name, False))
    else:
        if active_counts[src_name] > 0:
            active_counts[src_name] -= 1
        if active_counts[src_name] == 0:
            set_source_visibility(src_name, rest_visibility.get(src_name, False))


# ------------------------------
# Profile serialization
# ------------------------------

def build_profile_payload(name=None):
    profile_buttons = []
    for i in range(MAX_BUTTONS):
        profile_buttons.append({
            "field": i + 1,
            "field_id": field_id(i + 1),
            "source": button_sources[i] or "",
            "invert": bool(button_inverts[i]),
            "key_mode": button_keys[i],
            "modifiers": button_modifiers[i],
            "hotkey_bindings": slot_hotkey_bindings(i),
        })
    return {
        "schema": "controlcast_push_profiles",
        "schema_version": 3,
        "script_version": VERSION,
        "profile_name": name or current_profile,
        "master_enabled": bool(master_enabled),
        "setup_preview_enabled": bool(setup_preview_enabled),
        "input_count": int(input_count),
        "buttons": profile_buttons,
    }


def save_profile(name=None, show_error=True):
    target = (name or current_profile or "Default").strip() or "Default"
    ensure_profiles_folder()
    path = profile_path(target)
    try:
        atomic_write_json(path, build_profile_payload(target))
        log("Saved profile '%s' (%s)" % (target, os.path.basename(path)))
        return True
    except Exception as exc:
        log("Profile save failed: %s" % exc)
        if show_error:
            show_native_message(
                "InputCast - Profile Save Failed",
                "Could not save profile '%s'.\n\n%s" % (target, exc),
                error=True,
            )
        return False


def blank_profile_payload(name):
    return {
        "schema": "controlcast_push_profiles",
        "schema_version": 3,
        "script_version": VERSION,
        "profile_name": name,
        "master_enabled": True,
        "setup_preview_enabled": False,
        "input_count": 30,
        "buttons": [
            {
                "field": i + 1,
                "field_id": field_id(i + 1),
                "source": "",
                "invert": False,
                "key_mode": "OBS_HOTKEYS",
                "modifiers": "NONE",
                "hotkey_bindings": [],
            }
            for i in range(MAX_BUTTONS)
        ],
    }


def write_payload_to_profile(name, payload):
    ensure_profiles_folder()
    path = profile_path(name)
    payload = dict(payload)
    payload["profile_name"] = name
    atomic_write_json(path, payload)


def load_profile_payload(name):
    path = profile_path(name)
    if not os.path.isfile(path):
        return None
    return load_json_with_backup(path, name)


def apply_profile_payload(payload, settings):
    global master_enabled, setup_preview_enabled, input_count, current_profile
    global button_sources, button_inverts, button_keys, button_modifiers

    if payload.get("schema") not in ("controlcast_push_profiles", "akinraze_push_to_enable_30_profile"):
        raise ValueError("Unsupported InputCast profile format")

    profile_name = str(payload.get("profile_name", current_profile or "Default"))
    master_enabled = bool(payload.get("master_enabled", True))
    setup_preview_enabled = bool(payload.get("setup_preview_enabled", False))
    requested_count = int(payload.get("input_count", 30))
    input_count = normalize_input_count_value(requested_count)
    current_profile = profile_name

    obs.obs_data_set_string(settings, "selected_profile", current_profile)
    obs.obs_data_set_bool(settings, "master_enabled", master_enabled)
    obs.obs_data_set_bool(settings, "setup_preview_enabled", setup_preview_enabled)
    obs.obs_data_set_int(settings, "input_count", input_count)
    sync_registered_hotkeys(input_count)

    items = {}
    for entry in payload.get("buttons", []):
        try:
            items[int(entry.get("field"))] = entry
        except Exception:
            pass

    for i in range(MAX_BUTTONS):
        entry = items.get(i + 1, {})
        source = normalize_source_name(str(entry.get("source", "")))
        invert = bool(entry.get("invert", False))
        key_mode = str(entry.get("key_mode", "OBS_HOTKEYS")) or "OBS_HOTKEYS"
        modifiers = str(entry.get("modifiers", "NONE")) or "NONE"
        if "key_mode" not in entry:
            legacy_mods = legacy_modifier_only_code(entry.get("hotkey_bindings", []))
            if legacy_mods:
                key_mode = "MODIFIER_ONLY"
                modifiers = legacy_mods

        button_sources[i] = source
        button_inverts[i] = invert
        button_keys[i] = key_mode
        button_modifiers[i] = modifiers

        obs.obs_data_set_string(settings, "source_%d" % i, source or "")
        obs.obs_data_set_bool(settings, "invert_%d" % i, invert)
        obs.obs_data_set_string(settings, "key_%d" % i, key_mode)
        obs.obs_data_set_string(settings, "mod_%d" % i, modifiers)

        bindings = entry.get("hotkey_bindings", [])
        if key_mode not in ("OBS_HOTKEYS", "UNASSIGNED", "MODIFIER_ONLY"):
            bindings = binding_from_ui(key_mode, modifiers)
        elif key_mode in ("UNASSIGNED", "MODIFIER_ONLY"):
            bindings = []
        load_hotkey_binding(i, bindings)

    reset_all_states(hide_old=True)
    rebuild_visibility_maps()
    if setup_preview_enabled:
        apply_preview_visibility()
    else:
        apply_rest_states()
    refresh_hotkey_descriptions()


def switch_profile(name, settings):
    global current_profile, profile_operation_guard
    target = (name or "Default").strip() or "Default"
    if target == current_profile:
        return True

    # Save outgoing profile first so live switches never discard work.
    save_profile(current_profile, show_error=False)

    try:
        payload = load_profile_payload(target)
        if payload is None:
            # A profile listed in UI should normally exist; if not, create blank.
            payload = blank_profile_payload(target)
            write_payload_to_profile(target, payload)
    except Exception as exc:
        log("Profile '%s' could not be loaded: %s" % (target, exc))
        show_native_message(
            "InputCast - Profile Load Failed",
            "Profile '%s' is damaged and has no usable backup.\n\n%s" % (target, exc),
            error=True,
        )
        return False

    try:
        profile_operation_guard = True
        reset_all_states(hide_old=True)
        apply_profile_payload(payload, settings)
        current_profile = target
        obs.obs_data_set_string(settings, "selected_profile", target)
        log("Live profile switch -> %s" % target)
        return True
    except Exception as exc:
        log("Profile switch failed: %s" % exc)
        show_native_message(
            "InputCast - Profile Switch Failed",
            "Could not switch to profile '%s'.\n\n%s" % (target, exc),
            error=True,
        )
        return False
    finally:
        profile_operation_guard = False


def migrate_legacy_profile_if_needed():
    ensure_profiles_folder()
    existing_real = [p for p in list_profiles() if os.path.isfile(profile_path(p))]
    if existing_real:
        return
    old_path = legacy_profile_path()
    if not os.path.isfile(old_path):
        return
    try:
        with open(old_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        old_name = str(payload.get("profile_name", "Imported v2 Profile")) or "Imported v2 Profile"
        # Current apply function understands the legacy schema.
        write_payload_to_profile(old_name, payload)
        log("Migrated legacy profile to named profiles folder: %s" % old_name)
    except Exception as exc:
        log("Legacy profile migration skipped: %s" % exc)


# ------------------------------
# Diagnostics
# ------------------------------

def source_exists(name):
    if not name:
        return False
    src = obs.obs_get_source_by_name(name)
    if src is None:
        return False
    obs.obs_source_release(src)
    return True





def collect_self_check():
    errors = []
    warnings = []
    passes = ["Profile: %s" % current_profile, "Active input slots: %d / %d" % (input_count, MAX_BUTTONS)]
    configured = 0

    uses_modifier_only = any(
        idx < input_count and button_keys[idx] == "MODIFIER_ONLY"
        for idx in range(MAX_BUTTONS)
    )
    target = errors if uses_modifier_only else warnings

    if not x11_ready():
        target.append("Linux X11 input unavailable (no X11 display); Modifier Only inputs cannot be detected (OBS-managed hotkeys can still work)")
    else:
        passes.append("Linux X11 input")
    if is_wayland_session():
        warnings.append("Wayland desktop detected: input can only be read over X11/XWayland windows, so detection may not work while another app is focused. An X11 session is recommended.")

    for i in range(input_count):
        src = button_sources[i]
        if not src:
            continue
        configured += 1
        fid = field_id(i + 1)
        if source_exists(src):
            passes.append("%s source found: %s" % (fid, src))
        else:
            errors.append("%s source missing: %s" % (fid, src))
        mods = set((button_modifiers[i] or "NONE").split("+"))
        if button_keys[i] == "MODIFIER_ONLY":
            if button_modifiers[i] and button_modifiers[i] != "NONE":
                passes.append("%s input: modifier only (%s)" % (fid, button_modifiers[i].replace("+", " + ").title()))
            else:
                warnings.append("%s %s is Modifier Only but no modifier is selected" % (fid, src))
        elif "CAPS" in mods:
            warnings.append("%s %s uses Caps Lock, which is Windows-only; this input will not react on Linux" % (fid, src))
        else:
            bindings = slot_hotkey_bindings(i)
            if bindings:
                passes.append("%s input: %s" % (fid, describe_bindings(bindings)))
            else:
                warnings.append("%s %s has no hotkey binding" % (fid, src))

    # v4.0.3: one OBS Source used by several slots must use the same Invert
    # setting; otherwise the last slot silently decides how it behaves.
    invert_by_source = {}
    for i in range(input_count):
        if button_sources[i]:
            invert_by_source.setdefault(button_sources[i], []).append((field_id(i + 1), bool(button_inverts[i])))
    for src, slots in invert_by_source.items():
        if len(set(inv for _fid, inv in slots)) > 1:
            warnings.append("%s is used by %s with different Invert settings; set them all the same" % (
                src, ", ".join(fid for fid, _inv in slots)))

    if configured == 0:
        warnings.append("No active input slots have OBS Sources assigned")
    else:
        passes.append("%d source(s) configured" % configured)

    if errors:
        return "ERROR", errors + warnings + passes
    if warnings:
        return "WARNING", warnings + passes
    return "PASS", passes


def run_startup_self_check_once():
    try:
        obs.timer_remove(run_startup_self_check_once)
    except Exception:
        pass
    status, lines = collect_self_check()
    text = "InputCast Self-Check: %s\n\n%s" % (status, "\n".join(lines))
    log(text.replace("\n", " | "))
    if status != "PASS":
        show_native_message(
            "InputCast Self-Check - %s" % status,
            text,
            error=(status == "ERROR"),
            warning=(status == "WARNING"),
        )


# ------------------------------
# Hotkey descriptions
# ------------------------------

def refresh_hotkey_descriptions():
    setter = getattr(obs, "obs_hotkey_set_description", None)
    if not callable(setter):
        return
    for i in range(MAX_BUTTONS):
        hotkey_id = hotkey_ids[i]
        if hotkey_id == obs.OBS_INVALID_HOTKEY_ID:
            continue
        src = button_sources[i]
        label = src if src else ("Input %d" % (i + 1))
        try:
            setter(hotkey_id, "InputCast %s %s" % (field_id(i + 1), label))
        except Exception:
            pass


# ------------------------------
# OBS script interface
# ------------------------------

def script_description():
    return (
        "<b>%s</b><br><br>"
        "Linux (X11) Community Test build for <b>InputCast</b> named profiles and live switching.<br>"
        "<b>UNTESTED BY TESTERS:</b> this Linux build has not been run on a real Linux PC by any tester. It targets X11 desktops; on Wayland, mouse and modifier detection is limited. Expect problems and please report results on GitHub Issues.<br><br>"
        "Profiles can be switched while OBS is running/recording; no OBS restart is required.<br><b>Module:</b> InputCast — bind keys, modifiers, controller/special-device hotkeys, and other inputs to reactive OBS source actions.<br><br>"
        "<b>Profile controls:</b> select an Active Profile to switch live. Use New Profile Name + Profile Action, then check Confirm / Execute Selected Profile Action.<br>"
        "<b>Setup / Preview Mode:</b> when enabled, all assigned graphics are forced visible to help with placement and layout.<br>"
        "<b>Input count:</b> each profile can use 5 to 65 programmable inputs in steps of 5. Only active slots are registered in OBS Hotkeys.<br>"
        "<b>Shortcut assignment:</b> choose a Key and Modifiers directly here, choose 'Modifier only' for Shift/Ctrl/Alt/Super without a second key, or leave Key on 'Assign / keep binding in OBS Hotkeys' for controllers/special hardware. Caps Lock combinations are Windows-only for now.<br><br>"
        "NOTE: The number of visible rows is determined when the Scripts panel is opened. If you change Input Count or switch to a profile with a different count, the new profile becomes active immediately, but close/reopen the Scripts window to refresh the number of displayed rows."
    ) % VERSION


def script_properties():
    props = obs.obs_properties_create()
    source_names = get_source_names()

    add_profile_list(props)
    obs.obs_properties_add_text(props, "new_profile_name", "New Profile Name", obs.OBS_TEXT_DEFAULT)
    add_profile_action_list(props)
    obs.obs_properties_add_bool(props, "execute_profile_action", "Confirm / Execute Selected Profile Action")
    add_input_count_list(props)
    obs.obs_properties_add_bool(props, "master_enabled", "Master Enable InputCast Inputs")
    obs.obs_properties_add_bool(props, "setup_preview_enabled", "Setup / Preview Mode - Show All Assigned Graphics")

    # Show the active profile count when the properties window was opened.
    rows_to_show = input_count if input_count in INPUT_COUNT_CHOICES else 30
    for i in range(rows_to_show):
        fid = field_id(i + 1)
        add_source_list(props, "source_%d" % i, "%s OBS Source / Name" % fid, source_names)
        add_key_list(props, "key_%d" % i, "%s Shortcut Key" % fid)
        add_modifier_list(props, "mod_%d" % i, "%s Modifiers" % fid)
        obs.obs_properties_add_bool(props, "invert_%d" % i, "%s Invert (hide while pressed)" % fid)

    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "selected_profile", "Default")
    obs.obs_data_set_default_string(settings, "new_profile_name", "")
    obs.obs_data_set_default_string(settings, "profile_action", "")
    obs.obs_data_set_default_bool(settings, "execute_profile_action", False)
    obs.obs_data_set_default_int(settings, "input_count", 30)
    obs.obs_data_set_default_bool(settings, "master_enabled", True)
    obs.obs_data_set_default_bool(settings, "setup_preview_enabled", False)
    for i in range(MAX_BUTTONS):
        obs.obs_data_set_default_string(settings, "source_%d" % i, "")
        obs.obs_data_set_default_string(settings, "key_%d" % i, "OBS_HOTKEYS")
        obs.obs_data_set_default_string(settings, "mod_%d" % i, "NONE")
        obs.obs_data_set_default_bool(settings, "invert_%d" % i, False)


def execute_profile_action(action, settings):
    global current_profile, profile_operation_guard
    if not action:
        return

    new_name = obs.obs_data_get_string(settings, "new_profile_name").strip()
    # v4.0.3: untick "Confirm / Execute" before acting, so a settings refresh
    # during the action can never run the same action a second time.
    obs.obs_data_set_bool(settings, "execute_profile_action", False)
    obs.obs_data_set_string(settings, "new_profile_name", "")
    try:
        profile_operation_guard = True

        if action == "SAVE":
            save_profile(current_profile)

        elif action == "CREATE_BLANK":
            if not new_name:
                show_native_message("InputCast", "Enter a New Profile Name first.", warning=True)
            else:
                name = safe_profile_filename(new_name)
                write_payload_to_profile(name, blank_profile_payload(name))
                obs.obs_data_set_string(settings, "selected_profile", name)
                switch_profile(name, settings)

        elif action == "DUPLICATE":
            if not new_name:
                show_native_message("InputCast", "Enter a New Profile Name first.", warning=True)
            else:
                name = safe_profile_filename(new_name)
                save_profile(current_profile, show_error=False)
                payload = build_profile_payload(name)
                write_payload_to_profile(name, payload)
                obs.obs_data_set_string(settings, "selected_profile", name)
                current_profile = name
                log("Duplicated profile -> %s" % name)

        elif action == "RESET_BLANK":
            payload = blank_profile_payload(current_profile)
            write_payload_to_profile(current_profile, payload)
            apply_profile_payload(payload, settings)
            log("Reset profile '%s' to blank" % current_profile)

        elif action == "DELETE":
            doomed = current_profile
            path = profile_path(doomed)
            remove_profile_files(path)

            current_profile = "Default"
            obs.obs_data_set_string(settings, "selected_profile", "Default")

            if doomed == "Default":
                payload = blank_profile_payload("Default")
                write_payload_to_profile("Default", payload)
            else:
                payload = load_profile_payload("Default")
                if payload is None:
                    payload = blank_profile_payload("Default")
                    write_payload_to_profile("Default", payload)

            apply_profile_payload(payload, settings)
            log("Deleted profile '%s'" % doomed)

    except Exception as exc:
        log("Profile action failed: %s" % exc)
        show_native_message("InputCast - Profile Action Failed", str(exc), error=True)
    finally:
        obs.obs_data_set_bool(settings, "execute_profile_action", False)
        obs.obs_data_set_string(settings, "new_profile_name", "")
        profile_operation_guard = False


def script_update(settings):
    global master_enabled, setup_preview_enabled, input_count, current_profile
    global button_sources, button_inverts, button_keys, button_modifiers

    if profile_operation_guard:
        return

    requested_profile = obs.obs_data_get_string(settings, "selected_profile").strip() or "Default"

    # Selecting a different profile switches it live.
    if loaded and requested_profile != current_profile:
        switch_profile(requested_profile, settings)
        return

    master_enabled = obs.obs_data_get_bool(settings, "master_enabled")
    setup_preview_enabled = obs.obs_data_get_bool(settings, "setup_preview_enabled")
    requested_count = obs.obs_data_get_int(settings, "input_count")
    input_count = normalize_input_count_value(requested_count)

    # Read all slots, including hidden rows from settings storage.
    for i in range(MAX_BUTTONS):
        button_sources[i] = normalize_source_name(obs.obs_data_get_string(settings, "source_%d" % i))
        button_inverts[i] = obs.obs_data_get_bool(settings, "invert_%d" % i)
        button_keys[i] = obs.obs_data_get_string(settings, "key_%d" % i) or "OBS_HOTKEYS"
        button_modifiers[i] = obs.obs_data_get_string(settings, "mod_%d" % i) or "NONE"

    # Keep OBS Settings > Hotkeys clean: only register the active profile count.
    # Bindings for inactive slots are cached and restored if the count is increased later.
    sync_registered_hotkeys(input_count)

    # Apply UI-defined shortcuts immediately. OBS-managed slots are left untouched.
    for i in range(MAX_BUTTONS):
        if i < input_count:
            apply_ui_binding(i)

    reset_all_states(hide_old=False)
    rebuild_visibility_maps()
    if setup_preview_enabled:
        apply_preview_visibility()
    else:
        apply_rest_states()
    refresh_hotkey_descriptions()

    action = obs.obs_data_get_string(settings, "profile_action")
    execute_now = obs.obs_data_get_bool(settings, "execute_profile_action")
    if loaded and action and execute_now:
        execute_profile_action(action, settings)


def script_save(settings):
    # Persist OBS hotkeys in script settings as well as named profile JSON.
    for i in range(MAX_BUTTONS):
        try:
            bindings = slot_hotkey_bindings(i)
            arr = plain_dicts_to_binding_array(bindings)
            obs.obs_data_set_array(settings, hotkey_names[i], arr)
            obs.obs_data_array_release(arr)
        except Exception as exc:
            log("Could not save hotkey %s: %s" % (field_id(i + 1), exc))
    save_profile(current_profile, show_error=False)


def script_load(settings):
    global script_settings_ref, loaded, current_profile

    log("Loaded %s from %s" % (VERSION, __file__))
    ensure_profiles_folder()
    migrate_legacy_profile_if_needed()

    script_settings_ref = settings
    try:
        obs.obs_data_addref(script_settings_ref)
    except Exception:
        pass

    # Recover saved bindings into cache before registering only the active slots.
    for i in range(MAX_BUTTONS):
        saved = obs.obs_data_get_array(settings, hotkey_names[i])
        if saved is not None:
            try:
                count = obs.obs_data_array_count(saved)
                cached = []
                for j in range(count):
                    item = obs.obs_data_array_item(saved, j)
                    if item is None:
                        continue
                    try:
                        cached.append({
                            "key": obs.obs_data_get_string(item, "key"),
                            "shift": bool(obs.obs_data_get_bool(item, "shift")),
                            "control": bool(obs.obs_data_get_bool(item, "control")),
                            "alt": bool(obs.obs_data_get_bool(item, "alt")),
                            "command": bool(obs.obs_data_get_bool(item, "command")),
                        })
                    finally:
                        obs.obs_data_release(item)
                cached_hotkey_bindings[i] = cached
            finally:
                obs.obs_data_array_release(saved)

    input_count_from_settings = normalize_input_count_value(obs.obs_data_get_int(settings, "input_count"))
    sync_registered_hotkeys(input_count_from_settings)

    current_profile = obs.obs_data_get_string(settings, "selected_profile").strip() or "Default"
    loaded = True

    # If the selected profile exists, restore it. Otherwise create a genuinely blank
    # profile instead of resurrecting OBS-retained script settings.
    payload = load_profile_payload(current_profile)
    if payload is not None:
        try:
            apply_profile_payload(payload, settings)
        except Exception as exc:
            log("Initial profile restore failed: %s" % exc)
            payload = blank_profile_payload(current_profile)
            write_payload_to_profile(current_profile, payload)
            apply_profile_payload(payload, settings)
    else:
        payload = blank_profile_payload(current_profile)
        write_payload_to_profile(current_profile, payload)
        apply_profile_payload(payload, settings)
        log("No named profile file existed; created blank profile '%s'" % current_profile)

    refresh_hotkey_descriptions()
    try:
        obs.timer_add(poll_modifier_only_inputs, MODIFIER_POLL_MS)
    except Exception as exc:
        log("Could not start modifier-only input polling: %s" % exc)
    try:
        obs.timer_add(run_startup_self_check_once, 2200)
    except Exception as exc:
        log("Could not schedule startup self-check: %s" % exc)


def script_unload():
    global script_settings_ref, loaded
    loaded = False
    try:
        obs.timer_remove(poll_modifier_only_inputs)
    except Exception:
        pass
    save_profile(current_profile, show_error=False)
    reset_all_states(hide_old=True)
    for i in range(MAX_BUTTONS):
        unregister_hotkey_slot(i)
    if script_settings_ref is not None:
        try:
            obs.obs_data_release(script_settings_ref)
        except Exception:
            pass
        script_settings_ref = None
    x11_close()
