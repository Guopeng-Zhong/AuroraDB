'use strict';
// Loaded before the stylesheet so a saved light/dark choice applies before first paint.
// "system" leaves data-theme unset and the CSS follows prefers-color-scheme.
(() => {
  let choice = 'system';
  try { choice = localStorage.getItem('aurora-theme') || 'system'; } catch (error) { /* storage unavailable */ }
  if (choice === 'light' || choice === 'dark') document.documentElement.dataset.theme = choice;
})();
