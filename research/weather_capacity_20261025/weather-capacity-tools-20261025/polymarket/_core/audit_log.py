"""Append-only segmented audit logs; rotation never deletes previous segments."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback


def exception_details(error):
    """Describe the failure location without serializing secrets in exception text."""
    return {
        "error_type": type(error).__name__,
        "frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)
        ],
    }


def write_json_atomic(path, value):
    """Replace an application-owned state snapshot, with durable file contents."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AuditLog:
    """Thread-safe JSONL writer with a sequence spanning uniquely named segments."""

    def __init__(self, path, max_bytes=64 * 1024 * 1024, *, auto_flush=True):
        if max_bytes <= 0:
            raise ValueError("Positive audit segment bound required")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.auto_flush = auto_flush
        self.lock = threading.RLock()
        self.segment = 0
        self.sequence = 0
        self.bytes_written = 0
        self.error = None
        self.file = self._open(self.path)

    @staticmethod
    def _open(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")

    def append(self, kind, payload):
        with self.lock:
            if self.error:
                raise OSError("Audit writer failed; refusing further writes")
            record = {**payload, "kind": kind, "audit_sequence": self.sequence + 1}
            record.setdefault("recorded_at_ns", time.time_ns())
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            size = len(line.encode("utf-8"))
            if size > self.max_bytes:
                raise ValueError("Single audit record exceeds segment bound")
            try:
                if self.bytes_written + size > self.max_bytes:
                    next_segment = self.segment + 1
                    next_path = self.path.with_name(
                        f"{self.path.stem}.part-{next_segment:05d}{self.path.suffix}"
                    )
                    new_file = self._open(next_path)
                    self.file.close()
                    self.file = new_file
                    self.segment = next_segment
                    self.bytes_written = 0
                self.file.write(line)
                if self.auto_flush:
                    self.file.flush()
                self.bytes_written += size
                self.sequence += 1
            except Exception as e:
                self.error = type(e).__name__
                raise

    def flush(self):
        with self.lock:
            self.file.flush()

    def close(self):
        with self.lock:
            self.file.close()
