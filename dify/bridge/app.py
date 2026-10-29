"""布鲁姆酒馆 · 记忆桥（seekdb HTTP bridge）

给 Dify Workflow 用的最小 HTTP 服务：把「玩家说过的话 / 老板的反应」写进 seekdb，
并在下一轮按语义相似度把最相关的几条旧账捞回来。

只依赖 pyseekdb（嵌入式，零部署），用标准库 http.server 提供服务，避免额外框架依赖。

    python app.py                 # 默认 127.0.0.1:8077
    python app.py --port 9000     # 改端口

环境变量：
    SEEKDB_HOST    seekdb 服务地址（如 127.0.0.1）；不设则用嵌入式本地库（仅 Linux）
    SEEKDB_PORT    seekdb 端口，默认 2881
    SEEKDB_USER    seekdb 用户，默认 root
    SEEKDB_PASSWORD seekdb 密码，默认空
    SEEKDB_DB      数据库名，默认 test
    SEEKDB_PATH    嵌入式模式的数据目录，默认 ./seekdb_data
    BRIDGE_TOKEN   可选；设置后所有非 /health 请求需带 Authorization: Bearer <token>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyseekdb
from embedding import HashEmbedding

COLLECTION = "grudges"
EMBEDDING = HashEmbedding()
DEFAULT_PORT = 8077
DEFAULT_K = 3
MAX_K = 10
MAX_TEXT = 2000

_lock = threading.Lock()
_state: dict[str, Any] = {"client": None, "collection": None, "ops": []}

_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------- 存储层


def _build_client() -> Any:
    """两种连法：给了 SEEKDB_HOST 就连 seekdb 服务（Windows/macOS 只能用这种，
    因为 pylibseekdb 嵌入式库只有 Linux 版）；否则用嵌入式本地库（Linux 推荐）。"""
    host = os.environ.get("SEEKDB_HOST", "").strip()
    database = os.environ.get("SEEKDB_DB", "test")
    if host:
        return pyseekdb.Client(
            host=host,
            port=int(os.environ.get("SEEKDB_PORT", "2881")),
            tenant=os.environ.get("SEEKDB_TENANT", "sys"),
            database=database,
            user=os.environ.get("SEEKDB_USER", "root"),
            password=os.environ.get("SEEKDB_PASSWORD", ""),
        )
    path = os.environ.get("SEEKDB_PATH", "./seekdb_data")
    os.makedirs(path, exist_ok=True)
    return pyseekdb.Client(path=path, database=database)


def collection():
    """懒加载 seekdb 客户端与集合（首次调用时建库建表）。"""
    if _state["collection"] is not None:
        return _state["collection"]
    with _lock:
        if _state["collection"] is not None:
            return _state["collection"]
        client = _build_client()
        coll = client.get_or_create_collection(COLLECTION, embedding_function=EMBEDDING)
        _state["client"] = client
        _state["collection"] = coll
        return coll


def _record(op: str, **fields: Any) -> None:
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": op, **fields}
    _state["ops"].append(entry)
    del _state["ops"][:-50]


def _rows(result: Any) -> list[dict[str, Any]]:
    """把 pyseekdb 的列式返回（{"ids":[[...]], "documents":[[...]]}）摊平成行。"""
    if not isinstance(result, dict):
        return []
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    out = []
    for index, row_id in enumerate(ids):
        meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
        distance = dists[index] if index < len(dists) and dists[index] is not None else None
        out.append(
            {
                "id": row_id,
                "text": docs[index] if index < len(docs) else "",
                "user_id": meta.get("user_id", ""),
                "turn": meta.get("turn", 0),
                "ts": meta.get("ts", ""),
                "distance": round(float(distance), 4) if distance is not None else None,
                "score": round(1.0 - float(distance), 4) if distance is not None else None,
            }
        )
    return out


def recall(user_id: str, query: str, k: int) -> dict[str, Any]:
    coll = collection()
    k = max(1, min(int(k or DEFAULT_K), MAX_K))
    pool = max(k * 6, 20)  # 先多捞一些候选，再重排——短句记忆的向量分差很小
    try:
        raw = coll.query(query_texts=[query], n_results=pool, where={"user_id": user_id})
        rows = _rows(raw)
    except Exception as exc:  # where 语法或后端差异时退化为「查全量 + 本地过滤」
        raw = coll.query(query_texts=[query], n_results=pool)
        rows = [row for row in _rows(raw) if row["user_id"] == user_id]
        _record("recall-fallback", user_id=user_id, error=str(exc)[:200])
    rows = [row for row in rows if row["user_id"] == user_id]
    rows = rerank(query, rows)[:k]
    _record("recall", user_id=user_id, query=query[:80], k=k, hits=len(rows))
    return {
        "memories": rows,
        "memories_text": render_memories(rows),
        "count": len(rows),
        "total": count(user_id),
    }


def ngrams(text: str) -> set[str]:
    """中文字二元组 + 英文数字词，作为轻量词面特征。"""
    text = (text or "").lower()
    words = set(_WORD.findall(text))
    chars = _CJK.findall(text)
    grams = {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return words | grams


def rerank(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """向量召回 + IDF 加权词面覆盖重排。

    为什么要这一步：短句记忆的余弦分差很小（实测 0.06 / 0.18 混在一起），
    单靠向量会把「稽查员」排到「摔杯子」前面。加上「查询里的词有多少被这条记忆覆盖」
    这一项（按候选集内的 IDF 加权，越罕见的词越值钱），排序立刻变得符合直觉。
    """
    if not rows:
        return rows
    query_grams = ngrams(query)
    if not query_grams:
        return rows
    doc_grams = [ngrams(row["text"]) for row in rows]
    total = len(rows)
    idf: dict[str, float] = {}
    for gram in query_grams:
        df = sum(1 for grams in doc_grams if gram in grams)
        idf[gram] = math.log((total + 1) / (df + 0.5))
    weight_sum = sum(idf.values()) or 1.0
    for row, grams in zip(rows, doc_grams):
        covered = sum(idf[gram] for gram in query_grams if gram in grams)
        lexical = covered / weight_sum  # 0..1，查询的有效词被覆盖了多少
        cosine = row.get("score") if isinstance(row.get("score"), float) else 0.0
        row["lexical"] = round(lexical, 4)
        row["rank_score"] = round(0.6 * lexical + 0.4 * (cosine + 1) / 2, 4)
    rows.sort(key=lambda row: row["rank_score"], reverse=True)
    return rows


def render_memories(rows: list[dict[str, Any]]) -> str:
    """把检索结果渲染成一段直接可塞进 prompt 的文本（LLM 不用自己解析 JSON）。"""
    if not rows:
        return "（记忆库是空的：这是你和这个玩家的第一次见面，没有任何旧账可翻）"
    lines = []
    for index, row in enumerate(rows, start=1):
        score = row.get("score")
        hint = f"（相关度 {score:.2f}）" if isinstance(score, float) else ""
        lines.append(f"{index}. 第{row.get('turn', '?')}轮｜{row.get('text', '')}{hint}")
    return "\n".join(lines)


def remember(user_id: str, text: str, reply: str = "", kind: str = "grudge") -> dict[str, Any]:
    coll = collection()
    text = (text or "").strip()[:MAX_TEXT]
    reply = (reply or "").strip()[:MAX_TEXT]
    if not text:
        raise ValueError("text 不能为空")
    turn = count(user_id) + 1
    # 只把「记忆事实」本身向量化：老板的反应与轮次放 metadata，
    # 否则这些模板词会稀释相似度，把不相关的旧账顶上来（实测过）。
    document = text
    row_id = f"{user_id}-{turn:04d}-{uuid.uuid4().hex[:6]}"
    meta = {
        "user_id": user_id,
        "turn": turn,
        "kind": kind,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reply": reply,
    }
    coll.add(ids=[row_id], documents=[document], metadatas=[meta])
    try:
        coll.refresh_index()  # seekdb 的索引是异步的，写完要刷新才能立刻检索到
    except Exception as exc:
        _record("refresh-failed", error=str(exc)[:200])
    _record("remember", user_id=user_id, id=row_id, turn=turn)
    return {"ok": True, "id": row_id, "turn": turn, "total": count(user_id)}


def count(user_id: str) -> int:
    coll = collection()
    try:
        raw = coll.get(where={"user_id": user_id})
        ids = raw.get("ids") if isinstance(raw, dict) else None
        return len(ids or [])
    except Exception:
        try:
            return int(coll.count())
        except Exception:
            return 0


def list_memories(user_id: str) -> list[dict[str, Any]]:
    coll = collection()
    try:
        raw = coll.get(where={"user_id": user_id})
    except Exception:
        raw = coll.get()
    rows = _rows(raw) if isinstance(raw, dict) and raw.get("ids") else []
    if not rows and isinstance(raw, dict):
        rows = [
            {
                "id": row_id,
                "text": (raw.get("documents") or [""] * 99)[index],
                "user_id": (raw.get("metadatas") or [{}] * 99)[index].get("user_id", ""),
                "turn": (raw.get("metadatas") or [{}] * 99)[index].get("turn", 0),
                "ts": (raw.get("metadatas") or [{}] * 99)[index].get("ts", ""),
                "distance": None,
                "score": None,
            }
            for index, row_id in enumerate(raw.get("ids") or [])
        ]
    rows = [row for row in rows if row["user_id"] == user_id]
    rows.sort(key=lambda row: row["turn"])
    return rows


def reset(user_id: str) -> dict[str, Any]:
    coll = collection()
    removed = 0
    try:
        raw = coll.get(where={"user_id": user_id})
        ids = (raw or {}).get("ids") or []
        if ids:
            coll.delete(ids=ids)
            removed = len(ids)
        try:
            coll.refresh_index()
        except Exception:
            pass
    except Exception as exc:
        _record("reset-failed", user_id=user_id, error=str(exc)[:200])
        raise
    _record("reset", user_id=user_id, removed=removed)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------- HTTP 层


class Handler(BaseHTTPRequestHandler):
    server_version = "TavernBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        pass

    # --- 工具 ---

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        token = os.environ.get("BRIDGE_TOKEN", "").strip()
        if not token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # --- 路由 ---

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/health":
                coll_count = 0
                try:
                    coll_count = int(collection().count())
                except Exception:
                    pass
                return self._send(
                    200,
                    {
                        "ok": True,
                        "engine": "seekdb via pyseekdb",
                        "mode": "server" if os.environ.get("SEEKDB_HOST") else "embedded",
                        "target": os.environ.get("SEEKDB_HOST")
                        + ":"
                        + os.environ.get("SEEKDB_PORT", "2881")
                        if os.environ.get("SEEKDB_HOST")
                        else os.environ.get("SEEKDB_PATH", "./seekdb_data"),
                        "database": os.environ.get("SEEKDB_DB", "test"),
                        "collection": COLLECTION,
                        "embedding": EMBEDDING.name() + f"/{EMBEDDING.dimension}d",
                        "rows": coll_count,
                    },
                )
            if not self._auth_ok():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            if url.path == "/memory/list":
                user_id = (query.get("user_id") or [""])[0]
                if not user_id:
                    return self._send(400, {"ok": False, "error": "user_id 必填"})
                items = list_memories(user_id)
                return self._send(200, {"ok": True, "items": items, "count": len(items)})
            if url.path == "/debug/stats":
                return self._send(200, {"ok": True, "recent_ops": _state["ops"][-20:]})
            return self._send(404, {"ok": False, "error": f"unknown path {url.path}"})
        except Exception as exc:
            return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        body = self._body()
        user_id = str(body.get("user_id") or "").strip()
        try:
            if url.path == "/memory/recall":
                if not user_id:
                    return self._send(400, {"ok": False, "error": "user_id 必填"})
                query = str(body.get("query") or body.get("message") or "").strip()
                if not query:
                    return self._send(400, {"ok": False, "error": "query 必填"})
                return self._send(200, {"ok": True, **recall(user_id, query, body.get("k") or DEFAULT_K)})
            if url.path == "/memory/remember":
                if not user_id:
                    return self._send(400, {"ok": False, "error": "user_id 必填"})
                return self._send(
                    200,
                    {"ok": True, **remember(user_id, str(body.get("text") or ""), str(body.get("reply") or ""), str(body.get("kind") or "grudge"))},
                )
            if url.path == "/memory/reset":
                if not user_id:
                    return self._send(400, {"ok": False, "error": "user_id 必填"})
                return self._send(200, reset(user_id))
            return self._send(404, {"ok": False, "error": f"unknown path {url.path}"})
        except Exception as exc:
            return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    parser = argparse.ArgumentParser(description="seekdb memory bridge for the Tavern Dify app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        collection()  # 启动即建库/建集合，避免首个请求超时
        print(f"[bridge] seekdb ready: {collection().count()} rows in '{COLLECTION}'")
    except Exception as exc:  # 连不上也要能起来，健康检查会如实报告
        print(f"[bridge] WARNING seekdb not ready yet: {type(exc).__name__}: {exc}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[bridge] seekdb memory bridge on http://{args.host}:{args.port}  (GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] bye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
