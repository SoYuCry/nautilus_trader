"""Portable file identity helpers; no local-source or historical time gate."""
from datetime import UTC, datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent
NOT_BEFORE = datetime(1970, 1, 1, tzinfo=UTC)
def file_identity(path):
    s = Path(path).lstat()
    return dict(device=s.st_dev, inode=s.st_ino, size=s.st_size,
                mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns, mode=s.st_mode)
