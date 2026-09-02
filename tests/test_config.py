import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jobhunter.config import ConfigError, Settings, env_snapshot


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


def test_l2_codex_engine_and_effort() -> None:
    import pytest as _pytest

    s = Settings.load(
        _L2_BASE
        | {"JOB_HUNTER_L2_ENGINE": "codex-cli", "JOB_HUNTER_L2_MODEL_CANDIDATES": "gpt-5.6-sol"}
    )
    assert s.l2_engine == "codex-cli"
    assert s.l2_reasoning_effort == "low"  # extraction is labeling, not reasoning
    s.require_l2()  # codex-cli needs no base_url

    assert Settings.load(
        _L2_BASE | {"JOB_HUNTER_L2_REASONING_EFFORT": "medium"}
    ).l2_reasoning_effort == "medium"
    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_REASONING_EFFORT": "ludicrous"})
    with _pytest.raises(ConfigError):
        Settings.load(_L2_BASE | {"JOB_HUNTER_L2_ENGINE": "carrier-pigeon"})


def test_env_file_layering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobhunter.config import load_env_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "JOB_HUNTER_ARCHIVE_URL=file:///from-dotenv\n# comment\nPATH=/evil\n")
    cfg = tmp_path / "cfghome" / "job-hunter"
    cfg.mkdir(parents=True)
    (cfg / "env").write_text(
        "JOB_HUNTER_ARCHIVE_URL=file:///from-config\nJOB_HUNTER_DROP_RATIO=0.7\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfghome"))
    merged = load_env_files({"JOB_HUNTER_DATABASE_URL": "postgresql://x"})
    assert merged["JOB_HUNTER_ARCHIVE_URL"] == "file:///from-dotenv"  # .env beats config
    assert merged["JOB_HUNTER_DROP_RATIO"] == "0.7"                   # config fills gaps
    assert merged["JOB_HUNTER_DATABASE_URL"] == "postgresql://x"      # process env survives
    assert "PATH" not in merged                                       # non-prefixed keys ignored


def test_process_env_beats_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobhunter.config import load_env_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JOB_HUNTER_ARCHIVE_URL=file:///from-dotenv\n")
    merged = load_env_files({"JOB_HUNTER_ARCHIVE_URL": "file:///from-process"})
    assert merged["JOB_HUNTER_ARCHIVE_URL"] == "file:///from-process"


def test_dotenv_is_gitignored() -> None:
    """`./.env` is a config layer holding the R2 secret and the Neon DSN — never commit it."""
    repo = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None or not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=repo, check=False
    ).returncode
    assert ignored == 0, ".env must be listed in .gitignore (git check-ignore says it is not)"


def test_the_suite_never_reads_the_developers_config_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermeticity, guarding `conftest`'s autouse fixture.

    On a working machine both file layers hold the real R2 keys and the Neon
    DSN, so a `Settings.load()` anywhere in the suite silently comes back
    configured — assertions about missing configuration pass on the laptop and
    fail in CI. Neither layer may be visible from inside a test.
    """
    poisoned = tmp_path / "home" / ".config" / "job-hunter"
    poisoned.mkdir(parents=True)
    (poisoned / "env").write_text(
        "JOB_HUNTER_ARCHIVE_URL=s3://the-owners-bucket/corpus\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    # pytest is started from the repo root, where the developer's `.env` lives.
    assert Path.cwd() != Path(__file__).resolve().parents[1]
    assert not (Path.cwd() / ".env").exists()
    assert "JOB_HUNTER_ARCHIVE_URL" not in env_snapshot()
    with pytest.raises(ConfigError, match="JOB_HUNTER_ARCHIVE_URL"):
        Settings.load()


def test_the_suite_never_reads_the_developers_exported_variables() -> None:
    """Same invariant for the shell that ran pytest: a stray `JOB_HUNTER_L2_*`
    or AWS credential in it must not reach any test. `JOB_HUNTER_TEST_*` is the
    suite's own knob (the Postgres DSN), not configuration, so it stays."""
    leaked = sorted(
        k
        for k in os.environ
        if k.startswith(("JOB_HUNTER_", "AWS_")) and not k.startswith("JOB_HUNTER_TEST_")
    )
    assert leaked == []


def test_state_dir_default_and_override() -> None:
    s = Settings.load({"JOB_HUNTER_ARCHIVE_URL": "file:///a",
                       "JOB_HUNTER_STATE_DIR": "/tmp/js"})
    assert str(s.state_dir) == "/tmp/js"
    s2 = Settings.load({"JOB_HUNTER_ARCHIVE_URL": "file:///a", "HOME": "/home/u"})
    assert str(s2.state_dir).endswith(".local/state/job-hunter")


def test_l2_trust_requested_model_flag() -> None:
    assert Settings.load(_L2_BASE).l2_trust_requested_model is False  # fail-safe default
    for truthy in ("1", "true", "YES"):
        s = Settings.load(_L2_BASE | {"JOB_HUNTER_L2_TRUST_REQUESTED_MODEL": truthy})
        assert s.l2_trust_requested_model is True
    assert (
        Settings.load(
            _L2_BASE | {"JOB_HUNTER_L2_TRUST_REQUESTED_MODEL": "0"}
        ).l2_trust_requested_model
        is False
    )
