import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, stat, utimes } from 'node:fs/promises';
import { join as pathJoin } from 'node:path';
import { performance } from 'node:perf_hooks';

import { type ProgressCallback as _ProgressCallback, download as _download } from './download.js';
import { type Executor, PromisePoolExecutor, inlineExecutor } from './executor.js';


interface _CloudEntry {
    readonly token: string;
    readonly path: string;

    readonly name: string;
    readonly size: number;
    readonly last_modified: Date | null;
    readonly root: string | null;
    readonly can_download: boolean | null;
}

export type CloudEntry = CloudFile | CloudFolder;

export class CloudFile implements _CloudEntry {
    constructor(
        readonly token: string,
        readonly path: string,

        readonly name: string,
        readonly size: number,
        readonly last_modified: Date | null,
        readonly root: string | null,
        readonly can_download: boolean | null,

        protected _raw_path: string | null | undefined,
    ) {}

    get raw_path(): string | null {
        if (this._raw_path !== undefined)
            return this._raw_path;
        if (this.can_download)
            return this._raw_path = `https://cloud.tsinghua.edu.cn/d/${this.token}/files/?p=${encodeURIComponent(this.path)}&dl=1`;
        return this._raw_path = null;
    }

    async get_raw_path(exec: Executor = inlineExecutor): Promise<string> {
        const file = await _parse_file(`https://cloud.tsinghua.edu.cn/d/${this.token}/files/?p=${encodeURIComponent(this.path)}`, exec);
        const raw_path = file.raw_path;
        assert(raw_path != null, 'Parsed file did not provide a raw URL');
        return this._raw_path = raw_path;
    }
}

export class CloudFolder implements _CloudEntry {
    readonly file_count: number;
    readonly folder_count: number;

    constructor(
        readonly token: string,
        readonly path: string,

        readonly name: string,
        readonly size: number,
        readonly last_modified: Date | null,
        readonly root: string,
        readonly can_download: boolean,

        protected readonly _dirents: ReadonlyMap<string, CloudEntry>,
    ) {
        let file_count = 0;
        let folder_count = 0;
        for (const f of _dirents.values()) {
            if (f instanceof CloudFolder) {
                file_count += f.file_count;
                folder_count += f.folder_count + 1;
            } else {
                file_count += 1;
            }
        }
        this.file_count = file_count;
        this.folder_count = folder_count;
    }

    [Symbol.iterator](): IterableIterator<CloudEntry> {
        return this._dirents.values();
    }

    get length(): number {
        return this._dirents.size;
    }

    *iter_files(): IterableIterator<CloudFile> {
        for (const f of this) {
            if (f instanceof CloudFolder)
                yield* f.iter_files();
            else
                yield f;
        }
    }

    *iter_folders(): IterableIterator<CloudFolder> {
        for (const f of this) {
            if (f instanceof CloudFolder) {
                yield f;
                yield* f.iter_folders();
            }
        }
    }

    get(name: string): CloudEntry | undefined {
        return this._dirents.get(name);
    }

    has(name: string): boolean {
        return this._dirents.has(name);
    }
}


export async function parse(url: string, max_workers: number | null = 10): Promise<CloudEntry> {
    const exec = max_workers === null ? inlineExecutor : new PromisePoolExecutor(max_workers);
    const parsed = new URL(url);
    if (parsed.host !== 'cloud.tsinghua.edu.cn')
        throw new Error(`Invalid host: ${JSON.stringify(parsed.host)}`);
    const paths = _strip(parsed.pathname, '/').split('/');
    if (paths[1] === undefined || !/^[0-9a-f]{20}$/.test(paths[1]))
        throw new Error(`Unrecognized URL: ${JSON.stringify(url)}`);
    if (paths[0] === 'd') {
        if (paths.length >= 3) {
            if (paths.length > 3 || paths[2] !== 'files')
                throw new Error(`Unrecognized URL: ${JSON.stringify(url)}`);
            return _parse_file(url, exec);
        } else {
            return _parse_folder(url, exec);
        }
    } else if (paths[0] === 'f') {
        return _parse_file(url, exec);
    } else {
        throw new Error(`Unrecognized URL: ${JSON.stringify(url)}`);
    }
}

function _strip(str: string, chars: string): string {
    let start = 0, end = str.length - 1;
    while (start <= end && chars.includes(str.charAt(start))) ++start;
    while (end >= start && chars.includes(str.charAt(end))) --end;
    return str.slice(start, end + 1);
}

