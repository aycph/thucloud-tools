export type Resolver<T> = (value: T) => void;
export type Executor<T> = (resolve: Resolver<T>, reject: (reason?: any) => void) => void;
export type ExecutorWithArgs<T, Args extends unknown[]> = {
	executor: Executor<T>,
	args: Args
};

export type BeforeCallback<Args extends unknown[]> = (...args: Args) => void;
export type AfterCallback<T, Args extends unknown[]> = (value: T, ...args: Args) => void;
export type Callbacks<T, Args extends unknown[]> = {
	before?: BeforeCallback<Args>,
	after?: AfterCallback<T, Args>
};

type Recorder<T, Args extends unknown[], IDType = ExecutorWithArgs<T, Args>> = {
	id: IDType,
	value: Promise<T>
};

// 因为难以获得获得总数目，将 callback 中总数目的获取交给了外部

export class LimitPromise<T, Args extends unknown[] = []> implements PromiseLike<T[]> {
	private working: Recorder<T, Args>[];
	private waiting: ExecutorWithArgs<T, Args>[];
	private before: BeforeCallback<Args>;
	private after: AfterCallback<T, Args>;
	private static emptyCallback() {}
	constructor(private limit: number, executors?: ExecutorWithArgs<T, Args>[], callbacks?: Callbacks<T, Args>) {
		this.working = [];
		this.waiting = [];
		if (callbacks === undefined) {
			this.before = this.after = LimitPromise.emptyCallback;
		} else {
			this.before = callbacks.before || LimitPromise.emptyCallback;
			this.after = callbacks.after || LimitPromise.emptyCallback;
		}
		if (executors) {
			for (let exe of executors) {
				this.pushPromise(exe);
			}
		}
	}
	private async race(): Promise<T> {
		return await Promise.race(this.working.map(task => task.value));
	}
	async then<TResult>(onfulfilled: (value: T[]) => TResult | PromiseLike<TResult>): Promise<TResult> {
		const results: T[] = [];
		while (this.working.length > 0) {
			results.push(await this.race());
		}
		return onfulfilled(results);
	}
	pushPromise(executor: ExecutorWithArgs<T, Args>) {
		if (this.working.length >= this.limit) {
			this.waiting.push(executor);
		} else {
			this.working.push({
				id: executor,
				value: this.makePromise(executor)
			});
		}
	}
	private makePromise(exe: ExecutorWithArgs<T, Args>): Promise<T> {
		const { executor, args } = exe;
		const new_executor: Executor<T> = (resolve, reject) => {
			const new_resolve: Resolver<T> = value => {
				resolve(value);
				this.after(value, ...args);
				this.working = this.working.filter(val => val.id !== exe);
				if (this.waiting.length === 0) return;
				const next_exe = this.waiting.shift()!;
				this.pushPromise(next_exe);
			}
			this.before(...args);
			return executor(new_resolve, reject);
		}
		return new Promise(new_executor);
	}
}

export async function PromiseLimit<T, Args extends unknown[] = []>(limit: number, executors: ExecutorWithArgs<T, Args>[], callbacks?: Callbacks<T, Args>) {
	return await new LimitPromise(limit, executors, callbacks);
}

export default PromiseLimit;
