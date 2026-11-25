"""Catalogue entries for native view functions callable from revit_bridge.view_commands."""

ACTIVATE_VIEW = "activate_view_in_revit"
EXPORT_VIEW_IMAGE = "export_view_image_in_revit"


def list_host_functions() -> list[dict]:
    """Return fresh JSON-compatible descriptors. Each name identifies a Python and MCP function."""
    return [
        {
            "kind": "host_function",
            "name": "open_view",
            "description": "View only. Activate an existing Revit view by name.",
            "parameters": [{"name": "name", "type": "string", "required": True}],
            "class": "ui_only",
            "required_addin_command": ACTIVATE_VIEW,
        },
        {
            "kind": "host_function",
            "name": "capture_view_image",
            "description": "Read only. Export a Revit view as a base64 PNG with its name and pixel dimensions.",
            "parameters": [
                {"name": "width", "type": "integer", "required": False, "default": 1600},
                {"name": "view_id", "type": "integer", "required": False, "default": None},
            ],
            "class": "read_only",
            "required_addin_command": EXPORT_VIEW_IMAGE,
        },
    ]
