"""Typed errors. Every error Post-Radio raises on purpose is a PostRadioError, so a caller can catch one
base class and still tell the cases apart:

    PostRadioError
    ├── InvalidInput       the request itself is malformed (bad range, unknown stream, malformed MBID / link)
    ├── NotFound           well-formed, but the seed / release / playlist doesn't exist
    ├── ConfigError        a config file or setting is wrong
    ├── DataError          optional data is missing or corrupt (store, release checksum)
    └── SourceError        an outside service failed
        ├── SourceUnavailable   unreachable, timing out, blocked (e.g. Yandex 451), or answering garbage
        └── SourceAuthError     a credential (token, password) was refused

InvalidInput, NotFound, ConfigError and DataError also subclass ValueError, so older `except ValueError`
callers keep working. A host app maps them to HTTP codes: InvalidInput 422, NotFound 404, SourceAuthError
422, SourceUnavailable 503, DataError / ConfigError 500."""


class PostRadioError(Exception):
    """Base of every error Post-Radio raises on purpose."""


class InvalidInput(PostRadioError, ValueError):
    """The request itself is malformed (bad range, unknown stream, malformed MBID, track string or link)."""


class NotFound(PostRadioError, ValueError):
    """The request is well-formed but the thing it names doesn't exist upstream."""


class ConfigError(PostRadioError, ValueError):
    """A config file or setting is wrong (unknown key, wrong type, out of range, missing file)."""


class DataError(PostRadioError, ValueError):
    """Optional data is missing or corrupt: no data release, a checksum mismatch."""


class SourceError(PostRadioError, RuntimeError):
    """An outside service failed."""


class SourceUnavailable(SourceError):
    """Unreachable, timing out, blocked in this region, or answering something unusable."""


class SourceAuthError(SourceError):
    """A credential (ListenBrainz token, Navidrome password) was refused."""
