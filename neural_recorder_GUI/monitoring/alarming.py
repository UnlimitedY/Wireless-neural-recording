import os
import smtplib
import socket
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


ALARM_CONFIG_FILENAME = "alarm_config.json"


@dataclass(frozen=True)
class AlarmThresholds:
    battery_rsoc_critical_percent: float = 5.0
    packet_loss_critical_percent: float = 1.0
    status_stale_seconds: float = 120.0


@dataclass(frozen=True)
class EmailAlarmConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    use_tls: bool = True
    use_ssl: bool = False
    sender_email: str = ""
    smtp_username: str = ""
    sender_password: str = ""
    sender_password_env: str = "NEURAL_RECORDER_ALARM_EMAIL_PASSWORD"
    sender_name: str = "Neural Recorder Alarm"
    recipient_emails: Sequence[str] = field(default_factory=tuple)
    subject_prefix: str = "[Neural Recorder Alarm]"


@dataclass(frozen=True)
class AlarmConfig:
    enabled: bool = False
    check_interval_minutes: float = 10.0
    email: EmailAlarmConfig = field(default_factory=EmailAlarmConfig)
    thresholds: AlarmThresholds = field(default_factory=AlarmThresholds)


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _normalize_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _normalize_recipients(values: Any) -> Sequence[str]:
    if values is None:
        return tuple()
    if isinstance(values, str):
        values = [values]
    recipients = []
    for item in list(values):
        candidate = str(item or "").strip()
        if candidate:
            recipients.append(candidate)
    return tuple(recipients)


def load_alarm_config(path: str) -> AlarmConfig:
    try:
        from ..storage.runtime_cache import read_json
    except ImportError:
        from storage.runtime_cache import read_json

    payload = read_json(path, {}) or {}
    if not isinstance(payload, MappingABC):
        payload = {}
    email_payload = payload.get("email", {}) if isinstance(payload.get("email"), MappingABC) else {}
    thresholds_payload = (
        payload.get("thresholds", {})
        if isinstance(payload.get("thresholds"), MappingABC)
        else {}
    )

    email = EmailAlarmConfig(
        enabled=_normalize_bool(email_payload.get("enabled"), default=False),
        smtp_host=str(email_payload.get("smtp_host", "") or "").strip(),
        smtp_port=_normalize_int(email_payload.get("smtp_port"), default=587),
        use_tls=_normalize_bool(email_payload.get("use_tls"), default=True),
        use_ssl=_normalize_bool(email_payload.get("use_ssl"), default=False),
        sender_email=str(email_payload.get("sender_email", "") or "").strip(),
        smtp_username=str(email_payload.get("smtp_username", "") or "").strip(),
        sender_password=str(email_payload.get("sender_password", "") or "").strip(),
        sender_password_env=str(
            email_payload.get("sender_password_env", "NEURAL_RECORDER_ALARM_EMAIL_PASSWORD")
            or "NEURAL_RECORDER_ALARM_EMAIL_PASSWORD"
        ).strip(),
        sender_name=str(email_payload.get("sender_name", "Neural Recorder Alarm") or "Neural Recorder Alarm").strip(),
        recipient_emails=_normalize_recipients(email_payload.get("recipient_emails")),
        subject_prefix=str(email_payload.get("subject_prefix", "[Neural Recorder Alarm]") or "[Neural Recorder Alarm]").strip(),
    )
    thresholds = AlarmThresholds(
        battery_rsoc_critical_percent=_normalize_float(
            thresholds_payload.get("battery_rsoc_critical_percent"),
            default=5.0,
        ),
        packet_loss_critical_percent=_normalize_float(
            thresholds_payload.get("packet_loss_critical_percent"),
            default=1.0,
        ),
        status_stale_seconds=_normalize_float(
            thresholds_payload.get("status_stale_seconds"),
            default=120.0,
        ),
    )
    return AlarmConfig(
        enabled=_normalize_bool(payload.get("enabled"), default=False),
        check_interval_minutes=max(
            1.0,
            _normalize_float(payload.get("check_interval_minutes"), default=10.0),
        ),
        email=email,
        thresholds=thresholds,
    )


def resolve_email_password(config: EmailAlarmConfig) -> str:
    if config.sender_password:
        return config.sender_password
    env_name = str(config.sender_password_env or "").strip()
    if not env_name:
        return ""
    return str(os.environ.get(env_name, "") or "").strip()


def alarm_email_ready(config: AlarmConfig) -> bool:
    email = config.email
    return bool(
        config.enabled
        and email.enabled
        and email.smtp_host
        and email.sender_email
        and email.recipient_emails
        and resolve_email_password(email)
    )


def build_card_position(index: int) -> Dict[str, int]:
    return {
        "index": int(index) + 1,
        "row": 1,
        "column": int(index) + 1,
    }


