"""Windows DPAPI credential store.

Why DPAPI rather than a key of our own
--------------------------------------
The requirement is that a credential survives a reboot and a new process, and
that a *different Windows user on the same machine cannot read it*. DPAPI is the
operating system's answer to exactly that, and it is the right one here for a
specific reason: the key is derived from the user's logon credentials and managed
by the OS. Any scheme this project invented would have to store its own key
somewhere, and "somewhere" would be a file -- which is back to plaintext with
extra steps.

Two scopes exist, and the choice is load-bearing:

``CRYPTPROTECT_LOCAL_MACHINE``
    Any process on the machine can decrypt. Cheaper for a service, wrong here.
``CurrentUser`` (the default below)
    Only the user who encrypted it can decrypt. This is the property the
    requirement names, so it is the default and there is no flag to change it.

Implementation notes that are not optional
------------------------------------------
* ``CryptProtectData`` returns a ``DATA_BLOB`` whose buffer is allocated by the
  OS and must be released with ``LocalFree``. Skipping that leaks the
  *ciphertext* buffer on every save; worse, the same applies to
  ``CryptUnprotectData``, which leaks the *plaintext* -- a live credential
  sitting in the process heap with no owner.
* The plaintext never reaches a file. Serialisation happens in memory, is
  encrypted, and only the ciphertext is written, to a temporary path that is
  then ``os.replace``d. There is no window in which a readable credential file
  exists on disk, which is what a "write plaintext, then encrypt" design
  inevitably has.
* No exception carries credential material. :class:`CredentialStoreError`
  scrubs whatever it is given, and the Windows error path formats only a numeric
  code.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterator

from .store import (
    STORE_VERSION,
    CredentialBundle,
    CredentialStoreError,
    CredentialStoreStatus,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)

#: Where the encrypted blob lives. One location, documented once, never inside
#: the repository and never beside the executable -- a portable install would
#: otherwise scatter credential files across whatever directory it was run from.
#:
#: ``%LOCALAPPDATA%`` rather than ``%APPDATA%``: this file is machine-specific
#: and must not roam. A roaming profile would copy an encrypted blob to another
#: machine where DPAPI cannot decrypt it, producing a corrupt-looking file that
#: is really just a credential from elsewhere.
APP_DIRNAME = "OpenCSI"
CREDENTIAL_FILENAME = "credentials.dat"

#: The optional entropy argument to both DPAPI calls. Not a secret -- it is a
#: constant in a public repository -- and it is not doing the work of protecting
#: the file. It exists so that *another* application's ``CryptUnprotectData``
#: call, which would otherwise succeed against any blob this user encrypted,
#: fails against ours. That is a small amount of defence in depth against a
#: different program on the same account, not against an attacker with code
#: execution as this user (who could simply call DPAPI too).
_ENTROPY = b"opencsi.credentials.v1"

#: CRYPTPROTECT_UI_FORBIDDEN. Without it a failure can raise a modal dialog on
#: an interactive desktop, which in a tray process means the call never returns
#: and the user sees a stray prompt with no context.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    """The ``DATA_BLOB`` struct from ``dpapi.h``.

    ``cbData`` is a ``DWORD`` and ``pbData`` a byte pointer. Getting either wrong
    produces a crash rather than an error, so the field order and types are
    transcribed rather than approximated -- ctypes would happily accept a
    ``c_int`` here and corrupt the call.
    """

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def supported() -> bool:
    """Whether this platform has a secure store implementation.

    Checked before constructing a store so that callers can report
    "unsupported" rather than catching an exception at the first save.
    """
    return sys.platform == "win32"


#: How long ``_StoreWriteLock`` waits for the writer slot before giving up.
#: The critical section is two local file operations, so ten seconds of waiting
#: means something is genuinely wrong rather than merely contended.
_WRITE_LOCK_TIMEOUT_SECONDS = 10.0


class _StoreWriteLock:
    """A cross-process writer lock over one credential file.

    Why it exists
    -------------
    The store's writes are read-modify-write cycles that must not interleave:
    the tray renews the openCsiTool session while a CLI rotates the GitCode
    refresh token, and both operations ride the same file. Without
    synchronisation the writers collided in two measured ways
    (``tests/test_store_multiprocess.py`` reproduces both):

    * every writer stages its ciphertext through the *same* ``.tmp`` path, so
      two writers hold that file open at once and the first ``os.replace``
      fails with a Windows sharing violation -- a healthy renewal reported as
      "the credential file could not be written". A rotation lost that way
      leaves the server holding refresh token N+1 and the disk holding N, which
      the next refresh cannot recover from;
    * two writers truncating the shared ``.tmp`` concurrently can interleave
      their ciphertext into one blob, and whoever replaces last corrupts the
      store for every later reader.

    The lock is a separate ``.lock`` file holding no bytes of interest -- it
    never contains a credential (asserted by a test). The OS releases the lock
    when the owning process dies, so a crashed writer cannot strand the next
    one. Readers take no lock: ``os.replace`` is atomic, so a read sees either
    the old file or the new one, never a mixture.
    """

    def __init__(self, path: Path) -> None:
        self._lock_path = path.with_suffix(path.suffix + ".lock")

    @contextmanager
    def held(self) -> Iterator[None]:
        """Hold the writer slot. Raises :class:`CredentialStoreError` on timeout."""
        if sys.platform == "win32":
            import msvcrt

            handle = open(self._lock_path, "a+b")
            try:
                handle.seek(0)
                _lock_byte_with_retries(handle.fileno(), _WRITE_LOCK_TIMEOUT_SECONDS)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()
        else:
            import fcntl

            handle = open(self._lock_path, "a+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _lock_byte_with_retries(fd: int, timeout_seconds: float) -> None:
    """Take a one-byte mandatory lock on Windows, retrying until the timeout.

    ``msvcrt.LK_LOCK`` already retries once a second for ten attempts and then
    raises; it has no timeout parameter, so the retry budget here is expressed
    as a loop over ``LK_NDELAY`` instead. One attempt per poll keeps the wait
    bounded without silently exceeding the caller's stated timeout by much.
    """
    import msvcrt
    import time as _time

    deadline = _time.monotonic() + max(1.0, timeout_seconds)
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if _time.monotonic() >= deadline:
                raise
            _time.sleep(0.1)


def default_path() -> Path:
    """The credential file's location for the current user.

    ``%LOCALAPPDATA%`` when set. The fallback to ``~/.opencsi`` exists for a
    Windows service account or a stripped environment where the variable is
    missing; it is still a per-user directory, so the DPAPI user scope and the
    directory scope agree.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIRNAME / CREDENTIAL_FILENAME
    return Path.home() / ".opencsi" / CREDENTIAL_FILENAME


