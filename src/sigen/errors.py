"""Exit codes and the exceptions that carry them.

Every failure the CLI can report is a SigenError subclass, so `main()` maps
exceptions to exit codes in exactly one place.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    """The process exit codes `sigen` returns; each comment says when."""

    OK = 0  # success; for writes, verified by read-back, or already at the target
    ERROR = 1  # unexpected error (a bug, or something unclassified), e.g. capture won't overwrite
    USAGE = 2  # bad command-line usage (argparse's own code), an invalid config, or MAC lookup where it can't work
    # couldn't connect, or the connection dropped / timed out (incl. a wrong unit ID); a weather fetch failed; NOAA
    # or ntfy unreachable
    CONNECTION = 3
    MODBUS = 4  # the device answered with a Modbus exception (incl. a refused write)
    VERIFY_FAILED = 5  # read-back after a write doesn't match, or the state is unknown after a dropped write
    REFUSED = 6  # write not allowed: register not writable, or value out of range
    # --expect-current didn't match, undo after an outside change, the database schema isn't ready (migrations
    # pending), or `sigen satellite` is already running
    PRECONDITION = 7
    DRY_RUN = 8  # a change is needed, but --apply wasn't given
    NOT_FOUND = 9  # no SigenStor found, nothing to undo, or a missing file
    DATABASE = 10  # the database is unreachable, or rejected a query


class SigenError(Exception):
    """Base of every failure the CLI reports; `main()` exits with exit_code.

    Raised as-is for errors with no closer class (a journal entry naming an
    unknown register, a GOES grid that doesn't fit).
    """

    exit_code = ExitCode.ERROR


class ConnectionFailed(SigenError):
    """Couldn't reach the device, or the link dropped, timed out or garbled a
    reply; also an outside service (NOAA's GOES bucket, ntfy) unreachable."""

    exit_code = ExitCode.CONNECTION


class ModbusError(SigenError):
    """The device answered with a Modbus exception reply (a refused write too).

    function and code are the reply's; address is the register asked for, or
    None if unknown.
    """

    exit_code = ExitCode.MODBUS

    def __init__(self, function: int, code: int, address: int | None = None) -> None:
        self.function, self.code, self.address = function, code, address
        names = {
            1: "illegal function",
            2: "illegal data address",
            3: "illegal data value",
            4: "device failure",
            6: "device busy",
            11: "gateway target failed to respond",
        }
        where = f" at {address}" if address is not None else ""
        super().__init__(f"Modbus exception {code} ({names.get(code, 'unknown')}) for function {function}{where}")


class PreconditionFailed(SigenError):
    """The state isn't what the command needs, e.g. `sigen satellite` is
    already running (its satellite.pid names a live process)."""

    exit_code = ExitCode.PRECONDITION


class NotFound(SigenError):
    """Something looked for isn't there: no SigenStor found, nothing to undo,
    an unreadable file, or a host / MAC that didn't answer."""

    exit_code = ExitCode.NOT_FOUND


class ConfigError(SigenError):
    """The --config file exists but its contents are invalid."""

    exit_code = ExitCode.USAGE


class PlatformNotSupported(SigenError):
    """A feature this system can't do (MAC lookup: only Windows, and Linux with the `ip` command)."""

    exit_code = ExitCode.USAGE


class DatabaseError(SigenError):
    """The database is unreachable or rejected a query (or psycopg is missing).

    transient=True (unreachable, connection lost) lets the pollers warn, drop
    the connection and carry on; anything else they re-raise.
    """

    exit_code = ExitCode.DATABASE

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient  # unreachable / connection lost, rather than a rejected query


class SchemaNotReady(SigenError):
    """The database schema doesn't match this code: migrations pending or edited,
    or register_meta out of date. Fixed by `sigen db migrate` (or a new migration)."""

    exit_code = ExitCode.PRECONDITION


class CloudError(SigenError):
    """A problem talking to the Sigen cloud: unreachable, an HTTP/API error, or a token that
    expired or was rejected (re-run `sigen cloud login`, or `sigen cloud refresh`)."""

    exit_code = ExitCode.CONNECTION


class SyncError(SigenError):
    """A problem syncing from a peer database: the peer is unreachable or returned an error, or a
    request was malformed. (A schema mismatch raises SchemaNotReady instead -- that's a precondition.)"""

    exit_code = ExitCode.CONNECTION
