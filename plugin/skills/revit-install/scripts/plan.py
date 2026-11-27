"""Print a Windows Autodesk installation plan without changing the machine."""
import argparse
import json
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit


def absolute_directory(value, field):
    if not isinstance(value, str) or not value.strip() or any(c in '<>"|?*' or ord(c) < 32 for c in value):
        raise ValueError(f"Set {field} to an absolute Windows directory")
    path = PureWindowsPath(value)
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith(("\\\\?", "\\\\.")):
        raise ValueError(f"Set {field} to an absolute drive or UNC directory")
    if any(":" in part for part in path.parts[1:]):
        raise ValueError(f"Unexpected colon in {field}")
    return path


def string_list(config, field):
    values = config.get(field)
    if not isinstance(values, list) or any(not isinstance(s, str) or not s.strip() for s in values):
        raise ValueError(f"{field} must be an array of nonempty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate entry in {field}")
    return values


def proxy_endpoint(value):
    if value is None:
        return None
    if not isinstance(value, str) or any(c.isspace() or c in '<>\\' for c in value):
        raise ValueError("Set proxyUrl to an HTTP(S) endpoint or JSON null")
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.port == 0:
        raise ValueError("Use an HTTP(S) proxy endpoint")
    if url.username is not None or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("Proxy endpoints must omit credentials, query, path and fragment")
    return value


def make_plan(config):
    if not isinstance(config, dict):
        raise ValueError("Expected a JSON object")
    download = absolute_directory(config.get("downloadDir"), "downloadDir")
    products = string_list(config, "products")
    years = config.get("contentYears")
    if not isinstance(years, list) or any(type(y) is not int or not 2000 <= y <= 2099 for y in years):
        raise ValueError("contentYears must be an array of four-digit release years")
    if len(set(years)) != len(years) or not (products or years):
        raise ValueError("Choose at least one target and list each content year once")
    needs_root = years or any("revit" in p.casefold() for p in products)
    root = absolute_directory(config.get("contentRoot"), "contentRoot") if needs_root else None
    locales = string_list(config, "contentLocales")
    if years and (not locales or any(loc not in ("CHS", "ENU") for loc in locales)):
        raise ValueError("Choose CHS and/or ENU for content years")
    if "proxyUrl" not in config:
        raise ValueError("Set proxyUrl to an HTTP(S) URL or JSON null")
    proxy = proxy_endpoint(config["proxyUrl"])
    content = [
        {"year": y, "locale": loc, "verifiedName": f"RVTCP{loc}_{y}.exe", "target": str(root / str(y))}
        for y in years for loc in locales
    ]
    return {"downloadDir": str(download), "contentRoot": str(root) if root else None,
            "proxyUrl": proxy, "products": products, "content": content, "deferRestart": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        plan = make_plan(json.loads(args.config.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    print(json.dumps(plan, ensure_ascii=True, indent=2))
