(function () {
  const progress = document.getElementById('reading-progress');
  let storageKey = '';
  let saveFrame = null;

  function routePath() {
    return window.location.hash.replace(/^#\/?/, '/').split('?')[0];
  }

  function isBook() {
    return routePath().startsWith('/ebooks/') && routePath() !== '/ebooks/';
  }

  function updateProgress() {
    const maximum = document.documentElement.scrollHeight - window.innerHeight;
    const ratio = maximum > 0 ? Math.min(1, window.scrollY / maximum) : 0;
    progress.style.transform = `scaleX(${ratio})`;
    if (storageKey) {
      localStorage.setItem(storageKey, String(window.scrollY));
    }
  }

  window.addEventListener('scroll', function () {
    if (saveFrame || !isBook()) return;
    saveFrame = requestAnimationFrame(function () {
      updateProgress();
      saveFrame = null;
    });
  }, { passive: true });

  window.$docsify.plugins = (window.$docsify.plugins || []).concat(function (hook) {
    hook.doneEach(function () {
      const reading = isBook();
      document.body.classList.toggle('reading-book', reading);
      progress.style.transform = 'scaleX(0)';
      storageKey = reading ? `magicpowers-reading:${routePath()}` : '';
      if (!reading) return;

      const savedPosition = Number(localStorage.getItem(storageKey) || 0);
      if (savedPosition > 0 && !window.location.hash.includes('?id=')) {
        requestAnimationFrame(function () {
          window.scrollTo({ top: savedPosition, behavior: 'auto' });
          updateProgress();
        });
      } else {
        updateProgress();
      }
    });
  });
})();
