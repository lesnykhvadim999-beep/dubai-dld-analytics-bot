"""Storage adapter: abstract R2 / B2 / GitHub-releases / local.

API:
    backend = get_backend()
    url = backend.upload(local_path, key)
    backend.download(key, dest_path)
    backend.delete(key)
    backend.list(prefix) -> list[(key, size, mtime)]
"""
from __future__ import annotations
import datetime as _dt
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from . import _config as cfg

log = logging.getLogger("dr.storage")


class _Base:
    name = "base"
    def upload(self, src: Path, key: str) -> str: raise NotImplementedError
    def download(self, key: str, dest: Path) -> None: raise NotImplementedError
    def delete(self, key: str) -> None: raise NotImplementedError
    def list(self, prefix: str = "") -> list[tuple[str, int, _dt.datetime]]:
        raise NotImplementedError


class LocalBackend(_Base):
    name = "local"
    def __init__(self, root: Path = cfg.BACKUP_ROOT):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def upload(self, src: Path, key: str) -> str:
        dst = self._p(key)
        if Path(src).resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return f"file:///{dst.as_posix()}"

    def download(self, key: str, dest: Path) -> None:
        src = self._p(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def delete(self, key: str) -> None:
        p = self._p(key)
        if p.exists():
            p.unlink()

    def list(self, prefix: str = "") -> list[tuple[str, int, _dt.datetime]]:
        out = []
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return out
        for f in self.root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(self.root).as_posix()
            if prefix and not rel.startswith(prefix):
                continue
            st = f.stat()
            out.append((rel, st.st_size,
                        _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc)))
        return out


class GitHubReleasesBackend(_Base):
    """One release per backup_type (daily/weekly/monthly), assets per backup file.
    Free: 2GB per asset, unlimited assets, unlimited releases.
    Requires `gh` CLI authenticated via GITHUB_PAT env."""
    name = "github"
    def __init__(self, repo: str = cfg.GITHUB_BACKUP_REPO, token: str | None = cfg.GITHUB_PAT):
        self.repo = repo
        self.token = token
        if not token:
            raise RuntimeError("GitHubReleasesBackend: GITHUB_PAT missing")
        # Ensure repo exists (create empty private if not)
        self._ensure_repo()

    def _env(self) -> dict:
        import os
        env = os.environ.copy()
        env["GH_TOKEN"] = self.token
        return env

    def _run(self, args: list[str], timeout: int = cfg.UPLOAD_TIMEOUT_SEC):
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           env=self._env(), timeout=timeout)
        return r

    def _ensure_repo(self) -> None:
        r = self._run(["repo", "view", self.repo, "--json", "name"], timeout=60)
        if r.returncode != 0:
            log.info("[dr.storage.github] creating repo %s", self.repo)
            self._run(["repo", "create", self.repo, "--private",
                       "--description", "DR backups (Vadim Realty + RERA BRN 65011) — encrypted"],
                      timeout=60)

    def _release_tag(self, key: str) -> str:
        # key looks like "daily/intelligence_2026-06-03.dump.gpg"
        parts = key.split("/", 1)
        return parts[0] if len(parts) == 2 else "misc"

    def upload(self, src: Path, key: str) -> str:
        tag = self._release_tag(key)
        # Make sure release exists
        r = self._run(["release", "view", tag, "--repo", self.repo], timeout=60)
        if r.returncode != 0:
            self._run(["release", "create", tag,
                       "--repo", self.repo,
                       "--title", f"{tag} backups",
                       "--notes", f"DR {tag} bucket (encrypted GPG, Vadim Realty)"],
                      timeout=60)
        asset_name = key.split("/", 1)[-1]
        r = self._run(["release", "upload", tag, str(src),
                       "--repo", self.repo, "--clobber",
                       f"--name={asset_name}" if False else str(src)],
                      timeout=cfg.UPLOAD_TIMEOUT_SEC)
        # gh release upload <tag> file -- file uploaded with its basename
        if r.returncode != 0:
            raise RuntimeError(f"gh upload failed: {r.stderr[:400]}")
        return f"https://github.com/{self.repo}/releases/download/{tag}/{Path(src).name}"

    def download(self, key: str, dest: Path) -> None:
        tag = self._release_tag(key)
        asset = key.split("/", 1)[-1]
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self._run(["release", "download", tag,
                       "--repo", self.repo,
                       "--pattern", asset,
                       "--dir", str(dest.parent),
                       "--clobber"],
                      timeout=cfg.UPLOAD_TIMEOUT_SEC)
        if r.returncode != 0:
            raise RuntimeError(f"gh download failed: {r.stderr[:400]}")
        downloaded = dest.parent / asset
        if downloaded != dest and downloaded.exists():
            shutil.move(str(downloaded), str(dest))

    def delete(self, key: str) -> None:
        tag = self._release_tag(key)
        asset = key.split("/", 1)[-1]
        self._run(["release", "delete-asset", tag, asset,
                   "--repo", self.repo, "--yes"], timeout=60)

    def list(self, prefix: str = "") -> list[tuple[str, int, _dt.datetime]]:
        # List releases, then assets per matching tag
        out: list[tuple[str, int, _dt.datetime]] = []
        r = self._run(["release", "list", "--repo", self.repo, "--limit", "100",
                       "--json", "tagName"], timeout=60)
        if r.returncode != 0:
            return out
        import json as _json
        try:
            tags = [x["tagName"] for x in _json.loads(r.stdout or "[]")]
        except Exception:
            tags = []
        prefix_tag = prefix.split("/", 1)[0] if prefix else ""
        for tag in tags:
            if prefix_tag and tag != prefix_tag:
                continue
            r2 = self._run(["release", "view", tag, "--repo", self.repo,
                            "--json", "assets"], timeout=60)
            if r2.returncode != 0:
                continue
            try:
                assets = _json.loads(r2.stdout or "{}").get("assets", [])
            except Exception:
                assets = []
            for a in assets:
                k = f"{tag}/{a['name']}"
                size = int(a.get("size", 0))
                ts = a.get("updatedAt") or a.get("createdAt") or ""
                try:
                    mt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    mt = _dt.datetime.now(_dt.timezone.utc)
                out.append((k, size, mt))
        return out


