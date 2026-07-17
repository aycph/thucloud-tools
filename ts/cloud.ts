import { get as httpsGet } from 'https';
import * as fs from 'fs';

import { Executor, Callbacks, LimitPromise } from './PromiseLimit'

// 错误 url 的处理
// 对大文件传输不稳定，不鲁棒
// 对已存在文件的大小校验

export function httpsRequest(url: string): Promise<string> {
	return new Promise<string>(resolve => {
		let content = '';
		let started = false;
		httpsGet(url, res => {
			res.on('data', data => {
				if (started === false) {
					console.log('getting', decodeURIComponent(url));
					started = true;
				}
				content += data
			});
			res.on('end', () => resolve(content))
		});
	});
}

function urlretrieve(url: string, path: string, callback?: (err?: NodeJS.ErrnoException | null) => void): Promise<void> {
	return new Promise<void>(resolve => {
		const file = fs.createWriteStream(path);
		httpsGet(url, res => {
			res.pipe(file);
			file.on('finish', () => {
				file.close(callback);
				resolve();
			});
		});
	});
}

function size2str(size: number): string {
	if (size < 1024) return `${size.toFixed(2)} B`;
	else if ((size /= 1024) < 1024) return `${size.toFixed(2)} KB`;
	else if ((size /= 1024) < 1024) return `${size.toFixed(2)} MB`;
	else return `${(size /= 1024).toFixed(2)} GB`;
}

function sumNumber(array: number[]): number {
	return array.reduce((prev, curr) => prev + curr, 0);
}

type DirConfig = {
	dirName: string,
	dirPath: string,
	relativePath: string, // ? what's the format?
	token: string,
	mode: 'list',
	canDownload: boolean, // assert to be true?
}

type FileConfig = {
	fileName: string,
	filePath: string,
	fileSize: number,
	rawPath: string,
	canDownload: boolean, // assert to be true?
}

type FolderInfo = {
	size: number,
	last_modified: string, // could be parsed by new Date
	is_dir: true,
	folder_path: string, // relative path started with '/'
	folder_name: string
};

type FileInfo = {
	size: number,
	last_modified: string, // could be parsed by new Date
	is_dir: false,
	file_path: string, // relative path started and ended with '/'
	file_name: string,
	encoded_thumbnail_src: string,
};

type DirentItem = FileInfo | FolderInfo;

type Dirent = {
	dirent_list: DirentItem[],
};

const INFO_REGEXP = /<script type="text\/javascript">\s*?window\.shared\s*?=([\s\S]*?);\s*?<\/script>/;

type Result = {
	name: string,
	path: string,
	isdir: boolean,
	size: number,
	existed: boolean
};

export interface BaseFile {
	readonly name: string;
	readonly path: string;
	readonly isdir: boolean;
	readonly size: Promise<number>;
	download(path: string, group: LimitPromise<Result, [Result]>): Promise<Result>;
	// if group is provided, this promise would be return almost immediately
};

export class File implements BaseFile {
	readonly name: string;
	readonly path: string;
	readonly isdir: false;
	readonly size: Promise<number>;
	readonly rawPath: string;
	constructor(protected config: FileConfig) {
		this.name = config.fileName;
		this.path = config.filePath;
		this.isdir = false;
		this.size = Promise.resolve(config.fileSize);
		this.rawPath = config.rawPath;
	}
	async download(path?: string, group?: LimitPromise<Result, [Result]>): Promise<Result> {
		if (!this.config.canDownload) throw 'Cannot be download?';
		if (path === undefined) path = '.';
		const filePath = path + this.name;
		const size = await this.size;
		const result: Result = {
			name: this.name,
			path: filePath,
			isdir: false,
			size: size,
			existed: fs.existsSync(filePath) && fs.statSync(filePath).size === size
		};
		const exe: Executor<Result> = resolve => {
			if (result.existed) resolve(result);
			else {
				urlretrieve(this.rawPath, filePath).then(() => resolve(result));
			}
		};
		if (group === undefined) return await new Promise(exe);
		else {
			group.pushPromise({ executor: exe, args: [result] });
			return result;
		}
	}
};

