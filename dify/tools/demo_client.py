"""演示客户端：不装 Dify 也能看清这条链路的真实行为。

它做的四件事和 Dify Workflow 里的四个节点一一对应：

    1. POST /memory/recall      ← 节点「1. 检索旧账（seekdb）」
    2. （LLM 生成老板回话）      ← 节点「2. 老板回话（LLM）」，本脚本用固定文案代替
    3. （LLM 压缩成一条记忆）    ← 节点「3. 压缩成一条记忆」
    4. POST /memory/remember    ← 节点「4. 记住这一轮（seekdb）」

用法：
    python tools/demo_client.py                 # 默认 http://127.0.0.1:8077
    python tools/demo_client.py --reset         # 先清空 player-001 的记忆，恢复到初始状态
    python tools/demo_client.py --base http://127.0.0.1:8077
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

TURNS = [
    ("我昨天把你这儿的杯子摔了，还记得不？", "记！三个铜板，一个子儿都别想赖。"),
    ("忘了告诉你，我是港务局的稽查员。", "稽查员？就你这身打扮？穿得像来讨债的。"),
    ("行吧，下次我带条带鱼来赔你。", "带鱼？也行，别拿那种冻了半年的。"),
    ("隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱？", "少算？我给她记账，记在她爹头上。"),
]

PROBES = [
    "我摔坏的那个杯子还要赔多少？",
    "带鱼什么时候给你？",
    "稽查员的事你还记着吗？",
]


def call(base: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{path} -> HTTP {exc.code}: {body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8077")
    parser.add_argument("--user", default="player-001")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    health = call(args.base, "/health")
    print(f"[health] {health}")
    if args.reset:
        print(f"[reset ] {call(args.base, '/memory/reset', {'user_id': args.user})}")

    for turn, (spoken, reply) in enumerate(TURNS, start=1):
        print(f"\n=========== 第 {turn} 轮 ===========")
        print(f"玩家：{spoken}")
        recalled = call(args.base, "/memory/recall", {"user_id": args.user, "query": spoken, "k": 3})
        print(f"[seekdb] 检索到 {recalled['count']} 条旧账（该玩家共 {recalled['total']} 条）：")
        for line in recalled["memories_text"].splitlines():
            print("          " + line)
        print(f"老板：{reply}")
        fact = f"玩家{spoken.rstrip('？?。！!')}"
        written = call(args.base, "/memory/remember", {"user_id": args.user, "text": fact, "reply": reply})
        print(f"[seekdb] 写入记忆 #{written['turn']}（累计 {written['total']} 条）：{fact}")

    print("\n=========== 换一个玩家（记忆隔离验证） ===========")
    other = call(args.base, "/memory/recall", {"user_id": "player-002", "query": "你记得我摔杯子吗", "k": 3})
    print(f"[seekdb] player-002 检索结果 count={other['count']} / memories_text={other['memories_text']}")

    print("\n=========== 检索质量抽查（同一玩家，不同问法） ===========")
    for probe in PROBES:
        hit = call(args.base, "/memory/recall", {"user_id": args.user, "query": probe, "k": 2})
        top = hit["memories"][0] if hit["memories"] else {}
        print(f"问：{probe}")
        print(f"  第一名：第{top.get('turn')}轮 {top.get('text')}（相关度 {top.get('score')}，词面覆盖 {top.get('lexical')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
