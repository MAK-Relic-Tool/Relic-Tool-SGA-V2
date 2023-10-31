"""
TestCases for 'EssenceDriveFS not respecting // separator'
https://github.com/MAK-Relic-Tool/Issue-Tracker/issues/39
"""
import zlib
from contextlib import contextmanager

import fs
from fs.base import FS
from fs.memoryfs import MemoryFS

from relic.sga.core import StorageType
from relic.sga.core.essencefs import EssenceFS


@contextmanager
def _generate_fake_osfs() -> FS:
    raw_text = b"""Ready to unleash 11 barrels of lead.
Where's that artillery?!?!
Orks are da biggust and da strongest.
Fix bayonets!
Fear me, but follow!
Call for an earth-shaker?
My mind is too weary to fight on...
We'll be off as soon as the fuel arrives.
Where are those tech priests.
Fire until they see the glow of our barrels!"""

    comp_text = zlib.compress(raw_text)

    with MemoryFS() as fs:
        with fs.makedir("/samples") as samples_folder:
            with samples_folder.makedir("/strings") as strings_folders:
                with strings_folders.openbin("buffer.txt", "wb") as file:
                    file.write(comp_text)
                with strings_folders.openbin("stream.txt", "wb") as file:
                    file.write(comp_text)
                with strings_folders.openbin("store.txt", "wb") as file:
                    file.write(raw_text)
        yield fs


_CHUNK_SIZE = 1024 * 1024 * 16  # 16 MiB


def _pack_fake_osfs(osfs: FS, name: str) -> EssenceFS:
    # Create 'SGA' V2
    sga = SgaFsV2()
    sga.setmeta(
        {
            "name": name,  # Specify name of archive
            "header_md5": "0"
            * 16,  # Must be present due to a bug, recalculated when packed
            "file_md5": "0"
            * 16,  # Must be present due to a bug, recalculated when packed
        },
        "essence",
    )

    alias = "data"
    name = "test data"
    sga_drive = None  # sga.create_drive(alias)
    for path in osfs.walk.files():
        if (
            sga_drive is None
        ):  # Lazily create drive, to avoid empty drives from being created
            sga_drive = sga.create_drive(alias, name)

        if "stream" in path:
            storage = StorageType.STREAM_COMPRESS
        elif "buffer" in path:
            storage = StorageType.BUFFER_COMPRESS
        else:
            storage = StorageType.STORE

        with osfs.openbin(path, "r") as unpacked_file:
            parent, file = fs.path.split(path)
            with sga_drive.makedirs(parent, recreate=True) as folder:
                with folder.openbin(file, "w") as packed_file:
                    while True:
                        buffer = unpacked_file.read(_CHUNK_SIZE)
                        if len(buffer) == 0:
                            break
                        packed_file.write(buffer)
            sga_drive.setinfo(path, {"essence": {"storage_type": storage}})
    return sga


def _check_path(sga: EssenceFS, path: str):
    left_sep = path.replace("\\", "/")
    right_sep = path.replace("/", "\\")

    info = sga.getinfo(path)
    l_info = sga.getinfo(left_sep)
    r_info = sga.getinfo(right_sep)

    assert info == l_info
    assert l_info == r_info


def test_fix_39():
    with _generate_fake_osfs() as osfs:
        sga = _pack_fake_osfs(osfs, "Test Archive")
        for root, folders, files in sga.walk():
            _check_path(sga, root)
            # for folder in folders
            # folders are checked when we walk into them
            for file in files:
                full_path = fs.path.join(root, file.name)
                _check_path(sga, full_path)
