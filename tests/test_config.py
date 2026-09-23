"""Settings must be settings, not constants (REQUIREMENTS.md §4.2).

The library is expected to move to a different volume, so these tests exist to
make that a config edit rather than a code change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photo_manager.config import (
    CONFIG_PATH_ENV_VAR,
    ENV_PREFIX,
    ConfigError,
    LibraryRootError,
    Settings,
)


def write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------


def test_loads_library_root_from_a_file(tmp_path: Path) -> None:
    root = tmp_path / "Pictures" / "XT30"
    root.mkdir(parents=True)
    cfg = write_config(tmp_path / "config.toml", f'library_root = "{root.as_posix()}"\n')

    settings = Settings.load(cfg)

    assert settings.library_root == root
    assert settings.source_path == cfg


def test_loads_every_setting_from_a_file(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path / "config.toml",
        f"""
        library_root = "{(tmp_path / 'lib').as_posix()}"
        card_dcim_root = "{(tmp_path / 'card').as_posix()}"
        card_folder_glob = "2??_FUJI"
        state_dir = "{(tmp_path / 'state').as_posix()}"
        thumbnail_max_px = 512
        session_gap_hours = 4.5
        """,
    )

    settings = Settings.load(cfg)

    assert settings.card_dcim_root == tmp_path / "card"
    assert settings.card_folder_glob == "2??_FUJI"
    assert settings.thumbnail_max_px == 512
    assert settings.session_gap_hours == 4.5


def test_database_and_cache_default_beneath_state_dir(tmp_path: Path) -> None:
    settings = Settings(library_root=tmp_path / "lib", state_dir=tmp_path / "state")

    assert settings.database_path == tmp_path / "state" / "catalog.sqlite3"
    assert settings.thumbnail_cache_dir == tmp_path / "state" / "thumbnails"


def test_environment_overrides_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from_file = tmp_path / "old-volume"
    from_env = tmp_path / "new-volume"
    cfg = write_config(tmp_path / "config.toml", f'library_root = "{from_file.as_posix()}"\n')
    monkeypatch.setenv(ENV_PREFIX + "LIBRARY_ROOT", str(from_env))

    assert Settings.load(cfg).library_root == from_env


def test_config_path_env_var_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = write_config(tmp_path / "elsewhere.toml", f'library_root = "{tmp_path.as_posix()}"\n')
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(cfg))

    assert Settings.load().source_path == cfg


def test_missing_library_root_is_an_error(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", "thumbnail_max_px = 400\n")

    with pytest.raises(ConfigError, match="No library_root configured"):
        Settings.load(cfg)


def test_unknown_setting_is_an_error_not_a_silent_ignore(tmp_path: Path) -> None:
    # A typo in a setting name must not leave the app quietly running on a
    # default. That is how a library ends up in the wrong place.
    cfg = write_config(
        tmp_path / "config.toml",
        f'library_root = "{tmp_path.as_posix()}"\nlibary_root = "oops"\n',
    )

    with pytest.raises(ConfigError, match="libary_root"):
        Settings.load(cfg)


def test_malformed_toml_is_reported_clearly(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", "library_root = \n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        Settings.load(cfg)


def test_named_config_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        Settings.load(tmp_path / "nope.toml")


@pytest.mark.parametrize("bad", ["thumbnail_max_px = 0", "session_gap_hours = -1"])
def test_nonsense_values_are_rejected(tmp_path: Path, bad: str) -> None:
    cfg = write_config(tmp_path / "config.toml", f'library_root = "{tmp_path.as_posix()}"\n{bad}\n')

    with pytest.raises(ConfigError):
        Settings.load(cfg)


def test_the_committed_example_config_is_loadable() -> None:
    # config.example.toml is what a new machine copies. If it stops parsing,
    # setup breaks silently for the next person — including future the owner.
    example = Path(__file__).resolve().parents[1] / "config.example.toml"

    settings = Settings.load(example)

    assert settings.library_root == Path(r"C:\Users\you\Pictures\Photos")
    # Commented out in the example on purpose: a drive letter is the thing that
    # moves, so the default is to probe for the card rather than assume it.
    assert settings.card_dcim_root is None


# ----------------------------------------------------------------------
# The library root contract
# ----------------------------------------------------------------------


def test_verify_library_root_accepts_a_real_directory(settings: Settings) -> None:
    assert settings.verify_library_root() == settings.library_root


def test_verify_library_root_fails_loudly_when_the_library_has_moved(tmp_path: Path) -> None:
    # The whole point of the check: a library on a drive that isn't mounted
    # must stop the app, not produce a new empty one.
    settings = Settings(library_root=tmp_path / "unmounted-volume" / "XT30")

    with pytest.raises(LibraryRootError, match="does not exist"):
        settings.verify_library_root()


def test_verify_library_root_rejects_a_file(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "XT30"
    not_a_dir.write_text("", encoding="utf-8")

    with pytest.raises(LibraryRootError, match="not a directory"):
        Settings(library_root=not_a_dir).verify_library_root()


# ----------------------------------------------------------------------
# Relative paths — what makes relocation a config edit (§4.2)
# ----------------------------------------------------------------------


def test_relativise_produces_a_forward_slashed_relative_path(settings: Settings) -> None:
    photo = settings.library_root / "2026" / "2026-07-13" / "2026-07-13_st-aldo_DSCF0807.jpg"

    assert settings.relativise(photo) == "2026/2026-07-13/2026-07-13_st-aldo_DSCF0807.jpg"


def test_the_same_stored_path_resolves_under_two_different_roots(tmp_path: Path) -> None:
    # This is the relocation guarantee in one test: the same catalog row, read
    # with a different library_root, points at the moved file.
    stored = "2026/2026-07-13/2026-07-13_st-aldo_DSCF0807.jpg"
    old = Settings(library_root=tmp_path / "C_drive" / "XT30")
    new = Settings(library_root=tmp_path / "E_drive" / "Photos")

    assert old.resolve(stored) == tmp_path / "C_drive" / "XT30" / Path(stored)
    assert new.resolve(stored) == tmp_path / "E_drive" / "Photos" / Path(stored)


def test_relativise_round_trips_through_resolve(settings: Settings) -> None:
    original = settings.library_root / "2026" / "2026-09-06" / "DSCF1029.jpg"

    assert settings.resolve(settings.relativise(original)) == original


def test_relativise_round_trips_under_a_second_root(tmp_path: Path) -> None:
    for root in (tmp_path / "one", tmp_path / "two" / "nested" / "deeper"):
        root.mkdir(parents=True)
        settings = Settings(library_root=root)
        photo = root / "2026" / "2026-09-06" / "DSCF1029.jpg"

        assert settings.relativise(photo) == "2026/2026-09-06/DSCF1029.jpg"
        assert settings.resolve("2026/2026-09-06/DSCF1029.jpg") == photo


def test_relativise_refuses_a_path_outside_the_library(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the library root"):
        settings.relativise(tmp_path / "somewhere-else" / "DSCF1029.jpg")


def test_relativise_refuses_the_root_itself(settings: Settings) -> None:
    with pytest.raises(ValueError, match="library root itself"):
        settings.relativise(settings.library_root)


def test_resolve_refuses_an_absolute_path(settings: Settings) -> None:
    # Absolute paths in the catalog are exactly what §4.2 forbids.
    with pytest.raises(ValueError, match="must be relative"):
        settings.resolve(settings.library_root / "DSCF1029.jpg")


def test_resolve_refuses_to_escape_the_root(settings: Settings) -> None:
    with pytest.raises(ValueError, match="escape the library root"):
        settings.resolve("../../Windows/System32/DSCF1029.jpg")


def test_resolve_accepts_a_backslashed_stored_path(settings: Settings) -> None:
    # Defensive: if a Windows-flavoured path ever reaches the catalog, reading
    # it back should still land in the right place.
    assert settings.resolve(r"2026\2026-09-06\DSCF1029.jpg") == (
        settings.library_root / "2026" / "2026-09-06" / "DSCF1029.jpg"
    )


# ----------------------------------------------------------------------
# The card
# ----------------------------------------------------------------------


def test_no_card_mounted_is_a_normal_state_not_an_error(settings: Settings) -> None:
    assert settings.card_image_folders() == []


def test_card_image_folders_is_empty_when_nothing_is_configured(
    library_root: Path,
) -> None:
    """The default is now None, and this narrow helper has nothing to look at.
    Telling the three empty cases apart is cards.detect_card's job."""
    assert Settings(library_root=library_root).card_image_folders() == []


def test_card_folders_are_found_by_glob_not_by_name(tmp_path: Path) -> None:
    # Both the drive letter and the folder number change (FR-1.1).
    dcim = tmp_path / "DCIM"
    for name in ("100_FUJI", "101_FUJI", "MISC"):
        (dcim / name).mkdir(parents=True)
    settings = Settings(library_root=tmp_path, card_dcim_root=dcim)

    assert [p.name for p in settings.card_image_folders()] == ["100_FUJI", "101_FUJI"]
