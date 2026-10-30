/**
 * 端侧知识库 — 后端（只做两件事：把浏览器算好的向量写进 seekdb、把查询交给 seekdb 做混合检索）
 *
 * 端侧推理的边界很明确：**embedding 一律在浏览器里算**，本服务不加载任何模型、
 * 不调用云端推理 API，只接收向量。混合检索（向量路 + 全文路 + RRF 融合）完全由 seekdb 完成。
 *
 *   node server/index.mjs            默认 http://127.0.0.1:8090
 */

import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SeekdbClient } from 'seekdb';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(__dirname, '..', 'public');

const PORT = Number(process.env.PORT || 8090);
const COLLECTION = process.env.SEEKDB_COLLECTION || 'kb_chunks';
const DIMENSION = Number(process.env.EMBEDDING_DIM || 384);
const DEFAULT_KB = 'kb-default';

const client = new SeekdbClient({
  host: process.env.SEEKDB_HOST || '127.0.0.1',
  port: Number(process.env.SEEKDB_PORT || 2881),
  user: process.env.SEEKDB_USER || 'root',
  password: process.env.SEEKDB_PASSWORD || '',
  database: process.env.SEEKDB_DB || 'test',
});

let _collection = null;
async function collection() {
  if (_collection) return _collection;
  // embeddingFunction: null —— 向量一律由浏览器（端侧推理）算好传进来，
  // 库里不挂任何嵌入函数，也就不会去注册/下载服务端模型。
  _collection = await client.getOrCreateCollection({
    name: COLLECTION,
    dimension: DIMENSION,
    embeddingFunction: null,
  });
  return _collection;
}

const app = express();
app.use(express.json({ limit: '64mb' }));
app.use(express.static(PUBLIC));
// 内置语料给前端 fetch 用（浏览器本地读取、本地向量化，不经过服务端推理）
app.use('/samples', express.static(path.join(__dirname, '..', 'samples')));

const timer = () => {
  const started = process.hrtime.bigint();
  return () => Number(process.hrtime.bigint() - started) / 1e6;
};

/** 挑一个给全文路用的关键词：优先专有名词（B+ / INT8 / WebGPU / RRF），其次中文名词尾片段 */
const CJK_QUESTION_WORDS = /为什么|怎么样|怎么|如何|什么|哪些|是否|能不能|可以|请问|哪些|一下/g;

