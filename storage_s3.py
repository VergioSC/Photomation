#!/usr/bin/env python3
"""Read a shoot from an S3 bucket instead of a local folder.

LocalStorage hands out a PhotoRef with a real filesystem `path`, and everything
downstream depends on that: rawpy opens RAW files by path, pillow-heif opens
HEIC by path, the EXIF reader opens the file itself, and the exporter names the
output after `path.stem`. Rewriting all of that to take bytes would be a large
change for no gain, because every one of those libraries wants a file anyway.

So this syncs the bucket into a working folder and then IS a LocalStorage over
that folder. That is also how an image service actually behaves: pull the
object, do the work, push the result. What it adds is the pulling and the
pushing.

Configure with:
    PHOTOMATION_STORAGE=s3
    PHOTOMATION_S3_BUCKET=my-bucket
    PHOTOMATION_S3_PREFIX=shoots/2026-11/      (optional)
Credentials come from the usual AWS chain - environment, ~/.aws, or the
instance's own role - so none of them are ever read or stored by this app.
"""

from pathlib import Path

import config
import cull
from storage import LocalStorage


class S3Storage(LocalStorage):
    """A LocalStorage whose input folder is kept in step with a bucket."""

    def __init__(self, bucket, prefix="", client=None, **kwargs):
        super().__init__(**kwargs)
        self.bucket = bucket
        # One trailing slash, or no prefix at all. "a//b" and "a/b" are
        # different keys in S3, and the difference is invisible in a console.
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self._client = client

    @property
    def client(self):
        """Made on first use, so importing this module needs no credentials."""
        if self._client is None:
            import boto3
            self._client = boto3.client("s3")
        return self._client

    def _keys(self):
        """Every photo key under the prefix, as (key, size)."""
        found = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": self.prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                key = item["Key"]
                name = key[len(self.prefix):]
                # Only photos, and only at the top of the prefix: a key with a
                # slash in it belongs to a sub-folder, which is a different
                # shoot, and its name would not be safe in a URL either.
                if "/" in name or Path(name).suffix.lower() not in cull.EXTENSIONS:
                    continue
                found.append((key, item.get("Size", 0)))
            if not page.get("IsTruncated"):
                return found
            token = page.get("NextContinuationToken")

    def sync_down(self):
        """Fetch anything in the bucket that is not already here.

        Compared by size alone. An ETag is a better identity, but it is the
        MD5 only for a single-part upload, and a large RAW file is multipart,
        so a comparison that is right for small files and silently wrong for
        big ones is worse than one that is honestly approximate.
        """
        self.input_dir.mkdir(parents=True, exist_ok=True)
        fetched = 0
        for key, size in self._keys():
            target = self.input_dir / key[len(self.prefix):]
            if target.exists() and target.stat().st_size == size:
                continue
            self.client.download_file(self.bucket, key, str(target))
            fetched += 1
        return fetched

    def list_photos(self):
        self.sync_down()
        return super().list_photos()

    def copy_to_bucket(self, ref, bucket):
        """Sort the photo locally, then put the copy in the bucket too."""
        target = super().copy_to_bucket(ref, bucket)
        self.client.upload_file(str(target), self.bucket,
                                f"{self.prefix}{bucket}/{ref.name}")
        return target

    def upload_result(self, path, bucket="graded"):
        """Put a finished file into the bucket under its own folder."""
        key = f"{self.prefix}{bucket}/{Path(path).name}"
        self.client.upload_file(str(path), self.bucket, key)
        return key


def from_env():
    """Build the storage the environment asks for, or None for local."""
    if config.STORAGE_BACKEND != "s3":
        return None
    if not config.S3_BUCKET:
        raise SystemExit(
            "PHOTOMATION_STORAGE=s3 needs PHOTOMATION_S3_BUCKET as well.")
    return S3Storage(bucket=config.S3_BUCKET, prefix=config.S3_PREFIX)
