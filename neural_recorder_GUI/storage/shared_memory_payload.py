import pickle
import tempfile
import os
from multiprocessing import shared_memory
from typing import Any


SHARED_MESSAGE_KEY = "__shared_memory_payload__"


def _force_file_payload_transport() -> bool:
    return os.name == "nt"


def _pack_file_payload(payload: bytes) -> Any:
    fd, path = tempfile.mkstemp(prefix="codex_payload_", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        return {
            SHARED_MESSAGE_KEY: True,
            "transport": "file",
            "path": path,
            "size": len(payload),
        }
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def pack_for_queue(message: Any, threshold_bytes: int = 65536, prefer_shared_memory: bool = True) -> Any:
    payload = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(payload) < int(threshold_bytes):
        return message

    if not bool(prefer_shared_memory) or _force_file_payload_transport():
        return _pack_file_payload(payload)

    try:
        shm = shared_memory.SharedMemory(create=True, size=len(payload))
    except Exception:
        return _pack_file_payload(payload)
    try:
        shm.buf[:len(payload)] = payload
        return {
            SHARED_MESSAGE_KEY: True,
            "transport": "shm",
            "name": shm.name,
            "size": len(payload),
        }
    finally:
        shm.close()


def unpack_from_queue(message: Any) -> Any:
    if not isinstance(message, dict) or not message.get(SHARED_MESSAGE_KEY):
        return message

    if message.get("transport") == "file":
        path = message["path"]
        try:
            with open(path, "rb") as f:
                raw = f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return pickle.loads(raw)

    shm = shared_memory.SharedMemory(name=message["name"])
    try:
        raw = bytes(shm.buf[: int(message["size"])])
    finally:
        shm.close()
        try:
            shm.unlink()
        except FileNotFoundError:
            pass
    return pickle.loads(raw)
