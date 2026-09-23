# ControlCast – Custom Reactive Input Visuals for OBS

**Create fully custom mouse, keyboard, controller, and hotkey visuals that react live to your inputs.**

## About ControlCast

**ControlCast** is a free OBS Python toolkit that lets streamers build their own reactive on-screen controls using ordinary OBS Sources and custom graphics.

There are no required layouts, image styles, naming conventions, or asset folders. Build something realistic, minimal, over-the-top, or completely absurd.

**Your inputs. Your design. Your stream.**

## Included Files

### `mouse_click_overlay_Windows.py`

Windows mouse visualizer with left/right click detection, additional assignable inputs, physical mouse movement, sensitivity/range controls, persistent rest position, automatic profile backup/restore, and diagnostics.

### `push_to_enable_source_Windows.py`

Windows 30-button reactive input script. Links OBS Sources to hotkeys so visuals appear while assigned inputs are pressed. Supports Groups, nested Groups, inverted behavior, profiles, and diagnostics.

### `mouse_click_overlay_macOS.py`

macOS version of the mouse visualizer using macOS-specific input handling. Included for community testing because I do not currently own a Mac.

### `push_to_enable_source_macOS.py`

macOS version of the 30-button reactive input script. Included for community testing and feedback.

**Windows users should use the two files labeled `Windows`; Mac users should use the two files labeled `macOS`.**

## Main Features

### Mouse Overlay

- Left/right mouse-button detection
- Up to 10 additional inputs
- Reactive mouse movement
- Horizontal/vertical range and sensitivity
- Adjustable follow/return behavior
- Persistent rest position
- Automatic source naming
- Automatic JSON backup/restore
- Startup self-check

### 30-Button Overlay

- Up to 30 assignable OBS Sources
- Keyboard, mouse, controller, keypad, and other OBS hotkeys
- Group and nested-group support
- Stable `[01]–[30]` numbering
- Automatic OBS Source → Hotkey naming
- Optional inverted behavior
- Master enable/disable
- Automatic JSON backup/restore
- Saved hotkey bindings
- Startup self-check

## Creative Freedom

ControlCast does not lock you into somebody else’s keyboard, controller, mouse, or layout.

There is:

- no required look
- no required layout
- no required asset naming convention
- no required asset folder

**The only real limits are your time and your imagination.**

## Cost

**Completely free, with no hidden monthly charges.**

## Windows Support

Developed and tested with:

- **OBS Studio 32.2.2**
- **Python 3.12.7 64-bit**

**Python 3.12.7 is the recommended version.**

Newer Python 3.13.x versions were tested during development and did not function correctly with this OBS scripting setup.

## macOS Support

macOS-specific versions are included, but they have not yet been tested on physical Mac hardware.

Mac users are invited to report:

- macOS version
- OBS version
- Python version
- mouse movement results
- click detection
- hotkey behavior
- JSON profile restore results
- Accessibility/Input Monitoring permissions required

Community testing and feedback are welcome.

## Tutorial Video Series

### 1. Software Installation & Configuration

OBS Studio, Python 3.12.7, and initial setup.

**Video:** `[LINK TO BE ADDED]`

### 2. Creating Graphics & Adding Them to OBS

Finding images, editing them in GIMP, creating transparent assets, and arranging them in OBS.

**Video:** `[LINK TO BE ADDED]`

### 3. Installing & Configuring the ControlCast Scripts

Installing the scripts, assigning Sources and hotkeys, configuring mouse behavior, profiles, backups, and testing.

**Video:** `[LINK TO BE ADDED]`

## Useful Software

**OBS Studio** — required. ControlCast runs inside OBS and uses OBS Sources, Groups, Hotkeys, and Python scripting.

**Python 3.12.7 64-bit** — recommended and confirmed working during development.

**GIMP** — optional. Used in the tutorials for creating and editing transparent control graphics. Any capable image editor can be used instead.

## Development Disclosure

ControlCast was developed through extensive hands-on testing and iterative development with assistance from ChatGPT.

The Windows scripts were tested and refined directly by the project maintainer.

The macOS versions are currently community-test builds and have not yet been validated on physical Mac hardware.

## Support

Please use the repository’s **Issues** section to report bugs, request features, or provide macOS test results.

When reporting an issue, include:

- operating system
- OBS Studio version
- Python version
- which ControlCast script is affected
- a description of the problem
- any relevant OBS Script Log output

## License

ControlCast is released under the **MIT License**.

See the `LICENSE` file in this repository for details.
