/* UI translation is limited to a static catalog. User notes, chat messages,
 * picker values and input values are excluded from DOM translation. */
(function () {
  'use strict';
  const KEY = 'garmin-ui-language';
  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch (_) {}
  const language = saved === 'zh-CN' || saved === 'en' ? saved
    : document.documentElement.dataset.defaultLang === 'zh-CN' ? 'zh-CN'
    : (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh-CN' : 'en';
  document.documentElement.lang = language;
  const catalog = language === 'zh-CN' ? (window.GARMIN_ZH_CN || {}) : {};
  const translate = value => {
    if (typeof value !== 'string') return value;
    if (catalog[value]) return catalog[value];
    const count = language === 'zh-CN' && value.match(/^(\d+) charts?$/);
    return count ? `${count[1]} 个图表` : value;
  };
  window.healthTranslate = translate;

  function excluded(el) {
    if (!el || el.closest('script,style,pre,code,textarea,.journal-item-text,#cal-grid,#user-select,#activity-map-picker')) return true;
    return !!el.closest('#chat-messages') && el.id !== 'chat-messages' && !el.closest('#chat-welcome');
  }
  function translateNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (excluded(node.parentElement)) return;
      const original = node.nodeValue;
      const trimmed = original.trim();
      const result = translate(trimmed);
      if (result !== trimmed) node.nodeValue = original.replace(trimmed, result);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || excluded(node)) return;
    for (const attr of ['title', 'placeholder', 'aria-label']) {
      if (node.hasAttribute(attr)) {
        const before = node.getAttribute(attr), after = translate(before);
        if (before !== after) node.setAttribute(attr, after);
      }
    }
    for (const child of node.childNodes) translateNode(child);
  }

  const select = document.getElementById('language-select');
  if (select) {
    select.value = language;
    select.addEventListener('change', () => {
      try { localStorage.setItem(KEY, select.value); } catch (_) {}
      window.location.reload();
    });
  }
  if (language !== 'zh-CN') return;
  document.title = translate(document.title);
  translateNode(document.body);
  const observer = new MutationObserver(mutations => {
    for (const m of mutations) {
      if (m.type === 'characterData') translateNode(m.target);
      else if (m.type === 'attributes') translateNode(m.target);
      else for (const node of m.addedNodes) translateNode(node);
    }
  });
  observer.observe(document.body, {subtree:true, childList:true, characterData:true,
    attributes:true, attributeFilter:['title','placeholder','aria-label']});

  // Chart.js labels are drawn on canvas and cannot be translated by the DOM.
  if (typeof Chart !== 'undefined') {
    function translateChart(chart) {
      for (const dataset of chart.data.datasets || []) dataset.label = translate(dataset.label);
      for (const scale of Object.values(chart.options.scales || {})) {
        if (scale.title && typeof scale.title.text === 'string') scale.title.text = translate(scale.title.text);
      }
    }
    Chart.register({id:'garmin-i18n', beforeInit:translateChart, beforeUpdate:translateChart});
  }
})();
