'use strict';

const $ = (id) => document.getElementById(id);
const editor = $('sql-editor');
const state = { token: '', tables: [], results: [], selected: 0, busy: false, connected: false, toastTimer: null, aiBusy: false, aiGeneration: 0, aiResult: null, aiStatus: null, compareBusy: false, integrityBusy: false, researchStatus: null, researchBusy: {}, researchRewrite: null };
const toolNames = ['ai', 'compare', 'integrity', 'research'];
const examples = [
  { name: '01 · 浏览学生数据', sql: "-- 浏览教学数据集中的学生\nSELECT id, name, department, year, gpa\nFROM students\nORDER BY id;" },
  { name: '02 · B+ 树索引查找', sql: "-- 主键等值查询：观察计划与索引计数\nEXPLAIN ANALYZE\nSELECT id, name, department, gpa\nFROM students\nWHERE id = 1005;" },
  { name: '03 · 索引范围查询', sql: "-- 少量候选的范围索引扫描；命中比例较高时优化器可能选择全表扫描\nEXPLAIN ANALYZE\nSELECT student_id, course_id, score\nFROM enrollments\nWHERE score >= 97 AND score <= 98\nORDER BY score DESC;" },
  { name: '04 · 三表连接', sql: "-- 从选课记录连接学生与课程\nSELECT s.name AS student, c.title AS course, e.score\nFROM students s\nJOIN enrollments e ON s.id = e.student_id\nJOIN courses c ON e.course_id = c.id\nWHERE e.score >= 90\nORDER BY e.score DESC, s.id;" },
  { name: '05 · 分组与聚合', sql: "-- 按专业汇总学生人数与平均绩点\nSELECT department, COUNT(*) AS students, AVG(gpa) AS avg_gpa\nFROM students\nGROUP BY department\nHAVING COUNT(*) >= 3\nORDER BY avg_gpa DESC;" },
  { name: '06 · 左连接与 NULL', sql: "-- 左连接保留所有学生；COUNT 忽略 NULL\nSELECT s.id, s.name, COUNT(e.id) AS course_count, AVG(e.score) AS avg_score\nFROM students s\nLEFT JOIN enrollments e ON s.id = e.student_id\nGROUP BY s.id, s.name\nORDER BY s.id;" },
  { name: '07 · 事务回滚演示', sql: "-- 查看事务内修改，回滚后验证原始分数\nBEGIN;\nUPDATE enrollments SET score = 100 WHERE id = 1;\nSELECT id, score FROM enrollments WHERE id = 1;\nROLLBACK;\nSELECT id, score FROM enrollments WHERE id = 1;" },
  { name: '08 · 元数据与缓冲区', sql: "-- 数据目录与表结构\nSHOW TABLES;\nDESCRIBE students;\nDESCRIBE enrollments;" },
  { name: '09 · 可信改写（小表）', sql: "-- 研究实验 → 可信改写：规则可证明，但 12 行小表收益低于噪声门槛\nSELECT id, name, gpa\nFROM students\nWHERE department = '信息安全' OR department = '软件工程';",
    candidates: [
      { name: '等价但无规则证明', sql: "SELECT id, name, gpa FROM students WHERE department IN ('软件工程', '信息安全') AND id > 0;" },
      { name: '错误候选（少一个专业）', sql: "SELECT id, name, gpa FROM students WHERE department = '信息安全';" },
    ] },
];
// Each candidate pairs with its preset: rule-derived (empty), equivalent but unproven, and wrong.
const chinookRewriteCandidates = [
  { name: '等价但无规则证明', sql: "SELECT TrackId, Name, Milliseconds FROM Track WHERE GenreId IN (6, 2) AND Milliseconds >= 0;" },
  { name: '错误候选（少一个流派）', sql: "SELECT TrackId, Name, Milliseconds FROM Track WHERE GenreId = 2;" },
];
const chinookExamples = [
  { name: '01 · 艺术家与曲目 Top 10', sql: "-- Chinook：Artist → Album → Track 多表聚合\nSELECT ar.Name AS artist, COUNT(*) AS tracks\nFROM Artist ar\nJOIN Album al ON ar.ArtistId = al.ArtistId\nJOIN Track tr ON al.AlbumId = tr.AlbumId\nGROUP BY ar.Name\nORDER BY tracks DESC, artist\nLIMIT 10;" },
  { name: '02 · 流派曲目统计', sql: "-- Chinook：Track → Genre 连接与分组\nSELECT g.Name AS genre, COUNT(*) AS tracks, AVG(tr.UnitPrice) AS avg_price\nFROM Genre g\nJOIN Track tr ON g.GenreId = tr.GenreId\nGROUP BY g.Name\nHAVING COUNT(*) >= 10\nORDER BY tracks DESC, genre\nLIMIT 12;" },
  { name: '03 · 发票消费 Top 10', sql: "-- Chinook：Customer → Invoice → InvoiceLine → Track\nSELECT c.FirstName, c.LastName, SUM(il.UnitPrice * il.Quantity) AS amount\nFROM Customer c\nJOIN Invoice i ON c.CustomerId = i.CustomerId\nJOIN InvoiceLine il ON i.InvoiceId = il.InvoiceId\nGROUP BY c.CustomerId, c.FirstName, c.LastName\nORDER BY amount DESC, c.CustomerId\nLIMIT 10;" },
  { name: '04 · 索引执行计划', sql: "-- Chinook：按外键索引定位专辑\nEXPLAIN ANALYZE\nSELECT AlbumId, Title, ArtistId\nFROM Album\nWHERE ArtistId = 90\nORDER BY AlbumId;" },
  { name: '05 · 公开数据目录', sql: "-- Chinook：查看 11 张表和字段\nSHOW TABLES;\nDESCRIBE Artist;\nDESCRIBE Track;\nDESCRIBE InvoiceLine;" },
  { name: '06 · OR 条件的执行计划', sql: "-- Chinook：OR 条件无法使用 GenreId 索引，只能全表扫描\nEXPLAIN ANALYZE\nSELECT TrackId, Name, Milliseconds\nFROM Track\nWHERE GenreId = 2 OR GenreId = 6;" },
  { name: '07 · 可信改写：OR → IN', sql: "-- 研究实验 → 可信改写：候选留空，由本地规则生成并验证\nSELECT TrackId, Name, Milliseconds\nFROM Track\nWHERE GenreId = 2 OR GenreId = 6;",
    candidates: chinookRewriteCandidates },
];
let activeExamples = examples;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function toast(message) {
  clearTimeout(state.toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 2800);
}

function setConnection(connected, label) {
  state.connected = connected;
  const status = $('connection-status');
  status.replaceChildren(element('span', 'status-dot'), document.createTextNode(label));
  status.className = `connection-status ${connected ? 'ready' : 'failed'}`;
  $('connection-dot').classList.toggle('ready', connected);
}

async function api(path, payload, timeoutMs = 0) {
  const options = { cache: 'no-store', headers: { Accept: 'application/json' } };
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-Aurora-Token'] = state.token;
    options.body = JSON.stringify(payload);
  }
  const controller = timeoutMs ? new AbortController() : null;
  if (controller) options.signal = controller.signal;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    let response;
    try { response = await fetch(path, options); }
    catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，未收到完整结果。服务可能仍在处理，请稍后刷新状态。');
      setConnection(false, '连接已断开');
      throw new Error('无法连接 AuroraDB。请确认本机工作台服务正在运行，然后刷新页面。');
    }
    let data;
    try { data = await response.json(); }
    catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，未收到完整结果。服务可能仍在处理，请稍后刷新状态。');
      throw new Error(`服务返回了无效响应（HTTP ${response.status}）。`);
    }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `请求失败（HTTP ${response.status}）。`);
    return data;
  } finally { if (timer !== null) clearTimeout(timer); }
}

function aiRequestTimeout(status) {
  const seconds = status?.timeout_seconds;
  const valid = typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0.1 && seconds <= 300;
  return Math.min(315000, (valid ? seconds : 90) * 1000 + 15000);
}

