"""Panel ↔ Wings version compatibility for managed components on this machine.

Hard rules block combinations that cannot work. Pairs outside the deployment
matrix this installer has validated are allowed after an explicit warning.
"""

from .common import PANEL_VERSION, WINGS_VERSION, Cancelled, InstallerError, valid_version, version_key

# Panel major version -> (required Wings major version, oldest Wings release supported).
REQUIREMENTS = {2: (1, "v1.13.0")}
# Panel/Wings pairs exercised end to end by the installer's deployment matrix.
VALIDATED = frozenset({(PANEL_VERSION, WINGS_VERSION)})


def problem(panel, wings):
    """A blocking incompatibility message, or an empty string."""
    if not (valid_version(panel, "panel") and valid_version(wings, "wings")):
        return ""
    # valid_version() only accepts Panel majors listed in REQUIREMENTS.
    wings_major, oldest = REQUIREMENTS[version_key(panel)[0]]
    if version_key(wings)[0] != wings_major or version_key(wings) < version_key(oldest):
        return "Panel {panel} requires Wings {oldest} or newer within Wings {major}.x; this node has Wings {wings}.".format(
            panel=panel, oldest=oldest, major=wings_major, wings=wings
        )
    return ""


def local_versions(installer):
    """Installed (panel, wings) versions from managed state; None when absent or unreadable."""
    versions = []
    for path in (installer.p.state, installer.p.wings_state):
        try:
            value = installer.load(path).get("version") if path.is_file() else None
        except InstallerError:
            value = None
        versions.append(value if isinstance(value, str) else None)
    return tuple(versions)


def check(installer, panel=None, wings=None):
    """Validate a planned pair; missing sides use the installed versions on this machine."""
    installed_panel, installed_wings = local_versions(installer)
    panel, wings = panel or installed_panel, wings or installed_wings
    if not panel or not wings:
        return
    ui = installer.ui
    blocking = problem(panel, wings)
    if blocking:
        raise InstallerError(blocking)
    if (panel, wings) in VALIDATED:
        return
    ui.card(
        "Untested Panel and Wings combination",
        [
            ui.tr("Panel {panel} with Wings {wings}").format(panel=panel, wings=wings),
            ui.tr("Validated combination: Panel {panel} with Wings {wings}").format(panel=PANEL_VERSION, wings=WINGS_VERSION),
            "Both releases are compatible by version, but this installer has not tested them together.",
        ],
    )
    if not ui.yes("Continue with this combination?", True):
        raise Cancelled("Operation cancelled.")
