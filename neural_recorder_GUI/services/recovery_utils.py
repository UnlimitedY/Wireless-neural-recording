import os
from collections.abc import Mapping as MappingABC
from typing import Any, Dict, Iterable, Mapping, Optional


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def extract_port_fingerprint(port_info: Any) -> Dict[str, Any]:
    if isinstance(port_info, MappingABC):
        getter = port_info.get
    else:
        getter = lambda key, default=None: getattr(port_info, key, default)
    return {
        "device": str(getter("device", "") or "").strip(),
        "vid": getter("vid", None),
        "pid": getter("pid", None),
        "serial_number": str(getter("serial_number", "") or "").strip(),
        "location": str(getter("location", "") or "").strip(),
        "description": str(getter("description", "") or "").strip(),
    }


def fingerprint_has_identity(fingerprint: Mapping[str, Any]) -> bool:
    serial_number = _normalize_text(fingerprint.get("serial_number"))
    location = _normalize_text(fingerprint.get("location"))
    vid = fingerprint.get("vid", None)
    pid = fingerprint.get("pid", None)
    return bool(serial_number or location or (vid is not None and pid is not None))


def fingerprint_matches(candidate: Mapping[str, Any], fingerprint: Mapping[str, Any]) -> bool:
    if not fingerprint_has_identity(fingerprint):
        return False

    candidate = extract_port_fingerprint(candidate)
    fingerprint = extract_port_fingerprint(fingerprint)

    serial_number = _normalize_text(fingerprint.get("serial_number"))
    if serial_number and _normalize_text(candidate.get("serial_number")) != serial_number:
        return False

    location = _normalize_text(fingerprint.get("location"))
    if location and _normalize_text(candidate.get("location")) != location:
        return False

    fingerprint_vid = fingerprint.get("vid", None)
    fingerprint_pid = fingerprint.get("pid", None)
    if fingerprint_vid is not None and candidate.get("vid", None) != fingerprint_vid:
        return False
    if fingerprint_pid is not None and candidate.get("pid", None) != fingerprint_pid:
        return False

    description = _normalize_text(fingerprint.get("description"))
    if description and description == _normalize_text(candidate.get("description")):
        return True

    return True


def resolve_reconnect_port(
    preferred_port: str,
    fingerprint: Mapping[str, Any],
    ports: Iterable[Any],
) -> Dict[str, Any]:
    port_metadata = [extract_port_fingerprint(item) for item in list(ports)]
    preferred_port = str(preferred_port or "").strip()

    if preferred_port:
        for item in port_metadata:
            if str(item.get("device", "") or "").strip() == preferred_port:
                return {
                    "decision": "preferred",
                    "port": preferred_port,
                    "matched": item,
                    "candidates": [preferred_port],
                }

    if fingerprint_has_identity(fingerprint):
        candidates = []
        for item in port_metadata:
            if fingerprint_matches(item, fingerprint):
                device = str(item.get("device", "") or "").strip()
                if device:
                    candidates.append(device)
        if candidates:
            resolved_port = candidates[0]
            matched_item = next(
                (item for item in port_metadata if str(item.get("device", "") or "").strip() == resolved_port),
                None,
            )
            return {
                "decision": "fingerprint",
                "port": resolved_port,
                "matched": matched_item,
                "candidates": candidates,
            }

    return {
        "decision": "missing",
        "port": "",
        "matched": None,
        "candidates": [],
    }


def build_recovery_segment_path(base_path: str, recovery_index: int) -> str:
    base_path = str(base_path or "").strip()
    if not base_path:
        raise ValueError("base_path is required for recovery segment naming")
    recovery_index = max(1, int(recovery_index))
    root, ext = os.path.splitext(base_path)
    suffix = f"_recovery_{recovery_index:02d}"
    return f"{root}{suffix}{ext}"
