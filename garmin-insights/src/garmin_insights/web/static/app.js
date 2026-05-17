/* =========================================================
   Garmin Health Insights — Frontend app
   ========================================================= */

// ---- Tab navigation ----
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${target}`).classList.add('active');
  });
});

// ---- Health check / status dot + user badge + last-sync badge ----
const statusDot = document.getElementById('status-dot');
const userBadge = document.getElementById('user-badge');
const syncBadge = document.getElementById('sync-badge');

function renderUserBadge(user) {
  if (!user || (!user.name && !user.email)) {
    userBadge.innerHTML = '';
    userBadge.title = '';
    return;
  }
  const name = user.name || 'User';
  const email = user.email || '';
  userBadge.innerHTML = `<span class="badge-name">${escapeHtml(name)}</span>` +
    (email ? `<span class="badge-email">${escapeHtml(email)}</span>` : '');
  userBadge.title = email ? `Logged in as ${email}` : name;
  // Also reflect the user in the document title so multi-tab browsing is clear
  document.title = `${name} · Garmin Health Insights`;
}

function formatRelativeTime(date) {
  const sec = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (sec < 60)      return 'just now';
  if (sec < 3600)    return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400)   return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

// Track the last sync time so the relative label refreshes every 30s without
// needing another fetch.
let lastSyncDate = null;

function renderSyncBadge(iso) {
  if (!iso) {
    lastSyncDate = null;
    syncBadge.innerHTML = '';
    syncBadge.title = '';
    return;
  }
  lastSyncDate = new Date(iso);
  refreshSyncBadge();
}

function refreshSyncBadge() {
  if (!lastSyncDate) return;
  const ageMin = (Date.now() - lastSyncDate.getTime()) / 60000;
  // <15min = fresh, <2h = ok, else stale
  let cls = '';
  if (ageMin < 15)      cls = 'fresh';
  else if (ageMin > 120) cls = 'stale';
  syncBadge.className = `sync-badge ${cls}`.trim();
  syncBadge.innerHTML =
    `<span class="sync-dot"></span>Synced ${escapeHtml(formatRelativeTime(lastSyncDate))}`;
  syncBadge.title = `Last DB write: ${lastSyncDate.toLocaleString()}`;
}

async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    if (res.ok) {
      statusDot.className = 'status-dot ok';
      statusDot.title = 'Connected';
      try {
        const data = await res.json();
        renderUserBadge(data.user);
        renderSyncBadge(data.last_sync);
      } catch { /* ignore */ }
    } else {
      statusDot.className = 'status-dot error';
      statusDot.title = 'Service error';
    }
  } catch {
    statusDot.className = 'status-dot error';
    statusDot.title = 'Unreachable';
  }
}
checkHealth();
setInterval(checkHealth, 30_000);
// Refresh the relative-time label every 30s even between server polls
setInterval(refreshSyncBadge, 30_000);

// ---- Dashboard ----
let chartInstance = null;
let dashboardData = null;
let activeMetric = 'sleepScore';

const METRIC_CONFIG = {
  sleepScore:            { label: 'Sleep Score',        unit: '',    decimals: 0, good: v => v >= 80, warn: v => v >= 60 },
  restingHeartRate:      { label: 'Resting HR',         unit: ' bpm', decimals: 0, good: v => v <= 58, warn: v => v <= 65 },
  avgOvernightHrv:       { label: 'Overnight HRV',      unit: ' ms',  decimals: 0, good: v => v >= 50, warn: v => v >= 35 },
  bodyBatteryAtWakeTime: { label: 'Battery (wake)',      unit: '',    decimals: 0, good: v => v >= 70, warn: v => v >= 50 },
  totalSteps:            { label: 'Steps',              unit: '',    decimals: 0, format: v => v >= 1000 ? `${(v/1000).toFixed(1)}k` : String(Math.round(v)), good: v => v >= 10000, warn: v => v >= 7000 },
  stressPercentage:      { label: 'Stress %',           unit: '%',   decimals: 0, good: v => v <= 20, warn: v => v <= 35 },
};

function colorClass(metric, value) {
  const cfg = METRIC_CONFIG[metric];
  if (!cfg || value == null) return '';
  if (cfg.good && cfg.good(value)) return 'good';
  if (cfg.warn && cfg.warn(value)) return 'warn';
  return 'bad';
}

function formatValue(metric, value) {
  if (value == null) return '—';
  const cfg = METRIC_CONFIG[metric] || {};
  if (cfg.format) return cfg.format(value) + (cfg.unit || '');
  const v = parseFloat(value);
  if (isNaN(v)) return '—';
  return v.toFixed(cfg.decimals ?? 1) + (cfg.unit || '');
}

function getLatestAndPrev(summaries, key) {
  const sorted = [...summaries].sort((a, b) => b.date.localeCompare(a.date));
  // skip today (potentially incomplete) for cumulative metrics
  const cumulativeMetrics = ['totalSteps', 'stressPercentage'];
  const skip = cumulativeMetrics.includes(key) ? 1 : 0;
  const latest = sorted[skip];
  const prev = sorted[skip + 1];
  return {
    value: latest?.[key] ?? null,
    prevValue: prev?.[key] ?? null,
    date: latest?.date,
  };
}

function renderCards(summaries, baselines) {
  Object.keys(METRIC_CONFIG).forEach(key => {
    const card = document.getElementById(`card-${key}`);
    const valEl = document.getElementById(`val-${key}`);
    const subEl = document.getElementById(`sub-${key}`);
    if (!card || !valEl || !subEl) return;

    card.classList.remove('skeleton');
    const { value, prevValue, date } = getLatestAndPrev(summaries, key);

    valEl.textContent = formatValue(key, value);
    valEl.className = `metric-value ${colorClass(key, value)}`;

    const baseline = baselines?.[key];
    let sub = '';
    if (date) sub += date;
    if (baseline?.avg_7d != null && value != null) {
      const diff = value - baseline.avg_7d;
      const sign = diff >= 0 ? '+' : '';
      sub += ` · ${sign}${diff.toFixed(1)} vs 7d avg`;
    }
    subEl.textContent = sub;
  });
}

function getMetricSeries(summaries, key) {
  return [...summaries]
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(-14)
    .map(s => ({ x: s.date, y: s[key] ?? null }));
}

function renderChart(summaries, metric) {
  const data = getMetricSeries(summaries, metric);
  const cfg = METRIC_CONFIG[metric] || {};
  const ctx = document.getElementById('trend-chart').getContext('2d');

  if (chartInstance) chartInstance.destroy();

  chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label: cfg.label || metric,
        data,
        borderColor: '#4f9cf9',
        backgroundColor: 'rgba(79,156,249,0.08)',
        borderWidth: 2,
        pointBackgroundColor: '#4f9cf9',
        pointRadius: 4,
        pointHoverRadius: 6,
        tension: 0.3,
        fill: true,
        spanGaps: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      parsing: { xAxisKey: 'x', yAxisKey: 'y' },
      scales: {
        x: {
          type: 'category',
          ticks: { color: '#8892a4', maxTicksLimit: 7, maxRotation: 0 },
          grid: { color: '#2e3350' },
        },
        y: {
          ticks: { color: '#8892a4' },
          grid: { color: '#2e3350' },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#22263a',
          borderColor: '#2e3350',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#8892a4',
          callbacks: {
            label: ctx => `${cfg.label || metric}: ${formatValue(metric, ctx.parsed.y)}`,
          },
        },
      },
    },
  });
}

// ---- Auxiliary charts ----
const auxCharts = {};

function destroyAux(key) {
  if (auxCharts[key]) {
    auxCharts[key].destroy();
    delete auxCharts[key];
  }
}

function lastNDays(summaries, n) {
  return [...summaries]
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(-n);
}

function commonScales(yLabel = '') {
  return {
    x: {
      ticks: { color: '#8892a4', maxTicksLimit: 7, maxRotation: 0 },
      grid: { color: '#2e3350' },
    },
    y: {
      ticks: { color: '#8892a4' },
      grid: { color: '#2e3350' },
      title: yLabel ? { display: true, text: yLabel, color: '#8892a4' } : { display: false },
    },
  };
}

function commonPlugins(extra = {}) {
  return {
    legend: { labels: { color: '#8892a4', boxWidth: 12, padding: 10 } },
    tooltip: {
      backgroundColor: '#22263a',
      borderColor: '#2e3350',
      borderWidth: 1,
      titleColor: '#e2e8f0',
      bodyColor: '#8892a4',
    },
    ...extra,
  };
}

function renderSleepArchitecture(summaries) {
  const recent = lastNDays(summaries, 14);
  const labels = recent.map(s => s.date.slice(5));
  const toHours = secs => secs == null ? null : +(secs / 3600).toFixed(2);

  destroyAux('sleep');
  auxCharts.sleep = new Chart(document.getElementById('sleep-architecture-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Deep',  data: recent.map(s => toHours(s.deepSleepSeconds)),  backgroundColor: '#4f9cf9' },
        { label: 'REM',   data: recent.map(s => toHours(s.remSleepSeconds)),   backgroundColor: '#7c6af7' },
        { label: 'Light', data: recent.map(s => toHours(s.lightSleepSeconds)), backgroundColor: '#34d399' },
        { label: 'Awake', data: recent.map(s => toHours(s.awakeSleepSeconds)), backgroundColor: '#f87171' },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ...commonScales().x },
        y: { stacked: true, ...commonScales('hours').y },
      },
      plugins: commonPlugins(),
    },
  });
}

function renderRecoveryChart(summaries, baselines) {
  const recent = lastNDays(summaries, 14);
  const labels = recent.map(s => s.date.slice(5));

  // Normalize each metric to % of 7-day baseline so they share a Y scale
  function normalized(metric) {
    const base = baselines?.[metric]?.avg_7d;
    if (!base) return recent.map(() => null);
    return recent.map(s => s[metric] != null ? +((s[metric] / base) * 100).toFixed(1) : null);
  }

  destroyAux('recovery');
  auxCharts.recovery = new Chart(document.getElementById('recovery-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Sleep score', data: normalized('sleepScore'),       borderColor: '#4f9cf9', backgroundColor: 'transparent', tension: 0.3, spanGaps: true },
        { label: 'HRV',         data: normalized('avgOvernightHrv'),  borderColor: '#34d399', backgroundColor: 'transparent', tension: 0.3, spanGaps: true },
        { label: 'RHR (inv)',   data: normalized('restingHeartRate').map(v => v == null ? null : 200 - v), borderColor: '#fbbf24', backgroundColor: 'transparent', tension: 0.3, spanGaps: true, borderDash: [4, 4] },
        { label: 'Body Battery',data: normalized('bodyBatteryAtWakeTime'), borderColor: '#7c6af7', backgroundColor: 'transparent', tension: 0.3, spanGaps: true },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: commonScales('% of baseline'),
      plugins: commonPlugins({
        tooltip: {
          backgroundColor: '#22263a',
          borderColor: '#2e3350',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#8892a4',
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y == null ? '—' : ctx.parsed.y + '%'}`,
          },
        },
      }),
    },
  });
}

