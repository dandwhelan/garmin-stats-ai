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

// ---- Health check / status dot ----
const statusDot = document.getElementById('status-dot');
async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    if (res.ok) {
      statusDot.className = 'status-dot ok';
      statusDot.title = 'Connected';
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

async function loadDashboard() {
  try {
    const res = await fetch('/api/dashboard');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    dashboardData = await res.json();
    const { summaries, baselines } = dashboardData;
    renderCards(summaries, baselines);
    renderChart(summaries, activeMetric);
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

loadDashboard();
setInterval(loadDashboard, 5 * 60_000); // refresh every 5 min

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
  await fetch('/api/chat/reset', { method: 'POST' });
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

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') break;

        let payload;
        try { payload = JSON.parse(raw); } catch { continue; }

        if (payload.error) {
          removeTypingIndicator();
          addMessage('assistant', `<strong>Health Agent</strong><p style="color:var(--red)">Error: ${escapeHtml(payload.error)}</p>`);
          return;
        }

        const chunk = payload.text || '';
        if (!chunk) continue;

        // Tool status messages (italics lines)
        if (chunk.startsWith('_') && chunk.includes('Querying:')) {
          removeTypingIndicator();
          addMessage('assistant', escapeHtml(chunk), 'tool-status');
          addTypingIndicator();
          continue;
        }

        // Regular text — build up the assistant bubble
        removeTypingIndicator();
        assistantContent += chunk;

        if (!assistantDiv) {
          assistantDiv = addMessage('assistant', `<strong>Health Agent</strong><div class="md-content"></div>`);
        }
        assistantDiv.querySelector('.md-content').innerHTML = marked.parse(assistantContent);
        scrollToBottom();
      }
    }
  } catch (e) {
    removeTypingIndicator();
    addMessage('assistant', `<strong>Health Agent</strong><p style="color:var(--red)">Connection error: ${escapeHtml(e.message)}</p>`);
  } finally {
    sendBtn.disabled = false;
    chatInput.disabled = false;
    chatInput.focus();
    removeTypingIndicator();
  }
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
