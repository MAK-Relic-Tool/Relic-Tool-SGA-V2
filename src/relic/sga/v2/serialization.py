"""
Binary Serializers for Relic's SGA-V2
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Dict, Tuple, cast

from serialization_tools.structx import Struct
from relic.sga.core import serialization as _s
from relic.sga.core.definitions import StorageType
from relic.sga.core.filesystem import registry, EssenceFS
from relic.sga.core.protocols import StreamSerializer
from relic.sga.core.serialization import (
    FileDef,
    ArchivePtrs,
    TocBlock,
    TOCSerializationInfo,
)
from relic.sga.v2.definitions import version


class FileDefSerializer(StreamSerializer[FileDef]):
    """
    Serializes File information using the V2 format.
    """

    STORAGE2INT: Dict[StorageType, int] = {
        StorageType.STORE: 0,
        StorageType.BUFFER_COMPRESS: 16,  # 0x10
        StorageType.STREAM_COMPRESS: 32,  # 0x20
    }
    INT2STORAGE: Dict[int, StorageType] = {
        value: key for key, value in STORAGE2INT.items()
    }  # reverse the dictionary

    def __init__(self, layout: Struct):
        self.layout = layout

    def unpack(self, stream: BinaryIO) -> FileDef:
        """Unpacks a File Definition from the stream."""
        storage_type_val: int
        (
            name_pos,
            storage_type_val,
            data_pos,
            length_in_archive,
            length_on_disk,
        ) = self.layout.unpack_stream(stream)
        storage_type: StorageType = self.INT2STORAGE[storage_type_val]
        return FileDef(
            name_pos=name_pos,
            data_pos=data_pos,
            length_on_disk=length_on_disk,
            length_in_archive=length_in_archive,
            storage_type=storage_type,
        )

    def pack(self, stream: BinaryIO, value: FileDef) -> int:
        """Packs a File Definition into the stream."""
        storage_type = self.STORAGE2INT[value.storage_type]
        args = (
            value.name_pos,
            storage_type,
            value.data_pos,
            value.length_in_archive,
            value.length_on_disk,
        )
        packed: int = self.layout.pack_stream(stream, *args)
        return packed


@dataclass
class MetaBlock(_s.MetaBlock):
    """
    Container for header information used by V2
    """

    name: str
    ptrs: ArchivePtrs
    file_md5: bytes
    header_md5: bytes

    @classmethod
    def default(cls) -> MetaBlock:
        """Returns a Default, 'garbage' instance which can be used as a placeholder for write-backs."""
        default_md5: bytes = b"default hash.   "
        return cls(
            "Default Meta Block", ArchivePtrs.default(), default_md5, default_md5
        )


@dataclass
class ArchiveHeaderSerializer(StreamSerializer[MetaBlock]):
    """
    Serializer to convert header information to it's dataclass; ArchiveHeader
    """

    layout: Struct

    ENCODING = "utf-16-le"

    def unpack(self, stream: BinaryIO) -> MetaBlock:
        """Unpacks a MetaBlock from the stream."""
        (
            file_md5,
            encoded_name,
            header_md5,
            header_size,
            data_pos,
        ) = self.layout.unpack_stream(stream)
        header_pos = stream.tell()
        name = encoded_name.decode(self.ENCODING).rstrip("\0")
        ptrs = ArchivePtrs(header_pos, header_size, data_pos)
        return MetaBlock(name, ptrs, file_md5=file_md5, header_md5=header_md5)

    def pack(self, stream: BinaryIO, value: MetaBlock) -> int:
        """Packs a MetaBlock into the stream."""
        encoded_name = value.name.encode(self.ENCODING)
        args = (
            value.file_md5,
            encoded_name,
            value.header_md5,
            value.ptrs.header_size,
            value.ptrs.data_pos,
        )
        written: int = self.layout.pack_stream(stream, *args)
        return written


FILE_MD5_EIGEN = b"E01519D6-2DB7-4640-AF54-0A23319C56C3"
HEADER_MD5_EIGEN = b"DFC9AF62-FC1B-4180-BC27-11CCE87D3EFF"


def assemble_meta(_: BinaryIO, header: MetaBlock, __: None) -> Dict[str, object]:
    """Extracts information from the meta-block to a dictionary the FS can store."""
    return {"file_md5": header.file_md5.hex(), "header_md5": header.header_md5.hex()}


def disassemble_meta(
        _: BinaryIO, metadata: Dict[str, object]
) -> Tuple[MetaBlock, None]:
    """Converts the archive's metadata dictionary into a MetaBlock class the Serializer can use."""
    meta = MetaBlock(
        None,  # type: ignore
        None,  # type: ignore
        header_md5=bytes.fromhex(cast(str, metadata["header_md5"])),
        file_md5=bytes.fromhex(cast(str, metadata["file_md5"])),
    )
    return meta, None