function renderActivityChart(summaries) {
  const recent = lastNDays(summaries, 14);
  const labels = recent.map(s => s.date.slice(5));

  destroyAux('activity');
  auxCharts.activity = new Chart(document.getElementById('activity-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Moderate', data: recent.map(s => s.moderateIntensityMinutes ?? 0), backgroundColor: '#34d399' },
        { label: 'Vigorous', data: recent.map(s => s.vigorousIntensityMinutes ?? 0), backgroundColor: '#f87171' },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ...commonScales().x },
        y: { stacked: true, ...commonScales('minutes').y },
      },
      plugins: commonPlugins(),
    },
  });
}

function renderStressChart(summaries) {
  const recent = lastNDays(summaries, 14);
  const labels = recent.map(s => s.date.slice(5));

  destroyAux('stress');
  auxCharts.stress = new Chart(document.getElementById('stress-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Stress %',
          data: recent.map(s => s.stressPercentage ?? null),
          borderColor: '#f87171',
          backgroundColor: 'rgba(248,113,113,0.1)',
          tension: 0.3,
          yAxisID: 'y',
          spanGaps: true,
        },
        {
          label: 'Body Battery (peak)',
          data: recent.map(s => s.bodyBatteryHighestValue ?? null),
          borderColor: '#34d399',
          backgroundColor: 'rgba(52,211,153,0.1)',
          tension: 0.3,
          yAxisID: 'y1',
          spanGaps: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: commonScales().x,
        y: { ...commonScales('stress %').y, position: 'left', min: 0, max: 100 },
        y1: { ...commonScales('battery').y, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false } },
      },
      plugins: commonPlugins(),
    },
  });
}

