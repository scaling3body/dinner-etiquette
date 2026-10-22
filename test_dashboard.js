// Run the real dashboard script against mocked data and a text-only DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  set innerHTML(value) { throw new Error('Untrusted dashboard data must not use innerHTML'); }
}
const elements = new Map();
let failed = false;
const malicious = '<img src=x onerror=alert(1)>';
const data = {
  last_updated: '2026-10-22T02:00:00Z', runtime: {last_completed: '2026-10-22T02:00:00Z', games: []},
  recent_alerts: [{message: malicious, timestamp: '2026-10-22T02:00:00Z', notified: true}],
  delivery: {pending: 2, failed_messages: 1}, health: {}, pending_suggestions: []
};
class FixedDate extends Date { constructor(...args) { super(...(args.length ? args : ['2026-10-22T04:00:00Z'])); } static now() { return Date.parse('2026-10-22T04:00:00Z'); } }
const context = vm.createContext({Date: FixedDate, console,
  document: {createElement: tag => new Element(tag), getElementById: id => {
    if (!elements.has(id)) elements.set(id, new Element('div'));
    return elements.get(id);
  }},
  fetch: async () => { if (failed) throw new Error('offline'); return {ok: true, json: async () => data}; },
  setInterval: () => 0
});
async function test() {
  vm.runInContext(fs.readFileSync('docs/dashboard.js', 'utf8'), context);
  await new Promise(setImmediate);
  assert.equal(elements.get('check-status').children[0].textContent, 'Check is overdue');
  assert.equal(elements.get('alerts').children[0].children[0].textContent, malicious);
  assert.match(elements.get('delivery').children[0].children[1].textContent, /2 pending alerts/);
  failed = true;
  await vm.runInContext('refresh()', context);
  assert.match(elements.get('updated').textContent, /Refresh failed/);
  assert.equal(elements.get('alerts').children.length, 1, 'cached alerts must remain visible when fetch fails');
  console.log('Dashboard checks passed: stale warning, safe text rendering, queue status, failed-refresh fallback');
}
test().catch(error => { console.error(error); process.exitCode = 1; });
