"""An advisory process lease; stale files are reusable, live leases are not."""

import fcntl
import os
import stat
from pathlib import Path

from .radio import RadioError


class HostLease:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.descriptor = None

    def __enter__(self):
        descriptor = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
                # The empty coordination file contains no credentials. Shared
                # Docker profiles may use different unprivileged UIDs.
                os.fchmod(descriptor, 0o666)
            except FileExistsError:
                descriptor = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size:
                raise OSError("Invalid lock file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.descriptor = descriptor
            return self
        except OSError as err:
            if descriptor is not None:
                os.close(descriptor)
            raise RadioError("USB-host bridge lease is busy or inaccessible; stop the other bridge or inspect the lock path. Never restart during firmware maintenance") from err

    def __exit__(self, *args):
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None
