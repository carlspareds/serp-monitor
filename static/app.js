/* SERP Monitor UI runtime (no build step, no dependencies).
 *
 * OVERLAY API  (window.SM) -- full guide in docs/ui-architecture.md
 * ------------------------------------------------------------------
 *   SM.openModal(src, opts)     centred dialog; bottom sheet on phones (<=640px)
 *   SM.openPanel(src, opts)     side panel on the right (full width on phones)
 *   SM.popover(anchor, src, o)  menu/popover anchored to an element
 *   SM.close([el])              close the top-most overlay (or `el`'s overlay)
 *   SM.closeAll()
 *   SM.reload([el])             re-fetch the overlay's URL into it
 *   SM.toast(message, kind)     transient status message (kind: ok | error | info)
 *   SM.load(url)                fetch a server-rendered fragment -> Promise<string>
 *
 *   `src` is a URL (fetched with the header X-SM-Fragment: 1, so the server
 *   answers with the bare fragment -- see ui_helpers.fragment_or_page), an
 *   HTML string starting with "<", or "#template-id".
 *   opts: {wide: bool, title: str, target: element id (reuse a persistent
 *          panel such as #assistant-panel), className, onClose: fn}
 *   Fragments built with dashboard.ui.overlay(title, body, foot) carry their
 *   own header/close button; anything else is wrapped in a default header.
 *   <script> tags inside a fragment run after insertion, and an `sm:load`
 *   event (detail: {url, kind}) bubbles from the overlay element.
 *
 * DECLARATIVE TRIGGERS (no JS needed in templates)
 *   data-modal="/ui/x"   (or on an <a>, data-modal with the URL in href)
 *   data-modal-wide      wide modal
 *   data-panel="/ui/x"   data-panel-target="assistant-panel"
 *   data-popover="/ui/x" or "#template-id"; data-popover-class="alerts-pop"
 *   data-sm-close        close the enclosing overlay
 *   data-sm-submit       on a <form> inside an overlay: submit with fetch;
 *                        a fragment response replaces the overlay content
 *                        (header X-SM-Toast: url-encoded message -> toast),
 *                        a redirect navigates to the final URL.
 *   data-theme-toggle    switch light/dark
 *
 * Behaviour: focus moves into the overlay and is trapped there (modal/panel),
 * Escape or a click on the backdrop closes, focus returns to the opener
 * (or, when the opener is gone or hidden, to the overlay below / the docked
 * assistant), body scroll is locked while a modal/panel is open. Popovers
 * close on an outside click / Escape and support arrow-key navigation.
 *
 * STACKING: SM keeps one stack of modals/panels; each new entry gets a higher
 * z-index (Z_BASE + 2 * depth for its scrim, +1 for the overlay) so the
 * NEWEST overlay is always topmost and owns focus and the Tab trap. Popovers
 * sit above the stack. The assistant is NOT an SM overlay: it is docked at the
 * right on desktop (>= 1024px, the page narrows) and a full-height sheet on
 * smaller screens that minimizes to a floating bubble whenever it opens
 * another overlay (SM fires `sm:open` on document). See SM.assistant below.
 *
 * Also here: theme, PRG flash -> toast, ?alerts=1 auto-open, settings
 * section nav highlighting, native <dialog> helpers (data-dialog, kept for
 * older pages), delete confirmation, search submit guard, live job progress.
 */
