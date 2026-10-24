"""Replace-on-success file writes.

atomic_write_text() serializes nothing itself: the caller passes the complete text, so a failure
while building it never touches the destination. The text goes to a temporary file in the
destination directory, is flushed and fsync'd, and only then replaces the destination with
os.replace (an atomic rename on the same volume on POSIX and Windows). On any failure the
temporary file is removed and the previous destination is left as it was.

Guarantee: readers see either the complete previous file or the complete new one, never a
truncated mix. Not guaranteed: that the rename itself survives a power loss (the directory entry
is not fsync'd, which Windows does not support), or that the replace succeeds while another
process holds the destination open without delete sharing (Windows raises PermissionError; the
previous file is then kept and the error propagates).
"""
import os
import tempfile


def atomic_write_text(path, text, encoding="utf-8"):
    path = os.path.abspath(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