async function loadDashboard() {
  try {
    const res = await fetch('/api/dashboard');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    dashboardData = await res.json();
    const { summaries, baselines } = dashboardData;
    renderCards(summaries, baselines);
    renderChart(summaries, activeMetric);
    renderSleepArchitecture(summaries);
    renderRecoveryChart(summaries, baselines);
    renderActivityChart(summaries);
    renderStressChart(summaries);
  } catch (e) {
    console.error('Dashboard load failed:', e);
  }
}

// Chart metric toggles
document.querySelectorAll('.chart-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.chart-toggle').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeMetric = btn.dataset.metric;
    if (dashboardData) renderChart(dashboardData.summaries, activeMetric);
  });
});

loadDashboard().then(populateEntityMetrics);
setInterval(loadDashboard, 5 * 60_000); // refresh every 5 min

// ---- Entities tab ----
// Lets the user pick any numeric metric(s) from the daily summaries and
// render a custom Chart.js chart over a chosen day range.
let entitiesChart = null;

// Pretty-printable labels for known metrics; unknown ones fall through to
// their raw camelCase key.
const ENTITY_LABELS = {
  sleepScore: 'Sleep score',
  restingHeartRate: 'Resting HR (bpm)',
  avgOvernightHrv: 'Overnight HRV (ms)',
  bodyBatteryAtWakeTime: 'Body battery (wake)',
  bodyBatteryHighestValue: 'Body battery (peak)',
  bodyBatteryLowestValue: 'Body battery (low)',
  bodyBatteryChange: 'Body battery delta',
  totalSteps: 'Steps',
  stressPercentage: 'Stress %',
  highStressPercentage: 'High-stress %',
  averageSpo2: 'SpO2 avg (%)',
  averageRespirationValue: 'Respiration (rpm)',
  deepSleepSeconds: 'Deep sleep (s)',
  remSleepSeconds: 'REM sleep (s)',
  lightSleepSeconds: 'Light sleep (s)',
  awakeSleepSeconds: 'Awake (s)',
  sleepTimeSeconds: 'Total sleep (s)',
  awakeCount: 'Awakenings',
  restlessMomentsCount: 'Restless moments',
  avgSleepStress: 'Sleep stress avg',
  moderateIntensityMinutes: 'Moderate-intensity min',
  vigorousIntensityMinutes: 'Vigorous-intensity min',
};

