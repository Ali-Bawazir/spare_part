/* MMS shared UI helpers. All user-facing strings are passed via data-* attributes
 * from server-rendered HTML (which respects the active locale). Do NOT hardcode
 * English or any other language here.
 */
(function() {
  'use strict';

  // Intercept any click on an element that has data-confirm-msg and prompt the user.
  document.addEventListener('click', function(e) {
    var el = e.target.closest && e.target.closest('[data-confirm-msg]');
    if (!el) return;
    var msg = el.getAttribute('data-confirm-msg');
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
})();
