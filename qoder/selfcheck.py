"""第六步自查：5 个问题各查一次，打印「命中的文档 / 出处小节 / 耗时」。

用法：SEEKDB_HOST=... SEEKDB_COLLECTION=... python selfcheck.py
验收：每个问题的 Top1 属于预期那篇笔记，且前列命中不串到别的文档。
"""

from __future__ import annotations

import sys

import app

CASES = [
    ("REPEATABLE READ 是怎么避免幻读的", "事务与隔离级别"),
    ("为什么用 B+ 树而不是哈希索引", "索引结构"),
    ("RRF 融合排序怎么算", "融合排序"),
    ("seekdb 的 Mock AI Service 是干什么的", "AI 服务"),
    ("回表和覆盖索引", "索引结构"),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    rows = []
    failures = []
    for query, expect in CASES:
        res = app.search(query, k=5)
        top = res["results"][0] if res["results"] else None
        # 命中里有多少条属于预期文档（用 doc 标题片段匹配，出处字符串本身就带文档名）
        hits = [r for r in res["results"] if expect in r["title"] or expect in r["source"]]
        ok = top is not None and (expect in top["title"] or expect in top["source"])
        if not ok:
            failures.append((query, expect, top["title"] if top else "无命中"))
        rows.append(
            {
                "query": query,
                "expect": expect,
                "title": top["title"] if top else "-",
                "heading": top["heading"] if top else "-",
                "took_ms": res["took_ms"],
                "mode": res["mode"],
                "n": f"{len(hits)}/{res['count']}",
                "ok": "✓" if ok else "✗",
            }
        )

    header = f"{'问题':<34}{'命中的文档':<24}{'出处小节':<20}{'命中/总数':<10}{'耗时':<9}{'判定'}"
    print(header)
    print("-" * 92)
    for r in rows:
        print(
            f"{r['query']:<34}{r['title']:<24}{r['heading']:<20}{r['n']:<10}"
            f"{r['took_ms']:<9}{r['ok']}  ({r['mode']}, 预期含「{r['expect']}」)"
        )

    # ODPC 终端里全角宽度对不齐，再补一份逐行的结论，保证读数无歧义
    print()
    for r in rows:
        print(f"[{r['ok']}] {r['query']} -> {r['title']} › {r['heading']}")

    if failures:
        print(f"\n未通过 {len(failures)} 项：{failures}")
        return 1
    print("\n5 题全部命中预期文档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