function keywordOf(text) {
  const words = (text.match(/[A-Za-z][A-Za-z0-9+#._-]{1,}/g) || []).filter((word) => word.length >= 2);
  // 带数字或大写的词更可能是专有名词，排前面
  words.sort((a, b) => {
    const score = (word) => (/[0-9A-Z+#]/.test(word) ? 10 : 0) + word.length;
    return score(b) - score(a);
  });
  if (words.length) return words[0];

  // 中文问题里的名词通常在句尾（「……为什么要刷新索引」），去掉疑问词与句尾语气词后，
  // 取**最后一段**的中文尾部 4 个字，这样 $contains 才可能真的在正文里命中；
  // 把整句当关键词是永远匹配不上的。
  const cleaned = text.replace(CJK_QUESTION_WORDS, '|');
  let runs = (cleaned.match(/[\u4e00-\u9fff]{2,}/g) || []).map((run) => run.replace(/[的了吗呢吧啊呀]$/, ''));
  runs = runs.filter((run) => run.length >= 2);
  const tail = runs.length ? runs[runs.length - 1] : '';
  if (tail) return tail.length > 4 ? tail.slice(-4) : tail;
  return text.slice(0, 8);
}

app.get('/api/health', async (_req, res) => {
  try {
    const coll = await collection();
    const total = await coll.count();
    res.json({
      ok: true,
      engine: 'seekdb (seekdb-js)',
      collection: COLLECTION,
      dimension: DIMENSION,
      chunks: Number(total),
      inference: 'browser-side only (this server never embeds)',
    });
  } catch (error) {
    res.status(500).json({ ok: false, error: `${error.name}: ${error.message}` });
  }
});

/** 写入：浏览器算好的向量 + 原文 + 元数据，一次批量 upsert，随后刷新索引 */
app.post('/api/ingest', async (req, res) => {
  const { items } = req.body || {};
  if (!Array.isArray(items) || items.length === 0) {
    return res.status(400).json({ ok: false, error: 'items 不能为空' });
  }
  for (const item of items) {
    if (!Array.isArray(item.embedding) || item.embedding.length !== DIMENSION) {
      return res.status(400).json({
        ok: false,
        error: `向量维度不对：期望 ${DIMENSION}，收到 ${Array.isArray(item.embedding) ? item.embedding.length : typeof item.embedding}`,
      });
    }
  }
  const elapsed = timer();
  try {
    const coll = await collection();
    await coll.upsert({
      ids: items.map((item) => item.id),
      documents: items.map((item) => item.document),
      embeddings: items.map((item) => item.embedding),
      metadatas: items.map((item) => item.metadata || {}),
    });
    const writtenMs = elapsed();
    const refresh = timer();
    await coll.refresh_index();
    const refreshMs = refresh();
    const total = await coll.count();
    console.log(`[ingest] ${items.length} chunks · write ${writtenMs.toFixed(1)}ms · refresh ${refreshMs.toFixed(1)}ms · total ${total}`);
    res.json({
      ok: true,
      written: items.length,
      writtenMs: Number(writtenMs.toFixed(1)),
      refreshMs: Number(refreshMs.toFixed(1)),
      chunks: Number(total),
    });
  } catch (error) {
    console.error('[ingest] failed', error);
    res.status(500).json({ ok: false, error: `${error.name}: ${error.message}` });
  }
});

/** 检索：向量路 + 全文路一起交给 seekdb，RRF 融合也在库里做 */
app.post('/api/search', async (req, res) => {
  const { queryText, queryEmbedding, k = 5, kbId = DEFAULT_KB } = req.body || {};
  if (!Array.isArray(queryEmbedding) || queryEmbedding.length !== DIMENSION) {
    return res.status(400).json({ ok: false, error: 'queryEmbedding 缺失或维度不对' });
  }
  const elapsed = timer();
  try {
    const coll = await collection();
    const where = { kb_id: kbId };
    const result = await coll.hybridSearch({
      query: {
        whereDocument: { $contains: keywordOf(queryText || '') },
        where,
        nResults: Math.max(k * 6, 30),
      },
      knn: {
        queryEmbeddings: [queryEmbedding],
        where,
        nResults: Math.max(k * 6, 30),
      },
      rank: { rrf: { rankWindowSize: 60, rankConstant: 60 } },
      nResults: k,
      include: ['documents', 'metadatas', 'distances'],
    });
    const ms = elapsed();
    const ids = (result.ids && result.ids[0]) || [];
    const documents = (result.documents && result.documents[0]) || [];
    const metadatas = (result.metadatas && result.metadatas[0]) || [];
    const distances = (result.distances && result.distances[0]) || [];
    const hits = ids.map((id, index) => ({
      id,
      document: documents[index] || '',
      distance: distances[index] === null || distances[index] === undefined ? null : Number(distances[index]),
      ...(metadatas[index] || {}),
    }));
    res.json({
      ok: true,
      mode: 'hybrid (vector + fulltext, RRF inside seekdb)',
      keyword: keywordOf(queryText || ''),
      tookMs: Number(ms.toFixed(1)),
      count: hits.length,
      hits,
    });
  } catch (error) {
    console.error('[search] failed', error);
    res.status(500).json({ ok: false, error: `${error.name}: ${error.message}` });
  }
});

/** 清空当前知识库（演示用，方便重复跑同一个验证样例） */
app.post('/api/reset', async (req, res) => {
  const { kbId = DEFAULT_KB } = req.body || {};
  try {
    const coll = await collection();
    const existing = await coll.get({ where: { kb_id: kbId } });
    const ids = (existing.ids && existing.ids[0]) || existing.ids || [];
    if (ids.length) await coll.delete({ ids });
    await coll.refresh_index();
    res.json({ ok: true, removed: ids.length, chunks: Number(await coll.count()) });
  } catch (error) {
    res.status(500).json({ ok: false, error: `${error.name}: ${error.message}` });
  }
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[kb] http://127.0.0.1:${PORT}  collection=${COLLECTION} dim=${DIMENSION}`);
});
