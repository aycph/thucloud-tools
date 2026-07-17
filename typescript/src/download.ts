import { randomUUID } from 'crypto';
import { createWriteStream, existsSync } from 'fs';
import { link, open, rename, unlink } from 'fs/promises';
import { basename, dirname, join as pathJoin } from 'path';
import { pipeline } from 'stream/promises';


const renameNoReplace: typeof rename = async (oldPath, newPath) => {
    await link(oldPath, newPath);
    await unlink(oldPath);
};

function isFileExistsError(error: unknown): error is NodeJS.ErrnoException {
    return (
        error instanceof Error &&
        'code' in error &&
        error.code === 'EEXIST'
    );
}

const TEMP_SUFFIX = '.tmp';
const TMP_MAX = 20;

// Refactored from tempfile.py:_mkstemp_inner
async function mktemp_ofstream(path: string): Promise<{
    tempPath: string,
    stream: ReturnType<typeof createWriteStream>,
}> {
    const dir = dirname(path);
    const name = basename(path);
    for (let i = 0; i < TMP_MAX; ++i) {
        try {
            const tempPath = pathJoin(dir, `${randomUUID()}-${name}${TEMP_SUFFIX}`);
            const handle = await open(tempPath, 'wx', 0o600);
            return {
                tempPath,
                stream: handle.createWriteStream(),
            };
        } catch (error) {
            if (isFileExistsError(error)) continue;
            throw error;
        }
    }
    throw new Error('No usable temporary file name found');
}

function get_content_length(headers: Headers): number | null {
    if (headers.get('Transfer-Encoding'))
        return null;
    const encoding = headers.get('Content-Encoding');
    if (encoding && encoding.toLowerCase().trim() !== 'identity')
        return null;
    const value = headers.get('Content-Length');
    if (!value)
        return null;
    const length = Number(value);
    if (!Number.isSafeInteger(length) || length < 0) {
        console.warn(`Invalid Content-Length: ${value}`);
        return null;
    }
    return length;
}

export interface ProgressCallback {
    (event: 'start', downloaded: 0, total: number | null): void;
    (event: 'progress', downloaded: number, total: number | null): void;
    (event: 'end', downloaded: number, total: number | null): void;
}

export interface DownloadConfig {
    headers?: HeadersInit;
    overwrite?: boolean;
    callback?: ProgressCallback;
    signal?: AbortSignal;
}

export async function download(
    url: string,
    path: string,
    {
        headers,
        overwrite = true,
        callback,
        signal,
    }: DownloadConfig = {},
): Promise<void> {
    if (!overwrite && existsSync(path))
        throw new Error(`File already exists: ${path}`);

    const res = await fetch(url, { headers, signal });
    if (!res.ok)
        throw res;
    const body = res.body;
    if (body === null)
        throw new Error(`Empty body: ${url}`);

    const total = get_content_length(res.headers);
    let downloaded = 0;
    callback?.('start', 0, total);

    const { tempPath, stream } = await mktemp_ofstream(path);

    if (callback === undefined) {
        await pipeline(body, stream, { signal });
    } else {
        const transform = async function* (source: NonNullable<typeof body>) {
            for await (const chunk of source) {
                yield chunk;
                callback('progress', downloaded += chunk.length, total);
            }
        };
        await pipeline(body, transform, stream, { signal });
    }

    if (overwrite) {
        await rename(tempPath, path);
    } else {
        try {
            await renameNoReplace(tempPath, path);
        } catch (error) {
            if (isFileExistsError(error)) {
                await renameNoReplace(tempPath, tempPath.slice(0, -TEMP_SUFFIX.length));
                throw new Error(`File already exists: ${path}`);
            } else {
                throw error;
            }
        }
    }

    callback?.('end', downloaded, total);
}