const keywordPattern = /^(SELECT|FROM|WHERE|AND|OR|NOT|AS|ORDER|BY|ASC|DESC|GROUP|HAVING|LIMIT|OFFSET|DISTINCT|JOIN|INNER|LEFT|OUTER|ON|CREATE|DROP|TABLE|INDEX|UNIQUE|PRIMARY|KEY|INSERT|INTO|VALUES|UPDATE|SET|DELETE|BEGIN|TRANSACTION|COMMIT|ROLLBACK|EXPLAIN|ANALYZE|SHOW|TABLES|DESCRIBE|CHECKPOINT|INTEGER|INT|REAL|FLOAT|TEXT|VARCHAR|BOOLEAN|BOOL|NULL|IS|IN|LIKE|DEFAULT|TRUE|FALSE)$/i;
const functionPattern = /^(COUNT|SUM|AVG|MIN|MAX|COALESCE|LOWER|UPPER|LENGTH|ABS|ROUND)$/i;
function highlight() {
  const sql = editor.value;
  $('line-numbers').textContent = Array.from({ length: sql.split('\n').length }, (_, i) => i + 1).join('\n');
  const fragment = document.createDocumentFragment();
  const tokenPattern = /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$)|'(?:''|[^'])*(?:'|$)|"(?:""|[^"])*(?:"|$)|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z_0-9]*\b|[=<>+*/%!-])/g;
  let cursor = 0;
  for (const token of sql.matchAll(tokenPattern)) {
    fragment.append(document.createTextNode(sql.slice(cursor, token.index)));
    const value = token[0];
    let type = '';
    if (value.startsWith('--') || value.startsWith('/*')) type = 'comment';
    else if (value.startsWith("'") || value.startsWith('"')) type = 'string';
    else if (/^\d/.test(value)) type = 'number';
    else if (keywordPattern.test(value)) type = 'keyword';
    else if (functionPattern.test(value)) type = 'function';
    else if (/^[=<>+*/%!-]$/.test(value)) type = 'operator';
    fragment.append(type ? element('span', `syntax-${type}`, value) : document.createTextNode(value));
    cursor = token.index + value.length;
  }
  fragment.append(document.createTextNode(sql.slice(cursor) + '\n'));
  $('highlight-layer').replaceChildren(fragment);
  syncEditorScroll();
  updateSelectionHint();
}

function syncEditorScroll() {
  $('highlight-layer').scrollTop = editor.scrollTop;
  $('highlight-layer').scrollLeft = editor.scrollLeft;
  $('line-numbers').scrollTop = editor.scrollTop;
}

function updateSelectionHint() {
  const count = editor.selectionEnd - editor.selectionStart;
  $('editor-hint').textContent = count ? `执行选区 · ${count} 个字符` : '执行整个编辑器';
}

function setSQL(sql, focus = true) {
  editor.value = sql;
  editor.scrollTop = 0;
  editor.scrollLeft = 0;
  highlight();
  if (focus) { editor.focus(); editor.setSelectionRange(0, 0); }
}

function insertIdentifier(identifier) {
  const start = editor.selectionStart;
  const end = editor.selectionEnd;
  editor.setRangeText(identifier, start, end, 'end');
  editor.focus();
  highlight();
}

function renderSchema(tables) {
  state.tables = Array.isArray(tables) ? tables : [];
  const container = $('schema-list');
  const openNames = new Set(Array.from(container.querySelectorAll('.table-toggle.expanded')).map(node => node.dataset.name));
  container.replaceChildren();
  if (!state.tables.length) {
    container.append(element('p', 'schema-empty', '数据库中暂无表。可以编写 CREATE TABLE，或使用 --demo 启动教学数据集。'));
    return;
  }
  for (const [index, table] of state.tables.entries()) {
    const name = String(table.name || table.table || `table_${index + 1}`);
    const wrapper = element('div', 'schema-table');
    const toggle = element('button', 'table-toggle');
    toggle.type = 'button';
    toggle.dataset.name = name;
    const opened = openNames.has(name);
    toggle.classList.toggle('expanded', opened);
    toggle.setAttribute('aria-expanded', String(opened));
    toggle.setAttribute('aria-controls', `schema-columns-${index}`);
    toggle.append(element('span', 'table-glyph', '▦'), element('span', '', name));
    if (Number.isFinite(table.row_count)) toggle.append(element('span', 'table-row-count', `${table.row_count}`));
    toggle.append(element('span', 'table-chevron', '›'));
    const columns = element('div', 'schema-columns');
    columns.id = `schema-columns-${index}`;
    columns.hidden = !opened;
    for (const column of table.columns || []) {
      const item = element('button', 'column-item');
      item.type = 'button';
      const columnName = String(column.name);
      const indexed = (table.indexes || []).some(item => item.column === column.name);
      item.title = `${name}.${columnName} · ${column.type}${column.primary ? ' · PRIMARY KEY' : ''}${column.not_null ? ' · NOT NULL' : ''}${indexed ? ' · INDEX' : ''}（点击插入字段）`;
      item.append(element('span', 'column-key', column.primary ? '◆' : indexed ? '◇' : '·'), element('span', '', columnName), element('span', 'column-type', column.type || ''));
      item.addEventListener('click', () => insertIdentifier(`${name}.${columnName}`));
      columns.append(item);
    }
    toggle.addEventListener('click', () => {
      const expanded = toggle.classList.toggle('expanded');
      toggle.setAttribute('aria-expanded', String(expanded));
      columns.hidden = !expanded;
    });
    wrapper.append(toggle, columns);
    container.append(wrapper);
  }
}

function getMetric(stats, keys) {
  const sources = [stats, stats.cache, stats.buffer, stats.storage, stats.pager, stats.buffer_pool];
  for (const source of sources) {
    if (!source || typeof source !== 'object') continue;
    for (const key of keys) if (typeof source[key] === 'number' && Number.isFinite(source[key])) return source[key];
  }
  return null;
}

function renderStats(stats) {
  const sources = [stats, stats.cache, stats.buffer, stats.storage, stats.pager, stats.buffer_pool].filter(source => source && typeof source === 'object');
  const cachePolicy = sources.find(source => typeof source.cache_policy === 'string')?.cache_policy;
  const policyLabel = cachePolicy === 'predictive' ? '预测策略缓冲池' : cachePolicy === 'lru' ? 'LRU 缓冲池' : '缓冲池';
  const hits = getMetric(stats, ['cache_hits', 'hits', 'buffer_hits']);
  const misses = getMetric(stats, ['cache_misses', 'misses', 'buffer_misses']);
  const reads = getMetric(stats, ['disk_reads', 'page_reads', 'reads']);
  const writes = getMetric(stats, ['disk_writes', 'page_writes', 'writes']);
  const pages = getMetric(stats, ['cache_used', 'cached_pages', 'cache_pages', 'buffer_pages']);
  const capacity = getMetric(stats, ['cache_size', 'cache_capacity', 'capacity']);
  const format = (value) => value === null ? '—' : value.toLocaleString('zh-CN');
  $('metric-hit').replaceChildren(document.createTextNode(hits !== null && misses !== null && hits + misses > 0 ? (100 * hits / (hits + misses)).toFixed(1) : '—'), element('small', '', '%'));
  $('metric-hit-detail').textContent = hits !== null && misses !== null ? `${format(hits)} 命中 / ${format(misses)} 未命中` : '引擎未提供命中统计';
  $('metric-pages').replaceChildren(document.createTextNode(format(pages)), ...(capacity !== null ? [element('small', '', `/ ${format(capacity)}`)] : []));
  $('metric-pages-detail').textContent = capacity !== null ? `${policyLabel} · ${capacity * 4} KiB 容量` : policyLabel;
  $('metric-reads').textContent = format(reads);
  $('metric-writes').textContent = format(writes);
  const transaction = sources.find(source => typeof source.transaction_active === 'boolean');
  const badge = $('transaction-badge');
  badge.textContent = transaction ? transaction.transaction_active ? '事务进行中' : '自动提交' : '事务状态待更新';
  badge.classList.toggle('active', Boolean(transaction?.transaction_active));
  $('transaction-pages').textContent = `暂存页 ${format(getMetric(stats, ['staged_pages']))}`;
}

async function refreshMetadata() {
  const results = await Promise.allSettled([api('/api/schema'), api('/api/stats')]);
  if (results[0].status === 'fulfilled') renderSchema(results[0].value.tables || results[0].value);
  if (results[1].status === 'fulfilled') renderStats(results[1].value);
  const failed = results.find(result => result.status === 'rejected');
  if (failed) throw failed.reason;
}

function formatNumber(value, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: digits }) : '—';
}