def recalculate_md5(stream: BinaryIO, meta: MetaBlock) -> None:
    """
    Recalculates file and header
    """
    file_md5_helper = _s.Md5ChecksumHelper(
        expected=None,
        stream=stream,
        start=meta.ptrs.header_pos,
        eigen=FILE_MD5_EIGEN,
    )
    header_md5_helper = _s.Md5ChecksumHelper(
        expected=None,
        stream=stream,
        start=meta.ptrs.header_pos,
        size=meta.ptrs.header_size,
        eigen=HEADER_MD5_EIGEN,
    )
    meta.file_md5 = file_md5_helper.read()
    meta.header_md5 = header_md5_helper.read()


def meta2def(meta: Dict[str, object]) -> FileDef:
    """
    Converts metadata to a File Definitions

    V2.0 only stores 'storage_type', which should be overridden later in the pipeline.
    """
    return FileDef(None, None, None, None, meta["storage_type"])  # type: ignore


class EssenceFSSerializer(_s.EssenceFSSerializer[FileDef, MetaBlock, None]):
    """
    Serializer to read/write an SGA file to/from a stream from/to a SGA File System
    """

    def __init__(
            self,
            toc_serializer: StreamSerializer[TocBlock],
            meta_serializer: StreamSerializer[MetaBlock],
            toc_serialization_info: TOCSerializationInfo[FileDef],
    ):
        super().__init__(
            version=version,
            meta_serializer=meta_serializer,
            toc_serializer=toc_serializer,
            toc_meta_serializer=None,
            toc_serialization_info=toc_serialization_info,
            assemble_meta=assemble_meta,
            disassemble_meta=disassemble_meta,
            build_file_meta=lambda _: {},
            gen_empty_meta=MetaBlock.default,
            finalize_meta=recalculate_md5,
            meta2def=meta2def,
        )


_folder_layout = Struct("<I 4H")
_folder_serializer = _s.FolderDefSerializer(_folder_layout)

_drive_layout = Struct("<64s 64s 5H")
_drive_serializer = _s.DriveDefSerializer(_drive_layout)

_dow_file_layout = Struct("<5I")  # For DoW
_dow_file_serializer = FileDefSerializer(_dow_file_layout)

_ic_file_layout = Struct("<I B 3I")  # For IC
_ic_file_serializer = FileDefSerializer(_ic_file_layout)

_toc_layout = Struct("<IH IH IH IH")
_toc_header_serializer = _s.TocHeaderSerializer(_toc_layout)

_meta_header_layout = Struct("<16s 128s 16s 2I")
_meta_header_serializer = ArchiveHeaderSerializer(_meta_header_layout)

_dow_essence_fs_serializer = EssenceFSSerializer(
    meta_serializer=_meta_header_serializer,
    toc_serializer=_toc_header_serializer,
    toc_serialization_info=TOCSerializationInfo(
        file=_dow_file_serializer,
        drive=_drive_serializer,
        folder=_folder_serializer,
        name_toc_is_count=True,
    ),
)

_ic_essence_fs_serializer = EssenceFSSerializer(
    meta_serializer=_meta_header_serializer,
    toc_serializer=_toc_header_serializer,
    toc_serialization_info=TOCSerializationInfo(
        file=_ic_file_serializer,
        drive=_drive_serializer,
        folder=_folder_serializer,
        name_toc_is_count=True,
    ),
)


class V2EssenceFSSerializer(EssenceFSSerializer):
    def __init__(self, primary, fallback):
        super().__init__(None, None, None)
        self.primary = primary
        self.fallback = fallback

    def read(self, stream: BinaryIO) -> EssenceFS:
        try:
            return self.primary.read(stream)
        except:
            return self.fallback.read(stream)

    def write(self, stream: BinaryIO, essence_fs: EssenceFS) -> int:
        raise NotImplementedError


essence_fs_serializer = V2EssenceFSSerializer(_dow_essence_fs_serializer, _ic_essence_fs_serializer)
registry.auto_register(essence_fs_serializer)

__all__ = [
    "FileDefSerializer",
    "MetaBlock",
    "ArchiveHeaderSerializer",
    # "ArchiveSerializer",
    # "archive_serializer",
    "essence_fs_serializer",
]