// Excluded from the picker: non-numeric, identifier, or metadata fields.
const ENTITY_EXCLUDE = new Set(['date', 'is_complete', 'lifestyle']);

const ENTITY_COLORS = [
  '#4f9cf9', '#34d399', '#fbbf24', '#f87171',
  '#7c6af7', '#22d3ee', '#f472b6', '#a78bfa',
];

function entityLabel(key) {
  return ENTITY_LABELS[key] || key;
}

function isNumericMetric(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function collectAvailableMetrics(summaries) {
  // Union of numeric keys across all summaries — handles fields that only
  // appear on some days (e.g. activity metrics on workout days).
  const keys = new Set();
  for (const s of summaries) {
    for (const [k, v] of Object.entries(s)) {
      if (ENTITY_EXCLUDE.has(k)) continue;
      if (isNumericMetric(v)) keys.add(k);
    }
  }
  return [...keys].sort((a, b) => entityLabel(a).localeCompare(entityLabel(b)));
}

function populateEntityMetrics() {
  const container = document.getElementById('entities-metrics');
  if (!container) return;
  if (!dashboardData || !dashboardData.summaries?.length) {
    container.innerHTML = '<em>No cached daily summaries yet — run the fetcher first.</em>';
    return;
  }
  const metrics = collectAvailableMetrics(dashboardData.summaries);
  if (!metrics.length) {
    container.innerHTML = '<em>No numeric metrics found in the cache.</em>';
    return;
  }
  // Preserve any existing selections across re-populations
  const previouslyChecked = new Set(
    [...container.querySelectorAll('input[type=checkbox]:checked')].map(el => el.value)
  );
  container.innerHTML = metrics.map(k => {
    const checked = previouslyChecked.has(k) ? 'checked' : '';
    return `<label><input type="checkbox" value="${escapeHtml(k)}" ${checked}> ${escapeHtml(entityLabel(k))}</label>`;
  }).join('');
}

function selectedEntityMetrics() {
  return [...document.querySelectorAll('#entities-metrics input[type=checkbox]:checked')]
    .map(el => el.value);
}

function renderEntitiesChart() {
  const metrics = selectedEntityMetrics();
  const days = parseInt(document.getElementById('entities-days').value, 10);
  const type = document.getElementById('entities-type').value;
  const container = document.getElementById('entities-chart-container');
  const emptyMsg = document.getElementById('entities-empty');

  if (!metrics.length || !dashboardData?.summaries) {
    container.classList.remove('active');
    emptyMsg.textContent = 'Pick at least one metric, then click Build.';
    emptyMsg.style.display = '';
    return;
  }

  const sorted = [...dashboardData.summaries]
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(-days);
  const labels = sorted.map(s => s.date.slice(5));

  const datasets = metrics.map((key, i) => {
    const color = ENTITY_COLORS[i % ENTITY_COLORS.length];
    return {
      label: entityLabel(key),
      data: sorted.map(s => (s[key] != null ? s[key] : null)),
      borderColor: color,
      backgroundColor: type === 'bar' ? color : color + '22',
      tension: 0.3,
      spanGaps: true,
      pointRadius: type === 'line' ? 3 : 0,
      borderWidth: 2,
    };
  });

  if (entitiesChart) entitiesChart.destroy();
  entitiesChart = new Chart(document.getElementById('entities-chart'), {
    type,
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: commonScales(),
      plugins: commonPlugins(),
    },
  });
  container.classList.add('active');
  emptyMsg.style.display = 'none';
}

