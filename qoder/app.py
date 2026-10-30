"""NoteSeek 课程与文献速查台。

  build_client()          设了 SEEKDB_HOST 走服务模式，否则走嵌入式
  chunk_markdown(text)    Markdown -> 片段（每片记住所属 `## 小节`）
  ingest_document(...)    片段 -> collection，写完 refresh_index()
  search(query, k, tag)   hybrid_search 优先，后端不支持时回退向量
  GET /api/search         HTTP 层，检索失败一律 500，绝不吞成空结果
  /                       static/index.html 单文件前端

命令行：python app.py --ingest samples/notes [--tag cs101]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyseekdb
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

# 共享 seekdb 上 notes 可能已被别的项目占用，用环境变量隔离集合名
COLLECTION_NAME = os.getenv("SEEKDB_COLLECTION", "notes").strip() or "notes"
# 离线兜底：拿不到 HuggingFace 权重时可设 EMBEDDING_MODE=hash，改用内置确定性哈希嵌入
EMBEDDING_MODE = (os.getenv("EMBEDDING_MODE", "onnx") or "onnx").strip().lower()
SNIPPET_CHARS = 160
CHUNK_MAX_CHARS = 520
CHUNK_OVERLAP_CHARS = 60
# 正文预算：留满重叠余量，保证「重叠 + 正文」仍 <= CHUNK_MAX_CHARS
CHUNK_BODY_CHARS = CHUNK_MAX_CHARS - CHUNK_OVERLAP_CHARS

_SECTION_RE = re.compile(r"^##(?!#)\s*(.*?)\s*$")
# 一级标题就是文档标题本身，只作为出处回退值，不该混进正文片段
_H1_RE = re.compile(r"^#(?:\s+\S.*?)?\s*$")
# 从原始文本里取一级标题当文档名；没有就用文件名
_H1_LINE_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_FENCE = "```"
# 中文标点无条件断句；latin 标点须后接空白才断，避免 3.14 / e.g. 被腰斩
_SOFT_TERMINATORS = "。！？；\n"
_HARD_TERMINATORS = ".!?;"

_client = None
_collection = None


# ---------------------------------------------------------------- 连接

def build_client():
    """SEEKDB_HOST 非空 -> RemoteServerClient；否则 -> 嵌入式 Client。

    返回的是 pyseekdb 的 collection 操作代理；构造过程不连库，所以连接失败会在
    第一次 get_or_create_collection / upsert 时才浮出来。
    """
    host = (os.getenv("SEEKDB_HOST") or "").strip()
    database = os.getenv("SEEKDB_DATABASE", "test")

    if host:
        host, _, port_from_host = host.partition(":")
        return pyseekdb.Client(
            host=host,
            port=int(os.getenv("SEEKDB_PORT", "") or port_from_host or 2881),
            tenant=os.getenv("SEEKDB_TENANT", "sys"),
            database=database,
            user=os.getenv("SEEKDB_USER", "root"),
            password=os.getenv("SEEKDB_PASSWORD", ""),
        )

    try:
        import pylibseekdb  # noqa: F401  仅 linux / mac-arm64 提供
    except ImportError as exc:
        raise RuntimeError(
            "嵌入式模式需要 pylibseekdb，当前平台不可用。"
            "请设 SEEKDB_HOST 连服务端（如 docker run -p 2881:2881 oceanbase/seekdb），"
            "或换 linux/mac-arm64 环境。"
        ) from exc

    return pyseekdb.Client(path=os.getenv("SEEKDB_PATH", "./data/seekdb.db"), database=database)


def get_client():
    global _client
    if _client is None:
        _client = build_client()
    return _client


def get_collection():
    """collection 用 SDK 默认索引：HNSW + cosine + 384 维 + 默认 ONNX embedding。

    换 embedding 模型必须同时改维度并整库重建，故此处刻意不隐式带参。
    """
    global _collection
    if _collection is None:
        if EMBEDDING_MODE == "hash":
            # 维度不同（256）不能和默认 ONNX 集合（384）共用，集合名自动加后缀
            from embedding import HashEmbedding

            _collection = get_client().get_or_create_collection(
                COLLECTION_NAME + "_hash", embedding_function=HashEmbedding()
            )
        else:
            _collection = get_client().get_or_create_collection(COLLECTION_NAME)
    return _collection


def doc_id_of(title: str) -> str:
    return hashlib.sha1(title.strip().encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 切块

def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for i, ch in enumerate(text):
        buf.append(ch)
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in _SOFT_TERMINATORS or (ch in _HARD_TERMINATORS and nxt in " \t\n"):
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return [s for s in out if s] or [text]


def _overlap_prefix(prev: str) -> str:
    """上一块尾部至多 CHUNK_OVERLAP_CHARS 字符，作为下一块的重叠起点。

    起点尽量落在某个句末之后，避免半句起头；但块尾常常正好就是句末，此时对齐会把重叠
    吃成空串（重叠静默消失，检索召回变差且没人报错），所以对齐失败就退回原始尾巴。
    """
    if len(prev) <= CHUNK_OVERLAP_CHARS:
        return ""
    tail = prev[-CHUNK_OVERLAP_CHARS:]
    for i, ch in enumerate(tail):
        if (ch in _SOFT_TERMINATORS or ch in _HARD_TERMINATORS) and tail[i + 1 :].strip():
            return tail[i + 1 :].strip()
    return tail.strip()


def _pack(units: list[str], budget: int, capacity: int) -> list[str]:
    """把文本单元塞进 <=capacity 的块；超长单元先按句切到 <=budget，再逐块带重叠。"""
    sliced: list[str] = []
    for unit in units:
        if len(unit) <= budget:
            sliced.append(unit)
            continue
        atoms: list[str] = []
        for sent in _split_sentences(unit):
            while len(sent) > budget:  # 单句超长：硬切兜底，顺序保持
                atoms.append(sent[:budget])
                sent = sent[budget:]
            if sent:
                atoms.append(sent)

        pieces, cur, cur_len = [], [], 0
        for atom in atoms:
            if cur and cur_len + len(atom) > budget:
                pieces.append("".join(cur).strip())
                cur, cur_len = [], 0
            cur.append(atom)
            cur_len += len(atom)
        if cur:
            pieces.append("".join(cur).strip())

        chained: list[str] = []
        for piece in pieces:
            head = _overlap_prefix(chained[-1]) if chained else ""
            chained.append((head + "\n" + piece).strip() if head else piece)
        sliced.extend(chained)

    blocks: list[str] = []
    cur = ""
    for unit in sliced:
        if not cur:
            cur = unit
            continue
        cand = cur + "\n\n" + unit
        cap = capacity if not blocks else budget
        if len(cand) <= cap:
            cur = cand
        else:
            blocks.append(cur)
            head = _overlap_prefix(cur)
            cur = (head + "\n" + unit).strip() if head else unit
    if cur:
        blocks.append(cur)
    return blocks


def _iter_sections(body: str) -> list[tuple[str | None, list[str]]]:
    """按 `## 小节` 归行；首个小节之前的正文归属 None，由调用方回退文档标题。

    `# 一级标题` 行直接丢弃：它就是文档标题，重复进正文只会污染向量与全文索引。
    """
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith(_FENCE):
            in_fence = not in_fence
            sections[-1][1].append(line)
            continue
        stripped = line.strip()
        if in_fence:
            sections[-1][1].append(line)
            continue
        m = _SECTION_RE.match(stripped)
        if m:
            sections.append((m.group(1), []))
            continue
        if _H1_RE.match(stripped):
            continue
        sections[-1][1].append(line)
    return [(h, ls) for h, ls in sections if any(l.strip() for l in ls)]


def _iter_paragraphs(lines: list[str]) -> list[str]:
    """空行断段；代码围栏内的空行不断段。"""
    paras: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith(_FENCE):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and not line.strip():
            if buf:
                paras.append("\n".join(buf).strip("\n"))
                buf = []
            continue
        buf.append(line)
    if buf:
        paras.append("\n".join(buf).strip("\n"))
    return [p for p in paras if p.strip()]


def chunk_markdown(text: str) -> list[dict]:
    """Markdown 正文 -> [{"seq", "heading", "chars", "text"}]。

    - text 只含原文，每片 <= CHUNK_MAX_CHARS；同小节相邻片之间带 <= CHUNK_OVERLAP_CHARS
      的重叠，跨小节不重叠——出处变了还重复上一节的尾巴，只会把两个小节的命中混在一起。
    - heading 是该片段所属的 `## 小节`；落在第一个 `##` 之前的片段返回 None，
      由 ingest_document 回退成文档标题。
    - seq 是**小节内**的段号，从 1 起，对应出处「文档 > 小节 > 第 N 段」。
    """
    if not text or not text.strip():
        return []

    body = _FRONTMATTER_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"), count=1)
    chunks: list[dict] = []
    for heading, lines in _iter_sections(body):
        paragraphs = _iter_paragraphs(lines)
        if not paragraphs:
            continue
        blocks = _pack(paragraphs, CHUNK_BODY_CHARS, CHUNK_MAX_CHARS)
        for seq, block in enumerate(blocks, 1):
            chunks.append({"heading": heading, "seq": seq, "chars": len(block), "text": block})
    return chunks


# ---------------------------------------------------------------- 写入

def ingest_document(title: str, text: str, tag: str = "") -> dict:
    """一篇文档切块后写入 collection "notes"，写完 refresh_index() 让索引立刻可见。

    document 字段 = "标题 · 小节\\n正文"：小节进向量与全文索引，检索「IK 分词」这类
    只出现在标题里的词也能命中；展示用的正文靠 metadata 还原。
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("title 不能为空")

    doc_id = doc_id_of(title)
    chunks = chunk_markdown(text)
    if not chunks:
        return {"doc_id": doc_id, "collection": COLLECTION_NAME, "count": 0, "ids": []}

    collection = get_collection()

    added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids, documents, metadatas = [], [], []
    for index, chunk in enumerate(chunks, 1):
        heading = chunk["heading"] or title
        # seq 是小节内段号，每个小节都从 1 起，直接拿它当主键会让不同小节的同名片段互相覆盖
        ids.append(f"{doc_id}#{index:04d}")
        documents.append(f"{title} · {heading}\n{chunk['text']}")
        metadatas.append(
            {
                "doc_id": doc_id,
                "title": title,
                "heading": heading,
                "seq": chunk["seq"],
                "tag": tag,
                "chars": chunk["chars"],
                "added_at": added_at,
            }
        )

    # upsert 而非 add：同 doc_id 重跑覆盖。正文变短产生的孤儿片段清理放在下一部分。
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    collection.refresh_index()

    return {
        "doc_id": doc_id,
        "collection": COLLECTION_NAME,
        "count": len(ids),
        "chars_total": sum(c["chars"] for c in chunks),
        "ids": ids,
    }


