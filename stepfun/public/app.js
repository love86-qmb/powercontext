/**
 * 端侧知识库前端：所有 embedding 都在这个文件里、在你的浏览器里算。
 *   - Transformers.js + ONNX Runtime Web，优先 WebGPU，失败自动降级 WASM
 *   - 文档 → 切块 → 批量向量化 → POST /api/ingest（只传向量与原文）
 *   - 查询 → 本地向量化 → POST /api/search（混合检索由 seekdb 完成）
 */

const PIPELINE_CDN = 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.5';
const DIMENSION = 384;
const CHUNK_CHARS = 480;
const CHUNK_OVERLAP = 60;
const KB_ID = 'kb-default';

const state = {
  extractor: null,
  backend: 'unknown',
  dtype: 'q8',
  model: '',
  chunks: 0,
  timings: {
    模型加载: [],
    文档向量化: [],
    写入seekdb: [],
    查询向量化: [],
    seekdb检索: [],
    端到端: [],
  },
};

const $ = (id) => document.getElementById(id);

function log(target, text) {
  const el = typeof target === 'string' ? $(target) : target;
  const line = document.createElement('div');
  line.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function record(stage, ms) {
  const list = state.timings[stage];
  if (!list) return;
  list.push(ms);
  renderTimings();
}

function median(list) {
  if (!list.length) return null;
  const sorted = [...list].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function renderTimings() {
  const rows = $('timings').querySelectorAll('tr');
  rows.forEach((row, index) => {
    const stage = ['模型加载', '文档向量化', '写入seekdb', '查询向量化', 'seekdb检索', '端到端'][index];
    const list = state.timings[stage] || [];
    const last = list.length ? list[list.length - 1] : null;
    const med = median(list);
    row.children[1].textContent = last === null ? '-' : `${last.toFixed(1)} ms`;
    row.children[2].textContent = med === null ? '-' : `${med.toFixed(1)} ms`;
    row.children[3].textContent = String(list.length);
  });
}

// ------------------------------------------------------------------ 模型加载

async function pickBackend() {
  // 显式探测 WebGPU：有就优先用，没有就降级 WASM（题面要求「自动降级 + 界面提示」）
  try {
    if (navigator.gpu && (await navigator.gpu.requestAdapter())) return 'webgpu';
  } catch {
    /* 探测失败也走降级 */
  }
  return 'wasm';
}

async function loadModel() {
  const { pipeline, env } = await import(`${PIPELINE_CDN}`);
  env.allowLocalModels = false;
  env.useBrowserCache = true;

  state.model = $('model').value;
  state.dtype = $('dtype').value;
  const backend = await pickBackend();

  $('badge-status').textContent = '模型：加载中…';
  $('load').disabled = true;
  const started = performance.now();

  try {
    state.extractor = await pipeline('feature-extraction', state.model, {
      device: backend,
      dtype: state.dtype,
    });
    state.backend = backend;
  } catch (error) {
    // WebGPU 跑不起来时按题面要求自动降级
    if (backend === 'webgpu') {
      log('ingest-log', `WebGPU 初始化失败（${error.message}），自动降级到 WASM`);
      state.extractor = await pipeline('feature-extraction', state.model, {
        device: 'wasm',
        dtype: state.dtype,
      });
      state.backend = 'wasm';
    } else {
      throw error;
    }
  }

  const loadMs = performance.now() - started;
  record('模型加载', loadMs);

  $('badge-backend').textContent = `后端：${state.backend === 'webgpu' ? 'WebGPU' : 'WASM（降级）'}`;
  $('badge-backend').classList.toggle('warn', state.backend !== 'webgpu');
  $('badge-dtype').textContent = `精度：${state.dtype.toUpperCase()}`;
  $('badge-status').textContent = `模型：${state.model}（${DIMENSION} 维）`;
  $('load').disabled = false;
  $('ingest').disabled = false;
  $('search').disabled = false;
  $('bench').disabled = false;
  $('ingest-hint').textContent = '模型已就绪';
  log('ingest-log', `模型加载完成 ${loadMs.toFixed(0)} ms · 后端 ${state.backend} · ${state.dtype}`);
}

/** E5 系列要求 passage/query 前缀，不加会明显掉检索质量 */
function withPrefix(texts, kind) {
  if (!/e5/i.test(state.model)) return texts;
  const prefix = kind === 'query' ? 'query: ' : 'passage: ';
  return texts.map((text) => (text.startsWith(prefix) ? text : prefix + text));
}

/** 端侧推理的核心：把一个 batch 的文本变成 384 维归一化向量 */
async function embed(texts, onProgress, kind = 'passage') {
  const out = [];
  const batchSize = Number($('batch').value) || 8;
  const payload = withPrefix(texts, kind);
  for (let start = 0; start < payload.length; start += batchSize) {
    const slice = payload.slice(start, start + batchSize);
    const tensor = await state.extractor(slice, { pooling: 'mean', normalize: true });
    const dims = tensor.dims;
    const data = tensor.data;
    const width = dims[dims.length - 1];
    for (let row = 0; row < dims[0]; row += 1) {
      out.push(Array.from(data.slice(row * width, (row + 1) * width)));
    }
    if (onProgress) onProgress(Math.min(start + batchSize, texts.length), texts.length);
  }
  return out;
}

// ------------------------------------------------------------------ 切块

function headingBefore(lines, index) {
  for (let cursor = index; cursor >= 0; cursor -= 1) {
    if (/^#{1,6}\s/.test(lines[cursor])) return lines[cursor].replace(/^#{1,6}\s*/, '').trim();
  }
  return '';
}

function chunkText(title, text) {
  const lines = text.split('\n');
  const chunks = [];
  let buffer = [];
  let carry = '';
  let size = 0;
  let startIndex = 0;
  for (let index = 0; index < lines.length; index += 1) {
    // 起点必须跟着「这一块真正的开始行」走：只在 buffer 空时才更新 startIndex，
    // 否则重叠尾巴会让后面每一块都继承第一块的起点，出处全变成文档大标题。
    if (buffer.length === 0) {
      startIndex = index;
      if (carry) {
        buffer.push(carry);
        size = carry.length + 1;
      }
    }
    buffer.push(lines[index]);
    size += lines[index].length + 1;
    if (size >= CHUNK_CHARS) {
      const body = buffer.join('\n').trim();
      if (body) chunks.push({ heading: headingBefore(lines, startIndex), body });
      carry = body.slice(-CHUNK_OVERLAP);
      buffer = [];
      size = 0;
    }
  }
  const body = buffer.join('\n').trim();
  if (body) chunks.push({ heading: headingBefore(lines, startIndex), body });
  return chunks.map((chunk, seq) => ({
    id: `${slug(title)}-${seq}`,
    document: chunk.heading ? `${title} · ${chunk.heading}\n${chunk.body}` : `${title}\n${chunk.body}`,
    metadata: { kb_id: KB_ID, doc_id: slug(title), title, heading: chunk.heading, seq },
  }));
}

function slug(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || `doc${Date.now() % 100000}`;
}

// ------------------------------------------------------------------ 导入

async function collectDocs() {
  const docs = [];
  const files = $('files').files;
  for (const file of files) {
    docs.push({ title: file.name.replace(/\.[^.]+$/, ''), text: await file.text() });
  }
  const pasted = $('paste').value.trim();
  if (pasted) docs.push({ title: `粘贴的文本 ${new Date().toLocaleTimeString()}`, text: pasted });
  if (docs.length === 0) {
    log('ingest-log', '去载入内置语料，或选择/粘贴文本');
  }
  return docs;
}

async function loadSamples() {
  const response = await fetch('/samples/index.json');
  if (!response.ok) throw new Error('读不到 /samples/index.json');
  const files = await response.json();
  const docs = [];
  for (const name of files) {
    const text = await (await fetch(`/samples/corpus/${encodeURIComponent(name)}`)).text();
    docs.push({ title: name.replace(/\.[^.]+$/, ''), text });
  }
  return docs;
}

async function ingestDocs(docs) {
  if (!state.extractor) return log('ingest-log', '先加载模型');
  if (!docs.length) return;

  $('ingest').disabled = true;
  $('bar').style.width = '4%';

  const chunks = docs.flatMap((doc) => chunkText(doc.title, doc.text));
  log('ingest-log', `切块完成：${docs.length} 篇 → ${chunks.length} 片（≤${CHUNK_CHARS} 字符，重叠 ${CHUNK_OVERLAP}）`);

  const embedStart = performance.now();
  const vectors = await embed(
    chunks.map((chunk) => chunk.document),
    (done, total) => {
      $('bar').style.width = `${4 + Math.round((done / total) * 72)}%`;
      $('ingest-hint').textContent = `本地向量化 ${done}/${total}`;
    },
  );
  const embedMs = performance.now() - embedStart;
  record('文档向量化', embedMs);

  $('bar').style.width = '82%';
  const items = chunks.map((chunk, index) => ({ ...chunk, embedding: vectors[index] }));
  const writeStart = performance.now();
  const response = await fetch('/api/ingest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  });
  const result = await response.json();
  const writeMs = performance.now() - writeStart;
  record('写入seekdb', writeMs);

  if (!result.ok) {
    log('ingest-log', `写入失败：${result.error}`);
  } else {
    state.chunks = result.chunks;
    $('bar').style.width = '100%';
    log(
      'ingest-log',
      `写入 seekdb 成功：${result.written} 片 · 库内写入 ${result.writtenMs} ms · 刷新索引 ${result.refreshMs} ms · 当前 ${result.chunks} 片`,
    );
    log('ingest-log', `本机向量化耗时 ${embedMs.toFixed(0)} ms（吞吐 ${(chunks.length / (embedMs / 1000)).toFixed(1)} 片/秒）`);
  }
  $('ingest-hint').textContent = `知识库共 ${result.chunks ?? state.chunks} 片`;
  $('ingest').disabled = false;
  setTimeout(() => { $('bar').style.width = '0'; }, 800);
}

// ------------------------------------------------------------------ 检索

async function search(rawQuery) {
  const query = (rawQuery ?? $('query').value).trim();
  if (!query || !state.extractor) return;
  const endToEnd = performance.now();

  const embedStart = performance.now();
  const [vector] = await embed([query], null, 'query');
  record('查询向量化', performance.now() - embedStart);

  const searchStart = performance.now();
  const response = await fetch('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ queryText: query, queryEmbedding: vector, k: 5, kbId: KB_ID }),
  });
  const result = await response.json();
  record('seekdb检索', Number(result.tookMs ?? performance.now() - searchStart));
  record('端到端', performance.now() - endToEnd);

  if (!result.ok) {
    $('search-meta').textContent = `检索失败：${result.error}`;
    $('results').innerHTML = '';
    return;
  }

  $('search-meta').textContent =
    `命中 ${result.count} 条 · ${result.mode} · 关键词「${result.keyword}」· seekdb ${result.tookMs} ms`;

  const container = $('results');
  container.innerHTML = '';
  result.hits.forEach((hit, index) => {
    const card = document.createElement('article');
    card.className = 'hit';
    const title = document.createElement('h3');
    title.textContent = `${index + 1}. ${hit.title || hit.doc_id}`;
    const source = document.createElement('div');
    source.className = 'src';
    source.textContent = `出处：${hit.title || ''} › ${hit.heading || '正文'} › 片段 #${(hit.seq ?? 0) + 1} · 距离 ${hit.distance ?? '-'}`;
    const body = document.createElement('pre');
    body.textContent = hit.document;
    card.append(title, source, body);
    container.appendChild(card);
  });
  if (!result.hits.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = '没有命中。先把语料导入，或者换个说法。';
    container.appendChild(empty);
  }
  return result;
}

// ------------------------------------------------------------------ 基准

const EVAL_SET = [
  ['为什么 B+ 树比哈希索引更适合范围查询', '02-索引结构'],
  ['REPEATABLE READ 是怎么避免幻读的', '01-事务与隔离级别'],
  ['RRF 融合排序怎么算', '03-RRF融合排序'],
  ['WebGPU 和 WASM 后端有什么区别', '05-端侧推理与量化'],
  ['增量导入后为什么要刷新索引', '06-知识库工程笔记'],
  ['余弦距离和 L2 距离怎么选', '03-RRF融合排序'],
  ['INT8 量化对模型体积和精度的影响', '05-端侧推理与量化'],
  ['什么是 next-key lock', '01-事务与隔离级别'],
  ['覆盖索引为什么能避免回表', '02-索引结构'],
  ['seekdb 的混合检索怎么设置窗口', '03-RRF融合排序'],
];

async function runBench() {
  $('bench').disabled = true;
  let hits = 0;
  const latencies = [];
  for (const [question, expectedDoc] of EVAL_SET) {
    const started = performance.now();
    const result = await search(question);
    latencies.push(performance.now() - started);
    const top5 = (result?.hits || []).slice(0, 5).map((hit) => hit.title || '');
    const ok = top5.some((title) => title.includes(expectedDoc));
    if (ok) hits += 1;
    log('bench-log', `${ok ? '✓' : '✗'} ${question} → ${top5[0] || '(空)'}（期望 ${expectedDoc}）`);
  }
  const recall = hits / EVAL_SET.length;
  const med = median(latencies);
  log(
    'bench-log',
    `Recall@5 = ${(recall * 100).toFixed(1)}%（${hits}/${EVAL_SET.length}）· 端到端中位数 ${med.toFixed(0)} ms · 后端 ${state.backend} · ${state.dtype}`,
  );
  $('bench').disabled = false;
}

// ------------------------------------------------------------------ 事件绑定

$('load').addEventListener('click', () => loadModel().catch((error) => {
  $('badge-status').textContent = `模型：加载失败`;
  log('ingest-log', `加载失败：${error.message}`);
  $('load').disabled = false;
}));

$('load-samples').addEventListener('click', async () => {
  try {
    const docs = await loadSamples();
    log('ingest-log', `内置语料 ${docs.length} 篇已读入内存`);
    await ingestDocs(docs);
  } catch (error) {
    log('ingest-log', `载入内置语料失败：${error.message}`);
  }
});

$('ingest').addEventListener('click', async () => {
  try {
    await ingestDocs(await collectDocs());
  } catch (error) {
    log('ingest-log', `导入失败：${error.message}`);
    $('ingest').disabled = false;
  }
});

$('search').addEventListener('click', () => search());
$('query').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.isComposing) search();
});

$('bench').addEventListener('click', () => runBench());

$('reset').addEventListener('click', async () => {
  const response = await fetch('/api/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kbId: KB_ID }),
  });
  const result = await response.json();
  log('ingest-log', result.ok ? `知识库已清空，剩 ${result.chunks} 片` : `清空失败：${result.error}`);
  $('results').innerHTML = '';
  $('search-meta').textContent = '';
});

fetch('/api/health')
  .then((response) => response.json())
  .then((health) => {
    $('badge-status').textContent = health.ok ? `seekdb 已连接（${health.chunks} 片）` : 'seekdb 未连接';
    if (!health.ok) $('badge-status').classList.add('warn');
  })
  .catch(() => {
    $('badge-status').textContent = '后端未启动';
    $('badge-status').classList.add('warn');
  });