def _blob_from(data: bytes) -> _DataBlob:
    """Copy ``data`` into a DPAPI-owned buffer.

    The buffer must outlive the call, so it is created here and returned with the
    blob; letting a temporary go out of scope would leave ``pbData`` dangling.
    """
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _take_blob(blob: _DataBlob) -> bytes:
    """Copy a DPAPI-returned blob out and free its buffer.

    ``LocalFree`` is not optional. The buffer was allocated by the OS on our
    behalf, and for ``CryptUnprotectData`` its contents are the decrypted
    credential -- so leaking it leaves a live secret in the process heap with no
    owner and no lifetime.
    """
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(data: bytes) -> bytes:
    """Encrypt ``data`` for the current Windows user."""
    if not supported():
        raise CredentialStoreError("DPAPI is only available on Windows")
    blob_in = _blob_from(data)
    blob_out = _DataBlob()
    entropy = _blob_from(_ENTROPY)
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        # Only the numeric code: an error message built from the input could
        # carry the credential.
        raise CredentialStoreError(
            f"CryptProtectData failed (error {ctypes.GetLastError()})"
        )
    return _take_blob(blob_out)


def unprotect(data: bytes) -> bytes:
    """Decrypt ``data`` that :func:`protect` produced for this user."""
    if not supported():
        raise CredentialStoreError("DPAPI is only available on Windows")
    blob_in = _blob_from(data)
    blob_out = _DataBlob()
    entropy = _blob_from(_ENTROPY)
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise CredentialStoreError(
            f"CryptUnprotectData failed (error {ctypes.GetLastError()}); "
            "the credential file may belong to a different Windows user, or "
            "be corrupt. Run 'opencsi login --qr' to sign in again."
        )
    return _take_blob(blob_out)


