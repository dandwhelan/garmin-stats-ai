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

// Date range state — null means "use default" (30d)
let selectedStart = null;
let selectedEnd = null;

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
  const recent = [...summaries].sort((a, b) => a.date.localeCompare(b.date));
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
  const recent = [...summaries].sort((a, b) => a.date.localeCompare(b.date));
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
  const recent = [...summaries].sort((a, b) => a.date.localeCompare(b.date));
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
  const recent = [...summaries].sort((a, b) => a.date.localeCompare(b.date));
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

function buildDashboardUrl() {
  const params = new URLSearchParams();
  if (selectedStart) params.set('start', selectedStart);
  if (selectedEnd) params.set('end', selectedEnd);
  const qs = params.toString();
  return qs ? `/api/dashboard?${qs}` : '/api/dashboard';
}

function updateChartTitles(dateRange) {
  const { start, end } = dateRange;
  const days = Math.round((new Date(end) - new Date(start)) / 86400000) + 1;
  const label = selectedStart ? `${start} → ${end}` : `${days}-Day`;
  const title = document.getElementById('trend-chart-title');
  if (title) title.textContent = `${label} Trend`;
  const sleepTitle = document.getElementById('sleep-chart-title');
  if (sleepTitle) sleepTitle.textContent = `Sleep Architecture (${label})`;
}

async function loadDashboard() {
  try {
    const res = await fetch(buildDashboardUrl());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    dashboardData = await res.json();
    const { summaries, baselines, date_range } = dashboardData;
    updateChartTitles(date_range);
    renderCards(summaries, baselines);
    renderChart(summaries, activeMetric);
    renderSleepArchitecture(summaries);
    renderRecoveryChart(summaries, baselines);
    renderActivityChart(summaries);
    renderStressChart(summaries);
    loadVisualizations(date_range.start, date_range.end);
    loadIntradayHeatmap(activeHeatmapMetric);
  } catch (e) {
    console.error('Dashboard load failed:', e);
  }
}

// ---- Auxiliary visualizations ----
let vizData = null;
let activeBehaviorMetric = 'sleep';
let activeHeatmapMetric = 'stress';