class S3CompatBackend(_Base):
    """R2 / B2 (S3-compatible). Requires boto3.
    R2 endpoint: https://<account>.r2.cloudflarestorage.com
    B2 endpoint: https://s3.<region>.backblazeb2.com"""
    name = "s3"
    def __init__(self, endpoint: str, key: str, secret: str, bucket: str, label: str = "s3"):
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError(f"S3CompatBackend needs boto3 (pip install boto3): {e}")
        from botocore.config import Config  # type: ignore
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
        self.bucket = bucket
        self.label = label
        # ensure bucket exists (best-effort)
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            try: self.client.create_bucket(Bucket=bucket)
            except Exception as e: log.warning("[dr.storage.%s] bucket create: %s", label, e)

    def upload(self, src: Path, key: str) -> str:
        self.client.upload_file(str(src), self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(dest))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list(self, prefix: str = "") -> list[tuple[str, int, _dt.datetime]]:
        out: list[tuple[str, int, _dt.datetime]] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token: kw["ContinuationToken"] = token
            r = self.client.list_objects_v2(**kw)
            for o in r.get("Contents", []) or []:
                out.append((o["Key"], int(o["Size"]),
                            o["LastModified"].astimezone(_dt.timezone.utc)))
            if not r.get("IsTruncated"): break
            token = r.get("NextContinuationToken")
        return out


# Composite: primary + always-on local fallback
class FailoverBackend(_Base):
    name = "failover"
    def __init__(self, primary: _Base, fallback: _Base):
        self.primary = primary
        self.fallback = fallback

    def upload(self, src: Path, key: str) -> str:
        # always keep a local copy too
        try:
            self.fallback.upload(src, key)
        except Exception as e:
            log.warning("[dr.storage] local fallback upload err: %s", e)
        try:
            return self.primary.upload(src, key)
        except Exception as e:
            log.error("[dr.storage] primary %s upload FAILED, kept local only: %s",
                      self.primary.name, e)
            return f"file:///{(cfg.BACKUP_ROOT / key).as_posix()}"

    def download(self, key: str, dest: Path) -> None:
        try:
            self.primary.download(key, dest)
            return
        except Exception as e:
            log.warning("[dr.storage] primary download failed, trying local: %s", e)
        self.fallback.download(key, dest)

    def delete(self, key: str) -> None:
        for b in (self.primary, self.fallback):
            try: b.delete(key)
            except Exception: pass

    def list(self, prefix: str = "") -> list[tuple[str, int, _dt.datetime]]:
        try:
            return self.primary.list(prefix)
        except Exception:
            return self.fallback.list(prefix)


def get_backend() -> _Base:
    name = cfg.storage_backend_name()
    local = LocalBackend()
    try:
        if name == "r2":
            primary = S3CompatBackend(
                endpoint=cfg.R2_ENDPOINT,
                key=cfg.R2_ACCESS_KEY,
                secret=cfg.R2_SECRET_KEY,
                bucket=cfg.R2_BUCKET,
                label="r2",
            )
        elif name == "b2":
            primary = S3CompatBackend(
                endpoint="https://s3.us-west-002.backblazeb2.com",
                key=cfg.B2_KEY_ID,
                secret=cfg.B2_APP_KEY,
                bucket=cfg.B2_BUCKET,
                label="b2",
            )
        elif name == "github":
            primary = GitHubReleasesBackend()
        else:
            return local
        return FailoverBackend(primary, local)
    except Exception as e:
        log.error("[dr.storage] primary backend init failed (%s) → local only: %s", name, e)
        return local