class DpapiCredentialStore:
    """The real credential store: DPAPI-encrypted, current-user scoped.

    Reads and writes go through this class only. Note what is *absent*: there is
    no ``save_plaintext``, no ``export``, and no constructor flag that weakens
    the encryption. A store with an escape hatch is a store whose escape hatch
    eventually gets used.
    """

    name = "dpapi"

    def __init__(self, path: Path | None = None) -> None:
        if not supported():
            raise CredentialStoreError(
                "the DPAPI credential store is only available on Windows; "
                "there is deliberately no plaintext fallback"
            )
        self._path = Path(path) if path is not None else default_path()
        self._write_lock = _StoreWriteLock(self._path)

    @property
    def path(self) -> Path:
        """Where the encrypted blob lives. Not a secret."""
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    # ── read ──────────────────────────────────────────────────────────────
    def load(self) -> CredentialBundle:
        """Decrypt and parse the bundle.

        A missing file is an *empty* bundle, not an error: a user who has never
        signed in is the normal starting state, and ``opencsi usage`` must report
        "not signed in" rather than a storage failure.

        A file that exists but cannot be decrypted *is* an error, and it is
        reported without deleting anything. The file may be from another Windows
        user, or from a machine whose profile was copied; either way it is the
        user's only copy of something, and a tool that silently removes it to
        "fix" a read error has destroyed evidence it cannot recreate.
        """
        if not self._path.exists():
            return CredentialBundle()
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise CredentialStoreError(
                f"the credential file could not be read ({exc.__class__.__name__}); "
                "run 'opencsi login --qr' to sign in again"
            ) from None
        if not raw:
            # A zero-length file is what a crash between create and write leaves.
            # Treat it as empty rather than as corruption, but do not delete it.
            return CredentialBundle()

        plaintext = unprotect(raw)
        try:
            decoded = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialStoreError(
                "the credential store is unreadable (not valid JSON after "
                "decryption); run 'opencsi login --qr' to sign in again"
            ) from None
        finally:
            # Drop the plaintext copy as soon as it is parsed. Not a security
            # boundary -- Python strings are immutable and copies may persist --
            # but it shortens the window rather than lengthening it.
            del plaintext

        if not isinstance(decoded, dict):
            raise CredentialStoreError(
                "the credential store is unreadable (unexpected top-level shape); "
                "run 'opencsi login --qr' to sign in again"
            )
        try:
            return CredentialBundle.from_persisted(decoded)
        except ValueError as exc:
            raise CredentialStoreError(
                f"the credential store is unreadable ({exc}); "
                "run 'opencsi login --qr' to sign in again"
            ) from None

    # ── write ─────────────────────────────────────────────────────────────
    def _write(self, bundle: CredentialBundle) -> None:
        """Encrypt in memory, then atomically replace the file.

        The order is the point: serialise, encrypt, write ciphertext to a
        temporary path *in the same directory* (so ``os.replace`` is a rename on
        one filesystem rather than a cross-device copy), then rename. A crash at
        any point leaves either the old file or the new one, never a truncated
        credential.
        """
        payload = json.dumps(
            bundle.as_persisted(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        try:
            encrypted = protect(payload)
        finally:
            del payload

        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CredentialStoreError(
                f"the credential directory could not be created "
                f"({exc.__class__.__name__})"
            ) from None

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                # Flush to the device, not just the OS cache. Without this a
                # power loss can leave a renamed-but-empty file.
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise CredentialStoreError(
                f"the credential file could not be written "
                f"({exc.__class__.__name__})"
            ) from None

    def save(self, bundle: CredentialBundle) -> None:
        """Replace the whole bundle."""
        with self._write_lock.held():
            self._write(bundle)

    def _write_merged(self, replace: Callable[[CredentialBundle], CredentialBundle]) -> None:
        """Apply ``replace`` to the *current* on-disk bundle, under the lock.

        The re-read happens inside the lock, not before it. That is the whole
        point of the method: a caller that loaded the bundle and then wrote a
        replacement would serialize its *writes* while still merging into its
        own possibly stale view -- and would write back a half another process
        had already rotated. Merging into a fresh read under the lock is what
        makes ``save_gitcode``/``save_opencsi`` last-writer-wins per half
        instead of last-full-bundle-wins.
        """
        with self._write_lock.held():
            self._write(replace(self.load()))

    def save_gitcode(self, credential: StoredGitCodeCredential) -> None:
        """Persist the GitCode half, preserving the openCsiTool half."""
        self._write_merged(lambda bundle: bundle.with_gitcode(credential))

    def save_opencsi(self, credential: StoredOpenCsiCredential) -> None:
        """Persist the openCsiTool half, preserving the GitCode half.

        Preserving is the whole reason this method exists rather than a plain
        ``save``: this runs immediately after a renewal, and if it replaced the
        bundle wholesale it would delete the GitCode refresh token that made the
        renewal possible -- so the *next* expiry would need another QR scan.
        """
        self._write_merged(lambda bundle: bundle.with_opencsi(credential))

    # ── clear ─────────────────────────────────────────────────────────────
    def clear_gitcode(self) -> None:
        self._write_merged(lambda bundle: bundle.with_gitcode(None))

    def clear_opencsi(self) -> None:
        self._write_merged(lambda bundle: bundle.with_opencsi(None))

    def clear_all(self) -> None:
        """Remove the credential file entirely.

        Unlike the partial clears this deletes the file rather than writing an
        empty bundle, so ``logout`` leaves nothing on disk at all. Missing is
        already handled as "empty" by :meth:`load`.
        """
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CredentialStoreError(
                f"the credential file could not be removed "
                f"({exc.__class__.__name__})"
            ) from None

    # ── diagnose ──────────────────────────────────────────────────────────
    def status(self) -> CredentialStoreStatus:
        """Redacted status. Never raises: ``doctor`` must be able to report a
        broken store rather than failing on it."""
        try:
            bundle = self.load()
        except CredentialStoreError as exc:
            return CredentialStoreStatus(
                available=False,
                backend=self.name,
                path=str(self._path),
                detail=str(exc),
            )
        return CredentialStoreStatus(
            available=True,
            backend=self.name,
            path=str(self._path),
            has_gitcode=bundle.gitcode is not None,
            has_opencsi=bundle.opencsi is not None,
            opencsi_expires_at=(
                bundle.opencsi.expires_at if bundle.opencsi is not None else None
            ),
            gitcode_username=(
                bundle.gitcode.username if bundle.gitcode is not None else None
            ),
        )

    def __repr__(self) -> str:
        return f"DpapiCredentialStore(path={str(self._path)!r})"


def open_default_store() -> Any:
    """The credential store for this platform, or ``None`` if there is none.

    Returns ``None`` rather than raising on a non-Windows platform so a caller
    can distinguish "no secure store here" from "the store is broken", which are
    different messages to a user. It never returns a weaker store: there is no
    path from here to a plaintext file.
    """
    if not supported():
        return None
    return DpapiCredentialStore()
