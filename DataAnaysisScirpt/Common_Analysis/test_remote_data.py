import json
import os
import tempfile
from pathlib import Path

from remote_data import RemoteDataConnector, load_profile


def test_mounted_profile_resolves_remote_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        data_root = root / "Data"
        data_root.mkdir()
        target = data_root / "example.edf"
        target.write_text("placeholder", encoding="utf-8")
        profile_path = root / "profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "name": "test",
                    "protocol": "mounted",
                    "mount_point": str(root),
                    "data_root": "Data",
                }
            ),
            encoding="utf-8",
        )

        connector = RemoteDataConnector(load_profile(profile_path))
        assert connector.ensure_available() == data_root.resolve()
        assert connector.resolve_path("example.edf") == target.resolve()
        assert list(connector.iter_files("*.edf")) == [target.resolve()]


def test_path_escape_is_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        profile = {
            "name": "test",
            "protocol": "mounted",
            "mount_point": str(root),
            "data_root": ".",
        }
        profile_path = root / "profile.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        connector = RemoteDataConnector(profile_path)
        try:
            connector.resolve_path("../outside", must_exist=False)
        except ValueError:
            return
        raise AssertionError("Expected path escape to be rejected")


def test_smb_spec_uses_env_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["LENOVO_CLOUD_USER"] = "user name"
        os.environ["LENOVO_CLOUD_PASSWORD"] = "secret/value"
        profile = {
            "name": "test",
            "protocol": "smb",
            "mount_point": tmpdir,
            "remote_url": "smb://192.168.1.10/My Share",
        }
        connector = RemoteDataConnector(load_profile(_write_profile(tmpdir, profile)))
        spec = connector._build_mount_smb_spec()
        assert spec == "//user%20name:secret%2Fvalue@192.168.1.10/My%20Share"


def _write_profile(tmpdir, payload):
    profile_path = Path(tmpdir) / "profile.json"
    profile_path.write_text(json.dumps(payload), encoding="utf-8")
    return profile_path


if __name__ == "__main__":
    test_mounted_profile_resolves_remote_root()
    test_path_escape_is_rejected()
    test_smb_spec_uses_env_credentials()
    print("remote data tests passed")