def evaluate_slot_alarm_conditions(
    slot_label: str,
    slot_id: str,
    card_position: Mapping[str, Any],
    status: Mapping[str, Any],
    thresholds: AlarmThresholds,
    service_alive: bool = True,
    now_epoch: Optional[float] = None,
) -> List[Dict[str, Any]]:
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    issues: List[Dict[str, Any]] = []
    neural = status.get("neural", {}) if isinstance(status.get("neural"), Mapping) else {}
    battery = status.get("battery", {}) if isinstance(status.get("battery"), Mapping) else {}
    rf = status.get("rf", {}) if isinstance(status.get("rf"), Mapping) else {}
    camera = status.get("camera", {}) if isinstance(status.get("camera"), Mapping) else {}

    if not service_alive:
        issues.append({
            "kind": "service_down",
            "severity": "critical",
            "message": "Cage backend process is not running.",
        })

    last_update_epoch = float(status.get("last_update_epoch", 0.0) or 0.0)
    if last_update_epoch > 0 and (now_epoch - last_update_epoch) > float(thresholds.status_stale_seconds):
        issues.append({
            "kind": "status_stale",
            "severity": "critical",
            "message": f"No status update for {now_epoch - last_update_epoch:.0f} seconds.",
        })

    battery_rsoc = float(battery.get("rsoc", 0.0) or 0.0)
    if battery_rsoc > 0.0 and battery_rsoc < float(thresholds.battery_rsoc_critical_percent):
        issues.append({
            "kind": "battery_low",
            "severity": "critical",
            "message": (
                f"Battery is low: {battery_rsoc:.1f}% "
                f"(threshold {float(thresholds.battery_rsoc_critical_percent):.1f}%)."
            ),
        })

    packet_loss_percent = float(neural.get("packet_loss_percent_1h", 0.0) or 0.0)
    packet_loss_expected = int(neural.get("packet_loss_expected_1h", 0) or 0)
    if packet_loss_expected > 0 and packet_loss_percent >= float(thresholds.packet_loss_critical_percent):
        issues.append({
            "kind": "packet_loss_high",
            "severity": "critical",
            "message": (
                f"1-hour average packet loss is {packet_loss_percent:.2f}% "
                f"(threshold {float(thresholds.packet_loss_critical_percent):.2f}%)."
            ),
        })

    last_error = str(status.get("last_error", "") or "").strip()
    state = str(status.get("state", "") or "").strip().lower()
    sampling_active = bool(neural.get("sampling", False))
    save_mode = str(neural.get("save_mode", "") or "").strip()
    neural_connected = bool(neural.get("connected", False))
    rf_connection_state = str(rf.get("connection_state", "") or "").strip().lower()
    rf_reason = str(rf.get("last_failure_reason", "") or "").strip()
    camera_open = bool(camera.get("open", False))
    camera_recording = bool(camera.get("recording", False))
    camera_health_state = str(camera.get("health_state", "") or "").strip().lower()
    connection_messages = []
    if state == "error" and last_error:
        connection_messages.append(last_error)
    if (sampling_active or save_mode) and not neural_connected:
        connection_messages.append("Neural serial is disconnected while sampling/save is active.")
    if rf_connection_state in {"recovering", "reconnecting", "port_missing", "device_unresponsive", "failed"}:
        connection_messages.append(
            rf_reason or f"RF serial is {rf_connection_state}."
        )
    if camera_recording and not camera_open:
        connection_messages.append("Camera recording is marked active while the camera is closed.")
    if camera_health_state in {"recovering", "failed"}:
        connection_messages.append(f"Camera health is {camera_health_state}.")
    if connection_messages:
        issues.append({
            "kind": "connection_error",
            "severity": "critical",
            "message": " | ".join(connection_messages),
        })

    normalized = []
    for issue in issues:
        normalized.append({
            "slot_id": slot_id,
            "slot_label": slot_label,
            "card_position": dict(card_position),
            **issue,
        })
    return normalized


def build_alarm_email_payload(issues: Iterable[Mapping[str, Any]], subject_prefix: str) -> Dict[str, str]:
    issues = list(issues)
    hostname = socket.gethostname()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slot_summary = ", ".join(
        sorted({f"{item.get('slot_label', item.get('slot_id', 'Unknown'))}" for item in issues})
    ) or "Unknown cage"
    subject = f"{subject_prefix} {slot_summary} @ {hostname}"

    lines = [
        f"Neural Recorder alarm generated at {timestamp}.",
        f"Host: {hostname}",
        "",
        f"Active alarms: {len(issues)}",
        "",
    ]
    for idx, issue in enumerate(issues, start=1):
        position = issue.get("card_position", {}) if isinstance(issue.get("card_position"), Mapping) else {}
        row = int(position.get("row", 1) or 1)
        column = int(position.get("column", 1) or 1)
        lines.extend([
            f"{idx}. {issue.get('slot_label', issue.get('slot_id', 'Unknown cage'))}",
            f"Card position: row {row}, column {column}",
            f"Alarm type: {issue.get('kind', 'unknown')}",
            f"Message: {issue.get('message', '-')}",
            "",
        ])
    return {
        "subject": subject,
        "body": "\n".join(lines).rstrip() + "\n",
    }


def send_alarm_email(config: AlarmConfig, issues: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    issues = list(issues)
    if not issues:
        return {"sent": False, "reason": "no_issues"}
    if not alarm_email_ready(config):
        return {"sent": False, "reason": "config_not_ready"}

    email_config = config.email
    payload = build_alarm_email_payload(issues, email_config.subject_prefix)
    password = resolve_email_password(email_config)
    message = EmailMessage()
    sender_display = email_config.sender_name or email_config.sender_email
    message["Subject"] = payload["subject"]
    message["From"] = f"{sender_display} <{email_config.sender_email}>"
    message["To"] = ", ".join(email_config.recipient_emails)
    message.set_content(payload["body"])

    username = email_config.smtp_username or email_config.sender_email
    if email_config.use_ssl:
        with smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port, timeout=20) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(email_config.smtp_host, email_config.smtp_port, timeout=20) as server:
            if email_config.use_tls:
                server.starttls()
            server.login(username, password)
            server.send_message(message)
    return {"sent": True, "reason": "ok", "subject": payload["subject"], "body": payload["body"]}