async function fetch_json<O>(url: string): Promise<O> {
    const res = await fetch(url);
    if (!res.ok)
        throw new Error(`HTTP ${res.status} ${res.statusText}: ${JSON.stringify(url)}`);
    return await res.json() as O;
}

async function fetch_text(url: string): Promise<string> {
    const res = await fetch(url);
    if (!res.ok)
        throw new Error(`HTTP ${res.status} ${res.statusText}: ${JSON.stringify(url)}`);
    return await res.text();
}

type _PageOptions = {
    filePath: string;
    sharedToken: string;
    fileName: string;
    fileSize: number;
    rawPath: string;
    canDownload: boolean;
} | {
    dirName: string;
    relativePath: string;
    token: string;
    canDownload: boolean;
};

function _extract_page_options(html: string): _PageOptions | null {
    const str = html.match(/<script type="text\/javascript">\s*window\.shared = ([\s\S]*?);\s*<\/script>/)?.[1];
    if (str === undefined)
        return null;
    return eval(`(${str})`)['pageOptions'];
}

async function _parse_file(url: string, exec: Executor): Promise<CloudFile> {
    const html = await exec.submit(fetch_text, url);
    const info = _extract_page_options(html);
    if (info === null) {
        const token = url.match(/\/([0-9a-f]{20})\//)?.[1];
        if (token === undefined)
            throw new Error(`Unrecognized URL: ${JSON.stringify(url)}`);
        const path = new URL(url).searchParams.get('p');
        return await _parse_wopi_file(html, token, path, exec);
    }
    if (!('fileName' in info))
        throw new Error(`Unrecognized HTML: ${JSON.stringify(url)}`);
    return new CloudFile(
        info.sharedToken,
        info.filePath,
        info.fileName,
        info.fileSize,
        null,
        null,
        info.canDownload,
        info.rawPath,
    );
}

type WOPIInfo = {
    BaseFileName: string;
    Size: number;
    LastModifiedTime: string;
};

async function _parse_wopi_file(
    html: string,
    token: string,
    path: string | null,
    exec: Executor,
): Promise<CloudFile> {
    const action = html.match(/<form id="office_form" name="office_form" target="office_frame" action="(.*?)" method="post">/)?.[1];
    if (action === undefined)
        throw new Error('Unexpected HTML: office_form not found');
    const wopi = new URL(action.replaceAll('&amp;', '&')).searchParams.get('WOPISrc');
    if (wopi === null)
        throw new Error('Unexpected HTML: WOPISrc not found');
    const access_token = html.match(/<input name="access_token" value="([0-9a-f]{32})" type="hidden"\/>/)?.[1];
    if (access_token === undefined)
        throw new Error('Unexpected HTML: access_token not found');

    const info_url = `${wopi}?access_token=${access_token}`;
    const raw_path = `${wopi}/contents?access_token=${access_token}`;
    const info = await exec.submit(fetch_json<WOPIInfo>, info_url);
    return new CloudFile(
        token,
        path ?? '/' + info.BaseFileName,
        info.BaseFileName,
        info.Size,
        new Date(info.LastModifiedTime),
        null,
        null,
        raw_path,
    );
}

async function _parse_folder(url: string, exec: Executor): Promise<CloudFolder> {
    const html = await exec.submit(fetch_text, url);
    const info = _extract_page_options(html);
    if (info === null || !('dirName' in info))
        throw new Error(`Unrecognized HTML: ${JSON.stringify(url)}`);

    const token = info.token;
    const can_download = info.canDownload;
    const root = info.dirName;
    const path = info.relativePath;
    const name = _strip(path, '/').split('/').pop() || root;
    const dirents = await _get_dirents(path, token, can_download, root, exec);
    const size = dirents.values().reduce((s, f) => s + f.size, 0);
    return new CloudFolder(
        token,
        path,
        name,
        size,
        null,
        root,
        can_download,
        dirents,
    );
}

type Dirent = {
    folder_name: string;
    folder_path: string;
    is_dir: true;
    last_modified: string;
    size: 0;
} | {
    file_name: string;
    file_path: string;
    is_dir: false;
    last_modified: string;
    size: number;
};

async function _get_dirents(
    path: string,
    token: string,
    can_download: boolean,
    root: string,
    exec: Executor,
): Promise<ReadonlyMap<string, CloudEntry>> {
    async function parse_item(item: Dirent): Promise<CloudEntry> {
        if (item.is_dir) {
            const path = item.folder_path;
            const dirents = await _get_dirents(path, token, can_download, root, exec);
            const size = dirents.values().reduce((s, f) => s + f.size, 0);
            return new CloudFolder(
                token,
                path,
                item.folder_name,
                size,
                new Date(item.last_modified),
                root,
                can_download,
                dirents,
            );
        } else {
            return new CloudFile(
                token,
                item.file_path,
                item.file_name,
                item.size,
                new Date(item.last_modified),
                root,
                can_download,
                undefined,
            );
        }
    }

    const api = `https://cloud.tsinghua.edu.cn/api/v2.1/share-links/${token}/dirents/?path=${encodeURIComponent(path)}`;
    const _dirent_list = (await exec.submit(fetch_json<{ dirent_list: Dirent[] }>, api)).dirent_list;
    const dirent_list = await Promise.all(_dirent_list.map(parse_item));
    return new Map(dirent_list.map(f => [f.name, f]));
}


const RESERVED_NAME_RE = /^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$/iu;

const RESERVED_CHAR_RE = /[\x00-\x1f"*:<>\?|\/\\]/g;

function sanitize_filename(name: string): string {
    const name0 = name;
    if (typeof name !== 'string')
        throw new TypeError(`Invalid name: expected a string, got ${typeof name}`);
    if (name.includes('/') || name.includes('\\'))
        throw new Error(`Invalid name: cannot contain slash or backslash: ${JSON.stringify(name0)}`);
    name = name.replace(/[ .]+$/u, '');
    if (name === '')
        throw new Error(`Filename is empty after sanitization: ${JSON.stringify(name0)}`);
    const dot = name.indexOf('.');
    const stem = (dot === -1 ? name : name.slice(0, dot)).replace(/ +$/u, '');
    if (RESERVED_NAME_RE.test(stem))
        name = `_${name}`;
    return name.replace(RESERVED_CHAR_RE, '_');
}

export type IfExists = 'error' | 'overwrite' | 'skip';

export type MTimeMode = 'off' | 'reported' | 'derived';

export type ProgressEvent = 'start' | 'progress' | 'end' | 'skip';

export interface ProgressCallback {
    (root_entry: CloudEntry, file: CloudFile, target: string, event: ProgressEvent, downloaded: number): void;
    write?: (text: string) => void;
}

export interface DownloadConfig {
    workers?: number;
    if_exists?: IfExists;
    filename_sanitizer?: (filename: string) => string;
    mtime_mode?: MTimeMode;
    callback?: ProgressCallback;
    signal?: AbortSignal;
}

export type DownloadEntryTarget<Entry extends CloudEntry> = {
    entry: Entry;
    target: string;
};

export interface DownloadSummary {
    target: string;
    files_total: number;
    bytes_total: number;
    files_downloaded: number;
    bytes_downloaded: number;
    elapsed_ms: number;
    renamed: DownloadEntryTarget<CloudEntry>[];
    skipped: DownloadEntryTarget<CloudFile>[];
    overwritten: DownloadEntryTarget<CloudFile>[];
}

export async function download(
    entry: CloudEntry,
    output_dir: string = '.',
    {
        workers = 4,
        if_exists = 'skip',
        filename_sanitizer = sanitize_filename,
        mtime_mode = 'derived',
        callback,
        signal,
    }: DownloadConfig = {},
): Promise<DownloadSummary> {
    if (!Number.isInteger(workers) || workers <= 0)
        throw new RangeError(`Invalid workers: ${workers}`);
    if (!['error', 'overwrite', 'skip'].includes(if_exists))
        throw new RangeError(`Invalid if_exists: ${JSON.stringify(if_exists)}`);
    if (!['off', 'reported', 'derived'].includes(mtime_mode))
        throw new RangeError(`Invalid mtime_mode: ${JSON.stringify(mtime_mode)}`);

    const write = callback?.write?.bind(callback);

    const files_total = entry instanceof CloudFile ? 1 : entry.file_count;
    const bytes_total = entry.size;
    let files_downloaded = 0;
    let bytes_downloaded = 0;
    const t0 = performance.now();
    const renamed: DownloadEntryTarget<CloudEntry>[] = [];
    const skipped: DownloadEntryTarget<CloudFile>[] = [];
    const overwritten: DownloadEntryTarget<CloudFile>[] = [];

    const sanitized_paths = new Map<string, CloudEntry>();
    function reserve_sanitized_path(path: string, entry: CloudEntry) {
        const entry0 = sanitized_paths.get(path);
        if (entry0 === undefined) {
            sanitized_paths.set(path, entry);
        } else {
            if (entry0 !== entry)
                throw new Error(
                    'Sanitized filename collision: ' +
                    `${JSON.stringify(entry)} conflicts with ` +
                    `${JSON.stringify(entry0)} at ${JSON.stringify(path)}`,
                );
        }
    }

    async function dl(file: CloudFile, output_dir: string): Promise<string> {
        const target_name = filename_sanitizer(file.name);
        const target = pathJoin(output_dir, target_name);
        reserve_sanitized_path(target, file);
        if (target_name !== file.name) {
            renamed.push({ entry: file, target });
            write?.(`Renamed: ${JSON.stringify(target)} (from ${JSON.stringify(file.name)})`);
        }
        if (existsSync(target)) {
            if (!(await stat(target)).isFile())
                throw new Error(`Target exists but is not a file: ${JSON.stringify(target)}`);
            if (if_exists === 'error')
                throw new Error(`File already exists: ${JSON.stringify(target)}`);
            if (if_exists === 'skip') {
                skipped.push({ entry: file, target });
                write?.(`Skipped: ${JSON.stringify(target)}`);
                callback?.(entry, file, target, 'skip', 0);
                return target;
            } else if (if_exists === 'overwrite') {
                overwritten.push({ entry: file, target });
                write?.(`Overwriting: ${JSON.stringify(target)}`);
            } else {
                if_exists satisfies never;
                throw new Error(`Unknown if_exists: ${JSON.stringify(if_exists)}`);
            }
        }
        const url = file.raw_path ?? await file.get_raw_path();
        const overwrite = if_exists === 'overwrite';
        const dl_callback: _ProgressCallback = (event, downloaded, total) => {
            if (event === 'end') {
                files_downloaded += 1;
                bytes_downloaded += downloaded;
            }
            callback?.(entry, file, target, event, downloaded);
        };
        await _download(url, target, {
            overwrite,
            callback: dl_callback,
            signal,
        });
        return target;
    }

    let target: string;
    await mkdir(output_dir, { recursive: true });
    if (entry instanceof CloudFile) {
        target = await dl(entry, output_dir);
    } else if (entry instanceof CloudFolder) {
        const executor = new PromisePoolExecutor(workers);
        async function dl_folder(folder: CloudFolder, output_dir: string): Promise<string> {
            const target_name = filename_sanitizer(folder.name);
            const target = pathJoin(output_dir, target_name);
            reserve_sanitized_path(target, folder);
            if (target_name !== folder.name) {
                renamed.push({ entry: folder, target });
                write?.(`Renamed: ${JSON.stringify(target)} (from ${JSON.stringify(folder.name)})`);
            }
            await mkdir(target, { recursive: true });
            await Promise.all(Iterator.from(folder).map(f => {
                if (f instanceof CloudFile) {
                    return executor.submit(dl, f, target);
                } else {
                    return dl_folder(f, target);
                }
            }));
            return target;
        }
        try {
            target = await dl_folder(entry, output_dir);
        } catch (error) {
            void executor.shutdown(true);
            const errMessage = error instanceof Error ? error.message : String(error);
            write?.(
                `Download interrupted: ${errMessage}\n` +
                'Pending downloads have been cancelled.\n' +
                'Waiting for running downloads to finish.\n',
            );
            throw error;
        } finally {
            await executor.shutdown();
        }
    } else {
        entry satisfies never;
        throw new Error(`Unknown entry type: ${JSON.stringify(entry)}`);
    }

    if (mtime_mode !== 'off') {
        write?.('Restoring modification times...');
        const cache = new Map<CloudEntry, Date | null>();
        function get_mtime(entry: CloudEntry): Date | null {
            if (entry.last_modified !== null)
                return entry.last_modified;
            let mtime = cache.get(entry);
            if (mtime !== undefined)
                return mtime;
            mtime = null;
            if (mtime_mode === 'derived' && entry instanceof CloudFolder) {
                for (const f of entry) {
                    const mtime1 = get_mtime(f);
                    if (mtime1 !== null && (mtime === null || mtime < mtime1))
                        mtime = mtime1;
                }
            }
            cache.set(entry, mtime);
            return mtime;
        }

        await Promise.all(Iterator.from(sanitized_paths).map(async ([target, entry]) => {
            const mtime = get_mtime(entry);
            if (mtime !== null) {
                const atime = (await stat(target)).atime;
                await utimes(target, atime, mtime);
            }
        }));
    }

    const t1 = performance.now();
    return {
        target,
        files_total, bytes_total,
        files_downloaded, bytes_downloaded,
        elapsed_ms: t1 - t0,
        renamed, skipped, overwritten,
    };
}