function showEmptyResult(title, description) {
  $('table-container').hidden = true;
  $('table-container').replaceChildren();
  const empty = $('result-empty');
  empty.hidden = false;
  empty.replaceChildren(element('h3', '', title), element('p', '', description));
}

function renderTable(result) {
  const columns = Array.isArray(result.columns) ? result.columns : [];
  const rows = Array.isArray(result.rows) ? result.rows : [];
  if (!columns.length) {
    showEmptyResult(result.message || '语句执行成功', '该语句没有返回数据表。');
    return;
  }
  if (!rows.length) {
    showEmptyResult('查询成功，返回 0 行', `结果字段：${columns.join(' · ')}`);
    return;
  }
  $('result-empty').hidden = true;
  const container = $('table-container');
  container.hidden = false;
  container.replaceChildren();
  const table = element('table', 'result-table');
  table.setAttribute('aria-label', 'SQL 查询结果');
  const header = element('thead');
  const headerRow = element('tr');
  const numberHeader = element('th', 'row-number', '#');
  numberHeader.scope = 'col';
  headerRow.append(numberHeader);
  for (const column of columns) { const th = element('th', '', column); th.scope = 'col'; headerRow.append(th); }
  header.append(headerRow);
  const body = element('tbody');
  for (const [index, row] of rows.slice(0, 2000).entries()) {
    const tr = element('tr');
    tr.append(element('td', 'row-number', index + 1));
    for (const value of row) {
      const td = element('td');
      if (value === null) td.append(element('span', 'null-cell', 'NULL'));
      else {
        td.textContent = typeof value === 'object' ? JSON.stringify(value) : String(value);
        if (typeof value === 'number') td.classList.add('numeric-cell');
        if (typeof value === 'boolean') td.classList.add('boolean-cell');
        if (td.textContent.length > 70) td.title = td.textContent;
      }
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(header, body);
  container.append(table);
  if (rows.length > 2000) container.append(element('div', 'table-caption', `共 ${rows.length.toLocaleString('zh-CN')} 行，工作台显示前 2,000 行。请使用 LIMIT 或 WHERE 缩小结果。`));
}

function renderPlan(plan) {
  const container = $('plan-view');
  container.replaceChildren();
  if (!plan) {
    const empty = element('div', 'empty-state');
    empty.append(element('h3', '', '暂无执行计划'), element('p', '', '使用 EXPLAIN 或 EXPLAIN ANALYZE 查看查询计划。'));
    container.append(empty);
    return;
  }
  const root = plan.operator ? plan : plan.plan || plan.root || plan;
  let count = 0;
  function draw(node, depth) {
    if (!node || typeof node !== 'object' || depth > 24 || count++ > 100) return element('div', 'plan-detail', '计划过深，已省略后续节点。');
    const group = element('div', '');
    const box = element('div', 'plan-node');
    const title = element('div', 'plan-operator');
    title.append(element('span', '', node.operator || node.type || 'Plan'));
    if (typeof node.estimated_rows === 'number') title.append(element('span', 'plan-estimate', `预计 ${formatNumber(node.estimated_rows)} 行`));
    box.append(title);
    if (node.detail) box.append(element('div', 'plan-detail', node.detail));
    group.append(box);
    if (Array.isArray(node.children) && node.children.length) {
      const children = element('div', 'plan-children');
      for (const child of node.children) children.append(draw(child, depth + 1));
      group.append(children);
    }
    return group;
  }
  const tree = element('div', 'plan-tree');
  tree.append(draw(root, 0));
  container.append(tree);
}

function selectResult(index) {
  state.selected = index;
  const result = state.results[index];
  if (!result) return;
  $('statement-select').value = String(index);
  const rows = result.rows || [];
  const count = result.row_count ?? rows.length;
  $('result-count').textContent = formatNumber(rows.length, 0);
  $('statement-message').textContent = result.message || `${count} 行`;
  $('result-footer-text').textContent = result.message || `${formatNumber(count, 0)} 行 · ${formatNumber(result.elapsed_ms, 3)} ms`;
  $('query-scanned').textContent = formatNumber(getMetric(result.stats || {}, ['rows_scanned']), 0);
  $('query-index').textContent = formatNumber(getMetric(result.stats || {}, ['index_lookups']), 0);
  renderTable(result);
  renderPlan(result.plan);
  populateSemanticColumns(result);
}

function renderResults(results) {
  state.results = Array.isArray(results) ? results : [];
  $('error-panel').hidden = true;
  const select = $('statement-select');
  select.replaceChildren();
  for (const [index, result] of state.results.entries()) {
    const option = element('option', '', `${index + 1} / ${state.results.length} · ${result.message || '查询结果'}`);
    option.value = String(index);
    select.append(option);
  }
  $('statement-bar').hidden = state.results.length <= 1;
  const elapsed = state.results.reduce((sum, result) => sum + (typeof result.elapsed_ms === 'number' ? result.elapsed_ms : 0), 0);
  $('execution-summary').textContent = `${state.results.length} 条语句 · ${formatNumber(elapsed, 3)} ms`;
  $('execution-summary').classList.add('success');
  if (state.results.length) {
    // Prefer the last data result so a trailing COMMIT does not hide the query.
    let dataIndex = state.results.length - 1;
    while (dataIndex >= 0 && !state.results[dataIndex].columns?.length) dataIndex--;
    selectResult(dataIndex >= 0 ? dataIndex : state.results.length - 1);
  } else showEmptyResult('没有可执行语句', '请输入一条 SQL 语句。');
}

function showError(error) {
  state.results = [];
  $('error-message').textContent = error.message || String(error);
  $('error-panel').hidden = false;
  $('statement-bar').hidden = true;
  $('execution-summary').textContent = '执行失败';
  $('execution-summary').classList.remove('success');
  $('result-count').textContent = '0';
  $('query-scanned').textContent = '—';
  $('query-index').textContent = '—';
  $('result-footer-text').textContent = '请检查错误提示后重新执行';
  showEmptyResult('检查语句后再试一次', '编辑器保留了你的 SQL。', '!');
  renderPlan(null);
  switchTab('results');
}

async function runQuery() {
  if (state.busy) return;
  const selection = editor.value.slice(editor.selectionStart, editor.selectionEnd);
  const sql = selection.trim() ? selection : editor.value;
  if (!sql.trim()) { toast('先写一条 SQL，再开始探索。'); editor.focus(); return; }
  state.busy = true;
  $('run-query').disabled = true;
  $('run-query').classList.add('running');
  $('run-label').textContent = '正在执行';
  $('execution-summary').textContent = '引擎执行中…';
  $('execution-summary').classList.remove('success');
  try {
    if (!state.token) state.token = (await api('/api/session')).token;
    const data = await api('/api/query', { sql });
    renderResults(data.results);
    setConnection(true, '本机引擎已连接');
    try { await refreshMetadata(); }
    catch (error) { toast(`SQL 已完成，统计刷新失败：${error.message}`); }
  } catch (error) {
    showError(error);
    // Failed writes can change transaction state; refresh real metadata as well.
    try { await refreshMetadata(); } catch (_) { /* Original SQL error remains visible. */ }
  } finally {
    state.busy = false;
    $('run-query').disabled = false;
    $('run-query').classList.remove('running');
    $('run-label').textContent = '执行 SQL';
  }
}

function switchTab(name) {
  for (const tab of ['results', 'plan']) {
    const active = tab === name;
    $(`tab-${tab}`).classList.toggle('active', active);
    $(`tab-${tab}`).setAttribute('aria-selected', String(active));
    $(`tab-${tab}`).tabIndex = active ? 0 : -1;
    $(`${tab}-view`).hidden = !active;
  }
}

function switchTool(name) {
  for (const tool of toolNames) {
    const active = tool === name;
    $(`tool-tab-${tool}`).classList.toggle('active', active);
    $(`tool-tab-${tool}`).setAttribute('aria-selected', String(active));
    $(`tool-tab-${tool}`).tabIndex = active ? 0 : -1;
    $(`tool-view-${tool}`).hidden = !active;
  }
  if (name === 'research' && !state.researchStatus && !state.researchBusy.status) refreshResearchStatus();
}

function describeAI(mode, model) {
  if (mode === 'mock') return '本地规则演示 · 未调用模型';
  if (mode === 'remote') return `学校 API · ${model || '模型名称未返回'}`;
  if (mode === 'disabled') return 'AI 未配置';
  return 'AI 来源未标明';
}

function updateAIButton() {
  $('run-ai').disabled = state.aiBusy || state.aiStatus?.available !== true;
  $('ai-run-label').textContent = state.aiBusy ? '正在获取建议…' : '获取建议';
  $('run-ai').classList.toggle('running', state.aiBusy);
}

async function refreshAIStatus() {
  const button = $('refresh-ai-status');
  button.disabled = true;
  try {
    const status = await api('/api/ai/status');
    state.aiStatus = status;
    $('ai-mode-badge').textContent = status.mode === 'mock' ? '本地规则演示' : status.mode === 'remote' ? status.available ? '学校 API' : '学校 API 未就绪' : '未配置';
    $('ai-mode-badge').className = `mode-badge ${status.mode === 'remote' && status.available ? 'remote' : status.mode === 'mock' ? 'mock' : ''}`;
    $('ai-model-label').textContent = status.mode === 'remote' ? status.model || '尚未设置模型' : status.mode === 'mock' ? '没有调用远程模型' : '在后端配置后重启服务';
    $('ai-status-message').textContent = status.message || (status.available ? '建议通过 AuroraDB 校验后展示；密钥仅由后端读取。' : 'AI 未启用。SQL 编辑、执行和验证工具可以照常使用。');
  } catch (error) {
    state.aiStatus = null;
    $('ai-mode-badge').textContent = '状态读取失败';
    $('ai-mode-badge').className = 'mode-badge';
    $('ai-model-label').textContent = '可点击右侧刷新重试';
    $('ai-status-message').textContent = error.message;
  } finally { button.disabled = false; updateAIButton(); }
}

function updateAIAction() {
  const hints = { generate: '根据问题与真实表结构生成只读查询。', explain: '读取上方编辑器的 SQL 与服务器编译计划，解释每一步。', optimize: '读取上方编辑器的 SQL，提供可验证的优化建议。' };
  $('ai-action-hint').textContent = hints[$('ai-action').value];
  $('ai-prompt').placeholder = $('ai-action').value === 'generate' ? '例如：查询绩点不低于 3.8 的学生，按绩点降序显示。' : '可选：补充想理解的问题或优化目标。';
}

function renderAIList(listId, sectionId, values) {
  const list = $(listId);
  list.replaceChildren();
  const items = Array.isArray(values) ? values.filter(value => typeof value === 'string' && value.trim()) : [];
  for (const item of items) list.append(element('li', '', item));
  $(sectionId).hidden = !items.length;
}

function renderAI(result) {
  state.aiResult = result;
  $('ai-error').hidden = true;
  $('ai-empty').hidden = true;
  $('ai-output').hidden = false;
  $('ai-output-provider').textContent = describeAI(result.mode, result.model);
  $('ai-explanation').textContent = typeof result.explanation === 'string' ? result.explanation : '服务未返回说明。';
  const sql = typeof result.sql === 'string' ? result.sql : '';
  $('ai-sql').textContent = sql;
  $('ai-sql-section').hidden = !sql.trim();
  const validation = result.validation || {};
  const canImport = Boolean(sql.trim()) && validation.valid === true && validation.read_only === true;
  $('import-ai-sql').disabled = !canImport;
  const badge = $('ai-validation-badge');
  badge.textContent = canImport ? '只读校验通过' : validation.valid === true ? '已校验 · 非只读' : '校验未通过';
  badge.className = `validation-badge ${canImport ? 'valid' : 'invalid'}`;
  $('ai-validation-message').textContent = validation.valid !== true ? `未通过校验，无法一键填入。${typeof validation.error === 'string' ? ` ${validation.error}` : ' 请核对 SQL 与表结构。'}` : validation.read_only !== true ? '建议包含非只读语句，无法一键填入。请人工审阅后决定是否使用。' : `校验为只读 ${validation.kind || '查询'}。填入只替换编辑器内容，仍需点击“执行 SQL”运行。`;
  renderAIList('ai-suggestions', 'ai-suggestions-section', result.suggestions);
  renderAIList('ai-warnings', 'ai-warnings-section', result.warnings);
  $('ai-validation-details').hidden = !validation.plan;
  $('ai-validation-plan').textContent = validation.plan ? JSON.stringify(validation.plan, null, 2) : '';
  $('ai-validation-details').open = false;
}

function clearAI() {
  state.aiGeneration += 1;
  state.aiResult = null;
  $('ai-prompt').value = '';
  $('ai-error').hidden = true;
  $('ai-output').hidden = true;
  $('ai-sql').textContent = '';
  $('import-ai-sql').disabled = true;
  $('ai-empty').hidden = false;
  $('ai-empty').textContent = state.aiBusy ? '已清空。此前请求仍在结束中，返回内容不会覆盖这里。' : '描述想查询的内容，或选择解释、优化上方编辑器中的 SQL。';
}

async function runAI() {
  if (state.aiBusy || state.aiStatus?.available !== true) return;
  const action = $('ai-action').value;
  const prompt = $('ai-prompt').value.trim();
  const sql = editor.value;
  if (sql.length > 32768) { toast('AI 助手最多读取 32,768 字符 SQL，请缩短当前编辑器内容。'); return; }
  if (action === 'generate' && !prompt) { toast('请先描述想查询的内容。'); $('ai-prompt').focus(); return; }
  if (action !== 'generate' && !sql.trim()) { toast('请先在上方编辑器中输入 SQL。'); editor.focus(); return; }
  const generation = ++state.aiGeneration;
  state.aiBusy = true;
  state.aiResult = null;
  $('ai-error').hidden = true;
  $('ai-output').hidden = true;
  $('import-ai-sql').disabled = true;
  $('ai-empty').hidden = false;
  $('ai-empty').textContent = '正在获取建议并进行服务器校验。等待期间可以继续执行普通 SQL。';
  updateAIButton();
  try {
    if (!state.token) state.token = (await api('/api/session')).token;
    const result = await api('/api/ai', { action, prompt, sql }, aiRequestTimeout(state.aiStatus));
    if (generation !== state.aiGeneration) return;
    renderAI(result);
  } catch (error) {
    if (generation !== state.aiGeneration) return;
    $('ai-empty').hidden = true;
    $('ai-error').textContent = error.message;
    $('ai-error').hidden = false;
  } finally {
    state.aiBusy = false;
    updateAIButton();
    if (generation !== state.aiGeneration) $('ai-empty').textContent = '描述想查询的内容，或选择解释、优化上方编辑器中的 SQL。';
  }
}

function comparisonCard(label, result, extraMetrics = []) {
  const card = element('div', 'comparison-card');
  card.append(element('h4', '', label));
  const metrics = element('dl', 'comparison-metrics');
  for (const [name, value, suffix, digits] of [
    ['堆记录扫描', getMetric(result.stats || {}, ['rows_scanned']), '行', 0],
    ['文件读页', getMetric(result.stats || {}, ['disk_reads', 'file_reads', 'page_reads']), '页', 0],
    [result.repetitions ? '耗时中位数' : '查询耗时', result.elapsed_ms, 'ms', 3],
    ['返回结果', result.row_count, '行', 0],
    ...extraMetrics,
  ]) {
    const row = element('div');
    row.append(element('dt', '', name), element('dd', '', `${formatNumber(value, digits)} ${suffix}`));
    metrics.append(row);
  }
  card.append(metrics);
  if (Array.isArray(result.join_order)) {
    const names = { p: '学生', r: '选课', c: '课程' };
    card.append(element('p', 'tool-note', `连接顺序：${result.join_order.map(alias => names[alias] || alias).join(' → ')}`));
    if (Array.isArray(result.join_outputs)) card.append(element('p', 'tool-note', `各次连接输出：${result.join_outputs.map(value => formatNumber(value, 0)).join(' → ')} 行`));
  }
  if (result.plan) {
    const details = element('details', 'tool-details');
    details.append(element('summary', '', '查看执行计划'), element('pre', '', JSON.stringify(result.plan, null, 2)));
    card.append(details);
  }
  return card;
}

async function runComparison() {
  if (state.compareBusy) return;
  const selection = editor.value.slice(editor.selectionStart, editor.selectionEnd);
  const sql = selection.trim() ? selection : editor.value;
  if (!sql.trim()) { toast('先在编辑器中输入一条 SELECT。'); editor.focus(); return; }
  state.compareBusy = true;
  $('run-compare').disabled = true;
  $('run-compare').textContent = '正在对比…';
  $('compare-error').hidden = true;
  $('compare-output').hidden = true;
  $('compare-empty').hidden = false;
  $('compare-empty').textContent = '正在执行两条真实路径并核对完整结果…';
  try {
    if (!state.token) state.token = (await api('/api/session')).token;
    const result = await api('/api/compare', { sql });
    $('compare-empty').hidden = true;
    $('compare-output').hidden = false;
    $('compare-verdict').textContent = result.matched === true ? '✓ 两路结果一致' : '！两路结果不一致，请核对查询与存储结构';
    $('compare-verdict').className = `comparison-verdict ${result.matched === true ? 'valid' : 'invalid'}`;
    $('compare-sql').textContent = result.sql || sql;
    $('comparison-grid').replaceChildren(comparisonCard('强制全表扫描', result.scan || {}), comparisonCard('优化器选择', result.optimized || {}));
    const view = result.view === 'current transaction' ? '当前事务' : result.view === 'committed' ? '已提交数据' : '当前可见数据';
    $('compare-policy').textContent = `${view} · 按完整结果值及重复行核对，不要求同排序键内部顺序一致。对比使用不带 LIMIT / OFFSET 的单个 SELECT。`;
    try { await refreshMetadata(); } catch (error) { toast(`对比已完成，统计刷新失败：${error.message}`); }
  } catch (error) {
    $('compare-empty').hidden = true;
    $('compare-error').textContent = error.message;
    $('compare-error').hidden = false;
  } finally { state.compareBusy = false; $('run-compare').disabled = false; $('run-compare').textContent = '开始对比'; }
}

async function runIntegrity() {
  if (state.integrityBusy) return;
  state.integrityBusy = true;
  $('run-integrity').disabled = true;
  $('run-integrity').textContent = '正在检查…';
  $('integrity-error').hidden = true;
  $('integrity-output').hidden = true;
  $('integrity-empty').hidden = false;
  $('integrity-empty').textContent = '正在读取并验证当前可见的存储结构…';
  try {
    if (!state.token) state.token = (await api('/api/session')).token;
    const result = await api('/api/integrity', {});
    $('integrity-empty').hidden = true;
    $('integrity-output').hidden = false;
    $('integrity-verdict').textContent = result.ok === true ? '✓ 一致性检查通过' : '！发现需要核对的存储问题';
    $('integrity-verdict').className = `comparison-verdict ${result.ok === true ? 'valid' : 'invalid'}`;
    const summary = result.summary || {};
    const metrics = $('integrity-metrics');
    metrics.replaceChildren();
    for (const [key, label] of [['pages', '总页数'], ['tables', '表'], ['indexes', '索引'], ['rows', '记录'], ['reachable_pages', '可达页'], ['retained_pages', '保留页']]) {
      const item = element('div', 'integrity-metric');
      item.append(element('span', '', label), element('strong', '', formatNumber(summary[key], 0)));
      metrics.append(item);
    }
    const issues = $('integrity-issues');
    issues.replaceChildren();
    for (const issue of Array.isArray(result.issues) ? result.issues : []) {
      const item = element('li');
      item.append(element('span', 'issue-code', typeof issue === 'object' && issue ? issue.code || issue.severity || '问题' : '问题'), element('span', '', typeof issue === 'string' ? issue : issue?.message || JSON.stringify(issue)));
      issues.append(item);
    }
    const count = typeof result.issue_count === 'number' ? `${formatNumber(result.issue_count, 0)} 个问题` : '';
    $('integrity-detail').textContent = `${formatNumber(result.elapsed_ms, 3)} ms${count ? ` · ${count}` : ''}${result.issues_truncated ? ' · 问题列表已截断' : ''}。检查只读取数据；未自动修复或更改数据库。`;
    try { await refreshMetadata(); } catch (error) { toast(`检查已完成，统计刷新失败：${error.message}`); }
  } catch (error) {
    $('integrity-empty').hidden = true;
    $('integrity-error').textContent = error.message;
    $('integrity-error').hidden = false;
  } finally { state.integrityBusy = false; $('run-integrity').disabled = false; $('run-integrity').textContent = '检查一致性'; }
}

function populateSemanticColumns(result) {
  const list = $('research-semantic-columns');
  list.replaceChildren();
  for (const column of Array.isArray(result.columns) ? result.columns : []) {
    const option = element('option');
    option.value = String(column);
    list.append(option);
  }
}

function researchMode(value) {
  return ({ heuristic: '启发式', adaptive: '自适应', lru: 'LRU', predictive: '预测策略' })[value] || value || '—';
}

function researchCount(value) {
  if (typeof value === 'number') return formatNumber(value, 0);
  if (Array.isArray(value)) return formatNumber(value.length, 0);
  if (value && typeof value === 'object') return formatNumber(Object.keys(value).length, 0);
  return '—';
}

function renderResearchChips(id, entries) {
  const container = $(id);
  container.replaceChildren();
  for (const [label, value] of entries) {
    const chip = element('div', 'research-chip');
    chip.append(element('span', '', label), element('strong', '', value));
    container.append(chip);
  }
}

function updateResearchAvailability() {
  const status = state.researchStatus;
  const ai = status?.ai || state.aiStatus || {};
  const remote = ai.mode === 'remote' && ai.available === true;
  $('research-use-ai').disabled = !remote || Boolean(state.researchBusy.optimize);
  if (!remote) $('research-use-ai').checked = false;
  $('research-model-note').textContent = remote ? `${describeAI(ai.mode, ai.model)}。仅勾选时请求远程候选；不勾选时执行本地闭环。` : `${describeAI(ai.mode, ai.model)}。远程候选不可用，本地验证与实测仍可运行。`;
  const semantic = status?.semantic || {};
  const mode = semantic.mode || ai.mode;
  const available = typeof semantic.available === 'boolean' ? semantic.available : ['mock', 'remote'].includes(mode) && ai.available === true;
  $('run-research-semantic').disabled = !status || !available || Boolean(state.researchBusy.semantic);
  $('clear-research-semantic-cache').disabled = !status || Boolean(state.researchBusy.semantic);
  $('research-semantic-source').textContent = mode === 'mock' ? '本地关键词规则演示，不调用远程模型。切换远程模式后，仅本次选择的字段文本将发送给配置模型。' : mode === 'remote' ? `${describeAI(mode, semantic.model || ai.model)}。仅本次选择的字段文本将发送给配置模型；不会发送其他字段。` : '语义模型尚未启用。远程模式仅将本次选择的字段文本发送给配置模型；不会发送其他字段。';
}

function renderResearchStatus(status) {
  state.researchStatus = status;
  const optimizer = status.optimizer || {};
  const statistics = optimizer.statistics || {};
  const learning = optimizer.learning || {};
  const costModels = learning.cost_models && typeof learning.cost_models === 'object' ? Object.values(learning.cost_models).filter(model => model && typeof model === 'object') : null;
  const modelSamples = costModels && costModels.every(model => typeof model.samples === 'number' && Number.isFinite(model.samples)) ? costModels.reduce((sum, model) => sum + model.samples, 0) : learning.samples;
  const modelReady = costModels ? `${formatNumber(costModels.filter(model => model.ready === true).length, 0)} / ${formatNumber(costModels.length, 0)}` : typeof learning.ready === 'boolean' ? learning.ready ? '是' : '否' : researchCount(learning.ready);
  const cache = optimizer.cache || {};
  const buffer = status.buffer || {};
  const semantic = status.semantic || {};
  renderResearchChips('research-status-grid', [
    ['优化器', researchMode(optimizer.mode)],
    ['统计', statistics.available === true ? `已采集 · ${researchCount(statistics.tables)} 表` : statistics.available === false ? '未采集' : '—'],
    ['学习样本', researchCount(modelSamples)],
    ['学习就绪', modelReady],
    ['优化结论缓存', `${researchCount(cache.entries)} 条 / ${researchCount(cache.hits)} 命中`],
    ['缓存策略', researchMode(buffer.policy)],
    ['语义模式', describeAI(semantic.mode || status.ai?.mode, semantic.model || status.ai?.model)],
    ['实验版本', researchCount(optimizer.generation)],
    ['策略采样', researchCount(buffer.sample_size)],
    ['页历史', researchCount(buffer.history_pages)],
    ['推理缓存', `${researchCount(semantic.entries)} 条 / ${researchCount(semantic.hits)} 命中`],
    ['推理缓存有效期', typeof semantic.ttl_seconds === 'number' ? `${formatNumber(semantic.ttl_seconds, 0)} 秒` : '—'],
  ]);
  if (['heuristic', 'adaptive'].includes(optimizer.mode)) $('research-optimizer-mode').value = optimizer.mode;
  if (['lru', 'predictive'].includes(buffer.policy)) $('research-buffer-policy').value = buffer.policy;
  if (Number.isInteger(buffer.sample_size)) $('research-buffer-sample').value = String(buffer.sample_size);
  $('research-status-json').textContent = JSON.stringify(status, null, 2);
  $('research-status-error').hidden = true;
  updateResearchAvailability();
}

async function refreshResearchStatus() {
  if (state.researchBusy.status) return;
  state.researchBusy.status = true;
  $('refresh-research').disabled = true;
  try { renderResearchStatus(await api('/api/research/status')); }
  catch (error) {
    $('research-status-error').textContent = error.message;
    $('research-status-error').hidden = false;
  } finally { state.researchBusy.status = false; $('refresh-research').disabled = false; }
}

async function researchRequest(key, ids, errorId, path, payload, done, timeoutMs = 0) {
  if (state.researchBusy[key]) return;
  state.researchBusy[key] = true;
  for (const id of ids) {
    const button = $(id);
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
    button.disabled = true;
    button.textContent = '正在处理…';
  }
  $(errorId).hidden = true;
  updateResearchAvailability();
  try {
    if (!state.token) state.token = (await api('/api/session')).token;
    const result = await api(path, payload, timeoutMs);
    done(result);
    await refreshResearchStatus();
    try { await refreshMetadata(); } catch (error) { toast(`实验已完成，统计刷新失败：${error.message}`); }
  } catch (error) {
    $(errorId).textContent = error.message;
    $(errorId).hidden = false;
  } finally {
    state.researchBusy[key] = false;
    for (const id of ids) { $(id).disabled = false; $(id).textContent = $(id).dataset.idleLabel; }
    updateResearchAvailability();
  }
}

function readResearchInteger(id, label, maximum) {
  const value = Number($(id).value);
  if (!Number.isSafeInteger(value) || value < 1 || (maximum !== undefined && value > maximum)) {
    throw new Error(`${label}必须是 1${maximum === undefined ? ' 以上' : `–${maximum}`} 的整数。`);
  }
  return value;
}

function renderRewrite(result) {
  state.researchRewrite = result;
  const verification = result.verification || {};
  const recommendation = result.recommendation || {};
  const matched = verification.matched === true;
  const label = ({ proven: '已证明等价', tested: '当前提交数据实测一致', unknown: '等价性未确认' })[verification.level] || '等价性未确认';
  const pass = matched && ['proven', 'tested'].includes(verification.level);
  $('research-verification').textContent = `${pass ? '✓' : '！'} ${label}${verification.matched === false ? ' · 结果不一致' : ''}`;
  $('research-verification').className = `comparison-verdict ${pass ? 'valid' : 'invalid'}`;
  const origin = result.model_mode === 'local-rules' ? '本地验证与实测 · 未调用远程模型。' : result.model_mode ? describeAI(result.model_mode, state.researchStatus?.ai?.model) : '';
  $('research-verification-reason').textContent = [origin, verification.reason || '服务未返回验证说明。', result.model_explanation].filter(value => typeof value === 'string' && value).join('\n');
  $('research-candidate-output').textContent = typeof result.candidate_sql === 'string' ? result.candidate_sql : '';
  $('research-recommendation').textContent = `${recommendation.replace === true ? '建议替换：' : '保留原查询：'}${recommendation.reason || '没有替换建议。'}`;
  $('import-research-candidate').disabled = !(pass && recommendation.replace === true && typeof recommendation.sql === 'string' && recommendation.sql.trim());
  $('research-comparison').replaceChildren(comparisonCard('原查询', result.original || result.baseline || {}), comparisonCard('候选查询', result.candidate || {}));
  renderRewriteGates(verification, recommendation);
  const timings = result.timings || {};
  const cache = result.cache || {};
  renderResearchChips('research-rewrite-stats', [
    ['总耗时', `${formatNumber(timings.total_ms, 3)} ms`],
    ['编译', `${formatNumber(timings.compile_ms, 3)} ms`],
    ['验证', `${formatNumber(timings.validation_ms, 3)} ms`],
    ['模型调用', researchCount(result.model_calls)],
    ['模型耗时', `${formatNumber(result.model_elapsed_ms ?? timings.model_ms, 3)} ms`],
    ['结论缓存', typeof cache.hit === 'boolean' ? cache.hit ? '命中' : '未命中' : '—'],
  ]);
  const warnings = $('research-optimize-warnings');
  warnings.replaceChildren();
  for (const text of Array.isArray(result.warnings) ? result.warnings : []) warnings.append(element('li', '', text));
  $('research-optimize-json').textContent = JSON.stringify(result, null, 2);
  $('research-optimize-output').hidden = false;
}

const rewriteRuleNames = {
  'or-equalities-to-in': 'OR 等值合并为 IN',
  'duplicate-and': '重复 AND 消除',
  'where-true': 'WHERE TRUE 消除',
  'structural-identity': '结构相同',
};

// The three gates a candidate passes before replacement: rule evidence, full-result check, conservative benefit.
function renderRewriteGates(verification, recommendation) {
  const rules = Array.isArray(verification.rules) ? verification.rules : [];
  const ruleText = [...new Set(rules)].map(rule => rewriteRuleNames[rule] || rule).join('、');
  const benefit = recommendation.benefit_ms;
  const gates = [
    ['规则证据', rules.length ? 'pass' : 'fail',
      rules.length ? `可重放规则：${ruleText}` : '外部候选，没有可重放的规则推导'],
    ['结果核验', verification.matched === true ? 'pass' : verification.matched === false ? 'fail' : 'skip',
      verification.matched === true ? `完整结果一致（${verification.comparison === 'ordered-key-groups' ? '按排序键分组比较' : '多重集合比较'}）`
        : verification.matched === false ? '结果或输出列不同，候选被拒绝' : '未执行结果比较'],
    ['收益门槛', typeof benefit !== 'number' ? 'skip' : recommendation.replace === true ? 'pass' : 'fail',
      typeof benefit !== 'number' ? '只有通过前两关的已证明候选才评估收益'
        : `收益 ${formatNumber(benefit, 2)} ms ${recommendation.replace === true ? '≥' : '<'} 门槛 ${formatNumber(recommendation.margin_ms, 2)} ms${recommendation.speedup ? ` · ${formatNumber(recommendation.speedup, 2)}×` : ''}`],
  ];
  const list = $('research-gates');
  list.replaceChildren();
  for (const [index, [title, status, detail]] of gates.entries()) {
    const item = element('li', `research-gate ${status}`);
    item.append(element('span', 'gate-mark', status === 'pass' ? '✓' : status === 'fail' ? '✕' : '–'));
    const body = element('div', 'gate-body');
    body.append(element('strong', '', `${index + 1}. ${title}`), element('span', '', detail));
    item.append(body);
    list.append(item);
  }
}

async function runResearchOptimize() {
  if (state.researchBusy.optimize) return;
  const sql = editor.value;
  if (!sql.trim()) { toast('请先在上方编辑器中输入查询。'); editor.focus(); return; }
  const candidate = $('research-candidate').value.trim();
  const useAI = $('research-use-ai').checked;
  if (candidate && useAI) { $('research-optimize-error').textContent = '手动候选与远程候选请选择一种：清空候选 SQL，或取消远程模型勾选。'; $('research-optimize-error').hidden = false; return; }
  state.researchRewrite = null;
  $('research-optimize-output').hidden = true;
  $('import-research-candidate').disabled = true;
  const payload = { sql, use_ai: useAI };
  if (candidate) payload.candidate_sql = candidate;
  await researchRequest('optimize', ['run-research-optimize'], 'research-optimize-error', '/api/research/optimize', payload, renderRewrite, useAI ? aiRequestTimeout(state.researchStatus?.ai || state.aiStatus) : 0);
}

function renderSemanticTable(result, targetId = 'research-semantic-table', label = '语义查询结果') {
  const container = $(targetId);
  container.replaceChildren();
  const columns = Array.isArray(result.columns) ? result.columns : [];
  const rows = Array.isArray(result.rows) ? result.rows : [];
  if (!rows.length) { container.append(element('p', 'tool-note', '返回 0 行。')); return; }
  const table = element('table', 'result-table');
  table.setAttribute('aria-label', label);
  const head = element('thead');
  const tr = element('tr');
  for (const column of columns) { const th = element('th', '', column); th.scope = 'col'; tr.append(th); }
  head.append(tr);
  const body = element('tbody');
  for (const row of rows.slice(0, 500)) {
    const item = element('tr');
    for (const value of Array.isArray(row) ? row : []) item.append(element('td', value === null ? 'null-cell' : '', value === null ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : value));
    body.append(item);
  }
  table.append(head, body);
  container.append(table);
  if (rows.length > 500) container.append(element('p', 'tool-note', `显示前 500 行，实际返回 ${formatNumber(rows.length, 0)} 行。`));
}

async function runJoinDemo() {
  if (state.researchBusy.joinDemo) return;
  state.researchBusy.joinDemo = true;
  $('run-join-demo').disabled = true;
  $('run-join-demo').textContent = '正在运行…';
  $('join-demo-error').hidden = true;
  $('join-demo-output').hidden = true;
  $('join-demo-message').textContent = '正在执行策略';
  $('join-demo-message').hidden = false;
  try {
    if (!state.token) state.token = (await api('/api/session')).token;
    const result = await api('/api/research/join-demo', {}, 120000);
    $('join-demo-sql').textContent = result.sql;
    const matched = result.matched === true;
    $('join-demo-verdict').textContent = matched ? '✓ 完整结果一致（包含重复行核对）' : '！完整结果不一致，请检查执行证据';
    $('join-demo-verdict').className = `comparison-verdict ${matched ? 'valid' : 'invalid'}`;
    const cards = [['按 SQL 顺序执行（对照）', result.sql_order], ['自适应规划后执行', result.adaptive]];
    $('join-demo-comparison').replaceChildren(...cards.map(([label, run]) => comparisonCard(label, run, [
      ['连接候选比较', run.stats?.join_comparisons, '次', 0],
    ])));
    renderSemanticTable(result, 'join-demo-table', 'CS 学生选课查询结果');
    $('join-demo-json').textContent = JSON.stringify(result, null, 2);
    $('join-demo-output').hidden = false;
    $('join-demo-message').textContent = '';
    $('join-demo-message').hidden = true;
  } catch (error) {
    $('join-demo-message').textContent = '';
    $('join-demo-message').hidden = true;
    $('join-demo-error').textContent = error.message;
    $('join-demo-error').hidden = false;
  } finally {
    state.researchBusy.joinDemo = false;
    $('run-join-demo').disabled = false;
    $('run-join-demo').textContent = '运行用例对比';
  }
}

function renderSemantic(result) {
  const stats = result.stats || {};
  $('research-semantic-provider').textContent = describeAI(result.mode, result.model || state.researchStatus?.ai?.model);
  $('research-semantic-explanation').textContent = result.explanation || '';
  renderResearchChips('research-semantic-stats', [
    ['输入行', researchCount(result.input_rows)], ['返回行', researchCount(result.row_count)],
    ['模型调用', researchCount(stats.model_calls)], ['推理缓存命中', researchCount(stats.cache_hits)],
    ['推理缓存未命中', researchCount(stats.cache_misses)], ['批次数', researchCount(stats.batches)],
    ['耗时', `${formatNumber(stats.elapsed_ms, 3)} ms`], ['行数预算', researchCount(stats.budget_rows)],
  ]);
  renderSemanticTable(result);
  $('research-semantic-json').textContent = JSON.stringify(result, null, 2);
  $('research-semantic-output').hidden = false;
}

async function runResearchSemantic() {
  if (state.researchBusy.semantic) return;
  const sql = editor.value;
  const column = $('research-semantic-column').value.trim();
  const prompt = $('research-semantic-prompt').value.trim();
  if (!sql.trim() || !column || !prompt) { toast('请填写上方 SELECT、结果字段和判断条件。'); return; }
  let batchSize, maxRows, maxCalls, budgetSeconds;
  try {
    batchSize = readResearchInteger('research-semantic-batch', '每批行数', 16);
    maxRows = readResearchInteger('research-semantic-budget', '行数预算');
    maxCalls = readResearchInteger('research-semantic-calls', '调用预算', 32);
    budgetSeconds = readResearchInteger('research-semantic-seconds', '推理秒数', 90);
  }
  catch (error) { $('research-semantic-error').textContent = error.message; $('research-semantic-error').hidden = false; return; }
  $('research-semantic-output').hidden = true;
  $('research-semantic-cache-result').textContent = '';
  await researchRequest('semantic', ['run-research-semantic', 'clear-research-semantic-cache'], 'research-semantic-error', '/api/research/semantic', { sql, column, prompt, operation: $('research-semantic-operation').value, batch_size: batchSize, max_rows: maxRows, max_calls: maxCalls, budget_seconds: budgetSeconds }, renderSemantic, (budgetSeconds + 10) * 1000);
}

function populateExamples(nextExamples) {
  activeExamples = nextExamples;
  const select = $('example-select');
  select.replaceChildren(element('option', '', '选择 SQL 示例…'));
  select.options[0].value = '';  // the change handler resets to '' so the prompt shows again
  for (const [index, example] of activeExamples.entries()) {
    const option = element('option', '', example.name);
    option.value = String(index);
    select.append(option);
  }
}
populateExamples(examples);
$('example-select').addEventListener('change', (event) => {
  if (event.target.value === '') return;
  const example = activeExamples[Number(event.target.value)];
  setSQL(example.sql);
  populateRewriteCandidates(example.candidates);
  toast(example.candidates ? '示例已载入：在“研究实验 → 可信改写”中验证。' : '示例已载入，按 Ctrl + Enter 执行。');
  event.target.value = '';
});

function populateRewriteCandidates(candidates) {
  const select = $('research-candidate-example');
  select.replaceChildren(element('option', '', '演示候选…'));
  select.options[0].value = '';
  const entries = [{ name: '本地规则生成（留空）', sql: '' }, ...(Array.isArray(candidates) ? candidates : [])];
  for (const [index, candidate] of entries.entries()) {
    const option = element('option', '', candidate.name);
    option.value = String(index);
    select.append(option);
  }
  select.hidden = !Array.isArray(candidates);
  state.rewriteCandidates = entries;
  $('research-candidate').value = '';
}
$('research-candidate-example').addEventListener('change', (event) => {
  if (event.target.value === '') return;
  $('research-candidate').value = state.rewriteCandidates[Number(event.target.value)].sql;
  $('research-use-ai').checked = false;
  event.target.value = '';
});

// Theme: "system" follows prefers-color-scheme; light/dark pin the palette. theme.js applies it before first paint.
function showThemeChoice(choice) {
  const root = document.documentElement;
  if (choice === 'light' || choice === 'dark') root.dataset.theme = choice;
  else delete root.dataset.theme;
  for (const button of document.querySelectorAll('[data-theme-choice]')) button.setAttribute('aria-pressed', String(button.dataset.themeChoice === choice));
}
let savedTheme = 'system';
try { savedTheme = localStorage.getItem('aurora-theme') || 'system'; } catch (error) { /* storage unavailable */ }
showThemeChoice(['light', 'dark'].includes(savedTheme) ? savedTheme : 'system');
for (const button of document.querySelectorAll('[data-theme-choice]')) {
  button.addEventListener('click', () => {
    showThemeChoice(button.dataset.themeChoice);
    try { localStorage.setItem('aurora-theme', button.dataset.themeChoice); } catch (error) { /* storage unavailable */ }
  });
}
$('run-query').addEventListener('click', runQuery);
editor.addEventListener('input', highlight);
editor.addEventListener('scroll', syncEditorScroll);
editor.addEventListener('select', updateSelectionHint);
editor.addEventListener('keyup', updateSelectionHint);
editor.addEventListener('mouseup', updateSelectionHint);
editor.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); runQuery(); }
  if (event.key === 'Tab' && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault();
    editor.setRangeText('    ', editor.selectionStart, editor.selectionEnd, 'end');
    highlight();
  }
});
$('clear-sql').addEventListener('click', () => setSQL(''));
$('copy-sql').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(editor.value); toast('SQL 已复制。'); }
  catch (_) { editor.focus(); editor.select(); toast('浏览器未允许复制，已选中 SQL，可按 Ctrl + C。'); }
});
$('help-button').addEventListener('click', () => {
  $('help-panel').hidden = !$('help-panel').hidden;
  $('help-button').setAttribute('aria-expanded', String(!$('help-panel').hidden));
});
$('refresh-schema').addEventListener('click', async () => {
  $('refresh-schema').disabled = true;
  try { await refreshMetadata(); setConnection(true, '本机引擎已连接'); toast('结构与缓冲区统计已更新。'); }
  catch (error) { toast(error.message); }
  finally { $('refresh-schema').disabled = false; }
});
$('toggle-schema').addEventListener('click', () => {
  const expanded = $('toggle-schema').getAttribute('aria-expanded') !== 'true';
  $('toggle-schema').setAttribute('aria-expanded', String(expanded));
  document.querySelector('.sidebar').classList.toggle('mobile-expanded', expanded);
});
$('statement-select').addEventListener('change', event => selectResult(Number(event.target.value)));
for (const name of ['results', 'plan']) {
  $(`tab-${name}`).addEventListener('click', () => switchTab(name));
  $(`tab-${name}`).addEventListener('keydown', event => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'Home' ? 'results' : event.key === 'End' ? 'plan' : name === 'results' ? 'plan' : 'results';
      switchTab(next);
      $(`tab-${next}`).focus();
    }
  });
}

