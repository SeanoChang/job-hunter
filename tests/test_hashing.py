from jobhunter.hashing import canonical_json, sha256_hex


def test_canonical_json_sorts_keys_and_is_compact() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_canonical_json_keeps_unicode() -> None:
    assert canonical_json({"t": "東京"}) == '{"t":"東京"}'.encode()


def test_sha256_hex_known_value() -> None:
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
