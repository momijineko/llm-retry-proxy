/*
 * 管理页面外观控制：主题（浅色/深色/跟随系统）与密度（舒适/紧凑）。
 * 偏好存在 localStorage，首屏由页面 <head> 内联脚本提前应用，避免闪烁；
 * 本文件负责渲染右上角切换器并在运行时切换。
 */
(function (global) {
  'use strict';

  var THEME_KEY = 'lrp.theme';
  var DENSITY_KEY = 'lrp.density';
  var DARK_QUERY = '(prefers-color-scheme: dark)';

  function read(key, fallback) {
    try {
      var value = global.localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

  function write(key, value) {
    try { global.localStorage.setItem(key, value); } catch (error) { /* 隐私模式下忽略 */ }
  }

  function media() {
    return global.matchMedia ? global.matchMedia(DARK_QUERY) : null;
  }

  function resolveTheme(theme) {
    if (theme === 'dark' || theme === 'light') return theme;
    var query = media();
    return query && query.matches ? 'dark' : 'light';
  }

  function apply() {
    var root = document.documentElement;
    var theme = read(THEME_KEY, 'auto');
    var density = read(DENSITY_KEY, 'comfortable');
    if (theme !== 'auto' && theme !== 'dark' && theme !== 'light') theme = 'auto';
    if (density !== 'compact' && density !== 'comfortable') density = 'comfortable';
    root.setAttribute('data-theme', resolveTheme(theme));
    root.setAttribute('data-theme-mode', theme);
    root.setAttribute('data-density', density);
    document.dispatchEvent(new CustomEvent('adminprefschange', {
      detail: { theme: theme, resolved: resolveTheme(theme), density: density },
    }));
  }

  function setTheme(theme) {
    write(THEME_KEY, theme);
    apply();
  }

  function setDensity(density) {
    write(DENSITY_KEY, density);
    apply();
  }

  // 跟随系统时，系统切换主题要同步生效
  var query = media();
  if (query) {
    var onChange = function () { if (read(THEME_KEY, 'auto') === 'auto') apply(); };
    if (query.addEventListener) query.addEventListener('change', onChange);
    else if (query.addListener) query.addListener(onChange);
  }

  var POP_HTML =
    '<div class="prefs-label">主题</div>' +
    '<div class="prefs-row" data-prefs-group="theme">' +
    '<button type="button" data-theme-value="auto">跟随系统</button>' +
    '<button type="button" data-theme-value="light">浅色</button>' +
    '<button type="button" data-theme-value="dark">深色</button>' +
    '</div>' +
    '<div class="prefs-label">信息密度</div>' +
    '<div class="prefs-row" data-prefs-group="density">' +
    '<button type="button" data-density-value="comfortable">舒适</button>' +
    '<button type="button" data-density-value="compact">紧凑</button>' +
    '</div>';

  var ICON =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4' +
    'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';

  function syncState(wrapper) {
    var theme = read(THEME_KEY, 'auto');
    var density = read(DENSITY_KEY, 'comfortable');
    wrapper.querySelectorAll('[data-theme-value]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.themeValue === theme));
    });
    wrapper.querySelectorAll('[data-density-value]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.densityValue === density));
    });
  }

  function pickHost() {
    // 各页操作区容器不同：优先用页内操作区，最后回退到顶栏
    var selectors = ['.header-actions', '.toolbar', '.tools', 'header'];
    for (var i = 0; i < selectors.length; i++) {
      var node = document.querySelector(selectors[i]);
      if (node) return node;
    }
    return null;
  }

  function mount(target) {
    var host = typeof target === 'string' ? document.querySelector(target) : target;
    if (!host) host = pickHost();
    if (!host || host.querySelector('.admin-prefs')) return;
    var wrapper = document.createElement('div');
    wrapper.className = 'admin-prefs';
    wrapper.innerHTML =
      '<button type="button" class="admin-prefs-btn" aria-haspopup="true" aria-expanded="false" ' +
      'title="外观设置：主题与信息密度">' + ICON + '<span>外观</span></button>' +
      '<div class="admin-prefs-pop" role="group" aria-label="外观设置">' + POP_HTML + '</div>';
    host.appendChild(wrapper);

    var button = wrapper.querySelector('.admin-prefs-btn');
    var pop = wrapper.querySelector('.admin-prefs-pop');

    function close() {
      pop.classList.remove('open');
      button.setAttribute('aria-expanded', 'false');
    }

    function open() {
      syncState(wrapper);
      pop.classList.add('open');
      button.setAttribute('aria-expanded', 'true');
    }

    button.addEventListener('click', function (event) {
      event.stopPropagation();
      if (pop.classList.contains('open')) close(); else open();
    });

    pop.addEventListener('click', function (event) {
      var themeButton = event.target.closest('[data-theme-value]');
      if (themeButton) { setTheme(themeButton.dataset.themeValue); syncState(wrapper); return; }
      var densityButton = event.target.closest('[data-density-value]');
      if (densityButton) { setDensity(densityButton.dataset.densityValue); syncState(wrapper); }
    });

    document.addEventListener('click', function (event) {
      if (!wrapper.contains(event.target)) close();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') close();
    });
    syncState(wrapper);
  }

  apply();

  global.AdminPrefs = {
    apply: apply,
    mount: mount,
    setTheme: setTheme,
    setDensity: setDensity,
    theme: function () { return read(THEME_KEY, 'auto'); },
    density: function () { return read(DENSITY_KEY, 'comfortable'); },
    resolvedTheme: function () { return resolveTheme(read(THEME_KEY, 'auto')); },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { mount(); });
  } else {
    mount();
  }
})(window);
