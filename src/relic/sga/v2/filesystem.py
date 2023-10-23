from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from threading import RLock
from typing import (
    BinaryIO,
    List,
    Iterable,
    Optional,
    Mapping,
    Union,
    Dict,
    Any,
    Tuple,
    Collection,
)

import fs.errors
from fs import ResourceType
from fs.base import FS
from fs.info import Info
from fs.mode import Mode
from relic.core.errors import RelicToolError
from relic.sga.core import StorageType
from relic.sga.core.hashtools import crc32
from relic.sga.core.lazyio import BinaryWindow, read_chunks
from relic.sga.core.serialization import (
    SgaNameWindow,
    SgaTocFolder,
    SgaTocDrive,
)

from relic.sga.v2 import version
from relic.sga.v2.serialization import (
    SgaTocFileDataV2Dow,
    SgaTocFileV2Dow,
    RelicDateTimeSerializer,
    SgaFileV2,
)

NS_BASIC = "basic"
NS_DETAILS = "details"
NS_ESSENCE = "essence"


def build_ns_basic(name: str, is_dir: bool):
    return {"name": name, "is_dir": is_dir}


def build_ns_details(
    type: ResourceType,
    size: int,
    *,
    accessed: Optional[datetime] = None,
    created: Optional[datetime] = None,
    metadata_changed: Optional[datetime] = None,
    modified: Optional[datetime] = None,
):
    return {
        "type": int(type),
        "size": size,
        "accessed": accessed,
        "created": created,
        "metadata_changed": metadata_changed,
        "modified": modified,
    }


class SgaPathResolver:
    SEP = "/"
    ROOT = SEP

    # TODO, move pathing logic to this class
    #   SGA is picky about how to handle files,
    #   and using the base implementations in FS is liable to cause issues
    #   as evidenced by how validatepath doesn't work for makedirs
    #   because it only calls iterparts, which can also fail, I think with mismatched seperators?

    @classmethod
    def build(cls, *path: str, alias: Optional[str] = None):
        full_path = cls.join(*path)
        if alias:
            if len(full_path) == 0:
                full_path = cls.ROOT
            elif full_path[0] != cls.ROOT:
                full_path = cls.ROOT + full_path
            return f"{alias}:{full_path}"
        else:
            return full_path

    @classmethod
    def parse(cls, path: str) -> Tuple[Optional[str], str]:
        if ":" in path:
            alias, path = path.split(":", maxsplit=1)
        else:
            alias = None
        return alias, path

    @classmethod
    def fix_seperator(cls, path: str):
        return path.replace("\\", cls.SEP)

    @classmethod
    def split_parts(cls, path: str) -> List[str]:
        path = cls.fix_seperator(path)
        parts = path.split(cls.SEP)
        if parts[0] == "" and path[0] == cls.SEP:  # captured root
            parts[0] = cls.ROOT
        return parts

    @classmethod
    def join(cls, *parts: str, add_root: bool = False) -> str:
        parts = (cls.fix_seperator(part) for part in parts)
        result = ""
        for part in parts:
            if part[0] == cls.SEP or len(result) == 0:
                result = part
            elif result[-1] != cls.SEP:
                result += cls.SEP + part
            else:
                result += part

        if add_root and (len(result) == 0 or result[0] != cls.ROOT):
            result = cls.ROOT + result
        return result

    @classmethod
    def split(cls, path):
        parts = cls.split_parts(path)
        if len(parts) > 0:
            return cls.join(*parts[:-1]), parts[-1]
        else:
            return "", path

    @classmethod
    def basename(cls, path):
        return cls.split(path)[1]

    @classmethod
    def dirname(cls, path):
        return cls.split(path)[0]


