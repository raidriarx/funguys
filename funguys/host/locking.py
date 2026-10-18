"""One host process owns a state directory, including during app construction."""
import os
from pathlib import Path


class HostLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self):
        if self.stream is not None:
            return
        if self.path.is_symlink():
            raise ValueError('The host lock cannot be a symbolic link')
        flags = os.O_RDWR | os.O_CREAT | getattr(os,'O_NOFOLLOW',0)
        descriptor = os.open(self.path,flags,0o600)
        stream = os.fdopen(descriptor,'r+b')
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError('Cannot acquire host lock. Close any other FunGuys host using this state directory and check its file permissions.') from exc
        self.stream = stream

    def release(self):
        stream,self.stream = self.stream,None
        if stream is not None:
            stream.close()

    def __del__(self):
        self.release()
