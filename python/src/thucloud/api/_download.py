import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from typing import Literal, NamedTuple, Protocol

import requests

from ._entries import File, Folder
from ._utils import (
    DEFAULT_CHUNK_SIZE, DEFAULT_TIMEOUT,
    download as download_url, sanitize_filename,
)

__all__ = [
    'DownloadSummary',
    'ProgressEvent',
    'ProgressCallback',
    'download',
]


class DownloadSummary(NamedTuple):
    target: Path

    files_total: int
    bytes_total: int

    files_downloaded: int
    files_skipped: int
    files_overwritten: int
    bytes_downloaded: int

    elapsed: timedelta

type ProgressEvent = Literal['start', 'progress', 'end', 'skip']

class ProgressCallback(Protocol):
    def __call__(
        self,
        root_entry: File | Folder,
        file: File,
        target: Path,
        event: ProgressEvent,
        downloaded: int,
        /,
    ) -> None: ...
    # write: Callable[[str], None]

def download(
    entry: File | Folder,
    output_dir: str | os.PathLike[str],
    /,
    *,
    workers: int = 4,
    if_exists: Literal['error', 'overwrite', 'skip'] = 'skip',
    filename_sanitizer: Callable[[str], str] = sanitize_filename,
    timeout: float | tuple[float, float] | None = DEFAULT_TIMEOUT,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    callback: ProgressCallback | None = None,
) -> DownloadSummary:
    """将文件或文件夹下载到本地目录

    `workers` 用于指定线程池使用的最大线程数，但仅在下载目录时才会使用线程池并发下载文件

    `if_exists` 设置为 `'error'` 时会在目标文件已存在时抛出 `FileExistsError`

    `callback` 在下载文件夹时可能会从多个工作线程被并发调用，线程安全性需调用方自行保证

    下载文件夹时，若某个下载任务失败或下载过程被中断，会尝试取消尚未开始运行的任务，
    并通过 callback.write 输出提示信息（如果 callback 提供了该方法）。
    随后会等待已经开始运行的任务结束；在等待期间再次收到 KeyboardInterrupt 等异常时，
    会停止等待并继续向外抛出该异常。
    """
    sessions: dict[threading.Thread, requests.Session] = {}

    lock = threading.Lock()

    files_total = 1 if isinstance(entry, File) else entry.file_count
    bytes_total = entry.size
    files_downloaded = 0
    files_skipped = 0
    files_overwritten = 0
    bytes_downloaded = 0
    t0 = time.perf_counter()

    sanitized_paths: dict[Path, File | Folder] = {}
    def reserve_sanitized_path(path: Path, entry: File | Folder):
        with lock:
            if path in sanitized_paths:
                entry0 = sanitized_paths[path]
                if entry0 != entry:
                    raise FileExistsError(
                        f'Sanitized filename collision: '
                        f'{entry!r} conflicts with {entry0!r} as {path}'
                    )
            else:
                sanitized_paths[path] = entry

    def dl(file: File, output_dir: str | os.PathLike[str]) -> Path:
        nonlocal files_skipped

        target = None
        try:
            target = Path(output_dir, filename_sanitizer(file.name))
            reserve_sanitized_path(target, file)
            existed = target.exists()
            if existed:
                if not target.is_file():
                    raise FileExistsError(f'Target exists but is not a file: {target}')
                if if_exists == 'error':
                    raise FileExistsError(f'File already exists: {target}')
                if if_exists == 'skip':
                    with lock:
                        files_skipped += 1
                    if callback is not None:
                        callback(entry, file, target, 'skip', 0)
                    return target
            session = sessions.get(threading.current_thread(), None)
            url = file.raw_path
            if url is None:
                url = file.get_raw_path(get=requests.get if session is None else session.get)
            overwrite = if_exists == 'overwrite'
            def dl_callback(event: Literal['start', 'progress', 'end'], downloaded: int, total: int | None):
                nonlocal files_downloaded, files_overwritten, bytes_downloaded
                if event == 'end':
                    with lock:
                        files_downloaded += 1
                        bytes_downloaded += downloaded
                        if existed:
                            files_overwritten += 1
                if callback is not None:
                    callback(entry, file, target, event, downloaded)
            return download_url(
                url,
                target,
                headers=None,
                session=session,
                timeout=timeout,
                chunk_size=chunk_size,
                overwrite=overwrite,
                callback=dl_callback,
            )
        except BaseException as exc:
            if target is None:
                exc.add_note(f'while preparing to download {file!r}')
            else:
                exc.add_note(f'while downloading {file!r} to {target}')
            raise

    if isinstance(entry, File):
        os.makedirs(output_dir, exist_ok=True)
        target = dl(entry, output_dir)
    else:
        def initializer():
            sessions[threading.current_thread()] = requests.Session()
        executor = ThreadPoolExecutor(max_workers=workers, initializer=initializer)
        futures: set[Future[Path]] = set()

        def dl_folder(folder: Folder, output_dir: str | os.PathLike[str]) -> Path:
            target = Path(output_dir, filename_sanitizer(folder.name))
            reserve_sanitized_path(target, folder)
            target.mkdir(parents=True, exist_ok=True)
            for f in folder:
                if isinstance(f, File):
                    futures.add(executor.submit(dl, f, target))
                else:
                    dl_folder(f, target)
            return target

        try:
            target = dl_folder(entry, output_dir)
            for future in as_completed(futures):
                future.result()
        except BaseException as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            if (write := getattr(callback, 'write', None)) is not None:
                try:
                    write(
                        f'Download interrupted by {exc!r}.\n'
                        'Pending downloads have been cancelled.\n'
                        'Waiting for running downloads to finish; press Ctrl-C again to stop waiting.\n'
                    )
                except Exception as write_exc:
                    exc.add_note(f'Failed to write interruption message: {write_exc!r}')
            raise
        finally:
            executor.shutdown()
            for session in sessions.values():
                session.close()
            sessions.clear()

    elapsed_seconds = time.perf_counter() - t0
    return DownloadSummary(
        target=target,
        files_total=files_total,
        bytes_total=bytes_total,
        files_downloaded=files_downloaded,
        files_skipped=files_skipped,
        files_overwritten=files_overwritten,
        bytes_downloaded=bytes_downloaded,
        elapsed=timedelta(seconds=elapsed_seconds),
    )
