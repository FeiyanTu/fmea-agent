"""
FMEA 并发处理引擎
等价于 Java FmeaPipelineService 中的 8-thread ExecutorService

v1.1: 增加指数退避重试机制
"""
import asyncio
import logging
import random
from typing import List, Callable, Awaitable, TypeVar, Optional

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 默认并发数
DEFAULT_CONCURRENCY = 16

# 重试配置
MAX_RETRIES = 2
BASE_DELAY_MS = 1000
MAX_DELAY_MS = 10000


class ConcurrentFmeaEngine:
    """FMEA 并发处理引擎

    使用 asyncio.Semaphore 控制并发数，等价于 Java 的 ExecutorService(8 threads)
    支持指数退避重试（v1.1）
    """

    def __init__(self, concurrency: int = DEFAULT_CONCURRENCY,
                 max_retries: int = MAX_RETRIES):
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.semaphore = asyncio.Semaphore(concurrency)

    async def _process_with_retry(
        self,
        item: any,
        processor: Callable[[any], Awaitable[T]],
        index: int,
    ) -> tuple:
        """带重试的单个项目处理

        Returns:
            (index, result, error)
        """
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await processor(item)
                if attempt > 0:
                    logger.info(f"重试成功 item[{index}] (第{attempt}次)")
                return index, result, None
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = min(
                        BASE_DELAY_MS * (2 ** attempt) + random.randint(0, 500),
                        MAX_DELAY_MS,
                    )
                    logger.warning(
                        f"item[{index}] 第{attempt+1}次失败: {e}, "
                        f"{delay/1000:.1f}s后重试..."
                    )
                    await asyncio.sleep(delay / 1000)

        return index, None, last_error

    async def process_batch(
        self,
        items: list,
        processor: Callable[[any], Awaitable[T]],
        on_error: Optional[Callable[[any, Exception], Optional[T]]] = None,
    ) -> list:
        """
        并发处理一批项目

        Args:
            items: 待处理项目列表
            processor: 异步处理函数，接收单个项目，返回结果
            on_error: 错误处理函数，接收(项目, 异常)，返回Optional[结果]

        Returns:
            结果列表（保持与输入顺序一致）
        """

        async def wrapped(item, index: int):
            async with self.semaphore:
                _, result, error = await self._process_with_retry(item, processor, index)
                if result is not None:
                    return index, result, None
                if error:
                    logger.warning(f"Skipping failed item[{index}]: {error}")
                    if on_error:
                        try:
                            fallback = on_error(item, error)
                            return index, fallback, error
                        except Exception:
                            return index, None, error
                    return index, None, error

        # 并发执行
        tasks = [wrapped(item, i) for i, item in enumerate(items)]
        results_raw = await asyncio.gather(*tasks, return_exceptions=False)

        # 按原始索引排序
        results_raw.sort(key=lambda x: x[0])

        results = []
        for idx, result, error in results_raw:
            if result is not None:
                results.append(result)
            elif error:
                logger.warning(f"Skipping failed item[{idx}]: {error}")

        return results

    async def process_with_progress(
        self,
        items: list,
        processor: Callable[[any], Awaitable[T]],
        progress_callback: Optional[Callable[[int, int], Awaitable[None]]] = None,
        on_error: Optional[Callable[[any, Exception], Optional[T]]] = None,
    ) -> list:
        """
        带进度回调的并发处理

        Args:
            items: 待处理项目列表
            processor: 异步处理函数
            progress_callback: 进度回调 (completed, total)
            on_error: 错误处理函数

        Returns:
            结果列表
        """
        total = len(items)
        completed = 0
        lock = asyncio.Lock()

        async def wrapped(item, index: int):
            nonlocal completed
            async with self.semaphore:
                _, result, error = await self._process_with_retry(item, processor, index)
                if error and result is None:
                    logger.warning(f"Processing failed for item[{index}] after retries: {error}")
                    if on_error:
                        try:
                            result = on_error(item, error)
                        except Exception:
                            result = None

                async with lock:
                    completed += 1
                    if progress_callback:
                        await progress_callback(completed, total)

                return index, result

        tasks = [wrapped(item, i) for i, item in enumerate(items)]
        results_raw = await asyncio.gather(*tasks)

        results_raw.sort(key=lambda x: x[0])
        return [r for _, r in results_raw if r is not None]


# 全局单例
_default_engine: Optional[ConcurrentFmeaEngine] = None


def get_concurrent_engine(concurrency: int = DEFAULT_CONCURRENCY) -> ConcurrentFmeaEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = ConcurrentFmeaEngine(concurrency)
    return _default_engine