# ---------------------------------------------------------------- 检索

# 只有「后端根本没有 hybrid search 这套东西」才允许回退；连接失败、SQL 出错一律往上抛，
# 否则 seekdb 挂掉会被伪装成「查无结果」，这是最坑人的一种静默失败。
_HYBRID_UNSUPPORTED_RE = re.compile(
    r"dbms_hybrid_search|hybrid[_\s-]*search.*(not|unsupport|unknown)|"
    r"procedure.*(hybrid|search).*does\s*not\s*exist",
    re.I,
)
# 关键词：连续的英文/数字串，或连续的中文片段；取最长的那个
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*|[\u4e00-\u9fff]+")


def pick_keyword(query: str) -> str:
    tokens = _TOKEN_RE.findall(query or "")
    return max(tokens, key=len) if tokens else ""


def _is_hybrid_unsupported(exc: Exception) -> bool:
    return isinstance(exc, (AttributeError, NotImplementedError)) or bool(
        _HYBRID_UNSUPPORTED_RE.search(str(exc))
    )


def _rows(raw: dict) -> list[dict]:
    """把 SDK 的 [[...]] 双层结果拍平成逐条记录。少取一层会得到空列表。"""
    ids = (raw.get("ids") or [[]])[0]
    metas = (raw.get("metadatas") or [[]])[0]
    docs = (raw.get("documents") or [[]])[0]
    dists = (raw.get("distances") or [[]])[0]
    rows = []
    for i, _id in enumerate(ids):
        meta = metas[i] if i < len(metas) and metas[i] else {}
        rows.append(
            {
                "id": _id,
                "metadata": meta or {},
                "document": docs[i] if i < len(docs) and docs[i] else "",
                "distance": dists[i] if i < len(dists) else None,
            }
        )
    return rows