async function loadVisualizations(start, end) {
  try {
    const params = new URLSearchParams();
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    const res = await fetch(`/api/visualizations?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    vizData = await res.json();
    renderAcwrChart(vizData.training);
    renderReadinessChart(vizData.training);
    renderSleepTimeline(vizData.sleep_timeline);
    renderBodyComposition(vizData.body_composition);
    renderBehaviorImpact(vizData.behavior_impact, activeBehaviorMetric);
    renderAnomalyCalendar(vizData.anomaly_calendar);
    renderHrZones(vizData.hr_zones);
    renderCorrelationMatrix(vizData.correlations);
  } catch (e) {
    console.error('Visualizations load failed:', e);
  }
}

async function loadIntradayHeatmap(metric) {
  try {
    const res = await fetch(`/api/intraday/heatmap?metric=${metric}&days=14`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderIntradayHeatmap(data);
  } catch (e) {
    console.error('Intraday heatmap load failed:', e);
  }
}

function renderAcwrChart(training) {
  const ts = training?.training_status || [];
  const labels = ts.map(r => r.date.slice(5));

  destroyAux('acwr');
  const ctx = document.getElementById('acwr-chart');
  if (!ctx) return;
  auxCharts.acwr = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Acute load (7d)',
          data: ts.map(r => r.acute_load ?? null),
          borderColor: '#f87171',
          backgroundColor: 'rgba(248,113,113,0.1)',
          tension: 0.3, spanGaps: true, yAxisID: 'y',
        },
        {
          label: 'Chronic load (28d)',
          data: ts.map(r => r.chronic_load ?? null),
          borderColor: '#4f9cf9',
          backgroundColor: 'rgba(79,156,249,0.1)',
          tension: 0.3, spanGaps: true, yAxisID: 'y',
        },
        {
          label: 'ACWR (%)',
          data: ts.map(r => r.acwr_percent ?? null),
          borderColor: '#fbbf24',
          backgroundColor: 'transparent',
          borderDash: [4, 4],
          tension: 0.3, spanGaps: true, yAxisID: 'y1',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: commonScales().x,
        y: { ...commonScales('load').y, position: 'left' },
        y1: { ...commonScales('ACWR %').y, position: 'right', grid: { drawOnChartArea: false } },
      },
      plugins: commonPlugins(),
    },
  });
}

function renderReadinessChart(training) {
  const tr = training?.training_readiness || [];
  const labels = tr.map(r => r.date.slice(5));

  destroyAux('readiness');
  const ctx = document.getElementById('readiness-chart');
  if (!ctx) return;
  auxCharts.readiness = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Sleep',     data: tr.map(r => r.f_sleep ?? 0),    backgroundColor: '#4f9cf9' },
        { label: 'Recovery',  data: tr.map(r => r.f_recovery ?? 0), backgroundColor: '#34d399' },
        { label: 'ACWR',      data: tr.map(r => r.f_acwr ?? 0),     backgroundColor: '#fbbf24' },
        { label: 'Stress',    data: tr.map(r => r.f_stress ?? 0),   backgroundColor: '#f87171' },
        { label: 'HRV',       data: tr.map(r => r.f_hrv ?? 0),      backgroundColor: '#7c6af7' },
        {
          label: 'Score',
          type: 'line',
          data: tr.map(r => r.score ?? null),
          borderColor: '#e2e8f0',
          backgroundColor: 'transparent',
          tension: 0.3, spanGaps: true, pointRadius: 3,
          yAxisID: 'y1',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ...commonScales().x },
        y: { stacked: true, ...commonScales('factor %').y, beginAtZero: true },
        y1: { ...commonScales('score').y, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false } },
      },
      plugins: commonPlugins(),
    },
  });
}

function renderSleepTimeline(timeline) {
  const data = timeline || [];
  const labels = data.map(r => r.date.slice(5));

  destroyAux('sleepTimeline');
  const ctx = document.getElementById('sleep-timeline-chart');
  if (!ctx) return;

  // Plot bedtime (hours past noon, e.g. 23:00 = 23) and waketime separately.
  // To get meaningful "sleep band", plot bedtime as start, waketime as end.
  auxCharts.sleepTimeline = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Bedtime',
          data: data.map(r => r.bedtime),
          borderColor: '#7c6af7',
          backgroundColor: 'transparent',
          tension: 0.25,
          pointRadius: 3, spanGaps: true,
        },
        {
          label: 'Waketime',
          data: data.map(r => r.waketime),
          borderColor: '#fbbf24',
          backgroundColor: 'transparent',
          tension: 0.25,
          pointRadius: 3, spanGaps: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: commonScales().x,
        y: {
          ...commonScales('hour of day').y,
          ticks: {
            color: '#8892a4',
            callback: v => {
              const h = ((v % 24) + 24) % 24;
              return `${Math.floor(h).toString().padStart(2, '0')}:${Math.round((h % 1) * 60).toString().padStart(2, '0')}`;
            },
          },
          reverse: false,
        },
      },
      plugins: commonPlugins({
        tooltip: {
          backgroundColor: '#22263a',
          borderColor: '#2e3350',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#8892a4',
          callbacks: {
            label: ctx => {
              const v = ctx.parsed.y;
              if (v == null) return `${ctx.dataset.label}: —`;
              const h = ((v % 24) + 24) % 24;
              const hh = Math.floor(h).toString().padStart(2, '0');
              const mm = Math.round((h % 1) * 60).toString().padStart(2, '0');
              return `${ctx.dataset.label}: ${hh}:${mm}`;
            },
          },
        },
      }),
    },
  });
}

function renderBodyComposition(records) {
  const data = records || [];
  const labels = data.map(r => r.date.slice(5));

  destroyAux('bodyComp');
  const ctx = document.getElementById('body-comp-chart');
  if (!ctx) return;

  if (!data.length) {
    auxCharts.bodyComp = new Chart(ctx, {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } },
    });
    return;
  }

  auxCharts.bodyComp = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Weight (kg)',
          data: data.map(r => r.weight ?? null),
          borderColor: '#4f9cf9',
          backgroundColor: 'rgba(79,156,249,0.1)',
          tension: 0.3, spanGaps: true, yAxisID: 'y',
        },
        {
          label: 'Body fat %',
          data: data.map(r => r.body_fat ?? null),
          borderColor: '#f87171',
          backgroundColor: 'transparent',
          tension: 0.3, spanGaps: true, yAxisID: 'y1',
        },
        {
          label: 'Muscle mass',
          data: data.map(r => r.muscle_mass ?? null),
          borderColor: '#34d399',
          backgroundColor: 'transparent',
          tension: 0.3, spanGaps: true, yAxisID: 'y',
          borderDash: [4, 4],
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: commonScales().x,
        y: { ...commonScales('kg').y, position: 'left' },
        y1: { ...commonScales('%').y, position: 'right', grid: { drawOnChartArea: false } },
      },
      plugins: commonPlugins(),
    },
  });
}

function renderBehaviorImpact(rows, metric) {
  const data = (rows || []).filter(r => r[`${metric}_with`] != null && r[`${metric}_without`] != null);
  const labels = data.map(r => r.behavior);
  const ctx = document.getElementById('behavior-impact-chart');
  if (!ctx) return;

  destroyAux('behavior');

  const note = document.getElementById('behavior-note');
  if (note) {
    note.textContent = data.length
      ? `Showing ${data.length} behavior${data.length === 1 ? '' : 's'} with ≥3 occurrences. Bars show the average ${metric.toUpperCase()} on days WITH vs WITHOUT the behavior.`
      : 'Not enough lifestyle journal entries to compare. Log behaviours in Garmin Connect to populate this chart.';
  }

  auxCharts.behavior = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: `Without (${metric})`,
          data: data.map(r => r[`${metric}_without`]),
          backgroundColor: '#3a3f5a',
        },
        {
          label: `With (${metric})`,
          data: data.map(r => r[`${metric}_with`]),
          backgroundColor: '#4f9cf9',
        },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { color: '#8892a4' }, grid: { color: '#2e3350' } },
        y: { ticks: { color: '#8892a4' }, grid: { color: '#2e3350' } },
      },
      plugins: commonPlugins({
        tooltip: {
          backgroundColor: '#22263a',
          borderColor: '#2e3350',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#8892a4',
          callbacks: {
            afterBody: items => {
              const i = items[0]?.dataIndex;
              if (i == null) return '';
              const r = data[i];
              const delta = r[`${metric}_delta`];
              const sign = delta >= 0 ? '+' : '';
              return [`Δ ${sign}${delta} (${r.n_with} with / ${r.n_without} without)`];
            },
          },
        },
      }),
    },
  });
}

function renderAnomalyCalendar(payload) {
  const container = document.getElementById('anomaly-calendar');
  if (!container) return;
  container.innerHTML = '';

  const { dates = [], keys = [], matrix = [] } = payload || {};
  if (!dates.length) {
    container.innerHTML = '<div class="empty-state">No data in range</div>';
    return;
  }

  const labelMap = {
    sleepScore: 'Sleep',
    avgOvernightHrv: 'HRV',
    restingHeartRate: 'RHR',
    stressPercentage: 'Stress',
    bodyBatteryAtWakeTime: 'Battery',
  };

  // For metrics where lower is better (RHR, stress), flip sign so green = good.
  const inverted = new Set(['restingHeartRate', 'stressPercentage']);

  function colorFor(z, key) {
    if (z == null) return '#1a1d27';
    const adj = inverted.has(key) ? -z : z;
    // Clamp z between -3 and +3
    const c = Math.max(-3, Math.min(3, adj));
    if (c >= 0) {
      const a = Math.min(1, c / 2);
      return `rgba(52, 211, 153, ${0.15 + a * 0.7})`;
    }
    const a = Math.min(1, -c / 2);
    return `rgba(248, 113, 113, ${0.15 + a * 0.7})`;
  }

  const grid = document.createElement('div');
  grid.className = 'anomaly-grid';
  grid.style.gridTemplateColumns = `120px repeat(${dates.length}, 1fr)`;

  // Header row
  grid.appendChild(document.createElement('div'));
  dates.forEach(d => {
    const h = document.createElement('div');
    h.className = 'anomaly-col-label';
    h.textContent = d.slice(5);
    grid.appendChild(h);
  });

  keys.forEach((key, i) => {
    const label = document.createElement('div');
    label.className = 'anomaly-row-label';
    label.textContent = labelMap[key] || key;
    grid.appendChild(label);
    matrix[i].forEach((z, j) => {
      const cell = document.createElement('div');
      cell.className = 'anomaly-cell';
      cell.style.background = colorFor(z, key);
      cell.title = `${labelMap[key] || key} · ${dates[j]}\nz = ${z == null ? '—' : z.toFixed(2)}`;
      grid.appendChild(cell);
    });
  });

  container.appendChild(grid);
}

function renderHrZones(payload) {
  const rows = payload?.by_type || [];
  const ctx = document.getElementById('hr-zones-chart');
  if (!ctx) return;

  destroyAux('hrZones');
  auxCharts.hrZones = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.activity_type),
      datasets: [
        { label: 'Z1 warm-up',  data: rows.map(r => r.z1), backgroundColor: '#4f9cf9' },
        { label: 'Z2 easy',     data: rows.map(r => r.z2), backgroundColor: '#34d399' },
        { label: 'Z3 aerobic',  data: rows.map(r => r.z3), backgroundColor: '#fbbf24' },
        { label: 'Z4 threshold', data: rows.map(r => r.z4), backgroundColor: '#fb923c' },
        { label: 'Z5 max',      data: rows.map(r => r.z5), backgroundColor: '#f87171' },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ...commonScales('minutes').x, ticks: { color: '#8892a4' } },
        y: { stacked: true, ticks: { color: '#8892a4' }, grid: { color: '#2e3350' } },
      },
      plugins: commonPlugins(),
    },
  });
}

function renderCorrelationMatrix(payload) {
  const container = document.getElementById('correlation-matrix');
  if (!container) return;
  container.innerHTML = '';

  const { keys = [], matrix = [] } = payload || {};
  if (!keys.length || !matrix.length) {
    container.innerHTML = '<div class="empty-state">Not enough data</div>';
    return;
  }

  const shortNames = {
    sleepScore: 'Sleep',
    avgOvernightHrv: 'HRV',
    restingHeartRate: 'RHR',
    stressPercentage: 'Stress',
    bodyBatteryAtWakeTime: 'Battery',
    totalSteps: 'Steps',
    deepSleepSeconds: 'Deep',
    remSleepSeconds: 'REM',
    moderateIntensityMinutes: 'Mod min',
    vigorousIntensityMinutes: 'Vig min',
  };

  function colorFor(v) {
    if (v == null || isNaN(v)) return '#1a1d27';
    if (v >= 0) {
      const a = Math.min(1, v);
      return `rgba(79, 156, 249, ${0.15 + a * 0.7})`;
    }
    const a = Math.min(1, -v);
    return `rgba(248, 113, 113, ${0.15 + a * 0.7})`;
  }

  const grid = document.createElement('div');
  grid.className = 'correlation-grid';
  grid.style.gridTemplateColumns = `100px repeat(${keys.length}, minmax(60px, 1fr))`;

  // Header row
  grid.appendChild(document.createElement('div'));
  keys.forEach(k => {
    const h = document.createElement('div');
    h.className = 'corr-col-label';
    h.textContent = shortNames[k] || k;
    grid.appendChild(h);
  });

  keys.forEach((rowKey, i) => {
    const lbl = document.createElement('div');
    lbl.className = 'corr-row-label';
    lbl.textContent = shortNames[rowKey] || rowKey;
    grid.appendChild(lbl);
    matrix[i].forEach((v, j) => {
      const cell = document.createElement('div');
      cell.className = 'corr-cell';
      cell.style.background = colorFor(v);
      cell.textContent = (v ?? 0).toFixed(2);
      cell.title = `${shortNames[rowKey] || rowKey} ↔ ${shortNames[keys[j]] || keys[j]}: ${v == null ? '—' : v.toFixed(2)}`;
      grid.appendChild(cell);
    });
  });

  container.appendChild(grid);
}

function renderIntradayHeatmap(data) {
  const container = document.getElementById('intraday-heatmap');
  if (!container) return;
  container.innerHTML = '';

  const { dates = [], hours = [], matrix = [], metric } = data || {};
  if (!dates.length) {
    container.innerHTML = '<div class="empty-state">No intraday data available</div>';
    return;
  }

  // Determine value range for the metric
  let min = Infinity, max = -Infinity;
  matrix.forEach(row => row.forEach(v => {
    if (v != null) { if (v < min) min = v; if (v > max) max = v; }
  }));
  if (!isFinite(min) || !isFinite(max) || min === max) { min = 0; max = 100; }

  // Color: stress (red high), body_battery (green high), heart_rate (orange high)
  const palette = {
    stress:        ['#1a1d27', '#fbbf24', '#f87171'],
    body_battery:  ['#1a1d27', '#4f9cf9', '#34d399'],
    heart_rate:    ['#1a1d27', '#7c6af7', '#f87171'],
  };
  const stops = palette[metric] || palette.stress;

  function lerp(a, b, t) { return a + (b - a) * t; }
  function hexToRgb(h) {
    const x = h.replace('#', '');
    return [parseInt(x.slice(0, 2), 16), parseInt(x.slice(2, 4), 16), parseInt(x.slice(4, 6), 16)];
  }
  const c0 = hexToRgb(stops[0]), c1 = hexToRgb(stops[1]), c2 = hexToRgb(stops[2]);
  function colorFor(v) {
    if (v == null) return '#0f1117';
    const t = (v - min) / (max - min);
    const c = t < 0.5
      ? c0.map((x, i) => lerp(x, c1[i], t * 2))
      : c1.map((x, i) => lerp(x, c2[i], (t - 0.5) * 2));
    return `rgb(${c.map(Math.round).join(',')})`;
  }

  const grid = document.createElement('div');
  grid.className = 'heatmap-grid';
  grid.style.gridTemplateColumns = `60px repeat(24, 1fr)`;

  // Header
  grid.appendChild(document.createElement('div'));
  hours.forEach(h => {
    const el = document.createElement('div');
    el.className = 'heatmap-hour-label';
    el.textContent = h % 3 === 0 ? `${h}h` : '';
    grid.appendChild(el);
  });

  dates.forEach((d, i) => {
    const lbl = document.createElement('div');
    lbl.className = 'heatmap-date-label';
    lbl.textContent = d.slice(5);
    grid.appendChild(lbl);
    matrix[i].forEach((v, h) => {
      const cell = document.createElement('div');
      cell.className = 'heatmap-cell';
      cell.style.background = colorFor(v);
      cell.title = `${d} ${h.toString().padStart(2, '0')}:00 — ${v == null ? '—' : v}`;
      grid.appendChild(cell);
    });
  });

  container.appendChild(grid);

  const legend = document.getElementById('intraday-heatmap-legend');
  if (legend) {
    legend.innerHTML = `
      <span>${metric}: <strong>${min.toFixed(0)}</strong></span>
      <span class="heatmap-gradient" style="background: linear-gradient(to right, ${stops[0]}, ${stops[1]}, ${stops[2]})"></span>
      <span><strong>${max.toFixed(0)}</strong></span>
    `;
  }
}

// Toggle handlers for new charts
document.querySelectorAll('.heatmap-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.heatmap-toggle').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeHeatmapMetric = btn.dataset.metric;
    loadIntradayHeatmap(activeHeatmapMetric);
  });
});

document.querySelectorAll('.behavior-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.behavior-toggle').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeBehaviorMetric = btn.dataset.metric;
    if (vizData) renderBehaviorImpact(vizData.behavior_impact, activeBehaviorMetric);
  });
});

// Chart metric toggles
document.querySelectorAll('.chart-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.chart-toggle').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeMetric = btn.dataset.metric;
    if (dashboardData) renderChart(dashboardData.summaries, activeMetric);
  });
});

// ---- Date range controls ----
const dateStartInput = document.getElementById('date-start');
const dateEndInput = document.getElementById('date-end');

function setActivePreset(days) {
  document.querySelectorAll('.preset-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.days === String(days));
  });
}

function applyPreset(days) {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - (days - 1));
  selectedEnd = end.toISOString().slice(0, 10);
  selectedStart = start.toISOString().slice(0, 10);
  dateStartInput.value = selectedStart;
  dateEndInput.value = selectedEnd;
  setActivePreset(days);
  loadDashboard();
}

document.querySelectorAll('.preset-btn').forEach(btn => {
  btn.addEventListener('click', () => applyPreset(Number(btn.dataset.days)));
});

document.getElementById('date-apply-btn').addEventListener('click', () => {
  const s = dateStartInput.value;
  const e = dateEndInput.value;
  if (!s || !e) return;
  if (s > e) { alert('Start date must be before end date.'); return; }
  selectedStart = s;
  selectedEnd = e;
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
  loadDashboard();
});

// Initialise date inputs with default 30d range
(function initDateInputs() {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - 29);
  dateStartInput.value = start.toISOString().slice(0, 10);
  dateEndInput.value = end.toISOString().slice(0, 10);
  dateStartInput.max = end.toISOString().slice(0, 10);
  dateEndInput.max = end.toISOString().slice(0, 10);
})();

loadDashboard();
setInterval(loadDashboard, 5 * 60_000); // refresh every 5 min

// ---- AI Scan ----
const scanDateStart = document.getElementById('scan-date-start');
const scanDateEnd = document.getElementById('scan-date-end');
const scanDateClear = document.getElementById('scan-date-clear');

if (scanDateClear) {
  scanDateClear.addEventListener('click', () => {
    scanDateStart.value = '';
    scanDateEnd.value = '';
  });
}

document.querySelectorAll('.scan-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const focus = btn.dataset.focus;
    const output = document.getElementById('scan-output');
    document.querySelectorAll('.scan-btn').forEach(b => b.disabled = true);
    output.classList.remove('hidden');

    const startVal = scanDateStart?.value || null;
    const endVal = scanDateEnd?.value || null;

    if (startVal && endVal && startVal > endVal) {
      output.innerHTML = `<span style="color:var(--red)">Start date must be before end date.</span>`;
      document.querySelectorAll('.scan-btn').forEach(b => b.disabled = false);
      return;
    }

    const dateNote = startVal && endVal ? ` <em style="color:var(--muted);font-size:0.85em">(${startVal} → ${endVal})</em>` : '';
    output.innerHTML = `<em>Running scan, please wait...${dateNote}</em>`;

    const body = { focus };
    if (startVal) body.start_date = startVal;
    if (endVal) body.end_date = endVal;

    try {
      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
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
