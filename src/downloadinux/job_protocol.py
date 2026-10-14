"""Shared standard-library job reply predicates (Persistent worker and jobs
§1/§6; DEC-55, DEC-57). Extraction preserves existing host acceptance.
"""
import base64

from . import job_store


def _int(value: object) -> bool:
    return type(value) is int


def _positive(value: object) -> bool:
    return _int(value) and value >= 1


def _str(value: object) -> bool:
    return type(value) is str


def _optional_str(value: object) -> bool:
    return value is None or _str(value)


def _keys(value: object, *keys: str) -> bool:
    return type(value) is dict and set(value) == set(keys)


def wait(value: object) -> bool:
    return (value is None or value == {"reason": "vpn"}
            or _keys(value, "reason", "until") and value["reason"] == "throttle" and _str(value["until"]))


def record(value: object) -> bool:
    """Exactly §2's fields with their types and relations."""
    return (_keys(value, *job_store.RECORD_FIELDS)
            and all(_positive(value[key]) for key in ("id", "submission", "attempt"))
            and _str(value["preset"]) and value["preset"] in job_store.PRESETS
            and all(_str(value[key]) for key in ("url", "user_agent", "created_at", "queued_at"))
            and (value["lane"] is None or value["lane"] == value["preset"])
            and (_str(value["source"]) and _str(value["revision"])
                 or value["source"] is None and value["revision"] is None)
            and _str(value["state"]) and value["state"] in job_store.STATES
            and wait(value["wait"])
            and type(value["cancel_requested"]) is bool
            and all(_optional_str(value[key]) for key in ("started_at", "finished_at", "error"))
            and (value["exit_status"] is None or _int(value["exit_status"])))


def strict_base64(data: object) -> bytes | None:
    """Padded base64 (RFC 4648 §4) that encodes back to the same string, else None."""
    if not _str(data):
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError:  # binascii.Error, and a string that is not ASCII
        return None
    return decoded if base64.b64encode(decoded).decode("ascii") == data else None


