import { Executor, Callbacks, PromiseLimit } from './PromiseLimit';
import { downloadCloud } from './cloud';

async function test_limit() {
	const times = [1, 5, 3, 2, 6, 8, 9, 5, 3, 4];
	const LIMIT = 3;
	const all = times.length;

	console.log('main');
	const executors: Executor<number>[] = times.map((time, index) => resolve => {
		setTimeout(() => {
			resolve(index);
		}, time * 1000);
	});
	let cnt = 0;
	const callbacks: Callbacks<number, [number]> = {
		before: (arg) => console.log(`start: ${arg} ${cnt}/${all}`),
		after: (value) => console.log(`end:   ${value} ${++cnt}/${all}`)
	}
	console.log(await PromiseLimit<number, [number]>(LIMIT, executors.map((executor, i) => ({ executor, args: [i] })), callbacks));
}

async function test_cloud() {
	const url = 'url0'; // MY
	// const url = 'url1'; // PDF test
	// const url = 'url2?p=/2023毕业典礼合影&mode=list';
	// const url = 'url2';
	// const url = 'url3';
	await downloadCloud(url0, 'F:/MY');
}

test_cloud();