export class Folder implements BaseFile {
	readonly name: string;
	readonly path: string;
	readonly isdir: true;
	readonly size: Promise<number>;
	readonly count: Promise<number>;
	readonly files: Promise<(File | Folder)[]>;
	constructor(protected config: DirConfig) {
		this.name = config.dirName;
		this.path = config.relativePath;
		this.isdir = true;
		
		const id = config.token;
		this.files = Folder.getDirent(id, this.path)
			.then(dirent => Promise.all(dirent.dirent_list.map(info => Folder.makeFileOrFolder(id, info))));
		this.size = this.files.then(files => Promise.all(files.map(file => file.size)))
			.then(sumNumber);
		this.count = this.files.then(files => Promise.all(files.map(file => file.isdir ? file.count : 1)))
			.then(sumNumber);
	}
	async download(path?: string, group?: LimitPromise<Result, [Result]>) {
		if (!this.config.canDownload) throw 'Cannot be download?';
		if (path === undefined) {
			if (this.path === '/') path = './' + this.config.dirName;
			else path = '.';
		}
		const folderPath = path + this.path;
		fs.mkdirSync(folderPath, { recursive: true });
		const results = await Promise.all((await this.files).map(file => file.download(file.isdir ? path : folderPath, group)));
		const result: Result = {
			name: this.name,
			path: folderPath,
			isdir: true,
			size: await this.size,
			existed: results.every(r => r.existed)
		};
		return result; // 这部分完全没用上……
	}
	
	protected static async getDirent(id: string, path?: string): Promise<Dirent> {
		const url = `https://cloud.tsinghua.edu.cn/api/v2.1/share-links/${id}/dirents/?path=${encodeURIComponent(path || '')}`;
		return JSON.parse(await httpsRequest(url)) as Dirent;
	}
	protected static async makeFileOrFolder(id: string, info: DirentItem): Promise<File | Folder> {
		const url = info.is_dir === true
			? `https://cloud.tsinghua.edu.cn/d/${id}/?p=${encodeURIComponent(info.folder_path)}`
			: `https://cloud.tsinghua.edu.cn/d/${id}/files/?p=${encodeURIComponent(info.file_path)}`;
		return parseHTML(url);
	}
};

export async function parseHTML(url: string): Promise<File | Folder> {
	const content = await httpsRequest(url);
	const resultArray = content.match(INFO_REGEXP);
	if (resultArray === null) throw 'Unexpected HTML content';
	const config: DirConfig | FileConfig = eval('(' + resultArray[1] + ')')['pageOptions'];
	if ('dirName' in config) {
		return new Folder(config);
	} else {
		// 单纯 file 的话 filePath 会增加额外目录，去除
		const path = config.filePath
		config.filePath = path.substring(path.lastIndexOf('/'));
		return new File(config);
	}
}

export async function downloadCloud(url: string, path?: string, limit?: number) {
	if (limit === undefined) limit = 3;
	console.log('\x1B[31mscanning\x1B[0m');
	const file = await parseHTML(url);
	const size = await file.size;
	const all = file.isdir ? await file.count : 1;
	console.log('\x1B[31mscanned:\n\tsize: %s\n\tcount: %d\n\x1B[0m', size2str(size), all);
	
	let cnt = 0;
	let cntSize = 0;

	enum Color { RED = 31, GREEN, YELLOW, BLUE, MAGENTA, CYAN };
	function log(color: Color, tag: string, fileSize: number, path: string) {
		return console.log(`\x1B[${color}m${tag}\t${(cntSize / size * 100).toFixed(2).padStart(6)}%\t${size2str(cntSize).padStart(10)}/${size2str(size)}\t${cnt.toString().padStart(5)}/${all}\t${size2str(fileSize)}\t${path}\x1B[0m`);
	}

	const callbacks: Callbacks<Result, [Result]> = {
		before: file => log(Color.YELLOW, '↓', file.size, file.path),
		after: file => {
			++cnt; cntSize += file.size;
			if (file.existed)
				log(Color.CYAN, ' ∃', file.size, file.path); // ○▫△●
			else
				log(Color.GREEN, ' √', file.size, file.path);
		}
	};
	const group = new LimitPromise<Result, [Result]>(limit, undefined, callbacks);
	file.download(path, group);
	await group;
}

export default downloadCloud;