// Wire up Entities controls (deferred so DOM exists)
document.getElementById('entities-build')?.addEventListener('click', renderEntitiesChart);
document.getElementById('entities-clear')?.addEventListener('click', () => {
  document.querySelectorAll('#entities-metrics input[type=checkbox]')
    .forEach(el => { el.checked = false; });
  if (entitiesChart) { entitiesChart.destroy(); entitiesChart = null; }
  document.getElementById('entities-chart-container').classList.remove('active');
  const emptyMsg = document.getElementById('entities-empty');
  emptyMsg.textContent = 'No chart yet — pick at least one metric and click Build.';
  emptyMsg.style.display = '';
});

// Re-populate the metric list whenever the tab is opened (handles the case
// where dashboardData loaded after the initial render)
document.querySelector('.tab-btn[data-tab="entities"]')?.addEventListener('click', populateEntityMetrics);

// ---- AI Scan ----
document.querySelectorAll('.scan-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const focus = btn.dataset.focus;
    const output = document.getElementById('scan-output');
    document.querySelectorAll('.scan-btn').forEach(b => b.disabled = true);
    output.classList.remove('hidden');
    output.innerHTML = '<em>Running scan, please wait...</em>';

    try {
      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ focus }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      output.innerHTML = marked.parse(data.report || '(no report)');
    } catch (e) {
      output.innerHTML = `<span style="color:var(--red)">Error: ${e.message}</span>`;
    } finally {
      document.querySelectorAll('.scan-btn').forEach(b => b.disabled = false);
    }
  });
});