class _SgaFsFileV2:
    @property
    def name(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError()

    def getinfo(self, namespaces: Collection[str]) -> Info:
        raise NotImplementedError()

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        raise NotImplementedError()

    @contextmanager
    def openbin(self, mode: str) -> BinaryIO:
        raise NotImplementedError()

    def verify_crc32(self, error: bool) -> bool:
        raise NotImplementedError()

    def recalculate_crc32(self):
        raise NotImplementedError()


class SgaFsFileV2Lazy(_SgaFsFileV2):
    def __init__(self, info: SgaTocFileV2Dow, data: SgaTocFileDataV2Dow):
        # TODO
        #   we should probably accept a lock argument instead
        #   this will only protect this file from being read/written simultaneously
        #   reading/writing
        self._lock = RLock()

        # Disk (Lazy) Fields
        self._info = info
        self._data_info = data

    def close(self):
        pass

    def getinfo(self, namespaces: Collection[str]) -> Info:
        with self._lock:
            info = {NS_BASIC: build_ns_basic(self._info.name, False)}
            if NS_DETAILS in namespaces:
                modified_unix = self._data_info.header().modified
                modified_datetime = RelicDateTimeSerializer.unix2datetime(modified_unix)
                info[NS_DETAILS] = build_ns_details(
                    ResourceType.file,
                    self._info.decompressed_size,
                    modified=modified_datetime,
                )
            if NS_ESSENCE in namespaces:
                info["crc32"] = self._data_info.header().crc32
                info["storage_type"] = self._info.storage_type
            return Info(info)

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        raise RelicToolError(
            "Cannot write to a lazy file! Did the folder not convert this to a mem-file?"
        )

    @contextmanager
    def openbin(self, mode: str) -> BinaryIO:
        _mode = Mode(mode)
        if _mode.writing:
            raise RelicToolError(
                "Cannot write to a lazy file! Did the folder not convert this to a mem-file?"
            )

        with self._lock:
            yield self._data_info.data()

    def verify_crc32(self, error: bool) -> bool:
        hasher = crc32()
        # Locking should be handled by opening file, no need to lock here
        with self.openbin("r") as stream:
            expected = self._data_info.header().crc32
            if error:
                hasher.validate(stream, expected, name=f"File '{self.name}' CRC32")
                return True
            else:
                return hasher.check(stream, expected)

    def recalculate_crc32(self):
        raise RelicToolError(
            "Cannot write to a lazy file! Did the folder not convert this to a mem-file?"
        )


class SgaFsFileV2Mem(_SgaFsFileV2):
    def __init__(
        self,
        name: str,
        storage_type: Optional[StorageType] = None,
        data: Optional[Union[bytes, BinaryIO]] = None,
        modified: Optional[datetime] = None,
        crc: Optional[int] = None,
    ):
        self._lock = RLock()

        self._name: str = name
        self._modified: datetime = time.time() if modified is not None else modified
        self._storage_type: Optional[StorageType] = (
            storage_type if storage_type is not None else StorageType.STORE
        )

        # Create In-Memory handle
        self._handle = BytesIO()
        if data is None:
            pass
        elif isinstance(data, bytes):
            self._handle.write(data)
        else:
            for chunk in read_chunks(data):
                self._handle.write(chunk)

        self._size: int = (
            self._handle.tell()
        )  # Take advantage of ptr being at end of stream

        # crc32 hasher will read from start of stream, no need to seek
        self._crc32: int = crc if crc is not None else crc32(start=0).hash(self._handle)
        self._handle.seek(0)  # Ensure handle points to start of stream, again

    def close(self):
        if self._handle is not None:
            self._handle.close()

    @property
    def name(self) -> str:
        return self._name

    def getinfo(self, namespaces: Collection[str]) -> Info:
        info = {NS_BASIC: build_ns_basic(self.name, False)}
        if NS_DETAILS in namespaces:
            info[NS_DETAILS] = build_ns_details(
                ResourceType.file, self._handle.name, modified=self._modified
            )
        if NS_ESSENCE in namespaces:
            info["crc32"] = self._crc32
            info["storage_type"] = self._storage_type
        return Info(info)

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        if NS_DETAILS in info:
            self._modified = info[NS_DETAILS]["modified"]

        if NS_ESSENCE in info:
            self._crc32 = info[NS_ESSENCE].get("crc32", self._crc32)
            self._storage_type = info[NS_ESSENCE].get(
                "storage_type", self._storage_type
            )

    @contextmanager
    def openbin(self, mode: str) -> BinaryIO:
        _mode = Mode(mode)
        # TODO, Wrapper for 'mode' protections

        with self._lock:
            yield self._handle
            self._handle.seek(0)  # reset handle

    def verify_crc32(self, error: bool) -> bool:
        hasher = crc32()
        with self.openbin("r") as stream:
            expected = self._crc32
            if error:
                hasher.validate(stream, expected)
                return True
            else:
                return hasher.check(stream, expected)

    def recalculate_crc32(self):
        hasher = crc32(start=0)
        with self.openbin("r") as stream:
            self._crc32 = hasher.hash(stream)


class SgaFsFileV2(_SgaFsFileV2):
    def __init__(
        self,
        lazy: Optional[SgaFsFileV2Lazy] = None,
        mem: Optional[SgaFsFileV2Mem] = None,
    ):
        if lazy is not None and mem is not None:
            raise RelicToolError(
                "File trying to be created as both a lazy and in-memory file!"
            )
        elif lazy is None and mem is None:
            raise RelicToolError(
                "File trying to be created without specifying lazy/in-memory!"
            )

        self._is_lazy: bool = lazy is not None
        self._backing: _SgaFsFileV2 = lazy or mem  # type: ignore # at least one will not be None

    def close(self):
        return self._backing.close()

    def _convert_to_memfile(self):
        if not self._is_lazy:
            return
        self._is_lazy = False
        info = self.getinfo([NS_DETAILS, NS_ESSENCE])
        with self._backing.openbin("r") as data_src:
            self._backing = SgaFsFileV2Mem(
                name=info[NS_BASIC]["name"],
                storage_type=info[NS_ESSENCE]["storage_type"],
                data=data_src,
                modified=info[NS_DETAILS]["modified"],
                crc=info[NS_ESSENCE]["crc32"],
            )

    def getinfo(self, namespaces: Collection[str]) -> Info:
        return self._backing.getinfo(namespaces)

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        self._convert_to_memfile()
        self.setinfo(info)

    def openbin(self, mode: str) -> BinaryIO:
        _mode = Mode(mode)
        if _mode.writing:
            self._convert_to_memfile()
        return self._backing.openbin(mode)  # child instances handle context management

    def verify_crc32(self, error: bool) -> bool:
        return self._backing.verify_crc32(error)

    def recalculate_crc32(self):
        self._convert_to_memfile()
        self._backing.recalculate_crc32()


class _SgaFsFolderV2:
    @property
    def name(self):
        raise NotImplementedError

    @property
    def basename(self) -> str:
        return SgaPathResolver.basename(self.name)

    def getinfo(self, namespace: Collection[str]) -> Info:
        raise NotImplementedError

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        raise NotImplementedError

    def add_file(self, file: _SgaFsFileV2):
        raise NotImplementedError

    def add_folder(self, folder: _SgaFsFolderV2):
        raise NotImplementedError

    @property
    def folders(self) -> List[_SgaFsFolderV2]:
        raise NotImplementedError

    @property
    def files(self) -> List[_SgaFsFileV2]:
        raise NotImplementedError

    def scandir(self) -> Iterable[str]:
        raise NotImplementedError

    def get_child(self, name: str) -> Optional[Union[_SgaFsFileV2, _SgaFsFolderV2]]:
        raise NotImplementedError

    def remove_child(self, name: str):
        raise NotImplementedError

    def remove_file(self, name: str):
        raise NotImplementedError

    def remove_folder(self, name: str):
        raise NotImplementedError


class SgaFsFolderV2Mem(_SgaFsFolderV2):
    def get_child(self, name: str) -> Optional[Union[_SgaFsFileV2, _SgaFsFolderV2]]:
        return self._children.get(name)

    def __init__(self, name: str):
        self._name = name
        self._children: Dict[str, Union[_SgaFsFolderV2, _SgaFsFileV2]] = {}
        self._folders: Dict[str, _SgaFsFolderV2] = {}
        self._files: Dict[str, _SgaFsFileV2] = {}

    @property
    def name(self):
        return self._name

    def getinfo(self, namespace: Collection[str]) -> Info:
        return Info({NS_BASIC: build_ns_basic(self._name, True)})

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        raise RelicToolError("SGA Folder's have no settable information!")

    def _add_child(self, name: str, resource: Any, alt_lookup: Dict[str, Any]):
        if name in self._children:
            if name in self._files:
                raise fs.errors.FileExists(name)
            elif name in self._folders:
                raise fs.errors.DirectoryExists(name)
            else:
                raise fs.errors.ResourceError(
                    f"Child '{name}' ({str(resource)}) already exists ({str(alt_lookup[name])})!"
                )
        self._children[name] = resource
        alt_lookup[name] = resource

    def add_file(self, file: _SgaFsFileV2):
        self._add_child(file.name, file, self._files)

    def add_folder(self, folder: _SgaFsFolderV2):
        self._add_child(folder.name, folder, self._files)

    @property
    def folders(self) -> List[_SgaFsFolderV2]:
        return list(self._folders.values())

    @property
    def files(self) -> List[_SgaFsFileV2]:
        return list(self._files.values())

    def scandir(self) -> Iterable[str]:
        return list(self._children.keys())


class SgaFsFolderV2Lazy(_SgaFsFolderV2):
    def __init__(
        self,
        info: SgaTocFolder,
        name_window: SgaNameWindow,
        data_window: BinaryWindow,
        all_files: List[SgaFsFileV2],
        all_folders: List[SgaFsFolderV2],
    ):
        self._info = info
        self._name_window = name_window
        self._data_window = data_window
        self._all_files = all_files
        self._all_folders = all_folders
        self._filenames = None
        self._foldernames = None
        self._files = None
        self._folders = None

    def getinfo(self, namespace: Collection[str]) -> Info:
        return Info({NS_BASIC: build_ns_basic(self.name, True)})

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        pass

    def add_file(self, file: _SgaFsFileV2):
        raise RelicToolError(
            "Cannot add a file to a Lazy Folder! Was this not converted to a Mem-Folder?"
        )

    def add_folder(self, folder: _SgaFsFolderV2):
        raise RelicToolError(
            "Cannot add a folder to a Lazy Folder! Was this not converted to a Mem-Folder?"
        )

    def scandir(self) -> Iterable[str]:
        return list(*self._files.keys() * self._folders.keys())

    def get_child(self, name: str) -> Optional[Union[_SgaFsFileV2, _SgaFsFolderV2]]:
        if name in self._files_lookup:
            return self._files_lookup[name]
        elif name in self._folder_lookup:
            return self._folder_lookup[name]
        return None

    @property
    def name(self):
        return self._name_window.get_name(self._info.name_offset)

    @property
    def _files_lookup(self):
        if self._files is None:
            info = self._info
            sub_files = self._all_files[info.first_file : info.last_file]
            self._files = {f.name: f for f in sub_files}
        return self._files

    @property
    def _folder_lookup(self):
        if self._folders is None:
            info = self._info
            sub_folders = self._all_folders[info.first_folder : info.last_folder]
            self._folders = {f.name: f for f in sub_folders}
        return self._folders

    @property
    def files(self) -> List[SgaFsFileV2]:
        return list(self._files.values)

    @property
    def folders(self) -> List[SgaFsFolderV2]:
        return list(self._folders.values)


class SgaFsFolderV2(_SgaFsFolderV2):
    def __init__(
        self,
        lazy: Optional[SgaFsFolderV2Lazy] = None,
        mem: Optional[SgaFsFolderV2Mem] = None,
    ):
        if lazy is not None and mem is not None:
            raise RelicToolError(
                "Folder trying to be created as both a lazy and in-memory folder!"
            )
        elif lazy is None and mem is None:
            raise RelicToolError(
                "Folder trying to be created without specifying lazy/in-memory!"
            )

        self._is_lazy: bool = lazy is not None
        self._backing: _SgaFsFolderV2 = lazy or mem  # type: ignore # at least one will not be None

    def _convert_to_memfolder(self):
        if not self._is_lazy:
            return
        self._is_lazy = False
        root = self._backing = SgaFsFolderV2Mem(self._backing.name)
        # Migrate folder structure
        for folder in self._backing.folders:
            root.add_folder(folder)
        for file in self._backing.files:
            root.add_file(file)

    def getinfo(self, namespaces: Collection[str]) -> Info:
        return self._backing.getinfo(namespaces)

    def setinfo(self, info: Mapping[str, Mapping[str, object]]):
        self._convert_to_memfolder()
        self.setinfo(info)

    @property
    def name(self):
        return self._backing.name

    def add_file(self, file: _SgaFsFileV2):
        self._convert_to_memfolder()
        return self._backing.add_file(file)

    def add_folder(self, folder: _SgaFsFolderV2):
        self._convert_to_memfolder()
        return self._backing.add_folder(folder)

    @property
    def folders(self) -> List[_SgaFsFolderV2]:
        return self._backing.folders

    @property
    def files(self) -> List[_SgaFsFileV2]:
        return self._backing.files

    def scandir(self) -> Iterable[str]:
        return self._backing.scandir()

    def get_child(self, part):
        return self._backing.get_child(part)


class _SgaFsDriveV2:
    @property
    def name(self):
        raise NotImplementedError

    @property
    def alias(self):
        raise NotImplementedError

    @property
    def root(self) -> SgaFsFolderV2:
        raise NotImplementedError

    def getinfo(self, namespaces: Collection[str]) -> Info:
        raise NotImplementedError

    def setinfo(self, info: Mapping[Mapping[str, object]]):
        raise NotImplementedError


class SgaFsDriveV2Lazy(_SgaFsDriveV2):
    def __init__(
        self,
        info: SgaTocDrive,
        all_folders: List[SgaFsFolderV2],
    ):
        self._info = info
        self._all_folders = all_folders
        self._root = None

    @property
    def name(self):
        return self._info.name

    @property
    def alias(self):
        return self._info.alias

    @property
    def root(self) -> SgaFsFolderV2:
        if self._root is None:
            self._root = self._all_folders[self._info.root_folder]
        return self._root


class SgaFsDriveV2Mem(_SgaFsDriveV2):
    def __init__(self, name: str, alias: str):
        self._name = name
        self._alias = alias
        self._root = SgaFsFolderV2()

    @property
    def name(self):
        return self._name

    @property
    def alias(self):
        return self._alias

    @property
    def root(self) -> SgaFsFolderV2:
        return self._root


class SgaFsV2Packer:
    @classmethod
    def assemble(cls, filesystem: fs.base.FS, **settings) -> SgaFsV2:
        raise NotImplementedError

    @classmethod
    def serialize(cls, sga: SgaFsV2, handle: BinaryIO):
        raise NotImplementedError


class DriveExistsError(RelicToolError):
    ...


class SgaFsV2(FS):
    def __init__(self, handle: Optional[BinaryIO], parse_handle: bool = False):
        super().__init__()

        self._stream = handle
        self._file_md5: Optional[bytes] = None
        self._header_md5: Optional[bytes] = None
        self._drives: Dict[str, _SgaFsDriveV2] = {}
        self._lazy_file = None
        if parse_handle:
            if handle is None:
                raise RelicToolError("Cannot parse a null handle!")
            self._lazy_file = SgaFileV2(handle)
            self._load_lazy(self._lazy_file)
            self._file_md5 = self._lazy_file.meta.file_md5
            self._header_md5 = self._lazy_file.meta.header_md5

    def save(self, out: Optional[BinaryIO] = None):
        if self._stream is None and out is None:
            raise RelicToolError("Failed to save, out/handle not specified!")
        if out is None and self._lazy_file is not None:
            # we can't write to a lazily read file
            # we need to write to a temp-structure, then copy it over
            # this does make mem-fs only writing longer than neccessary
            #   But that also requires the archive to edit EVERY file, which probably doesn't happen often? I hope?
            with BytesIO() as temp:
                SgaFsV2Packer.serialize(self, temp)
                temp.seek(0)

    def getmeta(self, namespace="standard"):  # type: (Text) -> Mapping[Text, object]
        if namespace == NS_ESSENCE:
            return {
                "version": version,
                "file_md5": self._file_md5,
                "header_md5": self._header_md5,
            }
        else:
            return super().getmeta(namespace)

    def create_drive(self, name: str, alias: str) -> SgaFsDriveV2Mem:
        drive = SgaFsDriveV2Mem(name, alias)
        self.add_drive(drive)
        return drive

    def add_drive(self, drive: _SgaFsDriveV2):
        if drive.alias in self._drives:
            raise DriveExistsError(f"Drive Alias '{drive.alias}' already exists!")
        self._drives[drive.alias] = drive
        return drive

    def _load_lazy(self, file: SgaFileV2):
        toc = file.table_of_contents
        name_window = toc.names
        data_window = file.data_block

        files = [
            SgaFsFileV2(
                lazy=SgaFsFileV2Lazy(
                    file,
                    SgaTocFileDataV2Dow(file, name_window, data_window),
                )
            )
            for file in toc.files
        ]
        folders = []
        for folder in toc.folders:
            folders.append(
                SgaFsFolderV2(
                    lazy=SgaFsFolderV2Lazy(
                        folder, name_window, data_window, files, folders
                    )
                )
            )
        drives = [SgaFsDriveV2Lazy(drive_info, folders) for drive_info in toc.drives]
        for drive in drives:
            self.add_drive(drive)

    @property
    def drives(self) -> List[_SgaFsDriveV2]:
        return list(self._drives.values())

    @staticmethod
    def _getnode_from_drive(drive: _SgaFsDriveV2, path: str, exists: bool = False):
        current = drive.root

        for part in SgaPathResolver.split_parts(path):
            if current is None:
                raise fs.errors.ResourceNotFound(path)
            elif not current.getinfo("basic").get("basic", "is_dir"):
                raise fs.errors.DirectoryExpected(path)
            current = current.get_child(part)

        if exists and current is None:
            raise fs.errors.ResourceNotFound(path)

        return current

    def _getnode(
        self, path: str, exists: bool = False
    ) -> Optional[Union[_SgaFsFileV2, _SgaFsFolderV2]]:
        alias, _path = SgaPathResolver.parse(path)
        if alias is not None:
            if alias not in self._drives:
                raise fs.errors.ResourceNotFound(path)
            return self._getnode_from_drive(self._drives[alias], _path, exists=exists)
        else:
            for drive in self.drives:
                try:
                    return self._getnode_from_drive(drive, _path, exists=exists)
                except fs.errors.ResourceNotFound:
                    continue
            raise fs.errors.ResourceNotFound(path)

    def getinfo(self, path, namespaces=None):
        node = self._getnode(path, exists=True)
        return node.getinfo(namespaces)

    def listdir(self, path):
        node: _SgaFsFolderV2 = self._getnode(path, exists=True)
        if not node.getinfo("basic").get("basic", "is_dir"):
            raise fs.errors.DirectoryExpected(path)
        return node.scandir()

    def _get_parent_and_child(self, path: str) -> Tuple[_SgaFsFolderV2, str]:
        alias, _path = SgaPathResolver.parse(path)
        _parent, _child = SgaPathResolver.split(_path)
        parent_path = SgaPathResolver.build(alias, _parent)
        try:
            parent: _SgaFsFolderV2 = self._getnode(parent_path, exists=True)
        except fs.errors.ResourceNotFound as fnf_err:
            fnf_err.path = path  # inject path
            raise

        if not parent.getinfo("basic").get("basic", "is_dir"):
            raise fs.errors.ResourceNotFound(
                path
            )  # Resource not found; we want the child's error, not the dir's error

        return parent, _child

    def makedir(self, path, permissions=None, recreate=False):
        alias, _path = SgaPathResolver.parse(path)
        if alias is not None and _path == SgaPathResolver.ROOT:  # Make Drive
            try:
                self.create_drive("", alias)
            except DriveExistsError as exists_err:
                if not recreate:
                    raise fs.errors.DirectoryExists(path, exists_err)
        else:  # Make Folder
            parent, child_name = self._get_parent_and_child(path)

            try:
                parent.add_folder(SgaFsFolderV2Mem(child_name))
            except (
                fs.errors.DirectoryExists
            ) as dir_err:  # Ignore if recreate, otherwise inject path
                if not recreate:
                    dir_err.path = path
                    raise dir_err
            except fs.errors.FileExists as file_err:  # rethrow as a Dir Expected Error
                raise fs.errors.DirectoryExpected(path, file_err)
            except fs.errors.ResourceError as err:  # Inject path into this error
                err.path = path
                raise err

        return self.opendir(path)

    def makedirs(self, path, permissions=None, recreate=False):
        alias, _path = SgaPathResolver.parse(path)
        alias_path = SgaPathResolver.build(alias=alias)

        if alias is not None:
            if recreate:
                current = self.makedir(
                    alias_path, recreate=True
                )  # makedir instead of opendir
            else:
                current = self.opendir(alias_path)
        elif len(self._drives) == 1:
            current = self.opendir(
                SgaPathResolver.build(alias=list(self._drives.keys())[0])
            )
        elif len(self._drives) == 0:
            raise fs.errors.OperationFailed(
                path, msg="Filesystem contains no 'drives' to write to."
            )
        else:
            raise fs.errors.InvalidPath(
                path,
                "An alias must be specified when multiple 'drives' are present in the filesystem.",
            )

    def openbin(self, path, mode="r", buffering=-1, **options):
        node: _SgaFsFileV2 = self._getnode(path, exists=True)
        if node.getinfo("basic").get("basic", "is_dir"):
            raise fs.errors.FileExpected(path)
        return node.openbin(mode)

    def remove(self, path):
        _, path = SgaPathResolver.parse(path)
        if path == SgaPathResolver.ROOT:  # special case; removing root
            raise fs.errors.FileExpected(path)

        parent, child_name = self._get_parent_and_child(path)
        try:
            parent.remove_file(child_name)
        except fs.errors.ResourceNotFound as rnf_err:
            rnf_err.path = path
            raise
        except fs.errors.FileExpected as fe_err:
            fe_err.path = path
            raise

    def removedir(self, path):
        _, path = SgaPathResolver.parse(path)
        if path == SgaPathResolver.ROOT:  # special case; removing root
            raise fs.errors.RemoveRootError(path)

        parent, child_name = self._get_parent_and_child(path)
        try:
            parent.remove_folder(child_name)
        except fs.errors.ResourceNotFound as rnf_err:
            rnf_err.path = path
            raise
        except fs.errors.DirectoryExpected as de_err:
            de_err.path = path
            raise

    def setinfo(self, path, info):
        node = self._getnode(path, exists=True)
        node.setinfo(info)