def _to_hit(row: dict, meta: dict) -> dict:
    """document 存的是「标题 · 小节\\n正文」，展示时剥掉拼进去的前缀，只留正文。"""
    body = row["document"].split("\n", 1)[1] if "\n" in row["document"] else row["document"]
    title = meta.get("title") or ""
    heading = meta.get("heading") or title
    seq = int(meta.get("seq") or 1)
    snippet = body.replace("\n", " ").strip()
    if len(snippet) > SNIPPET_CHARS:
        snippet = snippet[:SNIPPET_CHARS].rstrip() + "…"
    return {
        "title": title,
        "heading": heading,
        "snippet": snippet,
        "source": f"{title} › {heading} › 片段 #{seq}",
        "distance": row["distance"],
    }


def search(query: str, k: int = 8, tag: str = "") -> dict:
    """一句话检索 -> {"query","mode","took_ms","count","results"}。

    hybrid 路：全文分支用 where_document $contains 抓关键词，向量分支用 knn.query_texts；
    tag 过滤必须两边各下一遍——只写一处会出现「向量结果没被过滤」的假象。
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    k = max(1, min(int(k), 50))
    where = {"tag": tag} if (tag or "").strip() else None
    started = time.perf_counter()

    collection = get_collection()
    mode = "hybrid"
    try:
        branch = {"where": where} if where else {}
        keyword = pick_keyword(query)
        fulltext = dict(branch)
        if keyword:
            fulltext["where_document"] = {"$contains": keyword}
        raw = collection.hybrid_search(
            query=fulltext or None,
            knn={**branch, "query_texts": [query]},
            rank={"rrf": {"rank_window_size": 60, "rank_constant": 60}},
            n_results=k,
            include=["metadatas", "documents"],
        )
    except Exception as exc:  # noqa: BLE001 - 只吞「后端不支持」，其余原样上抛给 HTTP 层
        if not _is_hybrid_unsupported(exc):
            raise
        mode = "vector"
        raw = collection.query(
            query_texts=[query],
            n_results=k,
            where=where,
            include=["metadatas", "documents"],
        )

    results = [_to_hit(row, row["metadata"]) for row in _rows(raw)][:k]
    return {
        "query": query,
        "mode": mode,
        "took_ms": round((time.perf_counter() - started) * 1000, 1),
        "count": len(results),
        "results": results,
    }


app = FastAPI(title="NoteSeek")


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1, description="一句话查询"),
    k: int = Query(8, ge=1, le=50),
    tag: str = Query(""),
):
    # 故意不 try/except：seekdb 不可用要冒泡成 500，不能退化成 count=0
    return search(q, k=k, tag=tag)


# 注册在 /api 之后，同路径前缀下已存在的路由优先匹配
app.mount(
    "/",
    StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), html=True),
    name="static",
)


# ---------------------------------------------------------------- 命令行


def _title_of(raw: str, path: Path) -> str:
    m = _H1_LINE_RE.search(raw)
    return m.group(1) if m else path.stem


def ingest_dir(dirpath: str, tag: str = "") -> dict:
    """导入目录下所有 .md，返回 {files, chunks, failed}。单篇失败不影响其余。"""
    root = Path(dirpath)
    if not root.is_dir():
        raise SystemExit(f"目录不存在：{dirpath}")

    files = sorted(root.rglob("*.md"))
    chunks = 0
    failed: list[tuple[str, str]] = []
    for path in files:
        rel = str(path.relative_to(root))
        try:
            raw = path.read_text(encoding="utf-8")
            res = ingest_document(_title_of(raw, path), raw, tag=tag)
        except Exception as exc:  # noqa: BLE001 - 逐篇报错，不让一篇坏文件中断整批
            failed.append((rel, f"{type(exc).__name__}: {exc}"))
            print(f"  {rel:34} -> 失败 {exc}")
            continue
        chunks += res["count"]
        print(f"  {rel:34} -> {res['count']:3} 片段  doc_id={res['doc_id']}")
    return {"files": len(files), "chunks": chunks, "failed": failed}


def _cli() -> int:
    parser = argparse.ArgumentParser(prog="python app.py", description="NoteSeek 命令行入口")
    parser.add_argument("--ingest", metavar="DIR", help="导入该目录下所有 .md 笔记")
    parser.add_argument("--tag", default="", help="写入 metadata.tag，检索时可按它过滤")
    args = parser.parse_args()

    if not args.ingest:
        parser.print_help()
        return 0

    print(f"导入 {args.ingest} -> collection {COLLECTION_NAME}（tag={args.tag or '无'}）")
    started = time.perf_counter()
    result = ingest_dir(args.ingest, tag=args.tag)
    print(
        f"完成：{result['files']} 篇 / {result['chunks']} 片段，"
        f"失败 {len(result['failed'])} 篇，耗时 {(time.perf_counter() - started) * 1000:.0f} ms"
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，中文输出会乱码，先把 stdout 拉回 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_cli())