for (const name of toolNames) {
  $(`tool-tab-${name}`).addEventListener('click', () => switchTool(name));
  $(`tool-tab-${name}`).addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = toolNames.indexOf(name);
    const next = event.key === 'Home' ? toolNames[0] : event.key === 'End' ? toolNames[toolNames.length - 1] : toolNames[(index + (event.key === 'ArrowRight' ? 1 : toolNames.length - 1)) % toolNames.length];
    switchTool(next);
    $(`tool-tab-${next}`).focus();
  });
}
$('ai-action').addEventListener('change', updateAIAction);
$('refresh-ai-status').addEventListener('click', refreshAIStatus);
$('run-ai').addEventListener('click', runAI);
$('clear-ai').addEventListener('click', clearAI);
$('copy-ai-sql').addEventListener('click', async () => {
  const sql = typeof state.aiResult?.sql === 'string' ? state.aiResult.sql : '';
  if (!sql) return;
  try { await navigator.clipboard.writeText(sql); toast('建议 SQL 已复制。'); }
  catch (_) {
    window.getSelection()?.selectAllChildren($('ai-sql'));
    toast('浏览器未允许复制，已选中建议，可按 Ctrl + C。');
  }
});
$('import-ai-sql').addEventListener('click', () => {
  const result = state.aiResult;
  if (!result?.sql || result.validation?.valid !== true || result.validation?.read_only !== true) return;
  setSQL(result.sql);
  toast('只读建议已填入编辑器，点击执行 SQL 后才会运行。');
});
$('run-compare').addEventListener('click', runComparison);
$('run-integrity').addEventListener('click', runIntegrity);