// ---- Chat ----
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const resetBtn = document.getElementById('reset-btn');

// Per-browser session id, persisted in localStorage
const SESSION_KEY = 'garmin-chat-session';
let sessionId = localStorage.getItem(SESSION_KEY) || null;

// Auto-resize textarea
chatInput.addEventListener('input', () => {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + 'px';
});

// Send on Enter (Shift+Enter = newline)
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

sendBtn.addEventListener('click', sendMessage);

resetBtn.addEventListener('click', async () => {
  if (sessionId) {
    await fetch('/api/chat/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });
  }
  chatMessages.innerHTML = `
    <div class="message assistant">
      <div class="message-bubble">
        <strong>Health Agent</strong>
        <p>Conversation cleared. What would you like to know about your health data?</p>
      </div>
    </div>`;
});

function scrollToBottom() {
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addMessage(role, html, extraClass = '') {
  const div = document.createElement('div');
  div.className = `message ${role} ${extraClass}`.trim();
  div.innerHTML = `<div class="message-bubble">${html}</div>`;
  chatMessages.appendChild(div);
  scrollToBottom();
  return div;
}

function addTypingIndicator() {
  if (document.getElementById('typing-indicator')) return;
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'typing-indicator';
  div.innerHTML = `<div class="message-bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>`;
  chatMessages.appendChild(div);
  scrollToBottom();
}

function removeTypingIndicator() {
  document.getElementById('typing-indicator')?.remove();
}

async function sendMessage() {
  const text = chatInput.value.trim();
  if (!text) return;

  chatInput.value = '';
  chatInput.style.height = 'auto';
  sendBtn.disabled = true;
  chatInput.disabled = true;

  addMessage('user', escapeHtml(text).replace(/\n/g, '<br>'));
  addTypingIndicator();

  let assistantDiv = null;
  let assistantContent = '';

  function ensureAssistantBubble() {
    if (!assistantDiv) {
      removeTypingIndicator();
      assistantDiv = addMessage('assistant', `<strong>Health Agent</strong><div class="md-content"></div>`);
    }
    return assistantDiv.querySelector('.md-content');
  }

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    outer: while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') break outer;

        let evt;
        try { evt = JSON.parse(raw); } catch { continue; }

        switch (evt.type) {
          case 'session':
            // Server assigned (or confirmed) our session id
            sessionId = evt.session_id;
            localStorage.setItem(SESSION_KEY, sessionId);
            break;
          case 'tool':
            // Tool dispatch — show a status message before the next round
            const names = (evt.names || []).join(', ');
            addMessage('assistant', `<em>Querying: ${escapeHtml(names)}…</em>`, 'tool-status');
            // Reset the assistant bubble so the next round starts a new bubble
            assistantDiv = null;
            assistantContent = '';
            addTypingIndicator();
            break;
          case 'text':
            assistantContent += evt.text || '';
            ensureAssistantBubble().innerHTML = marked.parse(assistantContent);
            scrollToBottom();
            break;
          case 'error':
            removeTypingIndicator();
            addMessage('assistant', `<strong>Health Agent</strong><p style="color:var(--red)">Error: ${escapeHtml(evt.error)}</p>`);
            break outer;
        }
      }
    }
  } catch (e) {
    removeTypingIndicator();
    addMessage('assistant', `<strong>Health Agent</strong><p style="color:var(--red)">Connection error: ${escapeHtml(e.message)}</p>`);
  } finally {
    removeTypingIndicator();
    sendBtn.disabled = false;
    chatInput.disabled = false;
    chatInput.focus();
  }
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
