"""汇总抓取入口：遍历所有适配器抓取并入库。

用法：
    python -m app.crawler.run                # 全量抓取
    python -m app.crawler.run --limit 2      # 每源限 2 页/条（调试用）
"""

import argparse

from app.core import config
from app.crawler import javaguide, mianshiya


def build_adapters() -> list:
    """构建已注册的数据源适配器列表（新增源在此登记即可）。"""
    adapters = [
        mianshiya.MianShiYaAdapter(),
        javaguide.JavaGuideAdapter(),
    ]
    # 力扣（算法题）默认不抓取：算法题仍属模拟面试第 2 阶段，只是抓取端默认关闭，
    # 需要时置 CRAWL_LEETCODE=1 开启（开启后才有 算法-* 标签的题入库）
    if config.CRAWL_LEETCODE:
        from app.crawler import leetcode

        adapters.insert(1, leetcode.LeetCodeAdapter())
    return adapters


ADAPTERS = build_adapters()


def crawl_all(limit_per_source: int | None = None) -> list[dict]:
    """遍历所有适配器抓取入库，返回各源统计；limit 透传给每个适配器。"""
    results = []
    for adapter in ADAPTERS:
        print(f"--- 正在抓取: {adapter.name} ---")
        try:
            stats = adapter.fetch_and_store(limit=limit_per_source)
        except Exception as e:
            stats = {"source": adapter.name, "error": str(e)}
        results.append(stats)
        print(stats)
    return results


def main() -> None:
    """命令行入口：解析参数并执行全量抓取。"""
    from app.core import db

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="每源最多抓取条数（调试）")
    args = parser.parse_args()

    db.init_db()
    crawl_all(limit_per_source=args.limit)


if __name__ == "__main__":
    main()
