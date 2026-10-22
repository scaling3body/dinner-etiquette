const labels = {sleeper_nfl: 'Sleeper NFL', sleeper_nba: 'Sleeper NBA', sleeper_nba_catchup: 'NBA catch-up', scoreboard: 'Game progress', yahoo: 'Yahoo', telegram: 'Telegram'};
let snapshot = null;
let refreshFailed = false;
function age(iso) {
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return 'never';
  const minutes = Math.max(0, Math.floor((Date.now() - time) / 60000));
  return minutes < 1 ? 'just now' : minutes < 60 ? `${minutes}m ago` : `${Math.floor(minutes / 60)}h ${minutes % 60}m ago`;
}
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function card(title, text, detail, cls = 'card') {
  const element = node('div', undefined, cls);
  if (title) element.append(node('strong', title));
  if (text) element.append(node('div', text, 'msg'));
  if (detail) element.append(node('div', detail, 'time'));
  return element;
}
function empty(id, message) {
  const target = document.getElementById(id);
  target.replaceChildren(node('div', message, 'empty'));
  return target;
}
function render() {
  if (!snapshot) return;
  const data = snapshot, runtime = data.runtime || {}, queue = data.delivery || {};
  const last = runtime.last_completed || data.last_updated;
  const minutes = (Date.now() - Date.parse(last)) / 60000;
  const checkingHours = new Date().getUTCHours() >= 16 || new Date().getUTCHours() <= 5;
  const stale = !Number.isFinite(minutes) || minutes > (data.stale_after_minutes || 15);
  const status = document.getElementById('check-status');
  status.className = 'card ' + (stale && checkingHours ? 'stale' : '');
  status.replaceChildren(node('strong', stale && checkingHours ? 'Check is overdue' : checkingHours ? 'Scheduled checking window' : 'Outside scheduled checking hours'),
    node('div', `Last completed check: ${age(last)}`),
    node('div', `Last run started: ${age(runtime.last_started)} · Schedule requests checks every 5 minutes`, 'time'));
  document.getElementById('updated').textContent = (refreshFailed ? 'Refresh failed — showing previous snapshot. ' : '') + 'Snapshot: ' + new Date(data.last_updated).toLocaleString();
  const health = document.getElementById('health');
  health.replaceChildren();
  for (const [key, value] of Object.entries(data.health || {})) {
    const idle = (runtime.sports || {})[key === 'sleeper_nba' ? 'nba' : 'nfl']?.status === 'idle' && key.startsWith('sleeper');
    const sourceStale = !idle && Date.now() - Date.parse(value.last_success) > (data.stale_after_minutes || 15) * 60000;
    const text = value.circuit_open ? 'Circuit open' : value.consecutive_failures ? `Retrying (${value.consecutive_failures})` : idle ? 'Idle — no live data expected' : sourceStale ? 'Data is old' : 'OK';
    const sourceTime = key === 'sleeper_nba' ? runtime.sports?.nba?.data_updated_at : null;
    health.append(card(labels[key] || key, text, `Last success ${age(value.last_success)}${sourceTime ? ` · Stats updated ${age(sourceTime)}` : ''}${value.last_failure ? ` · Last failure ${age(value.last_failure)}` : ''}`, 'health-card'));
  }
  const deliveries = document.getElementById('delivery');
  deliveries.replaceChildren(card('Delivery queue', `${queue.pending || 0} pending alerts · ${queue.failed_messages || 0} failed message parts`,
    queue.oldest_pending_at ? `Oldest waiting: ${age(queue.oldest_pending_at)}${queue.last_error ? ' · ' + queue.last_error : ''}` : 'No alerts waiting for retry'));
  const games = empty('games', 'No game progress available in this snapshot.');
  if ((runtime.games || []).length) {
    games.replaceChildren();
    for (const game of runtime.games) {
      const progress = game.final ? 'Final' : game.state === 'in' ? `Period ${game.period} · ${game.clock || 'Live'}` : game.detail || 'Scheduled';
      games.append(card(`${game.sport.toUpperCase()} · ${game.matchup}`, progress, `${game.date} · ${game.source} checked ${age(game.fetched_at)}`));
    }
  }
  const pending = empty('pending', 'No pending roster actions.');
  if ((data.pending_suggestions || []).length) {
    pending.replaceChildren();
    for (const item of data.pending_suggestions) pending.append(card(item.player_name, 'Use the Add / No thanks buttons in Telegram.', `Suggested ${age(item.created_at)}`));
  }
  const alerts = empty('alerts', 'No alerts yet.');
  if ((data.recent_alerts || []).length) {
    alerts.replaceChildren();
    for (const item of data.recent_alerts) alerts.append(card(item.game_label || '', item.message,
      `${new Date(item.timestamp).toLocaleString()} · ${item.notified ? 'Delivered' : 'Suppressed'}${item.delayed ? ' · Catch-up' : ''}`));
  }
}
async function refresh() {
  try {
    const response = await fetch('data.json?_=' + Date.now(), {cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    snapshot = await response.json();
    refreshFailed = false;
    render();
  } catch (error) {
    refreshFailed = true;
    if (snapshot) render();
    else document.getElementById('updated').textContent = 'Unable to load the bot snapshot.';
  }
}
refresh();
setInterval(render, 30000);
setInterval(refresh, 60000);