$('refresh-research').addEventListener('click', refreshResearchStatus);
$('toggle-join-demo').addEventListener('click', () => {
  const expanded = $('research-join-demo').hidden;
  $('research-join-demo').hidden = !expanded;
  $('toggle-join-demo').setAttribute('aria-expanded', String(expanded));
});
$('run-join-demo').addEventListener('click', runJoinDemo);
$('run-research-optimize').addEventListener('click', runResearchOptimize);
$('copy-research-candidate').addEventListener('click', async () => {
  const sql = state.researchRewrite?.candidate_sql;
  if (typeof sql !== 'string' || !sql.trim()) return;
  try { await navigator.clipboard.writeText(sql); toast('候选 SQL 已复制。'); }
  catch (_) { window.getSelection()?.selectAllChildren($('research-candidate-output')); toast('浏览器未允许复制，已选中候选，可按 Ctrl + C。'); }
});
$('import-research-candidate').addEventListener('click', () => {
  const result = state.researchRewrite;
  if (result?.recommendation?.replace !== true || result.verification?.matched !== true || !['proven', 'tested'].includes(result.verification?.level) || typeof result.recommendation.sql !== 'string' || !result.recommendation.sql.trim()) return;
  setSQL(result.recommendation.sql);
  toast('已验证的建议已填入编辑器，点击执行 SQL 后才会运行。');
});
$('run-research-analyze').addEventListener('click', async () => {
  $('research-optimizer-result').textContent = '';
  await researchRequest('optimizer', ['apply-research-optimizer', 'run-research-analyze'], 'research-optimizer-error', '/api/research/analyze', {}, result => {
    $('research-optimizer-result').textContent = `已采集 ${researchCount(result.table_count)} 张表，实验版本 ${researchCount(result.generation)}，耗时 ${formatNumber(result.elapsed_ms, 3)} ms。`;
    $('research-analyze-json').textContent = JSON.stringify(result, null, 2);
    $('research-analyze-detail').hidden = false;
  });
});
$('apply-research-optimizer').addEventListener('click', async () => {
  const mode = $('research-optimizer-mode').value;
  $('research-optimizer-result').textContent = '';
  await researchRequest('optimizer', ['apply-research-optimizer', 'run-research-analyze'], 'research-optimizer-error', '/api/research/optimizer', { mode }, result => {
    const actual = result.mode || result.optimizer?.mode;
    $('research-optimizer-result').textContent = actual ? `当前优化器：${researchMode(actual)}。后续查询使用此模式。` : '模式设置已返回，请在实验状态中查看当前模式。';
  });
});
$('apply-research-buffer').addEventListener('click', async () => {
  let sampleSize;
  try { sampleSize = readResearchInteger('research-buffer-sample', '采样数', 4096); }
  catch (error) { $('research-buffer-error').textContent = error.message; $('research-buffer-error').hidden = false; return; }
  const policy = $('research-buffer-policy').value;
  $('research-buffer-result').textContent = '';
  await researchRequest('buffer', ['apply-research-buffer'], 'research-buffer-error', '/api/research/buffer', { policy, sample_size: sampleSize }, result => {
    const actual = result.policy || result.buffer?.policy;
    const sample = result.sample_size ?? result.buffer?.sample_size;
    $('research-buffer-result').textContent = actual ? `当前缓存策略：${researchMode(actual)}，采样数 ${researchCount(sample)}。` : '策略设置已返回，请在实验状态中查看当前策略。';
  });
});
$('load-research-semantic-example').addEventListener('click', () => {
  setSQL('SELECT id, title FROM courses ORDER BY id;');
  $('research-semantic-column').value = 'title';
  $('research-semantic-prompt').value = '关键词：数据库|查询|索引';
  toast('课程示例已填入编辑器，尚未执行。');
});
$('run-research-semantic').addEventListener('click', runResearchSemantic);
$('clear-research-semantic-cache').addEventListener('click', async () => {
  $('research-semantic-cache-result').textContent = '';
  await researchRequest('semantic', ['run-research-semantic', 'clear-research-semantic-cache'], 'research-semantic-error', '/api/research/semantic/cache', {}, result => {
    $('research-semantic-output').hidden = true;
    $('research-semantic-cache-result').textContent = '推理缓存清理已完成；当前条目数请查看实验状态。';
    $('research-semantic-json').textContent = JSON.stringify(result, null, 2);
  });
});

async function initialize() {
  setSQL(examples[0].sql, false);
  switchTab('results');
  try {
    state.token = (await api('/api/session')).token;
    await refreshMetadata();
    setConnection(true, '本机引擎已连接');
    const isChinook = state.tables.some(table => ['artist', 'album', 'track'].includes(String(table.name || '').toLowerCase()));
    populateExamples(isChinook ? chinookExamples : examples);
    setSQL((isChinook ? chinookExamples : examples)[0].sql, false);
    if (!isChinook && !state.tables.some(table => table.name === 'students')) setSQL('SHOW TABLES;', false);
    // The initial read makes the workbench useful immediately, without mutations.
    await runQuery();
  } catch (error) {
    setConnection(false, '连接失败');
    showError(error);
  }
}
initialize();
switchTool('ai');
updateAIAction();
refreshAIStatus();