(function () {
  'use strict';

  var root = document.documentElement;

  // ---- Theme -------------------------------------------------------------
  // The choice is stored as 'light' | 'dark' and mirrored on <html data-theme>
  // (read by the CSS tokens) and on body.dark (legacy hook). With nothing
  // stored, the OS preference applies.
  function systemDark() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function isDark() {
    var t = root.getAttribute('data-theme');
    return t ? t === 'dark' : systemDark();
  }
  function applyTheme(theme) {
    if (theme) root.setAttribute('data-theme', theme);
    document.body.classList.toggle('dark', isDark());
  }
  try { applyTheme(localStorage.getItem('theme')); } catch (e) { applyTheme(null); }
  window.toggleTheme = function () {
    var next = isDark() ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('theme', next); } catch (e) {}
  };

  // ---- Overlay framework (SM) ----------------------------------------------
  var SM = window.SM = window.SM || {};
  var stack = [];          // open modals/panels, top-most last: {el, scrim, opener, opts, url, kind}
  var pop = null;          // the open popover: {el, anchor, opts, url}
  var Z_BASE = 80;         // scrim of the first overlay; each level adds 2 (see restack)
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function layer() {
    var l = document.getElementById('sm-layer');
    if (!l) { l = document.createElement('div'); l.id = 'sm-layer'; document.body.appendChild(l); }
    return l;
  }
  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var X_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  SM.load = function (url, init) {
    init = init || {};
    var headers = { 'X-SM-Fragment': '1', 'Accept': 'text/html' };
    return fetch(url, {
      method: init.method || 'GET', body: init.body, headers: Object.assign(headers, init.headers || {}),
      credentials: 'same-origin', cache: 'no-store'
    }).then(function (r) {
      if (r.redirected && /\/login(\?|$)/.test(r.url)) { location.href = r.url; throw new Error('signed out'); }
      var toast = r.headers.get('X-SM-Toast');
      if (toast) SM.toast(decodeURIComponent(toast), r.ok ? 'ok' : 'error');
      return r.text().then(function (text) {
        if (!r.ok) { var e = new Error('HTTP ' + r.status); e.status = r.status; e.html = text; throw e; }
        return { html: text, response: r };
      });
    });
  };

  function resolveSrc(src) {
    // -> Promise<{html, url}>
    if (typeof src !== 'string') return Promise.resolve({ html: '', url: '' });
    var s = src.trim();
    if (s.charAt(0) === '<') return Promise.resolve({ html: s, url: '' });
    if (s.charAt(0) === '#') {
      var tpl = document.getElementById(s.slice(1));
      return Promise.resolve({ html: tpl ? tpl.innerHTML : '', url: '' });
    }
    return SM.load(s).then(function (res) { return { html: res.html, url: s }; });
  }

  function wrapContent(html, opts) {
    if (/class=["'][^"']*\bov-head\b/.test(html)) return html;
    return '<div class="ov-head"><h2 data-sm-title>' + esc(opts.title || '') + '</h2>' +
      '<button type="button" class="iconbtn" data-sm-close aria-label="Close">' + X_ICON + '</button></div>' +
      '<div class="ov-body">' + html + '</div>';
  }

  function runScripts(el) {
    var scripts = el.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i], s = document.createElement('script');
      for (var j = 0; j < old.attributes.length; j++) s.setAttribute(old.attributes[j].name, old.attributes[j].value);
      s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    }
  }

  function fill(el, html, kind, url, opts) {
    // A fragment whose root repeats the persistent target's id (the assistant
    // panel) keeps one id in the document.
    if (el.id && html.indexOf("id='" + el.id + "'") !== -1 || el.id && html.indexOf('id="' + el.id + '"') !== -1) {
      var tmp = document.createElement('div');
      tmp.innerHTML = html.trim();
      var r0 = tmp.firstElementChild;
      if (r0 && tmp.childElementCount === 1 && r0.id === el.id) { r0.id = el.id + '-inner'; html = tmp.innerHTML; }
    }
    el.innerHTML = kind === 'popover' ? html : wrapContent(html, opts || {});
    var title = el.querySelector('[data-sm-title]');
    if (title && kind !== 'popover') {
      if (!title.id) title.id = 'sm-title-' + Math.random().toString(36).slice(2, 8);
      el.setAttribute('aria-labelledby', title.id);
    }
    if (kind === 'modal' && el.querySelector('[data-sm-wide]')) el.classList.add('wide');
    runScripts(el);
    syncAlertsBadge(el);
    syncReviewBadge(el);
    var jobs = el.querySelectorAll('[data-job-status-url]');
    for (var j = 0; j < jobs.length; j++) initJob(jobs[j]);
    var asst = el.querySelector('[data-assistant]');
    if (asst) initAssistant(asst);
    el.dispatchEvent(new CustomEvent('sm:load', { bubbles: true, detail: { url: url, kind: kind } }));
  }

  function showError(el, err, kind, opts) {
    var msg = err && err.status === 403 ? 'You don\'t have access to this.' :
      err && err.status === 404 ? 'This isn\'t available.' : 'Couldn\'t load this. Check your connection and try again.';
    var body = '<div class="ov-error" role="alert">' + esc(msg) + '</div>';
    el.innerHTML = kind === 'popover' ? body : wrapContent(body, opts || {});
  }

  function focusFirst(el) {
    var target = el.querySelector('[autofocus]') ||
      el.querySelector('.ov-body ' + FOCUSABLE.split(', ').join(', .ov-body ')) ||
      el.querySelector(FOCUSABLE);
    if (target) { try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); } }
    else { el.setAttribute('tabindex', '-1'); el.focus(); }
  }

  function lockScroll() { root.classList.add('sm-locked'); }
  function unlockScroll() { if (!stack.length) root.classList.remove('sm-locked'); }

  // The newest overlay is always on top: z-index follows the stack order.
  function restack() {
    for (var i = 0; i < stack.length; i++) {
      stack[i].scrim.style.zIndex = String(Z_BASE + 2 * i);
      stack[i].el.style.zIndex = String(Z_BASE + 2 * i + 1);
      stack[i].el.setAttribute('data-sm-depth', String(i));
    }
  }
  SM.zFor = function (depth) { return Z_BASE + 2 * depth + 1; };
  SM.depth = function () { return stack.length; };
  SM.top = function () {
    var e = stack[stack.length - 1];
    if (!e) return null;
    var t = e.el.querySelector('[data-sm-title]');
    return { url: e.url, kind: e.kind, title: (t && t.textContent.trim()) || e.opts.title || '' };
  };
  function visible(el) { return !!(el && document.contains(el) && (el.offsetParent !== null || el === document.body)); }

  function openOverlay(kind, src, opts) {
    opts = opts || {};
    closePopover(false);
    var opener = document.activeElement;
    var el, scrim, persistent = false;
    if (opts.target && document.getElementById(opts.target)) {
      el = document.getElementById(opts.target);
      persistent = true;
      // Toggle: opening an already-open persistent panel closes it.
      for (var i = 0; i < stack.length; i++) if (stack[i].el === el) { SM.close(el); return Promise.resolve(el); }
    } else {
      el = document.createElement('section');
    }
    el.className = (kind === 'panel' ? 'panel' : 'modal' + (opts.wide ? ' wide' : '')) +
      (opts.className ? ' ' + opts.className : '');
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.hidden = false;
    scrim = document.createElement('div');
    scrim.className = 'scrim';
    scrim.setAttribute('data-sm-backdrop', '');
    var l = layer();
    l.appendChild(scrim);
    if (!persistent) l.appendChild(el);
    var entry = { el: el, scrim: scrim, opener: opener, opts: opts, url: '', kind: kind, persistent: persistent };
    stack.push(entry);
    restack();
    lockScroll();
    document.dispatchEvent(new CustomEvent('sm:open', { detail: { kind: kind, url: typeof src === 'string' ? src : '', title: opts.title || '' } }));
    var keep = persistent && el.getAttribute('data-sm-loaded') === String(src);
    if (!keep) el.innerHTML = wrapContent('<div class="ov-loading" role="status">Loading…</div>', opts);
    if (keep) { focusFirst(el); return Promise.resolve(el); }
    return resolveSrc(src).then(function (res) {
      if (stack.indexOf(entry) === -1) return el;      // closed while loading
      entry.url = res.url;
      fill(el, res.html, kind, res.url, opts);
      if (persistent) el.setAttribute('data-sm-loaded', String(src));
      focusFirst(el);
      return el;
    }, function (err) {
      if (stack.indexOf(entry) !== -1) showError(el, err, kind, opts);
      return el;
    });
  }

  SM.openModal = function (src, opts) { return openOverlay('modal', src, opts); };
  SM.openPanel = function (src, opts) { return openOverlay('panel', src, opts); };

  function entryFor(el) {
    for (var i = stack.length - 1; i >= 0; i--) {
      if (!el || stack[i].el === el || stack[i].el.contains(el)) return i;
    }
    return -1;
  }

  SM.close = function (el) {
    if (pop && (!el || pop.el === el || pop.el.contains(el))) { closePopover(true); return; }
    var i = entryFor(el);
    if (i === -1) return;
    var e = stack.splice(i, 1)[0];
    var refresh = !!e.el.querySelector('[data-sm-reload-on-close]');
    if (e.scrim.parentNode) e.scrim.parentNode.removeChild(e.scrim);
    if (e.persistent) { e.el.hidden = true; }
    else if (e.el.parentNode) e.el.parentNode.removeChild(e.el);
    restack();
    unlockScroll();
    if (typeof e.opts.onClose === 'function') { try { e.opts.onClose(); } catch (err) {} }
    // Focus goes back to the opener when it is still there and visible (the
    // opener may live in the minimized assistant); else to the overlay below,
    // the docked assistant, or the page.
    if (visible(e.opener) && e.opener.focus) e.opener.focus();
    else if (stack.length) focusFirst(stack[stack.length - 1].el);
    else if (SM.assistant && (SM.assistant.focusIfDocked() || SM.assistant.focusBubble())) { /* focused */ }
    else { var m = document.getElementById('main'); if (m) { m.setAttribute('tabindex', '-1'); m.focus({ preventScroll: true }); } }
    document.dispatchEvent(new CustomEvent('sm:close', { detail: { kind: e.kind, url: e.url } }));
    if (refresh) location.reload();
  };
  SM.closeAll = function () {
    closePopover(false);
    while (stack.length) SM.close(stack[stack.length - 1].el);
  };
  SM.reload = function (el) {
    var target = null, kind = 'modal', url = '', opts = {};
    if (pop && (!el || pop.el.contains(el))) { target = pop.el; kind = 'popover'; url = pop.url; opts = pop.opts; }
    else {
      var i = entryFor(el);
      if (i === -1) return Promise.resolve(null);
      target = stack[i].el; kind = stack[i].kind; url = stack[i].url; opts = stack[i].opts;
    }
    if (!url) return Promise.resolve(target);
    return SM.load(url).then(function (res) { fill(target, res.html, kind, url, opts); return target; });
  };

  // ---- Popover ----
  function closePopover(returnFocus) {
    if (!pop) return;
    var p = pop;
    pop = null;
    if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
    if (p.anchor) p.anchor.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', onOutside, true);
    window.removeEventListener('resize', p.place);
    if (returnFocus && p.anchor && p.anchor.focus) p.anchor.focus();
  }
  function onOutside(e) {
    if (!pop) return;
    if (pop.el.contains(e.target) || (pop.anchor && pop.anchor.contains(e.target))) return;
    closePopover(false);
  }
  function placePopover(el, anchor) {
    var r = anchor.getBoundingClientRect();
    var fixed = !!anchor.closest('.top, .rail, .modal, .panel, .assistant-dock');
    el.style.position = fixed ? 'fixed' : 'absolute';
    var w = el.offsetWidth, vw = document.documentElement.clientWidth;
    var left = r.right - w;                    // right-aligned by default
    if (r.left + w <= vw - 12 && r.left < vw / 2) left = r.left;   // left-aligned for left-side anchors
    left = Math.max(12, Math.min(left, vw - w - 12));
    var top = r.bottom + 6;
    el.style.left = (left + (fixed ? 0 : window.scrollX)) + 'px';
    el.style.top = (top + (fixed ? 0 : window.scrollY)) + 'px';
  }
  SM.popover = function (anchor, src, opts) {
    opts = opts || {};
    if (pop && pop.anchor === anchor) { closePopover(true); return Promise.resolve(null); }
    closePopover(false);
    var el = document.createElement('div');
    el.className = 'pop' + (opts.className ? ' ' + opts.className : '');
    el.setAttribute('role', opts.role || 'menu');
    el.style.zIndex = String(Z_BASE + 2 * stack.length + 2);   // above every overlay
    if (anchor.getAttribute('aria-label')) el.setAttribute('aria-label', anchor.getAttribute('aria-label'));
    el.innerHTML = '<div class="ov-loading" role="status">Loading…</div>';
    layer().appendChild(el);
    var p = { el: el, anchor: anchor, opts: opts, url: '' };
    p.place = function () { placePopover(el, anchor); };
    pop = p;
    anchor.setAttribute('aria-expanded', 'true');
    p.place();
    setTimeout(function () { document.addEventListener('mousedown', onOutside, true); }, 0);
    window.addEventListener('resize', p.place);
    return resolveSrc(src).then(function (res) {
      if (pop !== p) return el;
      p.url = res.url;
      fill(el, res.html, 'popover', res.url, opts);
      if (!el.querySelector('[role=menuitem], .item')) el.setAttribute('role', 'dialog');
      p.place();
      var first = el.querySelector('[role=menuitem], .item, ' + FOCUSABLE);
      if (first) first.focus({ preventScroll: true });
      return el;
    }, function (err) {
      if (pop === p) { showError(el, err, 'popover', opts); p.place(); }
      return el;
    });
  };

  // ---- Toasts ----
  SM.toast = function (message, kind) {
    if (!message) return;
    var region = document.getElementById('sm-toasts');
    if (!region) {
      region = document.createElement('div');
      region.id = 'sm-toasts';
      region.className = 'toast-region';
      region.setAttribute('aria-live', 'polite');
      document.body.appendChild(region);
    }
    var t = document.createElement('div');
    t.className = 'toast' + (kind === 'error' ? ' toast-error' : '');
    t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    t.textContent = message;
    region.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, kind === 'error' ? 6000 : 3200);
  };

  // Alerts badge on the bell follows any fragment that reports a count.
  function syncAlertsBadge(el) {
    var src = el.querySelector('[data-sm-alerts-count]');
    if (!src) return;
    var n = parseInt(src.getAttribute('data-sm-alerts-count'), 10) || 0;
    var bell = document.getElementById('bell');
    if (!bell) return;
    var badge = bell.querySelector('.dotbadge');
    if (n && !badge) { badge = document.createElement('span'); badge.className = 'dotbadge'; bell.appendChild(badge); }
    if (badge) { if (n) badge.textContent = String(n); else badge.parentNode.removeChild(badge); }
    bell.setAttribute('aria-label', n ? 'Alerts, ' + n + ' open' : 'Alerts');
  }

  // Navigate after a fetch-submitted form. Same page (maybe another #anchor):
  // reload so the toast and the fresh data show.
  function navigate(target) {
    var u = new URL(target, location.href);
    if (u.pathname === location.pathname && u.search === location.search) {
      history.replaceState(null, '', u.href);
      location.reload();
    } else {
      location.assign(u.href);
    }
  }

  // ---- Fetch-submitted forms inside overlays ----
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-sm-submit')) return;
    e.preventDefault();
    if (e.submitter && e.submitter.hasAttribute('data-sm-action')) { runAction(e.submitter); return; }
    if (form.getAttribute('data-submitting') === '1') return;
    var container = form.closest('.pop, .modal, .panel');
    var method = (form.getAttribute('method') || 'GET').toUpperCase();
    var data = new FormData(form);
    if (e.submitter && e.submitter.name) data.append(e.submitter.name, e.submitter.value);
    var url = (e.submitter && e.submitter.getAttribute('formaction')) || form.getAttribute('action') || location.href;
    var init = { method: method };
    if (method === 'GET') url += (url.indexOf('?') === -1 ? '?' : '&') + new URLSearchParams(data).toString();
    else init.body = form.enctype === 'multipart/form-data' ? data : new URLSearchParams(data);
    var btns = form.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;
    form.setAttribute('data-submitting', '1');
    if (e.submitter && e.submitter.hasAttribute('data-busy-label')) e.submitter.textContent = e.submitter.getAttribute('data-busy-label');
    SM.load(url, init).then(function (res) {
      var r = res.response;
      form.removeAttribute('data-submitting');
      var go = r.headers.get('X-SM-Redirect');
      if (go) { navigate(go); return; }
      var openPanel = r.headers.get('X-SM-Panel'), openModal = r.headers.get('X-SM-Modal');
      if (openPanel || openModal) {
        if (container && !container.classList.contains('pop')) SM.close(container); else closePopover(false);
        if (openPanel) SM.openPanel(openPanel, {}); else SM.openModal(openModal, {});
        return;
      }
      if (r.redirected) { navigate(r.url); return; }
      if (!container) { location.reload(); return; }
      var kind = container.classList.contains('pop') ? 'popover' : container.classList.contains('panel') ? 'panel' : 'modal';
      var opts = (pop && pop.el === container) ? pop.opts : {};
      fill(container, res.html, kind, '', opts);
      if (kind === 'popover' && pop) pop.place();
      // The new fragment's [autofocus] wins (the Review dialog's next card puts
      // focus on its first verdict, not on the result's title link).
      var f = container.querySelector('[autofocus]') ||
        container.querySelector('[role=menuitem], .item, ' + FOCUSABLE);
      if (f) f.focus({ preventScroll: true });
    }, function (err) {
      form.removeAttribute('data-submitting');
      for (var j = 0; j < btns.length; j++) btns[j].disabled = false;
      if (err && err.html && container && (err.status === 400 || err.status === 409 || err.status === 422)) {
        // Validation errors come back as the same fragment with the message in it.
        fill(container, err.html, container.classList.contains('pop') ? 'popover' :
          container.classList.contains('panel') ? 'panel' : 'modal', '', {});
      } else if (!(err && err.message === 'signed out')) {
        SM.toast(err && err.status === 403 ? 'You don\'t have permission to do that.' :
          'That didn\'t work. Please try again.', 'error');
      }
    });
  });

  // ---- Keyboard: Escape, focus trap, menu arrows ----
  document.addEventListener('keydown', function (e) {
    // Review dialog: 1-4 pick a verdict (not while typing in a field).
    if (stack.length && !pop && !e.metaKey && !e.ctrlKey && !e.altKey) {
      var rv = stack[stack.length - 1].el.querySelector('[data-review-keys]');
      var tag = (document.activeElement && document.activeElement.tagName) || '';
      if (rv && !/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) {
        var kb = rv.querySelector('[data-key="' + e.key + '"]');
        if (kb && !kb.disabled) { e.preventDefault(); kb.click(); return; }
      }
    }
    if (e.key === 'Escape') {
      if (pop) { e.preventDefault(); closePopover(true); return; }
      if (stack.length) { e.preventDefault(); SM.close(stack[stack.length - 1].el); return; }
      if (SM.assistant && SM.assistant.escape(e)) { e.preventDefault(); return; }
    }
    // Assistant shortcuts: "/" (not while typing) or Ctrl/Cmd+K opens it and
    // focuses the message box.
    if (SM.assistant && !stack.length) {
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement && document.activeElement.tagName) || '') ||
        (document.activeElement && document.activeElement.isContentEditable);
      if ((e.key === 'k' || e.key === 'K') && (e.ctrlKey || e.metaKey) && !e.altKey) {
        e.preventDefault(); closePopover(false); SM.assistant.open({ focus: true }); return;
      }
      if (e.key === '/' && !typing && !pop && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault(); SM.assistant.open({ focus: true }); return;
      }
    }
    if (pop && (e.key === 'ArrowDown' || e.key === 'ArrowUp') && pop.el.contains(document.activeElement)) {
      var items = Array.prototype.slice.call(pop.el.querySelectorAll('.item, [role=menuitem]'));
      if (!items.length) return;
      e.preventDefault();
      var idx = items.indexOf(document.activeElement);
      idx = e.key === 'ArrowDown' ? (idx + 1) % items.length : (idx <= 0 ? items.length - 1 : idx - 1);
      items[idx].focus();
      return;
    }
    if (e.key === 'Tab' && !stack.length && !pop && SM.assistant && SM.assistant.isSheet()) {
      SM.assistant.trapTab(e);
      return;
    }
    if (e.key === 'Tab' && stack.length && !pop) {
      var top = stack[stack.length - 1].el;
      var f = Array.prototype.filter.call(top.querySelectorAll(FOCUSABLE), function (n) {
        return n.offsetParent !== null || n === document.activeElement;
      });
      if (!f.length) { e.preventDefault(); return; }
      var first = f[0], last = f[f.length - 1];
      if (!top.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // ---- Active row in data tables ----------------------------------------------
  // Clicking a row, or moving keyboard focus into it, marks it as the row being
  // inspected. Purely visual: links, buttons and forms inside keep working.
  function activateRow(target) {
    var row = target.closest && target.closest('tr');
    if (!row || !row.querySelector('td')) return;
    var table = row.closest('table');
    if (!table || !table.closest('.main, .modal, .panel')) return;
    var prev = table.querySelectorAll('tr.is-active');
    for (var i = 0; i < prev.length; i++) {
      if (prev[i] !== row) prev[i].classList.remove('is-active');
    }
    row.classList.add('is-active');
  }

  var pendingForm = null;

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    activateRow(t);

    if (t.closest('[data-theme-toggle]')) { window.toggleTheme(); closePopover(false); return; }

    // Assistant: the top-bar button toggles it (it is not an SM panel), a
    // row menu's "Ask the assistant about this" opens it with a question.
    if (t.closest('[data-assistant-toggle]') && SM.assistant) {
      e.preventDefault(); closePopover(false); SM.assistant.toggle(); return;
    }
    var ask = t.closest('[data-assistant-ask]');
    if (ask && SM.assistant) {
      e.preventDefault();
      var rid = ask.getAttribute('data-result-id');
      var row = rid ? document.getElementById('r-' + rid) : null;
      closePopover(false);
      if (row) activateRow(row);
      SM.assistant.ask(ask.getAttribute('data-assistant-ask') || '', row);
      return;
    }

    if (t.closest('[data-sm-backdrop]')) { if (stack.length) SM.close(stack[stack.length - 1].el); return; }
    var closer = t.closest('[data-sm-close]');
    if (closer) {
      e.preventDefault();
      if (closer.closest('.pop, .modal, .panel')) SM.close(closer);
      else if (history.length > 1) history.back();
      else location.href = '/';
      return;
    }
    var cf = t.closest('[data-sm-confirm]');
    if (cf && !cf.hasAttribute('data-sm-confirmed')) {
      e.preventDefault();
      askConfirm(cf);
      return;
    }
    if (cf) cf.removeAttribute('data-sm-confirmed');
    var nv = t.closest('[data-sm-nav]');
    if (nv && !e.metaKey && !e.ctrlKey && !e.shiftKey && nv.closest('.modal, .panel')) {
      e.preventDefault();
      navOverlay(nv.closest('.modal, .panel'), nv.getAttribute('data-sm-nav') || nv.getAttribute('href'));
      return;
    }
    var m = t.closest('[data-modal]');
    if (m && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      SM.openModal(m.getAttribute('data-modal') || m.getAttribute('href'),
        { wide: m.hasAttribute('data-modal-wide'), title: m.getAttribute('data-title') || m.textContent.trim() });
      return;
    }
    var p = t.closest('[data-panel]');
    if (p && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      SM.openPanel(p.getAttribute('data-panel') || p.getAttribute('href'),
        { target: p.getAttribute('data-panel-target') || '', title: p.getAttribute('data-title') || p.textContent.trim() });
      return;
    }
    var po = t.closest('[data-popover]');
    if (po && !(pop && pop.el.contains(po))) {
      e.preventDefault();
      SM.popover(po, po.getAttribute('data-popover'), { className: po.getAttribute('data-popover-class') || '' });
      return;
    }
    var ts = t.closest('[data-toast]');
    if (ts) SM.toast(ts.getAttribute('data-toast'));

    // Native <dialog> (older pages: data-dialog opener, data-close inside).
    var opener = t.closest('[data-dialog]');
    if (opener) {
      e.preventDefault();
      var dlg = document.getElementById(opener.getAttribute('data-dialog'));
      if (dlg && typeof dlg.showModal === 'function') dlg.showModal();
      return;
    }
    if (t.closest('[data-close]')) {
      e.preventDefault();
      var open = t.closest('dialog');
      if (open) open.close();
      return;
    }
    if (t.tagName === 'DIALOG' && t.open) {          // click on the backdrop: the target is the <dialog> itself
      var r = t.getBoundingClientRect();
      var inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      if (!inside) t.close();
      return;
    }

    var del = t.closest('.js-del');
    if (del) {
      e.preventDefault();
      pendingForm = del.closest('form');
      document.getElementById('confirm-msg').textContent = del.dataset.msg || 'Confirm?';
      document.getElementById('confirm-modal').showModal();
      return;
    }
    if (t.id === 'confirm-cancel') { document.getElementById('confirm-modal').close(); pendingForm = null; }
    if (t.id === 'confirm-ok' && pendingForm) { pendingForm.submit(); }
  });

  document.addEventListener('focusin', function (e) {
    if (e.target instanceof Element) activateRow(e.target);
  });

  // ---- In-overlay navigation, confirmations, auto-submit ------------------------
  // data-sm-nav: load another fragment into the SAME overlay (Runs -> one run
  // -> back), keeping SM.reload working on the new URL.
  function navOverlay(container, url) {
    var i = entryFor(container);
    if (i === -1 || !url) return;
    var entry = stack[i];
    SM.load(url).then(function (res) {
      entry.url = url;
      fill(container, res.html, entry.kind, url, entry.opts);
      focusFirst(container);
    }, function (err) { showError(container, err, entry.kind, entry.opts); });
  }

  // data-sm-confirm="Question?" on a submit button (or link): a small modal
  // with Cancel / the button's label instead of the browser's confirm().
  function askConfirm(btn) {
    var label = btn.getAttribute('data-confirm-label') || btn.textContent.trim() || 'Confirm';
    var html = '<div class="ov-head"><h2 data-sm-title>' + esc(btn.getAttribute('data-confirm-title') || 'Are you sure?') +
      '</h2><button type="button" class="iconbtn" data-sm-close aria-label="Close">' + X_ICON + '</button></div>' +
      '<div class="ov-body"><p class="confirm-text">' + esc(btn.getAttribute('data-sm-confirm')) + '</p></div>' +
      '<div class="ov-foot"><button type="button" class="btn ghost" data-sm-close>Cancel</button>' +
      '<button type="button" class="btn primary danger-btn" data-sm-confirm-ok>' + esc(label) + '</button></div>';
    SM.openModal(html, { className: 'confirm-modal' }).then(function (dlg) {
      var ok = dlg.querySelector('[data-sm-confirm-ok]');
      if (!ok) return;
      ok.addEventListener('click', function () {
        SM.close(dlg);
        var form = btn.form || btn.closest('form');
        if (form && btn.type === 'submit' && form.requestSubmit) { form.requestSubmit(btn); return; }
        btn.setAttribute('data-sm-confirmed', '');
        btn.click();
      });
    });
  }

  // data-autosubmit on a <form>: submit as soon as a control changes.
  // data-sm-refetch on a <select> inside an overlay: reload the overlay from
  // the attribute's URL + the chosen value (value "__new__" follows data-sm-new).
  document.addEventListener('change', function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    if (t.hasAttribute('data-sm-refetch')) {
      var box = t.closest('.modal, .panel');
      if (t.value === '__new__' && t.getAttribute('data-sm-new')) {
        SM.closeAll();
        SM.openModal(t.getAttribute('data-sm-new'), { title: 'New subject' });
        return;
      }
      if (box) navOverlay(box, t.getAttribute('data-sm-refetch') + encodeURIComponent(t.value));
      return;
    }
    var form = t.closest('form[data-autosubmit]');
    if (form) {
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }
  });

  // Review badges ("Review 3") follow the count a review fragment reports.
  function syncReviewBadge(el) {
    var src = el.querySelector('[data-sm-review-count]');
    if (!src) return;
    var n = parseInt(src.getAttribute('data-sm-review-count'), 10) || 0;
    var badges = document.querySelectorAll('[data-review-count]');
    for (var i = 0; i < badges.length; i++) {
      badges[i].textContent = String(n);
      badges[i].hidden = !n;
    }
  }

  // ---- Search submit -----------------------------------------------------------
  // The search runs server-side as a background job (POST -> 303 -> /job/{id}),
  // so no blocking overlay: just disable the submit button against double submits.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.classList.contains('run-form')) return;
    if (form.getAttribute('data-submitting') === '1') { e.preventDefault(); return; }
    form.setAttribute('data-submitting', '1');
    var btns = form.querySelectorAll('button[type=submit], button:not([type]), input[type=submit]');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      b.setAttribute('aria-disabled', 'true');
      b.classList.add('is-busy');
      b.setAttribute('data-label', b.tagName === 'INPUT' ? b.value : b.textContent);
      if (b.tagName === 'INPUT') b.value = 'Starting…'; else b.textContent = 'Starting…';
      // Disable after the submit has been dispatched (disabling first would drop the
      // button's own name/value from the form data in some browsers).
      setTimeout(function (btn) { return function () { btn.disabled = true; }; }(b), 0);
    }
  });
  // Coming back via the bfcache (Back button) must not leave the form stuck.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    var btns = document.querySelectorAll('form.run-form[data-submitting] .is-busy');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i], label = b.getAttribute('data-label') || '';
      b.disabled = false;
      b.removeAttribute('aria-disabled');
      b.classList.remove('is-busy');
      if (b.tagName === 'INPUT') b.value = label; else b.textContent = label;
      b.closest('form').removeAttribute('data-submitting');
    }
  });

  // ---- PRG flash -> toast ------------------------------------------------------
  var flashEl = document.querySelector('[data-sm-flash]');
  if (flashEl) {
    var kind = flashEl.getAttribute('data-sm-flash');
    SM.toast(flashEl.textContent.trim(), kind);
    if (kind !== 'error') flashEl.parentNode.removeChild(flashEl);
  }

  // ---- Deep links -----------------------------------------------------------------
  // ?alerts=1 opens the alerts popover (old /alerts links); ?panel=/ui/x and
  // ?modal=/ui/x open that overlay over the page (old /runs, /timeline, /diff...
  // pages redirect here). Only same-site /ui/ and /assistant/ paths.
  try {
    var params = new URLSearchParams(location.search);
    var changed = false;
    if (params.get('alerts') === '1') {
      var bellBtn = document.getElementById('bell');
      if (bellBtn) setTimeout(function () { bellBtn.click(); }, 0);
      params.delete('alerts');
      changed = true;
    }
    var okPath = function (p) { return /^\/(ui|assistant)\//.test(p || '') && p.indexOf('//') === -1; };
    var dlPanel = params.get('panel'), dlModal = params.get('modal');
    // Domains have one home (Settings → Projects and subjects → Domains); the
    // assistant panel isn't an SM overlay any more.
    if (/^\/ui\/domains/.test(dlModal || dlPanel || '')) { location.replace('/settings#domains'); dlModal = dlPanel = null; }
    if (/^\/assistant\/panel/.test(dlPanel || '')) { dlPanel = null; setTimeout(function () { if (SM.assistant) SM.assistant.open({ focus: true }); }, 0); }
    if (okPath(dlPanel)) setTimeout(function () { SM.openPanel(dlPanel, {}); }, 0);
    else if (okPath(dlModal)) setTimeout(function () { SM.openModal(dlModal, {}); }, 0);
    if (params.has('panel') || params.has('modal')) { params.delete('panel'); params.delete('modal'); changed = true; }
    if (changed) {
      var qs = params.toString();
      history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
    }
  } catch (e) {}

  // ---- #anchor -> open the <details> it names (Settings: #domains opens the
  // active subject's Domains area) -------------------------------------------------
  function openHashDetails() {
    var id = location.hash.slice(1);
    if (!id) return;
    var el = null;
    try { el = document.getElementById(decodeURIComponent(id)); } catch (e) { el = null; }
    if (!el) return;
    var d = el.tagName === 'DETAILS' ? el : el.closest('details');
    if (d && !d.open) d.open = true;
    if (d) setTimeout(function () { el.scrollIntoView({ block: 'start' }); }, 0);
  }
  openHashDetails();
  window.addEventListener('hashchange', openHashDetails);

  // ---- Settings: highlight the section in view -----------------------------------
  var snav = document.querySelector('.snav');
  if (snav && 'IntersectionObserver' in window) {
    var links = snav.querySelectorAll('a[href^="#"]');
    var byId = {};
    for (var k = 0; k < links.length; k++) byId[links[k].getAttribute('href').slice(1)] = links[k];
    var strip = snav.querySelector('.snav-links') || snav;
    var setOn = function (id) {
      for (var key in byId) {
        byId[key].classList.toggle('on', key === id);
        if (key === id) byId[key].setAttribute('aria-current', 'true');
        else byId[key].removeAttribute('aria-current');
      }
      var link = byId[id];
      // Phones: the nav is a horizontal strip; keep the current chip in view.
      if (link && strip.scrollWidth > strip.clientWidth + 2) {
        strip.scrollTo({ left: link.offsetLeft - strip.clientWidth / 2 + link.offsetWidth / 2, behavior: 'smooth' });
      }
    };
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) setOn(en.target.id); });
    }, { rootMargin: '-20% 0px -70% 0px' });
    for (var id in byId) { var sec = document.getElementById(id); if (sec) io.observe(sec); }
    if (location.hash && byId[location.hash.slice(1)]) setOn(location.hash.slice(1));
  }

  // ---- Live job progress (/job/{id}) ---------------------------------------------
  // Polls the JSON status endpoint and updates the progress card in place
  // (no full-page reloads). Markup is rendered by dashboard/routers/search_runner.py.
  var jobEl = document.querySelector('[data-job-status-url]');
  if (jobEl) initJob(jobEl);

  function initJob(el) {
    if (el._smJob) return;
    el._smJob = true;
    var url = el.getAttribute('data-job-status-url');
    var inOverlay = !!el.closest('.panel, .modal');
    var startedAt = parseFloat(el.getAttribute('data-started-at')) || 0;
    // Client clock minus server clock, so elapsed time ticks locally.
    var offset = Date.now() / 1000 - (parseFloat(el.getAttribute('data-server-now')) || Date.now() / 1000);
    var firstPoll = Date.now();
    var failures = 0;
    var timer = null;
    var stopped = false;
    var inflight = false;
    var finishedAt = null;
    var PILL = { pending: '', launching: 'info', running: 'info', saving: 'info',
                 done: 'positive', skipped: 'warning', error: 'negative' };
    var TERMINAL = { done: 1, skipped: 1, error: 1 };

    function q(sel) { return el.querySelector(sel); }
    function fmt(s) {
      s = Math.max(0, Math.floor(s));
      var m = Math.floor(s / 60), r = s % 60;
      return m + ':' + (r < 10 ? '0' : '') + r;
    }
    function tick() {
      if (!document.contains(el)) { clearInterval(clock); stop(); return; }
      var t = q('[data-job-elapsed]');
      if (!t) return;
      var now = finishedAt !== null ? finishedAt : (Date.now() / 1000 - offset);
      t.textContent = fmt(now - startedAt);
    }
    var clock = setInterval(tick, 1000);
    tick();

    function notice(msg, kind) {
      var n = q('[data-job-notice]');
      if (!n) return;
      if (!msg) { n.hidden = true; n.textContent = ''; n.className = 'job-notice'; return; }
      n.hidden = false;
      n.textContent = msg;
      n.className = 'job-notice' + (kind ? ' job-notice-' + kind : '');
    }
    function stepLabel(s) {
      var c = (s.country || '').toUpperCase();
      var e = s.engine ? s.engine.charAt(0).toUpperCase() + s.engine.slice(1) : '';
      return c ? c + ' · ' + e : e;
    }
    function stepDetail(s) {
      if (s.state === 'done' && s.summary) {
        var m = s.summary;
        return (m.total || 0) + ' results · ' + (m.positive || 0) + ' positive · ' +
          (m.negative || 0) + ' negative · ' + (m.neutral || 0) + ' neutral';
      }
      if ((s.state === 'skipped' || s.state === 'error') && s.message) return s.message;
      if (s.apify_status) {
        return 'Apify ' + s.apify_status +
          (s.apify_elapsed_s !== null && s.apify_elapsed_s !== undefined ? ' · ' + s.apify_elapsed_s + 's' : '');
      }
      if (s.state === 'launching') return 'Starting the Apify run…';
      if (s.state === 'saving') return 'Classifying and saving…';
      return '';
    }
    function renderStep(li, s) {
      li.setAttribute('data-state', s.state);
      var name = li.querySelector('.job-step-name');
      if (name) name.textContent = stepLabel(s);
      var pill = li.querySelector('[data-step-state]');
      if (pill) {
        pill.className = 'pill ' + (PILL[s.state] || '');
        pill.lastChild.textContent = s.state;
      }
      var det = li.querySelector('[data-step-detail]');
      if (det) det.textContent = stepDetail(s);
    }
    function newStep(s) {
      var li = document.createElement('li');
      li.className = 'job-step';
      li.setAttribute('data-step-key', s.key);
      var name = document.createElement('span'); name.className = 'job-step-name';
      var pill = document.createElement('span'); pill.setAttribute('data-step-state', '');
      var dot = document.createElement('span'); dot.className = 'dot'; dot.setAttribute('aria-hidden', 'true');
      pill.appendChild(dot); pill.appendChild(document.createTextNode(''));
      var det = document.createElement('span'); det.className = 'job-step-detail';
      det.setAttribute('data-step-detail', '');
      li.appendChild(name); li.appendChild(pill); li.appendChild(det);
      return li;
    }
    function render(d) {
      if (typeof d.server_now === 'number') offset = Date.now() / 1000 - d.server_now;
      if (typeof d.started_at === 'number') startedAt = d.started_at;
      var list = q('[data-job-steps]');
      if (!list && d.steps && d.steps.length) {
        list = document.createElement('ol');
        list.className = 'job-steps';
        list.setAttribute('data-job-steps', '');
        q('[data-job-live]').insertAdjacentElement('afterend', list);
      }
      (d.steps || []).forEach(function (s) {
        var li = null;
        var all = list.querySelectorAll('[data-step-key]');
        for (var i = 0; i < all.length; i++) if (all[i].getAttribute('data-step-key') === s.key) li = all[i];
        if (!li) { li = newStep(s); list.appendChild(li); }
        renderStep(li, s);
      });
      var total = d.total_steps || 0, done = d.done_steps || 0;
      var bar = q('[data-job-bar]');
      if (bar) bar.style.width = (total ? Math.round(done * 100 / total) : 0) + '%';
      var prog = q('[data-job-progress]');
      if (prog) { prog.setAttribute('aria-valuemax', total); prog.setAttribute('aria-valuenow', done); }
      var head = q('[data-job-step-count]');
      var headText = 'Step ' + done + ' of ' + total;
      if (head && head.textContent !== headText) head.textContent = headText;
      var live = q('[data-job-live]');
      var label = (d.status === 'done' ? 'Finished' : d.status === 'error' ? 'Failed' : 'Running') +
        ' · ' + done + ' of ' + total + ' steps finished';
      if (live && live.textContent !== label) live.textContent = label;   // only announce changes
      var log = q('[data-job-log]');
      if (log && d.log && log.children.length !== d.log.length) {
        log.textContent = '';
        d.log.forEach(function (m) { var item = document.createElement('li'); item.textContent = m; log.appendChild(item); });
      }
    }
    function renderError(d) {
      el.classList.add('job-error');
      el.setAttribute('role', 'alert');
      var bar = q('[data-job-progress]');
      if (bar) bar.classList.add('is-error');
      var n = q('[data-job-notice]');
      if (n) {
        n.hidden = false;
        n.className = 'job-notice job-notice-error';
        n.textContent = '';
        var b = document.createElement('b'); b.textContent = 'The search failed: ';
        n.appendChild(b);
        n.appendChild(document.createTextNode(d.error || 'unknown error'));
        n.appendChild(document.createTextNode(' '));
        var a = document.createElement('a'); a.href = '/ui/search'; a.textContent = 'Try again';
        a.setAttribute('data-modal', ''); a.setAttribute('data-title', 'New search');
        n.appendChild(a);
      }
      var foot = q('.job-foot');
      if (foot) foot.hidden = true;
    }
    // In the side panel: no auto-navigation; offer "View results" instead.
    function showDone(d) {
      var box = el.closest('.panel, .modal') || el;
      var view = box.querySelector('[data-job-view]');
      if (view) {
        if (d.view_url) view.setAttribute('href', d.view_url);
        view.hidden = false;
        try { view.focus({ preventScroll: true }); } catch (e) { view.focus(); }
      }
      var bg = box.querySelector('[data-job-background]');
      if (bg) bg.textContent = 'Close';
      var n = box.querySelector('[data-job-done-note]');
      if (n) { n.hidden = false; if (d.done_note) n.textContent = d.done_note; }
      var foot = box.querySelector('.job-foot');
      if (foot) foot.hidden = true;
    }
    function stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
      timer = null;
    }
    function delay() {
      var base = (Date.now() - firstPoll) > 60000 ? 5000 : 2000;
      if (failures) return Math.min(30000, base * Math.pow(2, failures - 1));
      return base;
    }
    function schedule() {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(poll, delay());
    }
    function poll() {
      timer = null;
      if (stopped) return;
      if (!document.contains(el)) { stop(); clearInterval(clock); return; }
      if (document.hidden || inflight) return;  // hidden: resumed by visibilitychange
      inflight = true;
      fetch(url, { headers: { 'Accept': 'application/json' }, cache: 'no-store', credentials: 'same-origin' })
        .then(function (resp) {
          inflight = false;
          if (resp.status === 404) {
            stop();
            finishedAt = Date.now() / 1000 - offset;
            notice('This search is no longer available (the server restarted).', 'error');
            return null;
          }
          var ct = resp.headers.get('content-type') || '';
          if (resp.redirected && /\/login/.test(resp.url)) { location.href = resp.url; stop(); return null; }
          if (!resp.ok || ct.indexOf('application/json') === -1) throw new Error('HTTP ' + resp.status);
          return resp.json();
        })
        .then(function (d) {
          if (!d) return;
          failures = 0;
          notice(null);
          render(d);
          if (d.status === 'done') {
            stop();
            finishedAt = d.started_at + d.elapsed_s;
            tick();
            if (inOverlay) { showDone(d); return; }
            if (d.redirect) location.replace(d.redirect);
            return;
          }
          if (d.status === 'error') {
            stop();
            finishedAt = d.started_at + d.elapsed_s;
            tick();
            clearInterval(clock);
            renderError(d);
            return;
          }
          schedule();
        })
        .catch(function () {
          inflight = false;
          failures += 1;
          notice('Connection lost, retrying…', 'warn');
          schedule();
        });
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && !stopped && !timer) poll();
    });
    var refresh = q('[data-job-refresh]');
    if (refresh) refresh.addEventListener('click', function (e) {
      if (stopped) return;                       // fall back to a real reload
      e.preventDefault();
      if (timer) clearTimeout(timer);
      poll();
    });
    schedule();
  }


  // ---- Assistant (GET /assistant/panel, contract in docs/assistant.md) -------------
  // SM.assistant is a controller OUTSIDE the overlay stack:
  //   desktop (>= 1024px)  docked side panel at the right; the page narrows
  //                        (html.asst-docked) and overlays opened from it render
  //                        above everything, centred over the remaining area.
  //   smaller screens      full-height sheet (html.asst-sheet) that minimizes to
  //                        a floating "Assistant" bubble (status: "Opened New
  //                        search") whenever an overlay opens; tapping the
  //                        bubble restores the chat with its history.
  // Open/minimized state survives page loads (localStorage); the chat history
  // is kept per subject in localStorage (Clear button). Keys: "/" or Ctrl+K
  // opens, Escape minimizes.
  var DOCK_MQ = window.matchMedia ? window.matchMedia('(min-width: 1024px)') : { matches: true };
  var ASST_STATE_KEY = 'sm-asst-state';
  var INTENTS = {
    review: ['modal', '/ui/review'], compare: ['modal', '/ui/compare', true], runs: ['panel', '/ui/runs'],
    timeline: ['panel', '/ui/timeline'], evidence: ['panel', '/ui/evidence'],
    reports: ['modal', '/ui/reports', true], notes: ['panel', '/ui/notes'],
    ai_checks: ['modal', '/ui/ai-checks'], search: ['modal', '/ui/search']
  };
  // Pages (not overlays): the feature's single home is a page section.
  var INTENT_PAGES = { domains: '/settings#domains' };

  function lsGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, v) { try { if (v === null) localStorage.removeItem(key); else localStorage.setItem(key, v); } catch (e) {} }

  var A = SM.assistant = (function () {
    var dock = null, bubble = null, loaded = null, api = null, state = 'closed';

    function aside() { return dock || (dock = document.getElementById('assistant-panel')); }
    function docked() { return !!DOCK_MQ.matches; }
    function setRoot() {
      var open = state === 'open';
      root.classList.toggle('asst-docked', open && docked());
      root.classList.toggle('asst-sheet', open && !docked());
      var a = aside();
      if (a) {
        a.hidden = !open;
        if (open && !docked()) { a.setAttribute('role', 'dialog'); a.setAttribute('aria-modal', 'true'); }
        else { a.setAttribute('role', 'complementary'); a.removeAttribute('aria-modal'); }
      }
      var btn = document.getElementById('aiBtn');
      if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (bubble) bubble.hidden = state !== 'min';
    }
    function remember() { lsSet(ASST_STATE_KEY, state === 'closed' ? null : state); }
    function ensureBubble() {
      if (bubble) return bubble;
      bubble = document.createElement('button');
      bubble.type = 'button';
      bubble.id = 'assistant-bubble';
      bubble.className = 'assistant-bubble';
      bubble.hidden = true;
      bubble.setAttribute('aria-label', 'Open the assistant');
      bubble.innerHTML = '<span class="assistant-spark" aria-hidden="true"><svg class="ico" viewBox="0 0 24 24" fill="none" ' +
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 ' +
        '2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/></svg></span><span class="ab-text"><b>Assistant</b>' +
        '<span class="ab-status" data-bubble-status></span></span>';
      bubble.addEventListener('click', function () { open({ focus: true }); });
      document.body.appendChild(bubble);
      return bubble;
    }
    function load() {
      var a = aside();
      if (!a) return Promise.resolve(null);
      if (loaded) return loaded;
      a.classList.add('assistant-dock');
      a.innerHTML = '<div class="ov-loading" role="status">Loading…</div>';
      loaded = SM.load('/assistant/panel').then(function (res) {
        var tmp = document.createElement('div');
        tmp.innerHTML = res.html.trim();
        var sec = tmp.firstElementChild;
        if (sec && sec.id === 'assistant-panel') sec.id = 'assistant-panel-inner';
        a.innerHTML = '';
        if (sec) a.appendChild(sec);
        var head = a.querySelector('[data-sm-title]');
        if (head) a.setAttribute('aria-labelledby', head.id || 'assistant-title');
        var s0 = a.querySelector('[data-assistant]');
        api = s0 ? initAssistant(s0) : null;
        return a;
      }, function () {
        loaded = null;
        a.innerHTML = '<div class="ov-head"><h2>Assistant</h2><button type="button" class="iconbtn" data-assistant-close ' +
          'aria-label="Close">' + X_ICON + '</button></div><div class="ov-error" role="alert">Couldn\'t load the assistant. ' +
          'Check your connection and try again.</div>';
        return a;
      });
      return loaded;
    }
    function open(opts) {
      opts = opts || {};
      state = 'open';
      remember();
      setRoot();
      return load().then(function (a) {
        if (opts.focus !== false) focusIn();
        return api;
      });
    }
    function focusIn() {
      var a = aside();
      if (!a) return;
      var input = a.querySelector('[data-assistant-input]');
      var t = input || a.querySelector(FOCUSABLE);
      if (t) { try { t.focus({ preventScroll: true }); } catch (e) { t.focus(); } }
    }
    function minimize(status) {
      if (state === 'closed' && !status) return;
      state = 'min';
      remember();
      ensureBubble();
      var st = bubble.querySelector('[data-bubble-status]');
      if (st) st.textContent = status || '';
      setRoot();
    }
    function close() {
      state = 'closed';
      remember();
      setRoot();
      var b = document.getElementById('aiBtn');
      if (b && !stack.length) b.focus();
    }
    // Something was opened from the assistant (intent, card button, link):
    // docked: stay; sheet: minimize to the bubble with a short status.
    function handoff(label) {
      if (state === 'open' && !docked()) minimize(label ? 'Opened ' + label : '');
    }
    document.addEventListener('sm:open', function (ev) {
      if (state === 'open' && !docked()) minimize(ev.detail && ev.detail.title ? 'Opened ' + ev.detail.title : 'Opened a dialog');
    });
    if (DOCK_MQ.addEventListener) DOCK_MQ.addEventListener('change', setRoot);
    else if (DOCK_MQ.addListener) DOCK_MQ.addListener(setRoot);

    // Restore after a page load: docked stays docked; a phone gets the bubble.
    var saved = lsGet(ASST_STATE_KEY);
    if (aside() && saved === 'open') { if (docked()) open({ focus: false }); else minimize(''); }
    else if (aside() && saved === 'min') minimize('');

    return {
      open: open,
      close: close,
      minimize: minimize,
      handoff: handoff,
      toggle: function () { if (state === 'open') close(); else open({ focus: true }); },
      state: function () { return state; },
      isSheet: function () { return state === 'open' && !docked(); },
      focusIfDocked: function () { if (state === 'open' && docked()) { focusIn(); return true; } return false; },
      focusBubble: function () { if (state === 'min' && bubble && !bubble.hidden) { bubble.focus(); return true; } return false; },
      escape: function (e) {
        var a = aside();
        if (state !== 'open' || !a) return false;
        if (!docked() || a.contains(document.activeElement)) { minimize(''); var b = document.getElementById('aiBtn'); if (b && docked()) b.focus(); return true; }
        return false;
      },
      trapTab: function (e) {
        var a = aside();
        var f = Array.prototype.filter.call(a.querySelectorAll(FOCUSABLE), function (n) { return n.offsetParent !== null; });
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (!a.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
        else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      },
      ask: function (text, row) {
        return open({ focus: true }).then(function (inst) { if (inst) inst.prefill(text, row); });
      }
    };
  })();

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof Element) || !t.closest('#assistant-panel')) return;
    if (t.closest('[data-assistant-close]')) { e.preventDefault(); A.close(); }
    else if (t.closest('[data-assistant-minimize]')) { e.preventDefault(); A.minimize(''); }
  });

  // Markdown-lite for replies: **bold**, `code`, headings (# / ## -> a bold
  // line), bullet/numbered lists, simple | tables | and links to in-app paths
  // only ([text](/path)); everything else is escaped text.
  function mdInline(text) {
    return esc(text)
      .replace(/\[([^\]\n]{1,120})\]\((\/(?!\/)[^\s()"'<>]{0,300})\)/g, function (m, label, href) {
        return '<a href="' + href + '" data-asst-link>' + label + '</a>';
      })
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>');
  }
  function mdCells(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); });
  }
  function md(text) {
    var lines = String(text || '').replace(/\r/g, '').split('\n');
    var out = [], para = [], list = null, table = null;
    function flushPara() { if (para.length) { out.push('<p>' + para.map(mdInline).join('<br>') + '</p>'); para = []; } }
    function flushList() { if (list) { out.push('<' + list.tag + '>' + list.items.map(function (i) { return '<li>' + mdInline(i) + '</li>'; }).join('') + '</' + list.tag + '>'); list = null; } }
    function flushTable() {
      if (!table) return;
      var rows = table.rows, head = null;
      if (rows.length > 1 && /^\s*\|?\s*:?-{2,}/.test(rows[1])) { head = mdCells(rows[0]); rows = rows.slice(2); }
      var html = '<div class="asst-table-wrap"><table class="asst-table">';
      if (head) html += '<thead><tr>' + head.map(function (c) { return '<th>' + mdInline(c) + '</th>'; }).join('') + '</tr></thead>';
      html += '<tbody>' + rows.map(function (r) {
        return '<tr>' + mdCells(r).map(function (c) { return '<td>' + mdInline(c) + '</td>'; }).join('') + '</tr>';
      }).join('') + '</tbody></table></div>';
      out.push(html);
      table = null;
    }
    function flushAll() { flushPara(); flushList(); flushTable(); }
    lines.forEach(function (line) {
      var heading = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/.exec(line);
      var bullet = /^\s*(?:[-*•])\s+(.*)$/.exec(line), num = /^\s*\d{1,2}[.)]\s+(.*)$/.exec(line);
      if (/^\s*\|.*\|\s*$/.test(line)) {
        flushPara(); flushList();
        if (!table) table = { rows: [] };
        table.rows.push(line);
      } else if (heading) {
        flushAll();
        out.push('<p class="asst-h"><b>' + mdInline(heading[1].replace(/\*\*/g, '')) + '</b></p>');
      } else if (bullet || num) {
        flushPara(); flushTable();
        var tag = bullet ? 'ul' : 'ol';
        if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
        list.items.push((bullet || num)[1]);
      } else if (!line.trim()) { flushAll(); }
      else { flushList(); flushTable(); para.push(line); }
    });
    flushAll();
    return out.join('') || '<p></p>';
  }
  SM.md = md;

  var LABEL_TEXT = { positive: 'Positive', negative: 'Negative', neutral: 'Neutral' };
  function pill(label) {
    var l = LABEL_TEXT[label] ? label : 'neutral';
    return '<span class="pill ' + l + '"><span class="dot" aria-hidden="true"></span>' + LABEL_TEXT[l] + '</span>';
  }
  function httpUrl(u) { return /^https?:\/\//i.test(u || ''); }
  function appPath(u) { return /^\/(?!\/)/.test(u || ''); }

  function initAssistant(sec) {
    if (sec._smAssistant) return sec._smAssistant;
    var inst = { prefill: function () {} };
    sec._smAssistant = inst;
    if (sec.getAttribute('data-enabled') !== 'true') return inst;
    var transcript = sec.querySelector('[data-assistant-transcript]');
    var chips = sec.querySelector('[data-assistant-chips]');
    var form = sec.querySelector('[data-assistant-composer]');
    var input = sec.querySelector('[data-assistant-input]');
    var sendBtn = sec.querySelector('[data-assistant-send]');
    var stopBtn = sec.querySelector('[data-assistant-stop]');
    var tpl = sec.querySelector('#assistant-confirm-template');
    var canAct = sec.getAttribute('data-can-act') === 'true';
    var isAdmin = sec.getAttribute('data-role') === 'admin';
    if (!transcript || !form || !input) return inst;
    var greeting = transcript.querySelector('[data-greeting]');
    var storeKey = 'sm-asst-' + (sec.getAttribute('data-subject-id') || '0');
    var hist = { messages: [], log: [] };
    var busy = null;              // {turn, ctrl, poll, stage}
    var focusRow = null;          // the row the user asked about (row menu)
    try { hist = JSON.parse(lsGet(storeKey) || 'null') || hist; } catch (e) {}
    if (!Array.isArray(hist.messages)) hist.messages = [];
    if (!Array.isArray(hist.log)) hist.log = [];
    function save() {
      hist.messages = hist.messages.slice(-20);
      hist.log = hist.log.slice(-60);
      lsSet(storeKey, JSON.stringify(hist));
    }
    function scroll() { transcript.scrollTop = transcript.scrollHeight; }
    function node(cls, html) {
      var d = document.createElement('div');
      d.className = cls;
      d.innerHTML = html;
      transcript.appendChild(d);
      scroll();
      return d;
    }
    function log(entry) { hist.log.push(entry); save(); return entry; }

    // ---- page context sent with each turn ----
    function highlighted() {
      if (focusRow && document.contains(focusRow)) return focusRow;
      return document.querySelector('table tr.is-active[data-result-id]');
    }
    function pageContext() {
      var ctx = { page: location.pathname };
      var h1 = document.querySelector('.page-head h1, main h1');
      if (h1) ctx.title = h1.textContent.trim().slice(0, 80);
      var top = SM.top();
      if (top) { ctx.overlay = top.url || ''; ctx.overlay_title = top.title; }
      var row = highlighted();
      if (row) {
        var t = row.querySelector('.rtitle');
        ctx.result = { id: row.getAttribute('data-result-id'), url: row.getAttribute('data-result-url') || '',
          title: t ? t.textContent.trim() : '', domain: row.getAttribute('data-domain') || '',
          label: row.getAttribute('data-label') || '' };
      }
      if (location.pathname === '/results') {
        var f = {}, q = new URLSearchParams(location.search);
        q.forEach(function (v, k) { f[k] = v; });
        var c = document.getElementById('f-country'), d = document.getElementById('f-date');
        if (c && c.value) f.country = c.value;
        if (d && d.value) f.date = d.value;
        ctx.filters = f;
      }
      return ctx;
    }

    // ---- rendering ----
    function addUser(text) { return node('assistant-msg assistant-msg-user', esc(text)); }
    function addBot(text) { return node('assistant-msg assistant-msg-bot', md(text)); }
    function addTool(text) { return node('assistant-tool', esc(text)); }
    function addError(text, hint) {
      var html = '<p>' + esc(text) + '</p>';
      if (hint) html += '<p class="assistant-hint" data-reasoning-hint>' + esc(hint) + '</p>';
      if (hint || /AI provider/i.test(text)) {
        html += '<p>' + (isAdmin ? '<a href="/settings#ai">Check Settings → AI provider</a>' : 'An admin can check Settings → AI provider.') + '</p>';
      }
      return node('assistant-card assistant-card-error', html);
    }
    function render(entry, live) {
      if (entry.k === 'user') return addUser(entry.text);
      if (entry.k === 'bot') return addBot(entry.text);
      if (entry.k === 'tool') return addTool(entry.text);
      if (entry.k === 'error') return addError(entry.text, entry.hint);
      if (entry.k === 'card') return card(entry);
      if (entry.k === 'confirm') return confirmCard(entry);
      if (entry.k === 'intent') return intent(entry, live);
      if (entry.k === 'followups') return followups(entry);
      return null;
    }
    hist.log.forEach(function (e) { render(e, false); });

    function openOverlay(kind, url, title, wide) {
      A.handoff(title);
      return (kind === 'panel' ? SM.openPanel : SM.openModal)(url, { title: title, wide: !!wide });
    }
    function goPage(url, label) {
      var u = new URL(url, location.href);
      if (u.pathname === location.pathname && u.search === location.search) {
        if (u.hash && u.hash !== location.hash) location.hash = u.hash; else openHashDetails();
        A.handoff(label);
      } else {
        location.assign(u.href);
      }
    }
    function intentAction(ev) {
      var p = ev.params || {};
      var map = INTENTS[ev.target], label = ev.label || 'Open';
      if (map) {
        var url = map[1];
        if (ev.target === 'compare' && (p.run_a || p.run_b)) {
          url += '?a=' + encodeURIComponent(p.run_a || '') + '&b=' + encodeURIComponent(p.run_b || '');
        }
        return function () { openOverlay(map[0], url, label, map[2]); };
      }
      if (INTENT_PAGES[ev.target]) return function () { goPage(INTENT_PAGES[ev.target], label); };
      if (ev.target === 'job' && p.job_id) {
        return function () { openOverlay('panel', '/ui/job/' + encodeURIComponent(p.job_id), 'Search progress'); };
      }
      if (ev.target === 'alerts') return function () { var b = document.getElementById('bell'); A.handoff('Alerts'); if (b) b.click(); };
      if (ev.target === 'notes' && p.url) {
        return function () { openOverlay('panel', '/ui/note?url=' + encodeURIComponent(p.url), 'Note'); };
      }
      if (ev.target === 'result' && p.run_id) {
        return function () { openOverlay('panel', '/ui/run/' + encodeURIComponent(p.run_id), label); };
      }
      if (ev.url && appPath(ev.url)) return function () { goPage(ev.url, label); };
      return null;
    }
    // Live intents (open a page/dialog) run only AFTER the whole turn is
    // persisted (see runLive): opening a dialog minimizes the sheet on a phone
    // and opening a page navigates away -- neither may lose the turn.
    var liveQueue = [];
    function runLive() {
      var q = liveQueue; liveQueue = [];
      if (q.length) setTimeout(function () { q.forEach(function (go) { try { go(); } catch (e) {} }); }, 0);
    }
    function intent(entry, live) {
      var ev = entry.ev;
      if (ev.target === 'download') return null;          // shown as a report card
      var go = intentAction(ev);
      if (!go) return null;
      var row = node('assistant-tool assistant-intent', '<button type="button" class="btn small">' + esc(ev.label || 'Open') + '</button>');
      row.querySelector('button').addEventListener('click', go);
      // A fallback button (the model described it without opening it) waits for a click.
      if (live && !ev.fallback && ev.target !== 'result' && ev.target !== 'settings') liveQueue.push(go);
      return row;
    }

    // Cards built from tool results ({type:'card', kind, data}).
    function card(entry) {
      var ev = entry.ev, d = ev.data || {}, html = '';
      if (ev.kind === 'results') {
        html = (d.title ? '<p class="assistant-card-title">' + esc(d.title) + '</p>' : '') + '<ul class="ac-list">' +
          (d.items || []).map(function (it) {
            var title = httpUrl(it.url) ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">' + esc(it.title || it.url) + '</a>' : esc(it.title);
            var btns = (httpUrl(it.url) ? '<a class="btn small ghost" href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">Open</a>' : '') +
              (d.can_review && it.result_id ? '<button type="button" class="btn small" data-ac-review="' + esc(it.result_id) + '">Review</button>' : '');
            var where = (it.position != null ? '#' + esc(it.position) + (it.country ? ' ' + esc(it.country) : '') + ' · ' : '');
            var by = it.source ? ' <span class="muted">by ' + esc(it.source) + '</span>' : '';
            var namesake = it.same_person === 'no' ? ' <span class="tag">not this person</span>' : '';
            return '<li class="ac-item"><div class="ac-main"><div class="ac-title">' + title + '</div><div class="ac-meta">' +
              '<span class="dom">' + where + esc(it.domain) + '</span> ' + pill(it.label) + by + namesake +
              (it.review_status === 'pending' ? ' <span class="tag tag-review">needs review</span>' : '') + '</div>' +
              (it.why ? '<div class="ac-why">' + esc(it.why) + '</div>' : '') + '</div><div class="ac-actions">' + btns + '</div></li>';
          }).join('') + '</ul>' + (d.more ? '<p class="muted">' + esc(d.more) + ' more in Results.</p>' : '');
      } else if (ev.kind === 'changes') {
        var s0 = d.summary || {};
        var bits = [['entered', '+', 'entered'], ['exited', '−', 'left'], ['moved_up', '↑', 'up'], ['moved_down', '↓', 'down'], ['new_negatives', '!', 'new negative']]
          .filter(function (b) { return s0[b[0]]; })
          .map(function (b) { return '<span class="tag">' + b[1] + ' ' + esc(s0[b[0]]) + ' ' + b[2] + '</span>'; }).join(' ');
        html = '<p class="assistant-card-title">Changes ' + esc(d.from || '') + ' → ' + esc(d.to || '') + ' · ' + esc(d.country || '') + '</p>' +
          '<div class="ac-tags">' + (bits || '<span class="muted">Nothing changed.</span>') + '</div>' +
          '<ul class="ac-changes">' + (d.items || []).map(function (it) {
            var word = it.kind === 'entered' ? 'entered at ' + esc(it.position) : it.kind === 'exited' ? 'left (was ' + esc(it.position) + ')' :
              'moved ' + (it.places > 0 ? 'up ' : 'down ') + esc(Math.abs(it.places || 0)) + ' to ' + esc(it.position);
            return '<li><b>' + esc(it.domain || '?') + '</b> ' + word + ' ' + pill(it.label) + '</li>';
          }).join('') + '</ul>' + (d.more ? '<p class="muted">' + esc(d.more) + ' more in compare.</p>' : '') +
          '<div class="ac-actions"><button type="button" class="btn small primary" data-ac-compare>Open compare</button></div>';
      } else if (ev.kind === 'review') {
        html = '<p class="assistant-card-title">Review · ' + esc(d.total) + ' waiting</p><ul class="ac-list">' +
          (d.items || []).map(function (it, i) {
            var done = it.done ? '<p class="ac-done" role="status">' + esc(it.done) + '</p>' : '';
            var title = httpUrl(it.url) ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">' + esc(it.title || it.url) + '</a>' : esc(it.title);
            var verdicts = it.done ? '' : '<div class="ac-verdicts" role="group" aria-label="Label for ' + esc(it.domain) + '">' +
              [['positive', 'Positive'], ['negative', 'Negative'], ['neutral', 'Neutral'], ['not_this_person', 'Not this person']].map(function (v) {
                return '<button type="button" class="btn small ac-verdict" data-verdict="' + v[0] + '" data-index="' + i + '">' +
                  (v[0] === 'not_this_person' ? esc(v[1]) : pill(v[0])) + '</button>';
              }).join('') + '</div>';
            return '<li class="ac-item ac-review-item" data-result-id="' + esc(it.result_id) + '"><div class="ac-main"><div class="ac-title">' + title +
              '</div><div class="ac-meta"><span class="dom">' + esc(it.domain) + '</span> now ' + pill(it.label) + '</div>' +
              (it.why ? '<div class="ac-why">' + esc(it.why) + '</div>' : '') + verdicts + done + '</div></li>';
          }).join('') + '</ul><div class="ac-actions"><button type="button" class="btn small" data-ac-review-all>Open the review dialog</button></div>';
      } else if (ev.kind === 'report') {
        html = '<p class="assistant-card-title">' + (d.fresh ? 'Report ready' : 'Previous reports') + '</p><ul class="ac-files">' +
          (d.files || []).map(function (f) {
            var dl = appPath(f.url) ? '<a class="btn small' + (d.fresh ? ' primary' : '') + '" href="' + esc(f.url) + '" download>Download</a>' : '';
            return '<li><div><b>' + esc(f.title || f.file) + '</b> <span class="muted">' + esc((f.kind || '').toUpperCase()) +
              (f.created ? ' · ' + esc(f.created) : '') + '</span></div>' + dl + '</li>';
          }).join('') + '</ul>';
      } else if (ev.kind === 'sov') {
        html = '<p class="assistant-card-title">Share of voice · latest run</p>' + (d.rows || []).map(function (r) {
          var seg = function (n, tok) { return n ? '<span style="flex:' + Number(n) + ';background:var(--' + tok + ')"></span>' : ''; };
          return '<div class="ac-sov-row"><div class="ac-sov-head"><b>' + esc(r.country) + '</b> <span class="muted">' + esc(r.engine) +
            ' · ' + esc(r.total) + ' results</span></div><div class="sov" role="img" aria-label="' + esc(r.positive) + ' positive, ' +
            esc(r.negative) + ' negative, ' + esc(r.neutral) + ' neutral">' + seg(r.positive, 'pos') + seg(r.negative, 'neg') + seg(r.neutral, 'neu') +
            '</div><div class="legend"><span><span class="swatch pos" aria-hidden="true"></span>' + esc(r.positive) + ' positive</span>' +
            '<span><span class="swatch neg" aria-hidden="true"></span>' + esc(r.negative) + ' negative</span>' +
            '<span><span class="swatch neu" aria-hidden="true"></span>' + esc(r.neutral) + ' neutral</span></div></div>';
        }).join('');
      } else return null;
      var el = node('assistant-card assistant-card-' + ev.kind, html);
      el.setAttribute('data-card-kind', ev.kind);
      el._entry = entry;
      return el;
    }

    // Confirmation cards; editable fields (ev.edit) are sent back as `args`.
    function fieldHtml(f, i) {
      var id = 'acf-' + Math.random().toString(36).slice(2, 8) + '-' + i;
      if (f.type === 'chips' || f.type === 'radio') {
        var multi = f.type === 'chips';
        var vals = multi ? (f.value || []) : [f.value];
        return '<fieldset class="ac-field" data-field="' + esc(f.name) + '" data-type="' + f.type + '"><legend>' + esc(f.label) + '</legend>' +
          '<div class="ac-toggles" role="group">' + (f.options || []).map(function (o) {
            return '<button type="button" class="ac-toggle" aria-pressed="' + (vals.indexOf(o.value) !== -1) + '" data-value="' + esc(o.value) + '">' + esc(o.label) + '</button>';
          }).join('') + '</div></fieldset>';
      }
      if (f.type === 'select') {
        return '<div class="ac-field" data-field="' + esc(f.name) + '" data-type="select"><label for="' + id + '">' + esc(f.label) + '</label>' +
          '<select id="' + id + '">' + (f.options || []).map(function (o) {
            return '<option value="' + esc(o.value) + '"' + (o.value === f.value ? ' selected' : '') + '>' + esc(o.label) + '</option>';
          }).join('') + '</select>' + (f.help ? '<span class="help">' + esc(f.help) + '</span>' : '') + '</div>';
      }
      if (f.type === 'textarea') {
        return '<div class="ac-field" data-field="' + esc(f.name) + '" data-type="textarea"><label for="' + id + '">' + esc(f.label) + '</label>' +
          '<textarea id="' + id + '" rows="6" maxlength="' + esc(f.maxlength || 4000) + '">' + esc(f.value || '') + '</textarea></div>';
      }
      return '';
    }
    function fieldValue(box) {
      var type = box.getAttribute('data-type');
      if (type === 'chips' || type === 'radio') {
        var on = Array.prototype.filter.call(box.querySelectorAll('.ac-toggle'), function (b) { return b.getAttribute('aria-pressed') === 'true'; })
          .map(function (b) { return b.getAttribute('data-value'); });
        return type === 'chips' ? on : (on[0] || '');
      }
      var c = box.querySelector('select, textarea');
      return c ? c.value : '';
    }
    function edits(cardEl, ev) {
      var out = {}, any = false;
      ((ev.edit || {}).fields || []).forEach(function (f) {
        var box = cardEl.querySelector('[data-field="' + f.name + '"]');
        if (!box) return;
        var v = fieldValue(box);
        if (JSON.stringify(v) !== JSON.stringify(f.value)) { out[f.name] = v; any = true; }
      });
      return any ? out : null;
    }
    function estimate(cardEl, ev) {
      var est = (ev.edit || {}).estimate, note = cardEl.querySelector('[data-confirm-note]');
      if (!est || !note) return;
      var get = function (n) { var b = cardEl.querySelector('[data-field="' + n + '"]'); return b ? fieldValue(b) : []; };
      var n = get('countries').length * get('engines').length;
      var ok = cardEl.querySelector('[data-assistant-confirm]');
      if (!n) { note.textContent = 'Pick at least one country and one engine.'; if (ok) ok.disabled = true; return; }
      if (ok) ok.disabled = false;
      var cost = est.per_run * n * (est.pages || 1), lo = 20 * n, hi = 90 * n;
      var time = hi < 180 ? lo + '-' + hi + ' seconds' : (Math.floor(lo / 60) || 1) + '-' + Math.floor(hi / 60) + ' minutes';
      // Same wording as common/money.py run_cost_phrase: never "about $0.00".
      var phrase = (Number(est.per_run) || 0) < 0.01 ? 'under $0.01 per country and engine' : 'about $' + cost.toFixed(2);
      var txt = 'Uses your Apify plan: ' + phrase + ' (' + n + ' run' + (n === 1 ? '' : 's') +
        (est.measured ? '' : ', rough estimate') + '). Takes about ' + time + '.';
      if (est.budget) txt += ' This month: $' + Number(est.spent || 0).toFixed(2) + ' of your $' + Number(est.budget).toFixed(2) + ' budget.';
      note.textContent = txt;
    }
    function confirmCard(entry) {
      var ev = entry.ev;
      if (entry.done) return addTool(entry.done);
      if (!tpl || !tpl.content) return null;
      var frag = tpl.content.cloneNode(true);
      var c = frag.querySelector('[data-assistant-confirm-card]');
      c.setAttribute('data-confirm-id', ev.id || '');
      var t = c.querySelector('[data-confirm-title]'); if (t) t.textContent = ev.title || 'Confirm this action?';
      var ul = c.querySelector('[data-confirm-details]');
      (ev.details || []).forEach(function (d) {
        if (ev.edit && /^(Countries|Engines|Type|Period):/.test(d)) return;   // shown as editable fields
        var li = document.createElement('li'); li.textContent = d; ul.appendChild(li);
      });
      var note = c.querySelector('[data-confirm-note]');
      if (ev.edit && ev.edit.fields) {
        var wrap = document.createElement('div');
        wrap.className = 'ac-fields';
        wrap.innerHTML = ev.edit.fields.map(fieldHtml).join('');
        (note || ul).parentNode.insertBefore(wrap, note || ul.nextSibling);
      }
      if (note) { if (ev.cost_note) note.textContent = ev.cost_note; else note.parentNode.removeChild(note); }
      var ok = c.querySelector('[data-assistant-confirm]');
      if (ok && ev.button) ok.textContent = ev.button;
      var err = document.createElement('p');
      err.className = 'ac-error'; err.setAttribute('role', 'alert'); err.hidden = true; err.setAttribute('data-confirm-error', '');
      c.appendChild(err);
      c._entry = entry;
      transcript.appendChild(frag);
      estimate(c, ev);
      scroll();
      return c;
    }

    function followups(entry) {
      var list = entry.items || [];
      if (!list.length) return null;
      var box = node('assistant-followups', '');
      box.setAttribute('role', 'group');
      box.setAttribute('aria-label', 'Suggested next steps');
      list.forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'assistant-chip assistant-followup';
        b.setAttribute('data-assistant-chip', ''); b.setAttribute('data-prompt', c.prompt || c.label || '');
        b.textContent = c.label || c.prompt || '';
        box.appendChild(b);
      });
      return box;
    }
    function renderChips(list, next) {
      if (!chips || !list) return;
      chips.innerHTML = '';
      chips.classList.toggle('is-followups', !!next);
      if (next) {
        var lab = document.createElement('span');
        lab.className = 'assistant-chips-label';
        lab.textContent = 'Next steps';
        chips.appendChild(lab);
      }
      list.forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'assistant-chip' + (next ? ' assistant-followup' : '');
        b.setAttribute('data-assistant-chip', ''); b.setAttribute('data-prompt', c.prompt || c.label || '');
        b.textContent = c.label || c.prompt || '';
        chips.appendChild(b);
      });
    }

    function postJSON(url, payload, signal) {
      return fetch(url, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: signal,
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) {
        if (r.redirected && /\/login/.test(r.url)) { location.href = r.url; throw new Error('signed out'); }
        return r.json().catch(function () { return {}; }).then(function (data) { return { status: r.status, data: data || {} }; });
      });
    }
    function problem(status, data) {
      if (status === 503) return (data && data.message) || 'The assistant is off: set up an AI provider in Settings → AI provider.';
      if (status === 429) return 'You are sending messages quickly. Wait a moment and try again.';
      if (status === 403) return (data && data.message) || 'You don\'t have permission to do that.';
      if (status === 404) return (data && data.message) || 'That proposal expired. Ask again to get a fresh one.';
      if (status === 409) return (data && data.message) || 'This was already handled, or you switched subject since.';
      if (status >= 400 && status < 500) return (data && data.message) || 'That message couldn\'t be sent.';
      return 'The assistant didn\'t answer. Try again in a moment.';
    }

    // ---- "thinking" indicator with the current step, Stop ----
    var STAGES = ['Thinking…', 'Looking at your data…', 'Writing the answer…'];
    function startBusy(turn) {
      var ind = node('assistant-typing', '<span class="dots" aria-hidden="true"><span></span><span></span><span></span></span>' +
        '<span class="asst-step" data-asst-step>Thinking…</span>');
      ind.setAttribute('role', 'status');
      var b = { turn: turn, ctrl: window.AbortController ? new AbortController() : null, ind: ind, stage: 0, seen: false };
      var stepEl = ind.querySelector('[data-asst-step]');
      b.timer = setInterval(function () {
        var url = (sec.getAttribute('data-progress-url') || '/assistant/progress') + '?turn=' + encodeURIComponent(turn);
        fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (p) {
            if (busy !== b) return;
            if (p && p.step) { b.seen = true; stepEl.textContent = p.step; }
            else if (!b.seen) { b.stage = Math.min(b.stage + 1, STAGES.length - 1); stepEl.textContent = STAGES[b.stage]; }
          }, function () {});
      }, 900);
      if (sendBtn) sendBtn.hidden = true;
      if (stopBtn) { stopBtn.hidden = false; }
      busy = b;
      return b;
    }
    function endBusy(b) {
      if (!b) return;
      clearInterval(b.timer);
      if (b.ind && b.ind.parentNode) b.ind.parentNode.removeChild(b.ind);
      if (busy === b) busy = null;
      if (!busy) { if (sendBtn) sendBtn.hidden = false; if (stopBtn) stopBtn.hidden = true; }
    }
    function stop() {
      var b = busy;
      if (!b) return;
      endBusy(b);
      b.stopped = true;
      if (b.ctrl) try { b.ctrl.abort(); } catch (e) {}
      postJSON(sec.getAttribute('data-stop-url') || '/assistant/stop', { turn: b.turn }).catch(function () {});
      log({ k: 'tool', text: 'Stopped. Nothing was changed.' });
      addTool('Stopped. Nothing was changed.');
      input.focus();
    }

    function applyEvents(list, live) {
      (list || []).forEach(function (ev) {
        if (ev.type === 'tool') { log({ k: 'tool', text: ev.summary || ('Used: ' + (ev.name || 'a tool')) }); addTool(ev.summary || ('Used: ' + (ev.name || 'a tool'))); }
        else if (ev.type === 'card') { var ce = log({ k: 'card', ev: ev }); card(ce); }
        else if (ev.type === 'confirm') { var cf = log({ k: 'confirm', ev: ev }); confirmCard(cf); }
        else if (ev.type === 'intent') { var ie = log({ k: 'intent', ev: ev }); intent(ie, live); }
      });
    }
    function clearFollowups() {
      var old = transcript.querySelectorAll('.assistant-followups');
      for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
      hist.log = hist.log.filter(function (e) { return e.k !== 'followups'; });
    }

    function send(text) {
      text = (text || '').trim();
      if (!text || busy) return;
      clearFollowups();
      hist.messages.push({ role: 'user', content: text.slice(0, 4000) });
      log({ k: 'user', text: text });
      addUser(text);
      var turn = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      var b = startBusy(turn);
      var payload = { messages: hist.messages, context: pageContext(), turn: turn };
      focusRow = null;
      postJSON(sec.getAttribute('data-chat-url') || '/assistant/chat', payload, b.ctrl ? b.ctrl.signal : undefined).then(function (res) {
        if (b.stopped || busy !== b) return;          // the user pressed Stop: ignore the late answer
        endBusy(b);
        var d = res.data;
        if (res.status !== 200) { var pe = problem(res.status, d); log({ k: 'error', text: pe }); addError(pe); return; }
        if (d.stopped) return;
        if (Array.isArray(d.messages)) hist.messages = d.messages.filter(function (m) { return m && (m.role === 'user' || m.role === 'assistant'); });
        else if (d.reply) hist.messages.push({ role: 'assistant', content: d.reply });
        applyEvents(d.events, true);
        if (d.reply && !(d.error && /couldn't get an answer/i.test(d.reply))) { log({ k: 'bot', text: d.reply }); addBot(d.reply); }
        if (d.error) { log({ k: 'error', text: d.error, hint: d.hint || '' }); addError(d.error, d.hint || ''); }
        // Next-step chips from this reply replace the bar; else the state chips.
        var fu = (d.followups && d.followups.length) ? d.followups : [];
        renderChips(fu.length ? fu : d.suggestions, !!fu.length);
        scroll();
        save();
        runLive();                                     // only now: the turn is persisted
      }, function (err) {
        if (b.stopped) return;
        endBusy(b);
        if (!(err && (err.message === 'signed out' || err.name === 'AbortError'))) {
          var m = 'The assistant didn\'t answer. Check your connection and try again.';
          log({ k: 'error', text: m }); addError(m);
        }
      });
    }

    function decide(cardEl, confirm) {
      var entry = cardEl._entry || {};
      var ev = entry.ev || {};
      var id = cardEl.getAttribute('data-confirm-id');
      var payload = { id: id };
      if (confirm) { var ed = edits(cardEl, ev); if (ed) payload.args = ed; }
      var btns = cardEl.querySelectorAll('button, select, textarea');
      for (var i = 0; i < btns.length; i++) btns[i].disabled = true;
      var errEl = cardEl.querySelector('[data-confirm-error]');
      var url = confirm ? (sec.getAttribute('data-confirm-url') || '/assistant/confirm') : (sec.getAttribute('data-cancel-url') || '/assistant/cancel');
      postJSON(url, payload).then(function (res) {
        var d = res.data || {};
        if (confirm && res.status === 400 && d.error === 'invalid_args') {
          // Still pending: show why, let the user fix the card.
          for (var j = 0; j < btns.length; j++) btns[j].disabled = false;
          if (errEl) { errEl.hidden = false; errEl.textContent = d.message || 'Those changes aren\'t allowed.'; }
          return;
        }
        var msg = d.message || (res.status === 200 ? (confirm ? 'Done.' : 'Cancelled. Nothing was changed.') : problem(res.status, d));
        var text = (res.status === 200 ? (confirm ? 'Confirmed. ' : '') : '') + msg;
        entry.done = text;
        var done = document.createElement('div');
        done.className = 'assistant-tool';
        done.textContent = text;
        cardEl.parentNode.replaceChild(done, cardEl);
        hist.messages.push({ role: 'assistant', content: msg });
        save();
        if (res.status === 200) applyEvents(d.events, true);
        save();
        scroll();
        runLive();
      }, function () {
        for (var j = 0; j < btns.length; j++) btns[j].disabled = false;
        addError('That didn\'t go through. Try again.');
      });
    }

    function verdict(cardEl, btn) {
      var li = btn.closest('[data-result-id]');
      var rid = li && li.getAttribute('data-result-id');
      var v = btn.getAttribute('data-verdict');
      if (!rid || !v) return;
      var group = btn.closest('.ac-verdicts');
      var all = group ? group.querySelectorAll('button') : [];
      for (var i = 0; i < all.length; i++) all[i].disabled = true;
      var path = v === 'not_this_person' ? '/review/' + encodeURIComponent(rid) + '/not-this-person' : '/review/' + encodeURIComponent(rid) + '/verdict';
      var body = new URLSearchParams({ mode: 'chat' });
      if (v !== 'not_this_person') body.set('classification', v);
      fetch(path, { method: 'POST', credentials: 'same-origin', cache: 'no-store', body: body,
        headers: { 'Accept': 'application/json' } }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) { return { status: r.status, data: d }; });
      }).then(function (res) {
        var d = res.data || {};
        if (res.status !== 200 || !d.ok) {
          for (var j = 0; j < all.length; j++) all[j].disabled = false;
          SM.toast(res.status === 403 ? 'Only an operator or admin can label results.' : (d.message || 'That didn\'t work. Try again.'), 'error');
          return;
        }
        var idx = parseInt(btn.getAttribute('data-index'), 10);
        var entry = cardEl._entry;
        if (entry && entry.ev && entry.ev.data && entry.ev.data.items && entry.ev.data.items[idx]) {
          entry.ev.data.items[idx].done = d.message;
          save();
        }
        if (group) group.outerHTML = '<p class="ac-done" role="status">' + esc(d.message) + '</p>';
        var badges = document.querySelectorAll('[data-review-count]');
        for (var k = 0; k < badges.length; k++) { badges[k].textContent = String(d.pending || 0); badges[k].hidden = !d.pending; }
      }, function () {
        for (var j = 0; j < all.length; j++) all[j].disabled = false;
        SM.toast('That didn\'t work. Check your connection and try again.', 'error');
      });
    }

    sec.addEventListener('click', function (e) {
      var t = e.target;
      if (!(t instanceof Element)) return;
      var chip = t.closest('[data-assistant-chip]');
      if (chip) { send(chip.getAttribute('data-prompt') || chip.textContent); return; }
      if (t.closest('[data-assistant-stop]')) { stop(); return; }
      if (t.closest('[data-assistant-clear]')) {
        if (busy) stop();
        hist = { messages: [], log: [] };
        save();
        Array.prototype.slice.call(transcript.children).forEach(function (c) { if (c !== greeting) transcript.removeChild(c); });
        input.focus();
        return;
      }
      var tog = t.closest('.ac-toggle');
      if (tog) {
        var box = tog.closest('[data-field]');
        if (box && box.getAttribute('data-type') === 'radio') {
          Array.prototype.forEach.call(box.querySelectorAll('.ac-toggle'), function (b) { b.setAttribute('aria-pressed', b === tog ? 'true' : 'false'); });
        } else tog.setAttribute('aria-pressed', tog.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
        var cc = tog.closest('[data-assistant-confirm-card]');
        if (cc && cc._entry) estimate(cc, cc._entry.ev);
        return;
      }
      var cardEl = t.closest('[data-assistant-confirm-card]');
      if (cardEl && t.closest('[data-assistant-confirm]')) { decide(cardEl, true); return; }
      if (cardEl && t.closest('[data-assistant-cancel]')) { decide(cardEl, false); return; }
      var ac = t.closest('.assistant-card');
      if (ac && t.closest('.ac-verdict')) { verdict(ac, t.closest('.ac-verdict')); return; }
      if (ac && t.closest('[data-ac-review]')) {
        openOverlay('modal', '/ui/review?id=' + encodeURIComponent(t.closest('[data-ac-review]').getAttribute('data-ac-review')) +
          '&back=' + encodeURIComponent(location.pathname + location.search), 'Change label');
        return;
      }
      if (ac && t.closest('[data-ac-review-all]')) { openOverlay('modal', '/ui/review', 'Review'); return; }
      if (ac && t.closest('[data-ac-compare]')) {
        var p = ((ac._entry || {}).ev || {}).data || {};
        var q = p.params || {};
        openOverlay('modal', '/ui/compare' + (q.run_a || q.run_b ? '?a=' + encodeURIComponent(q.run_a || '') + '&b=' + encodeURIComponent(q.run_b || '') : ''), 'Compare runs', true);
        return;
      }
      var link = t.closest('a[data-asst-link]');
      if (link && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
        var u = new URL(link.getAttribute('href'), location.href);
        var m = u.searchParams.get('modal'), pn = u.searchParams.get('panel');
        if ((m || pn) && /^\/(ui)\//.test(m || pn)) { e.preventDefault(); openOverlay(m ? 'modal' : 'panel', m || pn, link.textContent.trim()); }
        else { e.preventDefault(); goPage(u.pathname + u.search + u.hash, link.textContent.trim()); }
      }
    });
    sec.addEventListener('change', function (e) {
      var cc = e.target instanceof Element && e.target.closest('[data-assistant-confirm-card]');
      if (cc && cc._entry) estimate(cc, cc._entry.ev);
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var v = input.value;
      input.value = '';
      send(v);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true })); }
    });
    scroll();
    inst.prefill = function (text, row) {
      focusRow = row || null;
      input.value = text || '';
      try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); }
      input.setSelectionRange(input.value.length, input.value.length);
    };
    inst.send = send;
    inst.context = pageContext;
    return inst;
  }

  // ---- Settings & integration dialogs (dashboard/integrations.py) ----------------
  // data-sm-action="/url" on a button inside a form: POST the form's fields to
  //   that URL (fetch, X-SM-Fragment) and show the answer inside the element
  //   named by data-sm-target (Test, "Find my chat id"). Without JS the button
  //   is a normal submit with formaction, answered with a redirect + toast.
  function runAction(btn) {
    var form = btn.form || btn.closest('form');
    var url = btn.getAttribute('data-sm-action');
    var scope = btn.closest('.modal, .panel') || document;
    var sel = btn.getAttribute('data-sm-target');
    var target = sel ? (scope.querySelector(sel) || document.querySelector(sel)) : null;
    var data = form ? new FormData(form) : new FormData();
    var label = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    if (btn.getAttribute('data-busy-label')) btn.textContent = btn.getAttribute('data-busy-label');
    if (target) target.innerHTML = '<div class="test-result busy" role="status">' +
      esc(btn.getAttribute('data-busy-label') || 'Working…') + '</div>';
    SM.load(url, { method: 'POST', body: new URLSearchParams(data) }).then(function (res) {
      if (!target) return;
      target.innerHTML = res.html;
      var ok = target.querySelector('[data-test-ok="1"]');
      var steps = scope.querySelectorAll('.stepper li');
      if (ok && steps.length && btn.getAttribute('data-sm-action').indexOf('/test') !== -1) {
        for (var i = 0; i < steps.length; i++) {
          steps[i].className = i < steps.length - 1 ? 'done' : 'on';
          if (i === steps.length - 1) steps[i].setAttribute('aria-current', 'step');
          else steps[i].removeAttribute('aria-current');
        }
      }
    }, function (err) {
      if (!target || (err && err.message === 'signed out')) return;
      target.innerHTML = err && err.html && err.status < 500 && err.status !== 403 ? err.html :
        '<div class="test-result fail" role="alert"><b>' +
        (err && err.status === 403 ? 'You don\'t have permission to do that.' : 'That didn\'t work.') +
        '</b> Check your connection and try again.</div>';
    }).then(function () {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.innerHTML = label;
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy') ? resolve() : reject(); } catch (e) { reject(e); }
      document.body.removeChild(ta);
    });
  }

  function scopeOf(el) { return el.closest('form') || el.closest('.modal, .panel') || document; }

  // data-show-when="field=value1|value2": shown only while that field has one of the values.
  function applyShowWhen(root) {
    var els = (root || document).querySelectorAll('[data-show-when]');
    for (var i = 0; i < els.length; i++) {
      var rule = els[i].getAttribute('data-show-when').split('=');
      var scope = scopeOf(els[i]);
      var fields = scope.querySelectorAll('[name="' + rule[0] + '"]');
      var value = '';
      for (var j = 0; j < fields.length; j++) {
        var f = fields[j];
        if (f.type === 'radio' || f.type === 'checkbox') { if (f.checked) value = f.value; }
        else value = f.value;
      }
      els[i].hidden = (rule[1] || '').split('|').indexOf(value) === -1;
    }
  }
  applyShowWhen(document);
  document.addEventListener('sm:load', function (e) { applyShowWhen(e.target); });

  // A radio with data-fill / data-placeholder / data-text (JSON {selector: value}):
  // choosing it fills inputs, placeholders and helper texts (presets).
  function applyPreset(radio) {
    var scope = radio.closest('.modal, .panel') || document;
    function each(attr, fn) {
      var raw = radio.getAttribute(attr);
      if (!raw) return;
      var map;
      try { map = JSON.parse(raw); } catch (e) { return; }
      Object.keys(map).forEach(function (selector) {
        var el = scope.querySelector(selector);
        if (el) fn(el, map[selector]);
      });
    }
    each('data-fill', function (el, v) { el.value = v; });
    each('data-placeholder', function (el, v) { el.setAttribute('placeholder', v); });
    each('data-text', function (el, v) { el.textContent = v; });
  }

  document.addEventListener('change', function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    if (t.matches('input[type=radio][data-fill], input[type=radio][data-text]') && t.checked) applyPreset(t);
    if (t.name) applyShowWhen(scopeOf(t));
  });

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    var act = t.closest('[data-sm-action]');
    if (act && !act.disabled) { e.preventDefault(); e.stopPropagation(); runAction(act); return; }
    var rep = t.closest('[data-secret-replace]');
    if (rep) {
      var box = rep.closest('[data-secret]');
      box.setAttribute('data-state', 'replace');
      var inp = box.querySelector('input');
      if (inp) inp.focus();
      return;
    }
    var keep = t.closest('[data-secret-keep]');
    if (keep) {
      var kb = keep.closest('[data-secret]');
      var ki = kb.querySelector('input');
      if (ki) ki.value = '';
      kb.setAttribute('data-state', 'saved');
      var rb = kb.querySelector('[data-secret-replace]');
      if (rb) rb.focus();
      return;
    }
    var fill = t.closest('[data-sm-fill]');
    if (fill) {
      var fscope = fill.closest('.modal, .panel') || document;
      var input = fscope.querySelector(fill.getAttribute('data-sm-fill'));
      if (!input) return;
      var holder = input.closest('[data-secret]');
      if (holder && holder.getAttribute('data-state') === 'saved') holder.setAttribute('data-state', 'replace');
      input.value = fill.getAttribute('data-value') || '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.classList.add('just-filled');
      setTimeout(function () { input.classList.remove('just-filled'); }, 1200);
      var picks = fscope.querySelectorAll('[data-sm-fill="' + fill.getAttribute('data-sm-fill') + '"]');
      for (var p = 0; p < picks.length; p++) picks[p].classList.toggle('picked', picks[p] === fill);
      if (fill.closest('.chat-pick')) fill.textContent = 'Selected';
      return;
    }
    var cp = t.closest('[data-copy]');
    if (cp) {
      var src = document.querySelector(cp.getAttribute('data-copy'));
      if (!src) return;
      copyText(src.textContent.trim()).then(function () {
        cp.textContent = 'Copied';
        SM.toast('API key copied. Paste it into your tool now.', 'ok');
      }, function () { SM.toast('Couldn\'t copy. Select the key and copy it by hand.', 'error'); });
    }
  }, true);

  // Settings: "Find a setting" filters sections (and the cards inside them).
  var finder = document.querySelector('[data-settings-find]');
  if (finder) {
    var secs = Array.prototype.slice.call(document.querySelectorAll('.sgroup > section'));
    var empty = document.querySelector('[data-settings-empty]');
    finder.addEventListener('input', function () {
      var q = finder.value.trim().toLowerCase();
      var shown = 0;
      secs.forEach(function (sec) {
        var cards = sec.querySelectorAll(':scope > .card, :scope > div > .card');
        var any = false;
        if (cards.length > 1) {
          for (var i = 0; i < cards.length; i++) {
            var hit = !q || cards[i].textContent.toLowerCase().indexOf(q) !== -1;
            cards[i].hidden = !hit;
            any = any || hit;
          }
        } else {
          any = !q || sec.textContent.toLowerCase().indexOf(q) !== -1;
          if (cards[0]) cards[0].hidden = false;
        }
        sec.hidden = !any;
        var link = document.querySelector('.snav a[href="#' + sec.id + '"]');
        if (link) link.hidden = !any;
        if (any) shown++;
      });
      if (empty) empty.hidden = shown !== 0;
    });
  }

  var auto = document.body.getAttribute('data-open-dialog');
  if (auto) {
    var d = document.getElementById(auto);
    if (d && typeof d.showModal === 'function') d.showModal();
  }
})();
