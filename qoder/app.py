"""NoteSeek · 课程与文献速查台

把散落的课程笔记 / 论文摘录 / 会议记录丢进 seekdb，用一句话检索，
回答里必须带出处（文件 + 小节 + 片段）。核心是 seekdb 的**向量 + 全文混合检索**。

    python app.py            # http://127.0.0.1:8090
    python app.py --ingest samples/notes   # 先把示例笔记灌进去

环境变量：
    SEEKDB_HOST / SEEKDB_PORT / SEEKDB_USER / SEEKDB_PASSWORD / SEEKDB_DB
    （不设 SEEKDB_HOST 时用嵌入式本地库，仅 Linux）
"""

from __future__ import annotations

import argparse
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import pyseekdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from embedding import HashEmbedding

COLLECTION = "notes"
EMBEDDING = HashEmbedding()
STATIC = Path(__file__).parent / "static"
CHUNK_CHARS = 520
CHUNK_OVERLAP = 60

app = FastAPI(title="NoteSeek", version="1.0.0", description="课程与文献速查台（seekdb 混合检索）")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_client: Any = None
_collection: Any = None


# --------------------------------------------------------------------------- seekdb


def collection() -> Any:
    global _client, _collection
    if _collection is not None:
        return _collection
    host = os.environ.get("SEEKDB_HOST", "").strip()
    database = os.environ.get("SEEKDB_DB", "test")
    if host:
        _client = pyseekdb.Client(
            host=host,
            port=int(os.environ.get("SEEKDB_PORT", "2881")),
            tenant=os.environ.get("SEEKDB_TENANT", "sys"),
            database=database,
            user=os.environ.get("SEEKDB_USER", "root"),
            password=os.environ.get("SEEKDB_PASSWORD", ""),
        )
    else:
        path = os.environ.get("SEEKDB_PATH", "./seekdb_data")
        os.makedirs(path, exist_ok=True)
        _client = pyseekdb.Client(path=path, database=database)
    _collection = _client.get_or_create_collection(COLLECTION, embedding_function=EMBEDDING)
    return _collection


# --------------------------------------------------------------------------- 切块


def _heading_before(lines: list[str], index: int) -> str:
    for cursor in range(index, -1, -1):
        line = lines[cursor].strip()
        if line.startswith("#"):
            return line.lstrip("# ").strip()
    return ""


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """按段落切成 ≤CHUNK_CHARS 的片段，并带上它所在小节的标题。

    注意 `start_index` 必须跟着「这一块的起点」走：如果切块时只把上一块的尾巴
    塞回 buffer，起点就会被记住成全文第一行，所有片段都会挂上文档大标题、
    拿不到真正的 `## 小节` 标题（踩过这个坑，检索出处会变得没用）。
    """

    lines = text.split("\n")
    chunks: list[tuple[str, str]] = []
    buffer: list[str] = []
    carry = ""
    size = 0
    start_index = 0
    for index, line in enumerate(lines):
        if not buffer:
            start_index = index
            if carry:
                buffer.append(carry)
                size = len(carry) + 1
        buffer.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS:
            body = "\n".join(buffer).strip()
            if body:
                chunks.append((_heading_before(lines, start_index), body))
            carry = body[-CHUNK_OVERLAP:] if len(body) > CHUNK_OVERLAP else ""
            buffer, size = [], 0
    body = "\n".join(buffer).strip()
    if body:
        chunks.append((_heading_before(lines, start_index), body))
    return chunks


