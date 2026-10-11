from pathlib import Path

import pytest

from thinkhome_zulip_mcp.provisioning import read_secret, write_secret


def test_secret_files_are_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "key"
    write_secret(path, "synthetic-secret")
    assert read_secret(path) == "synthetic-secret"
    with pytest.raises(FileExistsError):
        write_secret(path, "replacement")
    assert read_secret(path) == "synthetic-secret"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        read_secret(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_secret(link)


@pytest.mark.parametrize("content", [b"", b"x" * 65537, b"sensitive-\xff"])
def test_invalid_secret_content_has_no_payload_in_error(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "key"
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(ValueError) as exc:
        read_secret(path)
    assert "sensitive" not in str(exc.value)
