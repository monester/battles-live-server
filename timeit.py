# source https://gist.github.com/Integralist/77d73b2380e4645b564c28c53fae71fb

import asyncio
import time


def timeit(func):
    async def process(func, *args, **params):
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **params)
        else:
            return func(*args, **params)

    async def helper(*args, **params):
        print('{}.time'.format(func.__name__))
        start = time.time()
        result = await process(func, *args, **params)

        # Test normal function route...
        # result = await process(lambda *a, **p: print(*a, **p), *args, **params)

        print(func.__name__, 'took', time.time() - start)
        return result

    return helper
