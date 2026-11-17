"""Photo storage, behind a small interface.

Today there is one implementation, LocalStorage, which reads a folder on disk.
It exists as a class rather than loose functions so that a cloud backend (S3,
GCS, Azure Blob) can be added later by implementing the same four methods -
the Flask app never touches a filesystem path directly.

    list_photos()          -> [PhotoRef]
    load_image(ref)        -> BGR numpy array, for scoring
    scaled_bytes(ref, px)  -> JPEG bytes at that size, cached
    copy_to_bucket(ref, b) -> copy the ORIGINAL into output/<bucket>/
"""

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2

import config
import cull


def trash_file(path, output_dir):
    """Move a file into output/.trash instead of deleting it.

    A name already in the trash gets a counter rather than being overwritten,
    so two runs cannot silently destroy the older copy.
    """
    trash = Path(output_dir) / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / path.name
    counter = 1
    while target.exists():
        target = trash / f"{path.stem}-{counter}{path.suffix}"
        counter += 1
    path.rename(target)
    return target


@dataclass(frozen=True)
class PhotoRef:
    """A photo we can score, identified by a name that is safe to put in a URL."""

    name: str          # the filename, e.g. DSC_0101.jpg
    path: Path         # absolute path on disk
    size_bytes: int
    mtime: float

    @property
    def cache_key(self):
        """Stable per (identity, content) so edits or replacements re-thumbnail."""
        raw = f"{self.path}|{self.size_bytes}|{self.mtime}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]


class LocalStorage:
    """Reads photos from a local folder."""

    def __init__(self, input_dir=None, output_dir=None, thumb_dir=None):
        self.input_dir = Path(input_dir or config.INPUT_DIR)
        self.output_dir = Path(output_dir or config.OUTPUT_DIR)
        self.thumb_dir = Path(thumb_dir or config.THUMB_DIR)

    def list_photos(self):
        if not self.input_dir.is_dir():
            return []
        refs = []
        for path in sorted(self.input_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in cull.EXTENSIONS:
                continue
            stat = path.stat()
            refs.append(
                PhotoRef(
                    name=path.name,
                    path=path,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
        return refs

    def resolve(self, name):
        """Look up a photo by name, refusing anything outside the input folder.

        The name arrives from a URL, so it is untrusted: "../../.ssh/id_rsa" must
        not resolve. We therefore match against the listing rather than joining
        the name onto a path, which makes traversal impossible by construction.
        """
        for ref in self.list_photos():
            if ref.name == name:
                return ref
        return None

    def load_image(self, ref):
        """Decode a photo into a BGR array for scoring. Raises cull.LoadError."""
        image, label = cull.load_image(ref.path)
        return image, label

    def scaled_array(self, ref, longest):
        """Decode a photo and shrink it so its long edge is `longest` pixels.

        Separate from scaled_bytes so the editor can apply adjustments to the
        small image before it is encoded.
        """
        # Only the full-size decode is worth forcing for big previews; the
        # reduced decode is still lossless at thumbnail sizes.
        image, _ = cull.load_image(ref.path, reduced=longest <= config.THUMB_SIZE)
        height, width = image.shape[:2]
        scale = longest / max(height, width)
        if scale < 1:
            image = cv2.resize(
                image,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        return image

    def scaled_bytes(self, ref, longest=None):
        """JPEG bytes at a given size, generated once and cached per size.

        Without this cache the browser would re-decode full-size RAW previews on
        every scroll, which makes the grid unusable.
        """
        longest = longest or config.THUMB_SIZE
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        cached = self.thumb_dir / f"{ref.cache_key}-{longest}.jpg"
        if cached.exists():
            return cached.read_bytes()

        image = self.scaled_array(ref, longest)
        quality = 82 if longest <= config.THUMB_SIZE else 90
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise cull.LoadError("could not encode thumbnail")
        data = buffer.tobytes()
        cached.write_bytes(data)
        return data

    def clear_bucket(self, bucket):
        """Clear output/<bucket>/ before a fresh apply.

        Without this, re-applying at a new threshold leaves the old copy behind
        and a photo that changed side ends up in BOTH keep/ and reject/.

        Photos are MOVED to output/.trash, never deleted. These folders look
        like a safe place to drop a favourite, and they are not ours to erase.
        """
        folder = self.output_dir / bucket
        if not folder.is_dir():
            return 0
        moved = 0
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() in cull.EXTENSIONS:
                trash_file(path, self.output_dir)
                moved += 1
        return moved

    def copy_to_bucket(self, ref, bucket):
        """Copy the ORIGINAL file into output/<bucket>/. Never moves or deletes."""
        destination = self.output_dir / bucket
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / ref.name
        shutil.copy2(ref.path, target)
        return target
