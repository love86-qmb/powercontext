"""验收：3 篇笔记 -> notes 集合，检查片段数与 heading 归属。

真库不可用时用内存 collection 顶替，断言的是 ingest_document 落库的同一份 payload。
"""

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
NOTES = sorted(glob.glob(os.path.join("data", "notes", "*.md")))
assert len(NOTES) == 3, NOTES


class MemCollection:
    def __init__(self):
        self.rows = {}
        self.refreshed = 0

    def upsert(self, ids, documents, metadatas, **kw):
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = (d, m)

    def refresh_index(self):
        self.refreshed += 1

    def count(self):
        return len(self.rows)


coll = MemCollection()
app._collection = coll

section_text: dict[str, str] = {}   # "文档标题||小节" -> 该小节正文（归一化）
doc_titles: list[str] = []

for path in NOTES:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    title = re.search(r"^#\s+(.+?)\s*$", raw, re.M).group(1)
    doc_titles.append(title)
    body = app._FRONTMATTER_RE.sub("", raw.replace("\r\n", "\n"))
    for heading, lines in app._iter_sections(body):
        text = "\n\n".join(app._iter_paragraphs(lines))
        section_text[f"{title}||{heading}"] = norm(text)
    res = app.ingest_document(title, raw, tag="cs101")
    print(f"{os.path.basename(path):26} -> {res['count']:2} 片段  doc_id={res['doc_id']}")

total = coll.count()
print(f"collection.count() = {total}   refresh_index 调用 {coll.refreshed} 次")

rows = [m for _, m in coll.rows.values()]
failures = []

# 验收 1：片段数量落在「十几条」
if not 12 <= total <= 19:
    failures.append(f"count={total} 不在十几条区间")

# 验收 2：每个片段都挂在真实小节上，绝不允许等于文档大标题
for m in rows:
    if m["heading"] == m["title"]:
        failures.append(f"heading 挂成了文档大标题: {m['title']} / seq={m['seq']}")
    if m["heading"] not in {k.split('||', 1)[1] for k in section_text}:
        failures.append(f"heading 不是任何 `##` 小节: {m['heading']!r}")

# 验收 3（核心）：片段正文必须完整落在自己小节的原文里。
# 跨节的尾巴一旦塞回 buffer，这一条立刻失配——它同时能抓错挂标题和错挂正文。
by_doc = {}
for _id, (doc, m) in coll.rows.items():
    by_doc.setdefault((m["title"], m["heading"]), []).append((m["seq"], doc))
for (title, heading), items in sorted(by_doc.items()):
    own = section_text.get(f"{title}||{heading}")
    if own is None:
        continue
    for seq, doc in sorted(items):
        body_part = doc.split("\n", 1)[1]  # document = "标题 · 小节\n正文"
        if norm(body_part) not in own:
            failures.append(f"正文跨节/错位: {title} > {heading} > 第{seq}段")

# 验收 4：正文里不该再出现任何 markdown 标题行（尤其一级大标题）
for _id, (doc, m) in coll.rows.items():
    body_part = doc.split("\n", 1)[1]
    for line in body_part.splitlines():
        if re.match(r"^#{1,6}\s", line.strip()):
            failures.append(f"正文含标题行 {line!r} @ {m['title']}/{m['heading']}/{m['seq']}")

# 验收 5：id 唯一 + 出处三元组齐全 + 段号在小节内从 1 连续递增
assert len(coll.rows) == sum(1 for _ in rows) == total
triples = {(m["doc_id"], m["title"], m["heading"], m["seq"]) for m in rows}
assert len(triples) == total, "出处三元组有重复"
assert all(m["added_at"] and m["tag"] == "cs101" and m["chars"] > 0 for m in rows)

per_section: dict = {}
for m in rows:
    per_section.setdefault((m["doc_id"], m["heading"]), []).append(m["seq"])
for (doc_id, heading), seqs in per_section.items():
    if sorted(seqs) != list(range(1, len(seqs) + 1)):
        failures.append(f"段号不是小节内连续: {heading} -> {sorted(seqs)} (doc={doc_id})")

# 验收 6：这批笔记必须至少触发一次「同小节切成多段 + 重叠」，否则上面的归属检查是空跑
split_sections = {k for k, v in per_section.items() if len(v) > 1}
if not split_sections:
    failures.append("没有任何小节被切成多段，重叠路径未被验证")
for _doc_id, heading in split_sections:
    items = sorted(
        ((m["seq"], doc) for (i, (doc, m)) in coll.rows.items() if m["heading"] == heading),
    )
    for (seq_prev, doc_prev), (seq_now, doc_now) in zip(items, items[1:]):
        tail = doc_now.split("\n", 1)[1].split("\n", 1)[0]
        if not tail or tail not in doc_prev:
            failures.append(f"第{seq_now}段开头不是上一段尾巴: {heading} / {tail[:18]}")

print("\n每个片段的出处（文档 > 小节 > 第几段）：")
for m in sorted(rows, key=lambda x: (x["title"], x["seq"])):
    flag = "  <-- 大标题" if m["heading"] == m["title"] else ""
    print(f"  {m['title']} > {m['heading']} > 第{m['seq']}段  ({m['chars']}字){flag}")

if failures:
    print("\nFAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"\n验收通过：{total} 条片段，{len(doc_titles)} 篇，无一挂在文档大标题上")
