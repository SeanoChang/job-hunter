from pathlib import Path

import pytest

from jobhunter.config import ConfigError, Settings


def test_load_requires_archive_url() -> None:
    with pytest.raises(ConfigError, match="JOB_HUNTER_ARCHIVE_URL"):
        Settings.load({})


def test_load_defaults(tmp_path: Path) -> None:
    s = Settings.load({"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}", "HOME": str(tmp_path)})
    assert s.archive_url == f"file://{tmp_path}"
    assert s.registry_path == Path("companies.toml")
    assert s.home == tmp_path / ".local/share/job-hunter"
    assert s.database_url is None


def test_load_overrides() -> None:
    s = Settings.load({
        "JOB_HUNTER_ARCHIVE_URL": "s3://b/p",
        "JOB_HUNTER_REGISTRY": "/etc/c.toml",
        "JOB_HUNTER_HOME": "/var/jh",
        "JOB_HUNTER_DATABASE_URL": "postgresql://x",
    })
    assert s.registry_path == Path("/etc/c.toml") and s.home == Path("/var/jh")
    assert s.database_url == "postgresql://x"
