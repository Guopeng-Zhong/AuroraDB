'use strict';

// Runs the actual workbench handlers with a small DOM double. This checks
// request budgets, import gates and error isolation; browser layout is reviewed
// separately in the real in-app browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'auroradb/web/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'auroradb/web/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'auroradb/web/styles.css'), 'utf8');

class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
    this.listeners = {}; this.dataset = {}; this.value = ''; this.hidden = false;
    this.disabled = false; this.checked = false; this._text = ''; this.className = '';
    this.selectionStart = 0; this.selectionEnd = 0; this.scrollTop = 0; this.scrollLeft = 0;
    this.classList = {
      add: name => { if (!this.className.split(/\s+/).includes(name)) this.className += ` ${name}`; },
      remove: name => { this.className = this.className.split(/\s+/).filter(x => x !== name).join(' '); },
      toggle: (name, wanted) => {
        const has = this.className.split(/\s+/).includes(name);
        const next = wanted === undefined ? !has : wanted;
        if (next) this.classList.add(name); else this.classList.remove(name);
        return next;
      },
    };
  }
  get textContent() { return this._text + this.children.map(x => typeof x === 'string' ? x : x.textContent).join(''); }
  get options() { return this.children.filter(node => node.tagName === 'OPTION'); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(_) { throw new Error('Unsafe innerHTML assignment'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, fn) { (this.listeners[name] ??= []).push(fn); }
  async dispatch(name, event = {}) {
    for (const fn of this.listeners[name] || []) await fn({ target: this, preventDefault() {}, ...event });
  }
  focus() { this.focused = true; }
  select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  querySelectorAll() { return []; }
}

const nodes = new Map();
for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
  const [, tag, attrs, id] = match;
  assert(!nodes.has(id), `Duplicate HTML id: ${id}`);
  const node = new Node(tag);
  for (const attribute of attrs.matchAll(/([\w-]+)="([^"]*)"/g)) node.attributes[attribute[1]] = attribute[2];
  node.value = node.attributes.value || '';
  node.className = node.attributes.class || '';
  node.disabled = /\bdisabled(?:\s|>|$)/.test(attrs);
  node.hidden = /\bhidden(?:\s|>|$)/.test(attrs);
  const start = match.index + match[0].length;
  const ending = html.indexOf(`</${tag}>`, start);
  if (ending >= start && ['button', 'textarea'].includes(tag)) node.textContent = html.slice(start, ending).replace(/<[^>]*>/g, '');
  if (tag === 'textarea') node.value = node.textContent;
  if (tag === 'select' && ending >= start) node.value = /<option\b[^>]*\bvalue="([^"]*)"/.exec(html.slice(start, ending))?.[1] || '';
  nodes.set(id, node);
}
for (const ref of source.matchAll(/\$\('([^']+)'\)/g)) assert(nodes.has(ref[1]), `Missing HTML id: ${ref[1]}`);
assert.equal(css.split('{').length, css.split('}').length, 'Unbalanced CSS braces');
// The existing theme switch intentionally persists one non-query preference.
// Keep the guard for all other storage access and HTML injection sinks.
const withoutThemeStorage = source
  .replaceAll("localStorage.getItem('aurora-theme')", 'themeRead')
  .replaceAll("localStorage.setItem('aurora-theme', button.dataset.themeChoice)", 'themeWrite');
assert(!/innerHTML|localStorage|sessionStorage/.test(withoutThemeStorage), 'Unsafe HTML or browser persistence');
const get = id => { assert(nodes.has(id), `Unknown DOM node ${id}`); return nodes.get(id); };
const requests = [], timeouts = [];
let intercept = null;
const status = {
  optimizer: {mode: 'heuristic', generation: 3, statistics: {available: true, tables: 3}, learning: {cost_models: {TableScan: {samples: 0, ready: false}, IndexScan: {samples: 0, ready: false}}, observations: 0}, cache: {entries: 1, hits: 2}},
  buffer: {policy: 'lru', sample_size: 8, history_pages: 5},
  semantic: {entries: 2, hits: 3, ttl_seconds: 300},
  ai: {mode: 'mock', provider: '本地规则演示', available: true},
};
const response = (data, ok = true, http = 200) => ({ok, status: http, json: async () => data});
const themeButtons = [...html.matchAll(/data-theme-choice="([^"]+)"/g)].map(match => {
  const button = new Node('button'); button.dataset.themeChoice = match[1]; return button;
});
let themePreference = null;
const document = {
  documentElement: new Node('html'),
  querySelectorAll: selector => selector === '[data-theme-choice]' ? themeButtons : [],
  getElementById: get,
  createElement: tag => new Node(tag),
  createTextNode: text => { const node = new Node('#text'); node.textContent = text; return node; },
  createDocumentFragment: () => new Node('#fragment'),
  querySelector: () => new Node(),
};
const context = vm.createContext({document, console, AbortController,
  localStorage: {
    getItem(key) { assert.equal(key, 'aurora-theme'); return themePreference; },
    setItem(key, value) {
      assert.equal(key, 'aurora-theme'); assert(['system', 'light', 'dark'].includes(value));
      themePreference = value;
    },
  },
  window: {getSelection: () => ({selectAllChildren() {}})},
  navigator: {clipboard: {writeText: async () => {}}},
  setTimeout: (_, ms) => { timeouts.push(ms); return timeouts.length; }, clearTimeout() {},
  fetch: async (url, options) => {
    requests.push({url, options, body: options.body ? JSON.parse(options.body) : null});
    if (intercept) { const found = intercept(url, options); if (found) return await found; }
    if (url === '/api/session') return response({token: 'test-token'});
    if (url === '/api/research/status') return response(status);
    if (url === '/api/schema') return response({tables: []});
    if (url === '/api/stats') return response({cache_policy: status.buffer.policy, cache_hits: 3, cache_misses: 2, cache_used: 2, cache_size: 8, transaction_active: false});
    if (url === '/api/research/buffer') { Object.assign(status.buffer, JSON.parse(options.body)); return response(status.buffer); }
    if (url === '/api/research/optimizer') { Object.assign(status.optimizer, JSON.parse(options.body)); return response(status.optimizer); }
    if (url === '/api/research/analyze') return response({generation: 3, table_count: 3, elapsed_ms: 1.25, tables: {courses: {row_count: 6}}});
    if (url === '/api/research/semantic/cache') { status.semantic.entries = 0; return response({cleared: 2, entries: 0}); }
    throw new Error(`Unexpected request: ${url}`);
  },
});
// Leave normal initialization untouched in production; only omit startup calls
// here so each handler's inputs and requests can be tested independently.
vm.runInContext(source.replace(/\ninitialize\(\);\s*switchTool\('ai'\);\s*updateAIAction\(\);\s*refreshAIStatus\(\);\s*$/, '\n'), context);
const run = code => vm.runInContext(code, context);

