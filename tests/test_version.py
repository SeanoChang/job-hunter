import jobhunter


def test_version_is_semver() -> None:
    major, minor, patch = jobhunter.__version__.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))
