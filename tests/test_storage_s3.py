#!/usr/bin/env python3
"""Checks for the S3 backend, against a stub client rather than a bucket.
Run: .venv/bin/python tests/test_storage_s3.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_s3 import S3Storage  # noqa: E402


class StubS3:
    """Just enough of the boto3 S3 client to answer this module's questions."""

    def __init__(self, objects, page_size=1000):
        self.objects = dict(objects)            # key -> bytes
        self.page_size = page_size
        self.downloaded = []
        self.uploaded = []

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        out = {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page]}
        if start + self.page_size < len(keys):
            out["IsTruncated"] = True
            out["NextContinuationToken"] = str(start + self.page_size)
        return out

    def download_file(self, Bucket, Key, Filename):
        self.downloaded.append(Key)
        Path(Filename).write_bytes(self.objects[Key])

    def upload_file(self, Filename, Bucket, Key):
        self.uploaded.append(Key)
        self.objects[Key] = Path(Filename).read_bytes()


def storage(objects, prefix="shoot/", **kwargs):
    folder = Path(tempfile.mkdtemp())
    client = StubS3(objects, **kwargs)
    return S3Storage(bucket="b", prefix=prefix, client=client,
                     input_dir=folder / "in", output_dir=folder / "out",
                     thumb_dir=folder / "thumbs"), client


def test_only_photos_at_the_top_of_the_prefix_are_listed():
    """A key with a slash in it is a different shoot, and its name would not
    be safe in a URL either. Non-photo keys are not photos."""
    store, _ = storage({
        "shoot/a.jpg": b"1", "shoot/b.heic": b"22",
        "shoot/notes.txt": b"x",          # not a photo
        "shoot/more/c.jpg": b"y",         # a sub-folder
        "other/d.jpg": b"z",              # a different prefix
    })
    names = sorted(ref.name for ref in store.list_photos())
    assert names == ["a.jpg", "b.heic"], names


def test_a_file_already_here_is_not_fetched_again():
    store, client = storage({"shoot/a.jpg": b"hello"})
    store.sync_down()
    assert client.downloaded == ["shoot/a.jpg"], client.downloaded
    store.sync_down()
    assert client.downloaded == ["shoot/a.jpg"], "it downloaded the same object twice"


def test_a_file_that_changed_size_is_fetched_again():
    store, client = storage({"shoot/a.jpg": b"hello"})
    store.sync_down()
    client.objects["shoot/a.jpg"] = b"hello, longer"
    store.sync_down()
    assert len(client.downloaded) == 2, "a replaced object was not picked up"


def test_listing_follows_every_page():
    """A shoot is easily more than one page of results, and stopping at the
    first one would silently lose the rest of it."""
    many = {f"shoot/{i:03d}.jpg": b"x" for i in range(250)}
    store, _ = storage(many, page_size=100)
    assert len(store.list_photos()) == 250


def test_sorting_a_photo_also_puts_it_in_the_bucket():
    store, client = storage({"shoot/a.jpg": b"hello"})
    ref = store.list_photos()[0]
    store.copy_to_bucket(ref, "keep")
    assert client.uploaded == ["shoot/keep/a.jpg"], client.uploaded


def test_a_prefix_never_doubles_its_slash():
    """"a//b" and "a/b" are different keys in S3 and look identical in a console."""
    for given in ("shoot", "shoot/", "/shoot/", ""):
        store, _ = storage({}, prefix=given)
        assert "//" not in store.prefix + "keep/x.jpg", store.prefix


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