def ingest_document(title: str, text: str, tag: str = "") -> dict[str, Any]:
    coll = collection()
    chunks = chunk_markdown(text)
    if not chunks:
        raise ValueError("文档是空的")
    doc_id = uuid.uuid4().hex[:10]
    ids, documents, metadatas = [], [], []
    for seq, (heading, body) in enumerate(chunks):
        ids.append(f"{doc_id}-{seq:03d}")
        # 把小节标题拼进被向量化的文本：标题往往就是答案所在的位置，正文却几乎没有词面重合
        documents.append(f"{title} · {heading}\n{body}" if heading else f"{title}\n{body}")
        metadatas.append(
            {
                "doc_id": doc_id,
                "title": title,
                "heading": heading,
                "seq": seq,
                "tag": tag,
                "chars": len(body),
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
    coll.add(ids=ids, documents=documents, metadatas=metadatas)
    try:
        coll.refresh_index()  # seekdb 的索引是异步的，写完必须刷新才能立刻检索到
    except Exception:  # noqa: BLE001 - 老版本没有这个方法
        pass
    return {"ok": True, "doc_id": doc_id, "title": title, "chunks": len(chunks)}


# --------------------------------------------------------------------------- 检索


def _rows(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    rows = []
    for index, row_id in enumerate(ids):
        meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
        distance = dists[index] if index < len(dists) and dists[index] is not None else None
        rows.append(
            {
                "id": row_id,
                "text": docs[index] if index < len(docs) else "",
                "title": meta.get("title", ""),
                "heading": meta.get("heading", ""),
                "seq": meta.get("seq", 0),
                "tag": meta.get("tag", ""),
                "distance": round(float(distance), 4) if distance is not None else None,
            }
        )
    return rows


_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def keywords(query: str, limit: int = 3) -> list[str]:
    """给全文检索挑几个关键词：优先较长的英文词，其次中文双字片段。"""
    words = [w for w in re.findall(r"[a-zA-Z]{3,}", query.lower())]
    words.sort(key=len, reverse=True)
    picked = words[:limit]
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    picked.extend(cjk[:limit])
    return picked[:limit] if picked else [query[:8]]


def search(query: str, k: int = 5, tag: str | None = None) -> dict[str, Any]:
    coll = collection()
    where = {"tag": tag} if tag else None
    started = time.perf_counter()
    mode = "hybrid"
    rows: list[dict[str, Any]] = []
    try:
        raw = coll.hybrid_search(
            query={"where_document": {"$contains": keywords(query)[0]}, "n_results": max(k * 6, 30)}
            if not where
            else {"where_document": {"$contains": keywords(query)[0]}, "where": where, "n_results": max(k * 6, 30)},
            knn={"query_texts": [query], "n_results": max(k * 6, 30), **({"where": where} if where else {})},
            rank={"rrf": {"rank_window_size": 60, "rank_constant": 60}},
            n_results=max(k * 4, 20),
            include=["documents", "metadatas"],
        )
        rows = _rows(raw)
    except Exception:  # noqa: BLE001 - 混合检索不可用时退回纯向量
        mode = "vector"
        raw = coll.query(query_texts=[query], n_results=max(k * 4, 20), **({"where": where} if where else {}))
        rows = _rows(raw)
    elapsed = (time.perf_counter() - started) * 1000

    results = []
    for row in rows[:k]:
        results.append(
            {
                "chunk_id": row["id"],
                "title": row["title"],
                "heading": row["heading"],
                "snippet": row["text"],
                "source": f"{row['title']} › {row['heading'] or '正文'} › 片段 #{row['seq'] + 1}",
                "distance": row["distance"],
            }
        )
    return {"query": query, "mode": mode, "took_ms": round(elapsed, 1), "count": len(results), "results": results}


# --------------------------------------------------------------------------- HTTP


class IngestRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1)
    tag: str = Field(default="", max_length=40)


@app.get("/")
def index_page() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    coll = collection()
    try:
        rows = int(coll.count())
    except Exception:  # noqa: BLE001
        rows = -1
    return {"ok": True, "collection": COLLECTION, "chunks": rows, "embedding": EMBEDDING.name()}


@app.post("/api/ingest")
def api_ingest(request: IngestRequest) -> dict[str, Any]:
    try:
        return ingest_document(request.title, request.text, request.tag)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"{type(error).__name__}: {error}") from error


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1, max_length=500),
    k: int = Query(5, ge=1, le=20),
    tag: str | None = None,
) -> dict[str, Any]:
    try:
        return search(q, k, tag)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(error).__name__}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="NoteSeek")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--ingest", default="", help="把一个目录下的 .md 全部导入")
    args = parser.parse_args()

    if args.ingest:
        folder = Path(args.ingest)
        for path in sorted(folder.glob("*.md")):
            result = ingest_document(path.stem, path.read_text(encoding="utf-8"), tag="sample")
            print(f"[ingest] {result['title']}: {result['chunks']} 块")
        return 0

    import uvicorn

    print(f"[noteseek] http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
