from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import dataclass_transform
from urllib.parse import quote

from ._utils import CachedProperty, UrlGetter

__all__ = ['File', 'Folder']


# root 只有 Folder 或 Folder 的子项可以获得
# path 一定以 / 打头，并且不含 root
# name 为短名，均不携带 '/'
# 目录的根目录无 name ，设置为 '.'
# 如果解析自文件链接，则 path == '/' + name

@dataclass_transform(eq_default=False, kw_only_default=True, frozen_default=True)
def _entry_dataclass[T](cls: type[T]) -> type[T]:
    return dataclass(cls, eq=False, frozen=True, match_args=False, kw_only=True, slots=True)

@_entry_dataclass
class _Entry:
    token: str
    can_download: bool | None
    root: str | None

    name: str
    path: str
    size: int
    last_modified: datetime | None

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if type(other) is not type(self):
            return NotImplemented
        return self.token == other.token and self.path == other.path

    def __hash__(self) -> int:
        return hash((self.token, self.path))

@_entry_dataclass
class File(_Entry):
    _raw_path: str | None = field(init=False, repr=False)

    @CachedProperty(slotname='_raw_path', readonly=True)
    def raw_path(self) -> str | None:
        # 只有从目录直接产生的 File 才没有 raw_path
        if self.can_download:
            return f'https://cloud.tsinghua.edu.cn/d/{self.token}/files/?p={quote(self.path)}&dl=1'
        return None

    def get_raw_path(self, /, *, get: UrlGetter) -> str:
        from ._parser import _parse_file # 避免循环导入
        file = _parse_file(f'https://cloud.tsinghua.edu.cn/d/{self.token}/files/?p={quote(self.path)}', get=get)
        raw_path = file.raw_path
        assert raw_path is not None, '_parse_file should always create a File with raw_path'
        object.__setattr__(self, '_raw_path', raw_path)
        return raw_path

@_entry_dataclass
class Folder(_Entry):
    can_download: bool
    root: str

    dirents: tuple[File | Folder, ...] = field(repr=False, compare=False)

    def __iter__(self) -> Iterator[File | Folder]:
        return iter(self.dirents)

    def iter_files(self) -> Iterator[File]:
        for f in self:
            if isinstance(f, Folder):
                yield from f.iter_files()
            else:
                yield f
