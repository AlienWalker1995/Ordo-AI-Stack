// Sets <html data-theme> before the first paint, so a viewer whose theme resolves to light never
// sees the dark palette flash first. A classic script in <head> blocks rendering until it runs;
// the app's module script is deferred and would run too late. Served from 'self', so it fits the
// strict CSP (script-src 'self', no inline scripts).
// Keep in step with src/lib/theme.jsx: the same storage key, choices and media query
// (test/theme.test.jsx runs this file and checks it agrees).
;(function () {
  var choice = 'auto'
  try {
    var stored = localStorage.getItem('ordo.theme')
    if (stored === 'light' || stored === 'dark') choice = stored
  } catch {
    // Storage blocked: follow the OS.
  }
  if (choice === 'auto') {
    var light = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: light)').matches
    choice = light ? 'light' : 'dark'
  }
  document.documentElement.dataset.theme = choice
})()