async function main() {
  assert.equal(themeButtons.length, 3);
  await themeButtons.find(button => button.dataset.themeChoice === 'dark').dispatch('click');
  assert.equal(document.documentElement.dataset.theme, 'dark');
  assert.equal(themePreference, 'dark');
  await themeButtons.find(button => button.dataset.themeChoice === 'system').dispatch('click');
  assert.equal(document.documentElement.dataset.theme, undefined);
  await get('tool-tab-research').dispatch('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get('tool-view-research').hidden, false);
  assert.equal(get('run-research-semantic').disabled, false);
  assert.equal(get('research-use-ai').disabled, true);
  assert(get('research-semantic-source').textContent.includes('不调用远程模型'));
  const chipText = label => get('research-status-grid').children.find(node => node.children[0].textContent === label)?.children[1].textContent;
  assert.equal(chipText('学习样本'), '0');
  assert.equal(chipText('学习就绪'), '0 / 2');
  status.optimizer.learning.cost_models.TableScan = {samples: 8, ready: true};
  status.optimizer.learning.cost_models.IndexScan = {samples: 3, ready: false};
  await run('refreshResearchStatus()');
  assert.equal(chipText('学习样本'), '11');
  assert.equal(chipText('学习就绪'), '1 / 2');
  await get('tool-tab-ai').dispatch('keydown', {key: 'End'});
  assert.equal(get('tool-tab-research').getAttribute('aria-selected'), 'true');

  await get('load-research-semantic-example').dispatch('click');
  assert.equal(get('sql-editor').value, 'SELECT id, title FROM courses ORDER BY id;');
  assert.equal(requests.filter(x => x.url === '/api/query').length, 0, 'Loading example must not execute');
  const semantic = {mode: 'mock', provider: '本地规则演示', columns: ['id', 'title'], rows: [[1, '<script>unsafe</script>']], row_count: 1, input_rows: 6, stats: {model_calls: 0, cache_hits: 0, cache_misses: 6, batches: 1, elapsed_ms: 0.5, budget_rows: 64}, explanation: '关键词演示'};
  intercept = url => url === '/api/research/semantic' ? response(semantic) : null;
  await run('runResearchSemantic()');
  const call = requests.findLast(x => x.url === '/api/research/semantic');
  assert.deepEqual(call.body, {sql: 'SELECT id, title FROM courses ORDER BY id;', column: 'title', prompt: '关键词：数据库|查询|索引', operation: 'filter', batch_size: 8, max_rows: 64, max_calls: 8, budget_seconds: 30});
  assert.equal(call.options.headers['X-Aurora-Token'], 'test-token');
  assert(timeouts.includes(40000), 'Default semantic timeout must be 40s');
  assert(get('research-semantic-table').textContent.includes('<script>unsafe</script>'), 'User text must remain text');

  intercept = url => url === '/api/research/semantic' ? response({error: '调用预算不足'}, false, 400) : null;
  await run('runResearchSemantic()');
  assert.equal(get('research-semantic-output').hidden, true, 'Error must not leave stale success output');
  assert.equal(get('research-semantic-error').textContent, '调用预算不足');
  assert.equal(get('run-query').disabled, false, 'Semantic errors must not disable ordinary SQL');
  assert.equal(run('state.researchBusy.semantic'), false);

  let resolvePending;
  intercept = url => url === '/api/research/semantic' ? new Promise(resolve => {resolvePending = resolve;}) : null;
  const pending = run('runResearchSemantic()');
  await Promise.resolve();
  const before = requests.filter(x => x.url === '/api/research/semantic').length;
  await run('runResearchSemantic()');
  assert.equal(requests.filter(x => x.url === '/api/research/semantic').length, before, 'No duplicate semantic request');
  assert.equal(get('run-query').disabled, false, 'SQL remains available during inference');
  resolvePending(response(semantic));
  await pending;

  const rewrite = {candidate_sql: 'SELECT id FROM students;', verification: {level: 'unknown', matched: true, reason: '范围外'}, recommendation: {replace: true, sql: 'SELECT id FROM students;', reason: '候选'}, original: {}, candidate: {}, timings: {model_ms: 0, total_ms: 1}, model_calls: 0, model_mode: 'local-rules'};
  context.testRewrite = rewrite;
  run('renderRewrite(testRewrite)');
  assert.equal(get('import-research-candidate').disabled, true, 'Unknown verification cannot import');
  const sqlBefore = get('sql-editor').value;
  await get('import-research-candidate').dispatch('click');
  assert.equal(get('sql-editor').value, sqlBefore);
  rewrite.verification.level = 'tested'; rewrite.recommendation.replace = false;
  run('renderRewrite(testRewrite)');
  assert.equal(get('import-research-candidate').disabled, true, 'Sample match without recommendation cannot import');
  rewrite.verification.level = 'proven'; rewrite.recommendation.replace = true;
  run('renderRewrite(testRewrite)');
  assert.equal(get('import-research-candidate').disabled, false);
  await get('import-research-candidate').dispatch('click');
  assert.equal(get('sql-editor').value, rewrite.recommendation.sql);
  assert.equal(requests.filter(x => x.url === '/api/query').length, 0, 'Import must not execute');
  assert(get('research-verification-reason').textContent.includes('未调用远程模型'));

  get('research-candidate').value = 'SELECT id FROM students;'; get('research-use-ai').checked = true;
  await run('runResearchOptimize()');
  assert.equal(requests.filter(x => x.url === '/api/research/optimize').length, 0, 'Two candidate sources must be rejected before sending');
  assert(get('research-optimize-error').textContent.includes('请选择一种'));

  get('research-buffer-sample').value = '4097';
  await get('apply-research-buffer').dispatch('click');
  assert.equal(requests.filter(x => x.url === '/api/research/buffer').length, 0);
  get('research-buffer-sample').value = '8'; get('research-buffer-policy').value = 'predictive';
  await get('apply-research-buffer').dispatch('click');
  assert(get('research-buffer-result').textContent.includes('预测策略'));
  assert(get('metric-pages-detail').textContent.includes('预测策略'), 'Actual policy label must follow metadata');
  await get('run-research-analyze').dispatch('click');
  assert(get('research-optimizer-result').textContent.includes('3 张表'));
  await get('clear-research-semantic-cache').dispatch('click');
  assert.equal(status.semantic.entries, 0);
  assert.equal(get('research-semantic-output').hidden, true);

  // A configured 300s school request must not be cancelled by the browser at
  // the former fixed 105s limit. Both remote entry points share this contract.
  for (const [value, expected] of [[0.1,15100], [90,105000], [300,315000],
                                  [301,105000], [0,105000], [NaN,105000],
                                  [Infinity,105000], ['300',105000], [null,105000]]) {
    context.testAIStatus = {timeout_seconds:value};
    assert.equal(run('aiRequestTimeout(testAIStatus)'), expected);
  }
  assert.equal(run('aiRequestTimeout(null)'), 105000);
  status.ai = {mode:'remote', model:'fixture', available:true, timeout_seconds:300};
  await run('refreshResearchStatus()');
  context.testAIStatus = status.ai;
  run('state.aiStatus = testAIStatus');
  get('ai-action').value = 'generate'; get('ai-prompt').value = '查看学生';
  intercept = url => url === '/api/ai' ? response({mode:'remote', model:'fixture',
    sql:'SELECT 1;', explanation:'fixture', validation:{valid:true, read_only:true}}) :
    url === '/api/research/optimize' ? response(rewrite) : null;
  await run('runAI()');
  assert.equal(timeouts.at(-1), 315000, 'AI browser deadline must follow school timeout');
  get('research-candidate').value = ''; get('research-use-ai').checked = true;
  await run('runResearchOptimize()');
  assert.equal(timeouts.at(-1), 315000, 'AI rewrite browser deadline must follow school timeout');

  status.ai = {mode: 'disabled', available: false};
  await run('refreshResearchStatus()');
  assert.equal(get('run-research-semantic').disabled, true);
  assert.equal(get('run-research-optimize').disabled, false, 'Local rewrite remains available without model');

  // The small PPT entry expands locally and never imports or executes its SQL
  // against the editor's database. A busy/error cycle must remain isolated.
  const editorBeforeDemo = get('sql-editor').value;
  const requestCount = requests.length;
  assert.equal(get('research-join-demo').hidden, true);
  await get('toggle-join-demo').dispatch('click');
  assert.equal(get('research-join-demo').hidden, false);
  assert.equal(get('toggle-join-demo').getAttribute('aria-expanded'), 'true');
  assert.equal(requests.length, requestCount, 'Opening the example must not start an experiment');
  const demoRun = {row_count:100, elapsed_ms:60, repetitions:3,
    stats:{rows_scanned:10110, disk_reads:200, join_comparisons:200},
    join_order:['p','r','c'], join_outputs:[100,100], plan:{operator:'HashJoin'}};
  const demo = {sql:'SELECT p.id, c.id FROM registrations r;', matched:true,
    repetitions:3, adaptive:demoRun, sql_order:{...demoRun, elapsed_ms:100,
      join_order:['r','c','p'], join_outputs:[10000,10000],
      stats:{...demoRun.stats, join_comparisons:20000}},
    columns:['p.id','c.id'], rows:[[100,8]]};
  let finishDemo;
  intercept = url => url === '/api/research/join-demo' ? new Promise(resolve => { finishDemo = resolve; }) : null;
  const pendingDemo = run('runJoinDemo()');
  assert.equal(get('run-join-demo').disabled, true);
  assert.equal(get('join-demo-message').textContent, '正在执行策略');
  assert.equal(get('join-demo-message').hidden, false);
  await run('runJoinDemo()');
  assert.equal(requests.filter(x => x.url === '/api/research/join-demo').length, 1);
  assert.deepEqual(requests.at(-1).body, {});
  assert.equal(requests.at(-1).options.headers['X-Aurora-Token'], 'test-token');
  finishDemo(response(demo));
  await pendingDemo;
  assert.equal(get('join-demo-output').hidden, false);
  assert(get('join-demo-verdict').textContent.includes('完整结果一致'));
  assert(get('join-demo-comparison').textContent.includes('20,000 次'));
  assert(get('join-demo-comparison').textContent.includes('学生 → 选课 → 课程'));
  assert.equal(get('join-demo-message').textContent, '');
  assert.equal(get('join-demo-message').hidden, true);
  assert.equal(get('run-join-demo').disabled, false);
  assert.equal(get('sql-editor').value, editorBeforeDemo);
  intercept = url => url === '/api/research/join-demo' ? response({error:'fixture failed'}, false, 400) : null;
  await run('runJoinDemo()');
  assert.equal(get('join-demo-output').hidden, true, 'Never show stale comparison after failure');
  assert.equal(get('join-demo-error').textContent, 'fixture failed');
  assert.equal(get('run-join-demo').disabled, false);
  await get('toggle-join-demo').dispatch('click');
  assert.equal(get('research-join-demo').hidden, true);

  // Keep the last SELECT visible after COMMIT even when the browser lacks
  // Array.prototype.findLastIndex.
  run('Array.prototype.findLastIndex = undefined');
  context.trailingResults = [
    {columns:['id'], rows:[[7]], row_count:1, message:'SELECT', elapsed_ms:1},
    {columns:[], rows:[], message:'COMMIT', elapsed_ms:1},
  ];
  run('renderResults(trailingResults)');
  assert.equal(get('statement-select').value, '0');
  assert.equal(get('result-count').textContent, '1');
  assert(get('table-container').textContent.includes('7'));
  context.trailingResults = [{columns:[], rows:[], message:'BEGIN'}, {message:'COMMIT'}];
  run('renderResults(trailingResults)');
  assert.equal(get('statement-select').value, '1');
  console.log(`UI research smoke passed: ${nodes.size} unique IDs, budget/CSRF payloads, import gates, errors, concurrent SQL, cache and status controls.`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
