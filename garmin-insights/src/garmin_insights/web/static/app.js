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
    loadLifestyle(date_range.start, date_range.end);
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

/* =========================================================
   Lifestyle visualizations (19 charts)
   ========================================================= */

let lifestyleData = null;
let activeDoseBehavior = null;

async function loadLifestyle(start, end) {
  try {
    const params = new URLSearchParams();
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    const res = await fetch(`/api/lifestyle?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    lifestyleData = await res.json();
    renderIllnessRadar(lifestyleData.illness_radar);
    renderRecoveryDebt(lifestyleData.recovery_debt);
    renderInflammation(lifestyleData.inflammation_index);
    renderSRI(lifestyleData.sleep_regularity);
    renderSocialJetLag(lifestyleData.social_jet_lag);
    renderResilience(lifestyleData.stress_resilience);
    renderBBDecay(lifestyleData.body_battery_decay);
    renderRecoveryCost(lifestyleData.recovery_cost);
    renderDoseControls(lifestyleData.dose_response);
    renderCaffeineCutoff(lifestyleData.caffeine_cutoff);
    renderHabitHalfLife(lifestyleData.habit_half_life);
    renderStreakCalendar(lifestyleData.streak_calendar);
    renderCooccurrence(lifestyleData.cooccurrence);
    renderStressTriggers(lifestyleData.stress_triggers);
    renderStepCDF(lifestyleData.step_distribution);
    renderWhoTarget(lifestyleData.who_target);
    renderStressFingerprint(lifestyleData.stress_hour_fingerprint);
    renderFitnessAge(lifestyleData.fitness_age_delta);
    renderCycleHrv(lifestyleData.cycle_hrv);
  } catch (e) {
    console.error('Lifestyle load failed:', e);
  }
}

// 8. Illness radar
function renderIllnessRadar(data) {
  const series = data?.series || [];
  const labels = series.map(r => r.date.slice(5));
  destroyAux('illnessRadar');
  const ctx = document.getElementById('illness-radar-chart');
  if (!ctx) return;
  auxCharts.illnessRadar = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'RHR z',         data: series.map(r => r.z_rhr),     borderColor: '#f87171', backgroundColor: 'transparent', tension: 0.3, spanGaps: true },
        { label: 'HRV z (inv)',   data: series.map(r => r.z_hrv_inv), borderColor: '#fbbf24', backgroundColor: 'transparent', tension: 0.3, spanGaps: true },
        { label: 'Respiration z', data: series.map(r => r.z_resp),    borderColor: '#7c6af7', backgroundColor: 'transparent', tension: 0.3, spanGaps: true },
        { label: 'Composite',     data: series.map(r => r.composite), borderColor: '#e2e8f0', backgroundColor: 'rgba(226,232,240,0.08)', tension: 0.3, spanGaps: true, fill: true },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: { ...commonScales('z-score').y, suggestedMin: -2, suggestedMax: 3 } },
      plugins: commonPlugins(),
    },
  });
  const alertEl = document.getElementById('illness-alerts');
  const alerts = data?.alerts || [];
  if (alertEl) {
    alertEl.innerHTML = alerts.length
      ? alerts.map(a => `<div class="alert"><strong>${a.date}</strong> · ${a.note} (composite z=${a.composite})</div>`).join('')
      : '<div class="alert ok">No illness signature in the current window.</div>';
  }
}

// 10. Recovery debt
function renderRecoveryDebt(rows) {
  const data = rows || [];
  const labels = data.map(r => r.date.slice(5));
  destroyAux('recoveryDebt');
  const ctx = document.getElementById('recovery-debt-chart');
  if (!ctx) return;
  auxCharts.recoveryDebt = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Cumulative debt', data: data.map(r => r.cumulative_debt), borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.2)', tension: 0.3, spanGaps: true, fill: true, yAxisID: 'y' },
        { label: 'Wake battery',    data: data.map(r => r.wake_battery),    borderColor: '#34d399', backgroundColor: 'transparent', tension: 0.3, spanGaps: true, yAxisID: 'y1' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: commonScales().x,
        y: { ...commonScales('debt').y, position: 'left' },
        y1: { ...commonScales('battery').y, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false } },
      },
      plugins: commonPlugins(),
    },
  });
}

// 9. Inflammation index
function renderInflammation(rows) {
  const data = rows || [];
  destroyAux('inflammation');
  const ctx = document.getElementById('inflammation-chart');
  if (!ctx) return;
  auxCharts.inflammation = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(r => r.date.slice(5)),
      datasets: [{
        label: 'Inflammation z-sum',
        data: data.map(r => r.index),
        borderColor: '#fb923c',
        backgroundColor: 'rgba(251,146,60,0.15)',
        tension: 0.3, spanGaps: true, fill: true,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: commonScales('z-sum').y },
      plugins: commonPlugins({ legend: { display: false } }),
    },
  });
}

// 3. Sleep Regularity Index
function renderSRI(payload) {
  const series = payload?.series || [];
  destroyAux('sri');
  const ctx = document.getElementById('sri-chart');
  if (!ctx) return;
  auxCharts.sri = new Chart(ctx, {
    type: 'line',
    data: {
      labels: series.map(r => r.date.slice(5)),
      datasets: [{
        label: 'SRI',
        data: series.map(r => r.sri),
        borderColor: '#7c6af7',
        backgroundColor: 'rgba(124,106,247,0.15)',
        tension: 0.3, spanGaps: true, fill: true,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: { ...commonScales('SRI').y, min: 0, max: 100 } },
      plugins: commonPlugins({ legend: { display: false } }),
    },
  });
  const cur = document.getElementById('sri-current');
  if (cur) {
    const v = payload?.current;
    cur.textContent = v == null ? 'No data' : `Current SRI: ${v.toFixed(1)} / 100`;
  }
}

// 4. Social jet lag
function renderSocialJetLag(d) {
  const el = document.getElementById('social-jetlag');
  if (!el) return;
  if (!d || d.weekday_midpoint_h == null || d.weekend_midpoint_h == null) {
    el.innerHTML = '<div class="empty-state">Not enough sleep records</div>';
    return;
  }
  const fmt = h => {
    const x = ((h % 24) + 24) % 24;
    const hh = Math.floor(x).toString().padStart(2, '0');
    const mm = Math.round((x % 1) * 60).toString().padStart(2, '0');
    return `${hh}:${mm}`;
  };
  el.innerHTML = `
    <div class="clock">
      <div class="clock-label">Weekday midpoint</div>
      <div class="clock-time">${fmt(d.weekday_midpoint_h)}</div>
      <div class="clock-sub">n=${d.weekday_n}</div>
    </div>
    <div class="clock">
      <div class="clock-label">Weekend midpoint</div>
      <div class="clock-time">${fmt(d.weekend_midpoint_h)}</div>
      <div class="clock-sub">n=${d.weekend_n}</div>
    </div>
    <div class="clock delta ${d.delta_h > 1 ? 'warn' : 'ok'}">
      <div class="clock-label">Δ Social jet lag</div>
      <div class="clock-time">${d.delta_h.toFixed(2)}h</div>
      <div class="clock-sub">${d.delta_h > 1 ? '⚠ &gt; 1h is metabolically significant' : 'Within healthy range'}</div>
    </div>
  `;
}

// 6. Stress resilience
function renderResilience(rows) {
  const data = rows || [];
  destroyAux('resilience');
  const ctx = document.getElementById('resilience-chart');
  if (!ctx) return;
  auxCharts.resilience = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(r => r.date.slice(5)),
      datasets: [{
        label: 'Resilience',
        data: data.map(r => r.resilience),
        borderColor: '#34d399',
        backgroundColor: 'rgba(52,211,153,0.15)',
        tension: 0.3, spanGaps: true, fill: true,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: { ...commonScales('score').y, min: 0, max: 100 } },
      plugins: commonPlugins({ legend: { display: false } }),
    },
  });
}

// 7. Body battery decay slope
function renderBBDecay(rows) {
  const data = rows || [];
  destroyAux('bbDecay');
  const ctx = document.getElementById('bb-decay-chart');
  if (!ctx) return;
  auxCharts.bbDecay = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: data.map(r => r.date.slice(5)),
      datasets: [{
        label: 'Decay (pts/h)',
        data: data.map(r => r.decay_per_hour),
        backgroundColor: data.map(r => (r.decay_per_hour ?? 0) < -3 ? '#f87171' : '#7c6af7'),
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: commonScales('points/hour').y },
      plugins: commonPlugins({ legend: { display: false } }),
    },
  });
}

// 5. Behavior recovery cost
function renderRecoveryCost(rows) {
  const data = rows || [];
  destroyAux('recoveryCost');
  const ctx = document.getElementById('recovery-cost-chart');
  if (!ctx) return;
  auxCharts.recoveryCost = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: data.map(r => r.behavior),
      datasets: [{
        label: 'Median days to baseline',
        data: data.map(r => r.median_recovery_days),
        backgroundColor: '#fbbf24',
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { ...commonScales('days').x, ticks: { color: '#8892a4' } },
        y: { ticks: { color: '#8892a4' }, grid: { color: '#2e3350' } },
      },
      plugins: commonPlugins({
        tooltip: {
          backgroundColor: '#22263a', borderColor: '#2e3350', borderWidth: 1,
          titleColor: '#e2e8f0', bodyColor: '#8892a4',
          callbacks: {
            afterBody: items => {
              const r = data[items[0]?.dataIndex ?? 0];
              return r ? [`${r.n_events} events · max ${r.max_recovery_days}d`] : '';
            },
          },
        },
      }),
    },
  });
}

// 1. Behavior dose-response
function renderDoseControls(payload) {
  const behaviors = payload?.behaviors || [];
  const ctrl = document.getElementById('dose-controls');
  if (!ctrl) return;
  if (!behaviors.length) {
    ctrl.innerHTML = '';
    destroyAux('dose');
    const ctx = document.getElementById('dose-response-chart');
    if (ctx) {
      const c = ctx.getContext('2d');
      c.clearRect(0, 0, ctx.width, ctx.height);
    }
    return;
  }
  if (!activeDoseBehavior || !behaviors.find(b => b.behavior === activeDoseBehavior)) {
    activeDoseBehavior = behaviors[0].behavior;
  }
  ctrl.innerHTML = behaviors.map(b =>
    `<button class="dose-toggle ${b.behavior === activeDoseBehavior ? 'active' : ''}" data-behavior="${escapeHtml(b.behavior)}">${escapeHtml(b.behavior)} (${b.n})</button>`
  ).join('');
  ctrl.querySelectorAll('.dose-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      activeDoseBehavior = btn.dataset.behavior;
      renderDoseControls(payload);
      renderDoseResponse(payload);
    });
  });
  renderDoseResponse(payload);
}

function renderDoseResponse(payload) {
  const behaviors = payload?.behaviors || [];
  const target = behaviors.find(b => b.behavior === activeDoseBehavior);
  if (!target) return;
  const points = target.points || [];
  destroyAux('dose');
  const ctx = document.getElementById('dose-response-chart');
  if (!ctx) return;
  auxCharts.dose = new Chart(ctx, {
    type: 'scatter',
    data: {
      datasets: [
        { label: 'Sleep score',     data: points.map(p => ({x: p.value, y: p.sleepScore})),     backgroundColor: '#4f9cf9' },
        { label: 'HRV (ms)',        data: points.map(p => ({x: p.value, y: p.hrv})),            backgroundColor: '#34d399' },
        { label: 'Deep sleep (h)*10', data: points.map(p => ({x: p.value, y: p.deepSleepHours == null ? null : p.deepSleepHours * 10})), backgroundColor: '#7c6af7' },
        { label: 'RHR',             data: points.map(p => ({x: p.value, y: p.rhr})),            backgroundColor: '#f87171' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { ...commonScales(`${target.behavior} (logged value)`).x, type: 'linear' },
        y: commonScales().y,
      },
      plugins: commonPlugins(),
    },
  });
}

// 2. Caffeine cutoff comparison
function renderCaffeineCutoff(payload) {
  const el = document.getElementById('caffeine-cutoff');
  if (!el) return;
  const groups = payload?.groups || [];
  if (!groups.length || groups.every(g => g.n === 0)) {
    el.innerHTML = '<div class="empty-state">Log "Caffeine" / "Late Caffeine" in Garmin Connect to populate</div>';
    return;
  }
  el.innerHTML = `
    <div class="caffeine-row caffeine-head">
      <div></div><div>n</div><div>Sleep</div><div>Deep h</div><div>HRV</div><div>Wake-ups</div>
    </div>
    ${groups.map(g => `
      <div class="caffeine-row">
        <div class="caffeine-label">${escapeHtml(g.group)}</div>
        <div>${g.n}</div>
        <div>${g.sleep_score ?? '—'}</div>
        <div>${g.deep_sleep_h ?? '—'}</div>
        <div>${g.hrv ?? '—'}</div>
        <div>${g.awakenings ?? '—'}</div>
      </div>
    `).join('')}
  `;
}

// 12. Habit half-life
function renderHabitHalfLife(rows) {
  const el = document.getElementById('habit-half-life');
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="empty-state">No habits logged in last 90 days</div>';
    return;
  }
  el.innerHTML = rows.map(r => {
    const stale = r.days_since > 7 ? 'stale' : (r.days_since > 3 ? 'warn' : 'fresh');
    return `
      <div class="habit-row">
        <div class="habit-name">${escapeHtml(r.behavior)}</div>
        <div class="habit-meta">${r.frequency_30d}× /30d</div>
        <div class="habit-days ${stale}">${r.days_since}d ago</div>
      </div>`;
  }).join('');
}

// 11. Streak calendar
function renderStreakCalendar(payload) {
  const el = document.getElementById('streak-calendar');
  if (!el) return;
  el.innerHTML = '';
  const dates = payload?.dates || [];
  const behaviors = payload?.behaviors || [];
  if (!dates.length || !behaviors.length) {
    el.innerHTML = '<div class="empty-state">No lifestyle journal entries</div>';
    return;
  }
  const grid = document.createElement('div');
  grid.className = 'streak-grid';
  grid.style.gridTemplateColumns = `140px repeat(${dates.length}, 1fr)`;
  grid.appendChild(document.createElement('div'));
  dates.forEach((d, i) => {
    const lbl = document.createElement('div');
    lbl.className = 'streak-col-label';
    lbl.textContent = (i % 7 === 0) ? d.slice(5) : '';
    grid.appendChild(lbl);
  });
  behaviors.forEach(b => {
    const lbl = document.createElement('div');
    lbl.className = 'streak-row-label';
    lbl.textContent = `${b.behavior} (${b.count})`;
    grid.appendChild(lbl);
    b.cells.forEach((v, i) => {
      const cell = document.createElement('div');
      cell.className = 'streak-cell';
      cell.style.background = v == null ? '#1a1d27' : '#34d399';
      cell.title = `${b.behavior} · ${dates[i]} · ${v == null ? '—' : v}`;
      grid.appendChild(cell);
    });
  });
  el.appendChild(grid);
}

// 13. Co-occurrence
function renderCooccurrence(payload) {
  const el = document.getElementById('cooccurrence-matrix');
  if (!el) return;
  el.innerHTML = '';
  const keys = payload?.behaviors || [];
  const matrix = payload?.matrix || [];
  if (!keys.length) { el.innerHTML = '<div class="empty-state">No data</div>'; return; }
  let max = 0;
  matrix.forEach(row => row.forEach(v => { if (v > max) max = v; }));
  const grid = document.createElement('div');
  grid.className = 'correlation-grid';
  grid.style.gridTemplateColumns = `120px repeat(${keys.length}, minmax(50px, 1fr))`;
  grid.appendChild(document.createElement('div'));
  keys.forEach(k => {
    const h = document.createElement('div');
    h.className = 'corr-col-label';
    h.textContent = k;
    grid.appendChild(h);
  });
  keys.forEach((rk, i) => {
    const lbl = document.createElement('div');
    lbl.className = 'corr-row-label';
    lbl.textContent = rk;
    grid.appendChild(lbl);
    matrix[i].forEach((v, j) => {
      const cell = document.createElement('div');
      cell.className = 'corr-cell';
      const a = max ? v / max : 0;
      cell.style.background = `rgba(124, 106, 247, ${0.15 + a * 0.7})`;
      cell.textContent = v;
      cell.title = `${rk} & ${keys[j]}: ${v} days`;
      grid.appendChild(cell);
    });
  });
  el.appendChild(grid);
}

// 19. Stress trigger leaderboard
function renderStressTriggers(payload) {
  const el = document.getElementById('stress-triggers');
  if (!el) return;
  const triggers = payload?.triggers || [];
  if (!triggers.length) {
    el.innerHTML = '<div class="empty-state">Not enough stress + lifestyle overlap</div>';
    return;
  }
  const max = Math.max(...triggers.map(t => Math.abs(t.lift)), 0.001);
  el.innerHTML = `
    <div class="trigger-head">High-stress threshold: ${payload.top_quintile_threshold}% stress</div>
    ${triggers.map(t => {
      const w = Math.abs(t.lift) / max * 100;
      const color = t.lift > 0 ? 'var(--red)' : 'var(--green)';
      return `
        <div class="trigger-row">
          <div class="trigger-name">${escapeHtml(t.behavior)}</div>
          <div class="trigger-bar"><div class="trigger-fill" style="width:${w}%;background:${color}"></div></div>
          <div class="trigger-lift">${t.lift > 0 ? '+' : ''}${(t.lift * 100).toFixed(0)}pp</div>
        </div>`;
    }).join('')}
  `;
}

// 14. Step CDF
function renderStepCDF(payload) {
  const sorted = payload?.sorted_steps || [];
  destroyAux('stepCdf');
  const ctx = document.getElementById('step-cdf-chart');
  if (!ctx) return;
  // Build CDF: rank vs steps
  const points = sorted.map((v, i) => ({ x: 100 * i / Math.max(sorted.length - 1, 1), y: v }));
  auxCharts.stepCdf = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        { label: 'Steps survival',
          data: points,
          borderColor: '#4f9cf9', backgroundColor: 'rgba(79,156,249,0.1)',
          tension: 0.1, fill: true, pointRadius: 0 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      parsing: false,
      scales: {
        x: { ...commonScales('% of days').x, type: 'linear', min: 0, max: 100 },
        y: commonScales('steps').y,
      },
      plugins: commonPlugins({
        legend: { display: false },
        annotation: undefined,
      }),
    },
  });
  const stat = document.getElementById('step-cdf-stats');
  if (stat) {
    stat.innerHTML = payload && payload.median != null
      ? `Median ${payload.median.toLocaleString()} steps · ${payload.pct_over_7500}% of days ≥ 7.5k · ${payload.pct_over_10000}% ≥ 10k`
      : 'No step data';
  }
}

// 16. WHO target
function renderWhoTarget(payload) {
  const weeks = payload?.weeks || [];
  destroyAux('who');
  const ctx = document.getElementById('who-target-chart');
  if (!ctx) return;
  auxCharts.who = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: weeks.map(w => w.week.slice(5)),
      datasets: [
        { label: 'Moderate',         data: weeks.map(w => w.moderate), backgroundColor: '#34d399', stack: 's' },
        { label: 'Vigorous (×2)',    data: weeks.map(w => w.vigorous * 2), backgroundColor: '#f87171', stack: 's' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ...commonScales().x },
        y: { stacked: true, ...commonScales('min/wk equiv').y,
             ticks: { color: '#8892a4' },
             grid: { color: ctx => ctx.tick.value === 150 ? '#fbbf24' : '#2e3350' } },
      },
      plugins: commonPlugins({
        annotation: undefined,
        tooltip: {
          backgroundColor: '#22263a', borderColor: '#2e3350', borderWidth: 1,
          titleColor: '#e2e8f0', bodyColor: '#8892a4',
          callbacks: {
            afterBody: items => {
              const w = weeks[items[0]?.dataIndex];
              return w ? [`${w.target_pct}% of WHO target`] : '';
            },
          },
        },
      }),
    },
  });
}

// 18. Stress hour-of-day fingerprint
function renderStressFingerprint(payload) {
  destroyAux('stressFp');
  const ctx = document.getElementById('stress-fingerprint-chart');
  if (!ctx) return;
  auxCharts.stressFp = new Chart(ctx, {
    type: 'line',
    data: {
      labels: (payload?.hours || []).map(h => `${h}:00`),
      datasets: [
        { label: 'Weekday', data: payload?.weekday || [], borderColor: '#4f9cf9', backgroundColor: 'rgba(79,156,249,0.1)', tension: 0.4, spanGaps: true, fill: true },
        { label: 'Weekend', data: payload?.weekend || [], borderColor: '#fbbf24', backgroundColor: 'rgba(251,191,36,0.1)', tension: 0.4, spanGaps: true, fill: true },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: { ...commonScales('avg stress').y, min: 0, max: 100 } },
      plugins: commonPlugins(),
    },
  });
}

// 15. VO2 max / fitness age
function renderFitnessAge(rows) {
  const data = rows || [];
  destroyAux('fitnessAge');
  const ctx = document.getElementById('fitness-age-chart');
  if (!ctx) return;
  if (!data.length) {
    auxCharts.fitnessAge = new Chart(ctx, { type: 'line', data: { labels: [], datasets: [] }, options: { responsive: true, maintainAspectRatio: false } });
    return;
  }
  // Find numeric columns (excluding 'date')
  const numericKeys = Object.keys(data[0]).filter(k => k !== 'date' && typeof data[0][k] === 'number');
  auxCharts.fitnessAge = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(r => r.date.slice(5)),
      datasets: numericKeys.map((k, i) => ({
        label: k,
        data: data.map(r => r[k]),
        borderColor: ['#4f9cf9', '#34d399', '#fbbf24', '#7c6af7'][i % 4],
        backgroundColor: 'transparent',
        tension: 0.3, spanGaps: true,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: commonScales().x, y: commonScales().y },
      plugins: commonPlugins(),
    },
  });
}

// 17. Cycle HRV (placeholder)
function renderCycleHrv(payload) {
  const el = document.getElementById('cycle-hrv');
  if (!el) return;
  if (payload?.available === false) {
    el.textContent = payload.note || 'Cycle data not available.';
  } else {
    el.textContent = 'Cycle visualization coming soon.';
  }
}

/* =========================================================
   Show / hide and collapse for chart sections
   ========================================================= */

const PREFS_KEY = 'garmin-chart-prefs-v1';

function slugify(s) {
  return (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60);
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; }
  catch { return {}; }
}

function savePrefs(prefs) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch {}
}

function initChartCustomization() {
  const dash = document.getElementById('tab-dashboard');
  if (!dash) return;

  const prefs = loadPrefs();

  // Walk through every chart-section AND charts-row, assign an id, and
  // group them by the most recent .section-divider.
  let currentGroup = 'Recovery & Activity';
  const groups = new Map(); // group label -> [{id, label, el}]
  groups.set(currentGroup, []);

  const nodes = dash.querySelectorAll('h2.section-divider, .chart-section');
  const idCounts = new Map();
  nodes.forEach(node => {
    if (node.classList.contains('section-divider')) {
      currentGroup = node.textContent.trim();
      if (!groups.has(currentGroup)) groups.set(currentGroup, []);
      // Tag the divider too so the group itself can collapse
      const gid = `group-${slugify(currentGroup)}`;
      node.dataset.groupId = gid;
      node.classList.add('collapsible-divider');
      node.addEventListener('click', () => toggleGroup(gid, currentGroup));
      return;
    }
    const h2 = node.querySelector('.chart-header h2');
    const label = (h2 && h2.textContent.trim()) || 'Chart';
    let id = slugify(label);
    const n = (idCounts.get(id) || 0) + 1;
    idCounts.set(id, n);
    if (n > 1) id = `${id}-${n}`;
    node.dataset.chartId = id;
    groups.get(currentGroup).push({ id, label, el: node });

    // Apply hidden pref
    if (prefs[id] === false) node.classList.add('chart-hidden');

    // Make header collapsible (independent of show/hide)
    const header = node.querySelector('.chart-header');
    if (header) {
      header.classList.add('collapsible-header');
      const collapseKey = `collapsed:${id}`;
      if (prefs[collapseKey]) node.classList.add('chart-collapsed');
      header.addEventListener('click', e => {
        // Don't collapse when clicking a button/toggle inside the header
        if (e.target.closest('button, input')) return;
        node.classList.toggle('chart-collapsed');
        const updated = loadPrefs();
        updated[collapseKey] = node.classList.contains('chart-collapsed');
        savePrefs(updated);
      });
    }
  });

  // Build the customize panel
  const list = document.getElementById('customize-list');
  if (list) {
    list.innerHTML = '';
    for (const [groupName, items] of groups) {
      if (!items.length) continue;
      const wrap = document.createElement('div');
      wrap.className = 'customize-group';
      wrap.innerHTML = `<div class="customize-group-title">${escapeHtml(groupName)}</div>`;
      const itemsEl = document.createElement('div');
      itemsEl.className = 'customize-items';
      items.forEach(({ id, label, el }) => {
        const lbl = document.createElement('label');
        lbl.className = 'customize-item';
        const checked = !el.classList.contains('chart-hidden');
        lbl.innerHTML = `<input type="checkbox" data-chart-id="${id}" ${checked ? 'checked' : ''}/> <span>${escapeHtml(label)}</span>`;
        itemsEl.appendChild(lbl);
      });
      wrap.appendChild(itemsEl);
      list.appendChild(wrap);
    }
    list.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.addEventListener('change', () => {
        const id = cb.dataset.chartId;
        const sec = dash.querySelector(`[data-chart-id="${id}"]`);
        if (!sec) return;
        sec.classList.toggle('chart-hidden', !cb.checked);
        const updated = loadPrefs();
        updated[id] = cb.checked;
        savePrefs(updated);
      });
    });
  }

  function toggleGroup(gid, name) {
    const sections = dash.querySelectorAll(`[data-chart-id]`);
    // A group "owns" sections between its divider and the next divider
    let active = false;
    let allHidden = true;
    const owned = [];
    dash.querySelectorAll('h2.section-divider, .chart-section').forEach(node => {
      if (node.classList.contains('section-divider')) {
        active = node.dataset.groupId === gid;
        return;
      }
      if (active) {
        owned.push(node);
        if (!node.classList.contains('chart-hidden')) allHidden = false;
      }
    });
    // If any visible, hide all; if all hidden, show all
    const newHidden = !allHidden;
    const updated = loadPrefs();
    owned.forEach(sec => {
      sec.classList.toggle('chart-hidden', newHidden);
      updated[sec.dataset.chartId] = !newHidden;
      const cb = document.querySelector(`#customize-list input[data-chart-id="${sec.dataset.chartId}"]`);
      if (cb) cb.checked = !newHidden;
    });
    savePrefs(updated);
  }

  // Wire panel buttons
  document.getElementById('customize-btn')?.addEventListener('click', () => {
    document.getElementById('customize-panel')?.classList.toggle('hidden');
  });
  document.getElementById('customize-close-btn')?.addEventListener('click', () => {
    document.getElementById('customize-panel')?.classList.add('hidden');
  });
  document.getElementById('customize-all-btn')?.addEventListener('click', () => {
    setAllVisible(true);
  });
  document.getElementById('customize-none-btn')?.addEventListener('click', () => {
    setAllVisible(false);
  });

  function setAllVisible(visible) {
    const updated = loadPrefs();
    dash.querySelectorAll('[data-chart-id]').forEach(sec => {
      sec.classList.toggle('chart-hidden', !visible);
      updated[sec.dataset.chartId] = visible;
    });
    document.querySelectorAll('#customize-list input[type="checkbox"]').forEach(cb => {
      cb.checked = visible;
    });
    savePrefs(updated);
  }
}

initChartCustomization();
