import argparse
import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse


SUPPORTED_PROTOCOLS = {"mounted", "smb"}


@dataclass(frozen=True)
class RemoteDataProfile:
    name: str
    protocol: str
    mount_point: Path
    data_root: Path = Path(".")
    remote_url: str = ""
    username_env: str = "LENOVO_CLOUD_USER"
    password_env: str = "LENOVO_CLOUD_PASSWORD"

    @classmethod
    def from_dict(cls, payload):
        protocol = str(payload.get("protocol", "mounted")).lower()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                f"Unsupported remote protocol '{protocol}'. "
                f"Supported protocols: {sorted(SUPPORTED_PROTOCOLS)}"
            )
        mount_point = Path(os.path.expanduser(str(payload["mount_point"]))).resolve()
        data_root = Path(str(payload.get("data_root", ".")))
        return cls(
            name=str(payload.get("name", "remote_data")),
            protocol=protocol,
            mount_point=mount_point,
            data_root=data_root,
            remote_url=str(payload.get("remote_url", "")),
            username_env=str(payload.get("username_env", "LENOVO_CLOUD_USER")),
            password_env=str(payload.get("password_env", "LENOVO_CLOUD_PASSWORD")),
        )


def load_profile(path):
    with open(path, "r", encoding="utf-8") as handle:
        return RemoteDataProfile.from_dict(json.load(handle))


def is_mount_point(path):
    path = Path(path)
    return path.exists() and os.path.ismount(path)


class RemoteDataConnector:
    def __init__(self, profile):
        if isinstance(profile, (str, os.PathLike)):
            profile = load_profile(profile)
        self.profile = profile

    @property
    def data_root(self):
        return (self.profile.mount_point / self.profile.data_root).resolve()

    def ensure_available(self, mount=False):
        if self.profile.protocol == "mounted":
            return self._require_data_root()

        if self.profile.protocol == "smb":
            if is_mount_point(self.profile.mount_point):
                return self._require_data_root()
            if not mount:
                raise RuntimeError(
                    f"{self.profile.mount_point} is not mounted. "
                    "Mount it in Finder first, or run this connector with mount=True."
                )
            self.mount_smb()
            return self._require_data_root()

        raise ValueError(f"Unsupported protocol: {self.profile.protocol}")

    def resolve_path(self, relative_path=".", must_exist=True):
        root = self.ensure_available(mount=False)
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError(f"Path escapes remote data root: {relative_path}")
        if must_exist and not target.exists():
            raise FileNotFoundError(target)
        return target

    def iter_files(self, pattern="*.edf", recursive=True) -> Iterable[Path]:
        root = self.ensure_available(mount=False)
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        yield from sorted(path for path in iterator if path.is_file())

    def mount_smb(self):
        if platform.system() != "Darwin":
            raise RuntimeError("Automatic SMB mount is implemented for macOS only.")
        if not self.profile.remote_url:
            raise ValueError("remote_url is required for SMB mounting.")

        self.profile.mount_point.mkdir(parents=True, exist_ok=True)
        smb_spec = self._build_mount_smb_spec()
        subprocess.run(
            ["/sbin/mount_smbfs", smb_spec, str(self.profile.mount_point)],
            check=True,
        )

    def _require_data_root(self):
        root = self.data_root
        if not root.exists():
            raise FileNotFoundError(
                f"Remote data root does not exist: {root}. "
                "Check mount_point and data_root in the profile."
            )
        if not os.access(root, os.R_OK):
            raise PermissionError(f"Remote data root is not readable: {root}")
        return root

    def _build_mount_smb_spec(self):
        parsed = urlparse(self.profile.remote_url)
        if parsed.scheme not in ("", "smb"):
            raise ValueError("SMB remote_url must look like smb://host/share or //host/share")

        if parsed.scheme == "":
            raw = self.profile.remote_url
            if raw.startswith("//"):
                raw = "smb:" + raw
            parsed = urlparse(raw)

        host = parsed.hostname
        share = parsed.path.strip("/")
        if not host or not share:
            raise ValueError("SMB remote_url must include host and share, for example smb://192.168.1.10/Data")

        username = parsed.username or os.environ.get(self.profile.username_env, "")
        password = parsed.password or os.environ.get(self.profile.password_env, "")
        auth = ""
        if username:
            auth = quote(username, safe="")
            if password:
                auth += ":" + quote(password, safe="")
            auth += "@"
        return f"//{auth}{host}/{quote(share, safe='/')}"


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Connect analysis code to mounted Lenovo Personal Cloud data.")
    parser.add_argument("--profile", required=True, help="Path to remote data profile JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Validate that the configured data root is readable.")
    subparsers.add_parser("mount", help="Mount the remote SMB share, then validate the data root.")

    list_parser = subparsers.add_parser("list", help="List files under the remote data root.")
    list_parser.add_argument("--glob", default="*.edf", help="File glob to list, default: *.edf")
    list_parser.add_argument("--no-recursive", action="store_true", help="Do not recurse into subfolders.")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum number of files to print.")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a remote relative path to a local path.")
    resolve_parser.add_argument("relative_path", nargs="?", default=".")
    resolve_parser.add_argument("--allow-missing", action="store_true")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    connector = RemoteDataConnector(args.profile)

    if args.command == "check":
        root = connector.ensure_available(mount=False)
        print(root)
        return 0

    if args.command == "mount":
        root = connector.ensure_available(mount=True)
        print(root)
        return 0

    if args.command == "list":
        files = connector.iter_files(args.glob, recursive=not args.no_recursive)
        for idx, path in enumerate(files):
            if idx >= args.limit:
                break
            print(path)
        return 0

    if args.command == "resolve":
        path = connector.resolve_path(args.relative_path, must_exist=not args.allow_missing)
        print(path)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
