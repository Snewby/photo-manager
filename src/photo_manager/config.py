"""Application settings.

Nothing in this app may hardcode where the library lives or which drive the card
is on. Storage is being expanded separately (REQUIREMENTS.md C-2), so the library
root must be relocatable to another volume with a config edit and no catalog
migration. Two consequences are enforced here:

* Every setting comes from a TOML file (or an environment override), never from a
  constant in the code.
* :meth:`Settings.relativise` and :meth:`Settings.resolve` are the only sanctioned
  way to cross between an on-disk path and the relative ``library_path`` stored in
  the catalog. Absolute paths never reach the database.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath

__all__ = [
    "ConfigError",
    "LibraryRootError",
    "Settings",
    "DEFAULT_CONFIG_FILENAME",
    "CONFIG_PATH_ENV_VAR",
    "ENV_PREFIX",
]

DEFAULT_CONFIG_FILENAME = "config.toml"
CONFIG_PATH_ENV_VAR = "PHOTO_MANAGER_CONFIG"
ENV_PREFIX = "PHOTO_MANAGER_"

# The repository root, i.e. two levels above src/photo_manager/config.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    """Configuration is missing, malformed, or contradicts itself."""


class LibraryRootError(ConfigError):
    """The configured library root is absent or does not look like a library.

    Raised loudly rather than silently creating an empty library, which would
    look identical to "all your photos have vanished" (REQUIREMENTS.md §4.2).
    """


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved application settings.

    Paths are stored resolved and absolute *here*; the catalog stores paths
    relative to :attr:`library_root` instead.
    """

    #: Root of the photo library. The one setting with no sensible default.
    library_root: Path

    #: Where the camera's DCIM tree is mounted, as an **override**. None — the
    #: default — means "probe the attached volumes for it", which is what you
    #: want, because the drive letter changes between readers, ports and
    #: machines (FR-1.1). It used to default to ``D:/DCIM``, and the asymmetry
    #: with ``library_root`` (no default, fails loudly) was the smaller half of
    #: the problem: the larger half was that a wrong letter and no card at all
    #: produced the same empty result. See :func:`photo_manager.cards.detect_card`.
    card_dcim_root: Path | None = None

    #: Glob for the camera's image folders inside DCIM. The folder number rolls
    #: over (100_FUJI, 101_FUJI, ...), so this is a pattern, not a name (FR-1.1).
    card_folder_glob: str = "1??_FUJI"

    #: Derived state. Defaults sit beside the code, not inside the library, so
    #: the library stays a clean tree of photos and sidecars.
    state_dir: Path = _REPO_ROOT / "state"
    database_path: Path | None = None
    thumbnail_cache_dir: Path | None = None

    #: Where the app's Backup button writes snapshots — a folder on another
    #: physical drive, e.g. ``/Volumes/Backup/PhotoManagerBackup``. None
    #: means the button says it is not set up. It is a setting rather than
    #: something the page sends, because no endpoint accepts a path (§4.1).
    backup_root: Path | None = None

    #: Long edge of generated thumbnails, in pixels (FR-2.2).
    thumbnail_max_px: int = 400

    #: Long edge of the large preview the culling view shows (FR-4.1).
    #: 1560 is not arbitrary: this sensor is 6240 x 4160, so 1560 x 1040 is
    #: exactly a quarter, which is the largest preview the JPEG decoder can
    #: hand back with no resampling at all. Measured on a real frame, 453 ms
    #: against 956 ms for 1600 px — bigger is *slower and worse* here, because
    #: 1600 forces a half-size decode and a resample down. Change it if the
    #: camera changes.
    preview_max_px: int = 1560

    #: Gap between consecutive frames above which a sub-session split is
    #: *suggested* (FR-3.2). A suggestion, never forced.
    session_gap_hours: float = 3.0

    #: Path the settings were loaded from, for error messages. Not a setting.
    source_path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        # Fill the two paths that derive from state_dir unless explicitly set.
        if self.database_path is None:
            object.__setattr__(self, "database_path", self.state_dir / "catalog.sqlite3")
        if self.thumbnail_cache_dir is None:
            object.__setattr__(self, "thumbnail_cache_dir", self.state_dir / "thumbnails")
        if self.thumbnail_max_px <= 0:
            raise ConfigError(f"thumbnail_max_px must be positive, got {self.thumbnail_max_px}")
        if self.preview_max_px <= 0:
            raise ConfigError(f"preview_max_px must be positive, got {self.preview_max_px}")
        if self.session_gap_hours <= 0:
            raise ConfigError(f"session_gap_hours must be positive, got {self.session_gap_hours}")

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | os.PathLike[str] | None = None,
             env: dict[str, str] | None = None) -> Settings:
        """Load settings from TOML, then apply environment overrides.

        Search order for the file: the explicit *config_path*, then
        ``$PHOTO_MANAGER_CONFIG``, then ``config.toml`` in the repo root. A
        missing file is only an error when nothing supplies ``library_root``.
        """
        env = os.environ if env is None else env

        path = cls._locate(config_path, env)
        data: dict[str, object] = {}
        if path is not None:
            if not path.is_file():
                raise ConfigError(f"Config file not found: {path}")
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"Config file {path} is not valid TOML: {exc}") from exc

        known = {f.name for f in fields(cls)} - {"source_path"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"Unknown setting(s) in {path}: {', '.join(sorted(unknown))}. "
                f"Known settings: {', '.join(sorted(known))}"
            )

        # Environment wins over the file, so a test or a one-off run can point at
        # a different library without editing anyone's config.
        for name in known:
            value = env.get(ENV_PREFIX + name.upper())
            if value is not None:
                data[name] = value

        if "library_root" not in data:
            raise ConfigError(
                "No library_root configured. Set it in "
                f"{path or DEFAULT_CONFIG_FILENAME} or via {ENV_PREFIX}LIBRARY_ROOT. "
                "See config.example.toml."
            )

        kwargs: dict[str, object] = {"source_path": path}
        for name in ("library_root", "card_dcim_root", "state_dir",
                     "database_path", "thumbnail_cache_dir", "backup_root"):
            if name in data:
                kwargs[name] = _as_path(name, data[name])
        if "card_folder_glob" in data:
            kwargs["card_folder_glob"] = _as_str("card_folder_glob", data["card_folder_glob"])
        if "thumbnail_max_px" in data:
            kwargs["thumbnail_max_px"] = _as_int("thumbnail_max_px", data["thumbnail_max_px"])
        if "preview_max_px" in data:
            kwargs["preview_max_px"] = _as_int("preview_max_px", data["preview_max_px"])
        if "session_gap_hours" in data:
            kwargs["session_gap_hours"] = _as_float("session_gap_hours", data["session_gap_hours"])

        return cls(**kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _locate(config_path: str | os.PathLike[str] | None,
                env: dict[str, str]) -> Path | None:
        if config_path is not None:
            return Path(config_path).expanduser()
        from_env = env.get(CONFIG_PATH_ENV_VAR)
        if from_env:
            return Path(from_env).expanduser()
        default = _REPO_ROOT / DEFAULT_CONFIG_FILENAME
        return default if default.is_file() else None

    # ------------------------------------------------------------------
    # The library root contract
    # ------------------------------------------------------------------

    def verify_library_root(self) -> Path:
        """Confirm the configured root exists and is a directory.

        Called on startup. Failing here is the intended behaviour when a library
        has been moved to a new volume and the config has not caught up — far
        better than quietly treating an empty or wrong folder as the library.
        """
        root = self.library_root
        if not root.exists():
            raise LibraryRootError(
                f"Library root does not exist: {root}"
                + (f" (configured in {self.source_path})" if self.source_path else "")
            )
        if not root.is_dir():
            raise LibraryRootError(f"Library root is not a directory: {root}")
        return root

    def relativise(self, path: str | os.PathLike[str]) -> str:
        """Convert an absolute path inside the library to its stored form.

        Returns a forward-slashed relative path — the exact string that goes in
        ``photo.library_path``. Forward slashes keep the catalog readable and
        portable even though this runs on Windows.
        """
        resolved = Path(path).expanduser().resolve()
        root = self.library_root.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            raise ValueError(
                f"Path is outside the library root and cannot be stored "
                f"relative to it: {resolved} (root: {root})"
            ) from None
        if not relative.parts:
            raise ValueError(f"Path is the library root itself, not a file within it: {resolved}")
        return PurePosixPath(*relative.parts).as_posix()

    def resolve(self, library_path: str | os.PathLike[str]) -> Path:
        """Turn a stored relative ``library_path`` back into an absolute path."""
        candidate = PurePosixPath(str(library_path).replace("\\", "/"))
        if candidate.is_absolute() or Path(library_path).is_absolute():
            raise ValueError(
                f"library_path must be relative to the library root, got: {library_path}"
            )
        if ".." in candidate.parts:
            raise ValueError(f"library_path must not escape the library root: {library_path}")
        return self.library_root.joinpath(*candidate.parts)

    def card_image_folders(self) -> list[Path]:
        """Every ``1xx_FUJI`` folder under the **configured** DCIM root, sorted.

        Deliberately narrow, and not what an application should call. It
        answers only "what is at the path I was told about", and returns an
        empty list for a card that is somewhere else, a card that is empty, and
        no card at all — three different situations that a user needs told
        apart. Use :func:`photo_manager.cards.detect_card`, which probes the
        attached volumes and returns a status.
        """
        if self.card_dcim_root is None or not self.card_dcim_root.is_dir():
            return []
        return sorted(p for p in self.card_dcim_root.glob(self.card_folder_glob) if p.is_dir())


def _as_path(name: str, value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError(f"Setting {name} must be a path string, got {type(value).__name__}")
    text = str(value).strip()
    if not text:
        raise ConfigError(f"Setting {name} must not be empty")
    return Path(text).expanduser()


def _as_str(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"Setting {name} must be a string, got {type(value).__name__}")
    return value


def _as_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"Setting {name} must be an integer, got {type(value).__name__}")
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Setting {name} must be an integer, got {value!r}") from exc


def _as_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"Setting {name} must be a number, got {type(value).__name__}")
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"Setting {name} must be a number, got {value!r}") from exc
