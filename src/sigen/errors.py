"""Exit codes and the exceptions that carry them.

Every failure the CLI can report is a SigenError subclass, so `main()` maps
exceptions to exit codes in exactly one place.
"""
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0                   # success; for writes, verified by read-back, or already at the target
    ERROR = 1                # unexpected error (a bug, or something unclassified), e.g. capture won't overwrite
    USAGE = 2                # bad command-line usage (argparse's own code), an invalid config, or MAC lookup
                             # where it can't work
    CONNECTION = 3           # couldn't connect, or the connection dropped / timed out (incl. a wrong unit ID); a
                             # weather fetch failed; NOAA or ntfy unreachable
    MODBUS = 4               # the device answered with a Modbus exception (incl. a refused write)
    VERIFY_FAILED = 5        # read-back after a write doesn't match, or the state is unknown after a dropped write
    REFUSED = 6              # write not allowed: register not writable, or value out of range
    PRECONDITION = 7         # --expect-current didn't match, undo after an outside change, the database
                             # schema isn't ready (migrations pending), or `sigen satellite` is already running
    DRY_RUN = 8              # a change is needed, but --apply wasn't given
    NOT_FOUND = 9            # no SigenStor found, nothing to undo, or a missing file
    DATABASE = 10            # the database is unreachable, or rejected a query


class SigenError(Exception):
    exit_code = ExitCode.ERROR


class ConnectionFailed(SigenError):
    exit_code = ExitCode.CONNECTION


class ModbusError(SigenError):
    exit_code = ExitCode.MODBUS

    def __init__(self, function, code, address=None):
        self.function, self.code, self.address = function, code, address
        names = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
                 4: "device failure", 6: "device busy", 11: "gateway target failed to respond"}
        where = f" at {address}" if address is not None else ""
        super().__init__(f"Modbus exception {code} ({names.get(code, 'unknown')}) "
                         f"for function {function}{where}")


class VerifyFailed(SigenError):
    exit_code = ExitCode.VERIFY_FAILED


class Refused(SigenError):
    exit_code = ExitCode.REFUSED


class PreconditionFailed(SigenError):
    exit_code = ExitCode.PRECONDITION


class NotFound(SigenError):
    exit_code = ExitCode.NOT_FOUND


class ConfigError(SigenError):
    """The --config file exists but its contents are invalid."""
    exit_code = ExitCode.USAGE


class PlatformNotSupported(SigenError):
    """A feature this system can't do (MAC lookup: only Windows, and Linux with the `ip` command)."""
    exit_code = ExitCode.USAGE


class DatabaseError(SigenError):
    exit_code = ExitCode.DATABASE

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient   # unreachable / connection lost, rather than a rejected query


class SchemaNotReady(SigenError):
    """The database schema doesn't match this code: migrations pending or edited,
    or register_meta out of date. Fixed by `sigen db migrate` (or a new migration)."""
    exit_code = ExitCode.PRECONDITION
