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


def test_drop_ratio_default_and_override(tmp_path: Path) -> None:
    base = {"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}"}
    assert Settings.load(base).drop_ratio == 0.5
    assert Settings.load({**base, "JOB_HUNTER_DROP_RATIO": "0.8"}).drop_ratio == 0.8
    with pytest.raises(ConfigError, match="JOB_HUNTER_DROP_RATIO"):
        Settings.load({**base, "JOB_HUNTER_DROP_RATIO": "2"})


def test_ping_url_default_and_override(tmp_path: Path) -> None:
    base = {"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}"}
    assert Settings.load(base).ping_url is None
    s = Settings.load({**base, "JOB_HUNTER_PING_URL": "https://hc.example.com/ping/uuid"})
    assert s.ping_url == "https://hc.example.com/ping/uuid"


def test_require_database_url(tmp_path: Path) -> None:
    s = Settings.load({"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}"})
    with pytest.raises(ConfigError, match="JOB_HUNTER_DATABASE_URL"):
        s.require_database_url()
    s2 = Settings.load({"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}",
                        "JOB_HUNTER_DATABASE_URL": "postgresql://x"})
    assert s2.require_database_url() == "postgresql://x"


_L2_BASE = {"JOB_HUNTER_ARCHIVE_URL": "file:///tmp/a"}


def test_l2_defaults() -> None:
    s = Settings.load(_L2_BASE)
    assert s.l2_engine == "openai-compat"
    assert s.l2_base_url is None and s.l2_api_key is None
    assert s.l2_models == ("*",)
    assert s.l2_model_candidates == ()
    assert s.l2_max_docs == 300 and s.l2_max_usd == 5.0


def test_l2_parsing_and_trimming() -> None:
    s = Settings.load(
        _L2_BASE
        | {
            "JOB_HUNTER_L2_ENGINE": "claude-cli",
            "JOB_HUNTER_L2_MODELS": " z-ai/glm-5.2*, nvidia/* ,",
            "JOB_HUNTER_L2_MODEL_CANDIDATES": (
                "z-ai/glm-5.2:free, nvidia/nemotron-3-ultra-550b-a55b:free"
            ),
            "JOB_HUNTER_L2_MAX_DOCS": "50",
            "JOB_HUNTER_L2_MAX_USD": "1.25",
        }
    )
    assert s.l2_engine == "claude-cli"
    assert s.l2_models == ("z-ai/glm-5.2*", "nvidia/*")
    assert s.l2_model_candidates == (
        "z-ai/glm-5.2:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    assert s.l2_max_docs == 50 and s.l2_max_usd == 1.25


def test_l2_invalid_values() -> None:
    import pytest as _pytest

    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_ENGINE": "carrier-pigeon"})
    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_MAX_DOCS": "many"})
    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_MAX_USD": "-1"})


def test_require_l2() -> None:
    import pytest as _pytest

    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE).require_l2()  # no candidates
    with _pytest.raises(ConfigError):  # claude-cli still needs candidates (no '?' fallback)
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_ENGINE": "claude-cli"}).require_l2()
    Settings.load(
        _L2_BASE
        | {
            "JOB_HUNTER_L2_BASE_URL": "https://openrouter.ai/api/v1",
            "JOB_HUNTER_L2_MODEL_CANDIDATES": "z-ai/glm-5.2:free",
        }
    ).require_l2()
    Settings.load(
        _L2_BASE
        | {
            "JOB_HUNTER_L2_ENGINE": "claude-cli",
            "JOB_HUNTER_L2_MODEL_CANDIDATES": "sonnet",
        }
    ).require_l2()


def test_l2_models_defaults_to_candidates_and_empty_is_error() -> None:
    import pytest as _pytest

    s = Settings.load(_L2_BASE | {"JOB_HUNTER_L2_MODEL_CANDIDATES": "a, b"})
    assert s.l2_models == ("a", "b")  # strict by default: accept what was requested
    wide = Settings.load(
        _L2_BASE | {"JOB_HUNTER_L2_MODEL_CANDIDATES": "a", "JOB_HUNTER_L2_MODELS": "*"}
    )
    assert wide.l2_models == ("*",)
    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_MODELS": " , "})


def test_l2_price_parse() -> None:
    import pytest as _pytest

    s = Settings.load(_L2_BASE | {"JOB_HUNTER_L2_PRICE": "0.35,0.75"})
    assert s.l2_price == (0.35, 0.75)
    assert Settings.load(_L2_BASE).l2_price is None
    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_PRICE": "cheap"})
