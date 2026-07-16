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
        else
            return this._raw_path = null;
    }

    async get_raw_path(): Promise<string> {
        const file = await _parse_file(`https://cloud.tsinghua.edu.cn/d/${this.token}/files/?p=${encodeURIComponent(this.path)}`);
        const raw_path = file.raw_path;
        if (raw_path === undefined || raw_path === null)
            throw Error('Parsed file did not provide a raw URL')
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
        for (let f of _dirents.values()) {
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
        for (let f of this) {
            if (f instanceof CloudFolder)
                yield* f.iter_files();
            else
                yield f;
        }
    }

    *iter_folders(): IterableIterator<CloudFolder> {
        for (let f of this) {
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


export async function parse(url: string): Promise<CloudEntry> {
    const parsed = new URL(url);
    if (parsed.host !== 'cloud.tsinghua.edu.cn')
        throw new Error(`Invalid host: ${parsed.host}`);
    const paths = _strip(parsed.pathname, '/').split('/');
    if (paths[1] === undefined || !/^[0-9a-f]{20}$/.test(paths[1]))
        throw Error(`Unrecognized url: ${url}`);
    if (paths[0] === 'd') {
        if (paths.length >= 3) {
            if (paths.length > 3 || paths[2] !== 'files')
                throw Error(`Unrecognized url: ${url}`);
            return _parse_file(url);
        } else {
            return _parse_folder(url);
        }
    } else if (paths[0] === 'f') {
        return _parse_file(url);
    } else {
        throw Error(`Unrecognized url: ${url}`);
    }
}

function _strip(str: string, chars: string): string {
    let start = 0, end = str.length - 1;
    while (start <= end && chars.includes(str.charAt(start))) ++start;
    while (end >= start && chars.includes(str.charAt(end))) --end;
    return str.slice(start, end + 1);
}

type _PageOptions = {
    filePath: string,
    sharedToken: string,
    fileName: string,
    fileSize: number,
    rawPath: string,
    canDownload: boolean,
} | {
    dirName: string,
    relativePath: string,
    token: string,
    canDownload: boolean,
};

function _extract_page_options(html: string): _PageOptions | null {
    const str = html.match(/<script type="text\/javascript">\s*window\.shared = ([\s\S]*?);\s*<\/script>/)?.[1];
    if (str === undefined)
        return null;
    return eval(`(${str})`)['pageOptions'];
}

async function _parse_file(url: string): Promise<CloudFile> {
    const html = await fetch(url).then(res => res.text());
    const info = _extract_page_options(html);
    if (info === null) {
        const token = url.match(/\/([0-9a-f]{20})\//)?.[1];
        if (token === undefined)
            throw Error(`Unrecognized url: ${url}`);
        const path = new URL(url).searchParams.get('p');
        return await _parse_wopi_file(html, token, path);
    }
    if ('dirName' in info)
        throw Error('Got folder page when parsing file link');
    return new CloudFile(
        info['sharedToken'],
        info['filePath'],
        info['fileName'],
        info['fileSize'],
        null,
        null,
        info['canDownload'],
        info['rawPath']
    );
}

type _WOPIInfo = {
    BaseFileName: string,
    Size: number,
    LastModifiedTime: string,
};

async function _parse_wopi_file(html: string, token: string, path: string | null): Promise<CloudFile> {
    const action = html.match(/<form id="office_form" name="office_form" target="office_frame" action="(.*?)" method="post">/)?.[1];
    if (action === undefined)
        throw Error('Unexpected html: office_form not found');
    const wopi = new URL(action.replaceAll('&amp;', '&')).searchParams.get('WOPISrc');
    if (wopi === null)
        throw Error('Unexpected html: WOPISrc not found');
    const access_token = html.match(/<input name="access_token" value="([0-9a-f]{32})" type="hidden"\/>/)?.[1];
    if (access_token === undefined)
        throw Error('Unexpected html: access_token not found');

    const info_url = `${wopi}?access_token=${access_token}`;
    const raw_path = `${wopi}/contents?access_token=${access_token}`;
    const info = await fetch(info_url).then(res => res.json()) as _WOPIInfo;
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

async function _parse_folder(url: string): Promise<CloudFolder> {
    const html = await fetch(url).then(res => res.text());
    const info = _extract_page_options(html);
    if (info === null)
        throw Error(`Unrecognized HTML: ${url}`);
    if ('fileName' in info)
        throw Error('Got file page when parsing folder link');

    const token = info.token;
    const can_download = info.canDownload;
    const root = info.dirName;
    const path = info.relativePath;
    const name = _strip(path, '/').split('/').pop() || root;
    const dirents = await _get_dirents(path, token, can_download, root);
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
    folder_name: string,
    folder_path: string,
    is_dir: true,
    last_modified: string,
    size: 0,
} | {
    file_name: string,
    file_path: string,
    is_dir: false,
    last_modified: string,
    size: number,
};

async function _get_dirents(path: string, token: string, can_download: boolean, root: string): Promise<ReadonlyMap<string, CloudEntry>> {
    async function parse_item(item: Dirent): Promise<CloudEntry> {
        if (item.is_dir) {
            const path = item.folder_path;
            const dirents = await _get_dirents(path, token, can_download, root);
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
    const _dirent_list = (await fetch(api).then(res => res.json()))['dirent_list'] as Dirent[];
    const dirent_list = await Promise.all(_dirent_list.map(parse_item));
    return new Map(dirent_list.map(f => [f.name, f]));
}
