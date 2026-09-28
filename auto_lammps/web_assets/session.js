/* Desktop-entry page activity. Loaded by the local app page only (index.html).
 *
 * When the entry opens the page with ?session=<token>, this file tells the supervising
 * process that the page is still open (every 5 s, plus on visibility changes) and that it is
 * closing (pagehide/beforeunload). The local service records only "recent activity" in one
 * small file; the supervisor stops the service about 30 s after the page is gone. Nothing is
 * stored in the browser, nothing leaves this computer, and no task state is touched.
 *
 * Without ?session= (an ordinary launch, a bookmark, a second tab opened by hand) this file
 * does nothing at all.
 */
(() => {
  const params = new URLSearchParams(location.search);
  const session = (params.get('session') || '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64);
  if (!session) return;
  const base = '/api/session/';
  const every = 5000;
  let finished = false;

  const showHint = () => {
    const note = document.createElement('div');
    note.id = 'session-lifecycle-hint';
    note.setAttribute('role', 'note');
    note.textContent = '关闭本页约 30 秒后，本地服务会自动停止；任务记录、账本与运行记录保留。';
    const style = note.style;
    style.position = 'fixed';
    style.right = '10px';
    style.bottom = '8px';
    style.padding = '2px 10px';
    style.borderRadius = '10px';
    style.background = 'rgba(255,255,255,0.92)';
    style.color = '#41637f';
    style.font = '12px/1.6 system-ui,-apple-system,"PingFang SC",sans-serif';
    style.pointerEvents = 'none';
    style.zIndex = '40';
    style.boxShadow = '0 1px 4px rgba(31,63,92,0.18)';
    document.body.appendChild(note);
    setTimeout(() => note.remove(), 25000);
  };

  const url = (path, extra) => `${base}${path}?session=${encodeURIComponent(session)}${extra || ''}`;

  const beat = () => {
    if (finished) return;
    try {
      fetch(url('heartbeat', `&hidden=${document.hidden ? 1 : 0}`), {cache: 'no-store'}).catch(() => {});
    } catch (error) { /* an unreachable service must never disturb the page */ }
  };

  const closed = () => {
    if (finished) return;
    finished = true;
    try {
      fetch(url('close'), {method: 'POST', keepalive: true, cache: 'no-store',
                           headers: {'Content-Type': 'application/json', 'X-Task-Review': '1'},
                           body: '{}'}).catch(beaconImage);
    } catch (error) { beaconImage(); }
    beaconImage();
  };

  // Fallback transport: a plain GET image request needs no custom headers and survives unload.
  const beaconImage = () => {
    const image = new Image();
    image.src = url('close', `&via=beacon&at=${Date.now()}`);
  };

  const wait = () => {
    if (finished) return;
    beat();
    setTimeout(wait, every);
  };

  document.addEventListener('visibilitychange', beat);
  addEventListener('pageshow', beat);
  addEventListener('pagehide', event => { if (event.persisted) beat(); else closed(); });
  addEventListener('beforeunload', closed);
  wait();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', showHint);
  } else {
    showHint();
  }
})();
