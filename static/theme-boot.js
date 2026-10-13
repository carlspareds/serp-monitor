// Applies the saved theme before first paint (no flash of the wrong theme).
// Loaded synchronously in <head>: the Content-Security-Policy allows no
// inline scripts, so this can't be an inline <script>. app.js owns the toggle.
document.documentElement.classList.add('js');
try {
  var t = localStorage.getItem('theme');
  if (t) document.documentElement.setAttribute('data-theme', t);
} catch (e) { /* storage blocked: system theme */ }
