/* Invincible's corner: a silly little toy on the home page. Poke the hero, he says things.
   Pure client-side; pokes are kept in localStorage. No network calls. */
(function () {
  'use strict';
  var root = document.querySelector('[data-inv]');
  if (!root) return;
  var hero = root.querySelector('[data-inv-hero]');
  var quip = root.querySelector('[data-inv-quip]');
  var pow = root.querySelector('[data-inv-pow]');
  var pokesEl = root.querySelector('[data-inv-pokes]');
  var hypeEl = root.querySelector('[data-inv-hype]');
  var moodEl = root.querySelector('[data-inv-mood]');
  var bar = root.querySelector('[data-inv-bar]');
  var KEY = 'inv.pokes';
  var pokes = 0;
  try { pokes = parseInt(localStorage.getItem(KEY) || '0', 10) || 0; } catch (e) { pokes = 0; }
  var hype = 0, idle = null, last = -1;

  var QUIPS = [
    'I cut three Shorts before breakfast. Breakfast was also a Short.',
    'The algorithm and I have an understanding. It does not understand me.',
    'Every clip is a hook if you yell first.',
    'Retention is just friendship with a stopwatch.',
    'I fly. I edit. I occasionally render.',
    'Thumbnail? I barely know her.',
    'Cecil says strategy changes need approval. Cecil is no fun.',
    'If a Short uploads and nobody watches, it still counts. Right?',
    'Sixty seconds is plenty of time to save the world. Or a view.',
    'My cape is made of 100% recycled watch time.',
    'Ow. Okay, that one actually hurt my feelings.',
    'Uploading vibes. Please hold.'
  ];
  var POWS = ['POW', 'BAM', 'ZOOM', 'VIRAL', 'CLIP', 'WHAM', '+1 VIEW'];
  var MOODS = [[0, 'chill'], [25, 'warming up'], [50, 'pumped'], [75, 'unstoppable'], [95, 'ascended']];

  function pick(list) {
    var i;
    do { i = Math.floor(Math.random() * list.length); } while (list.length > 1 && i === last);
    last = i;
    return list[i];
  }
  function render() {
    pokesEl.textContent = String(pokes);
    hypeEl.textContent = Math.round(hype) + '%';
    bar.style.width = hype + '%';
    var m = 'chill';
    MOODS.forEach(function (x) { if (hype >= x[0]) m = x[1]; });
    moodEl.textContent = m;
  }
  function flash(cls, ms) {
    hero.classList.add(cls);
    setTimeout(function () { hero.classList.remove(cls); }, ms);
  }
  function napLater() {
    clearTimeout(idle);
    hero.classList.remove('is-sleepy');
    idle = setTimeout(function () {
      hero.classList.add('is-sleepy');
      quip.textContent = 'zzz... dreaming of a million views...';
    }, 45000);
  }

  hero.addEventListener('click', function () {
    pokes += 1;
    try { localStorage.setItem(KEY, String(pokes)); } catch (e) { /* private mode: fine */ }
    hype = Math.min(100, hype + 12 + Math.random() * 10);
    pow.textContent = POWS[Math.floor(Math.random() * POWS.length)];
    pow.classList.add('is-on');
    setTimeout(function () { pow.classList.remove('is-on'); }, 650);
    if (hype >= 100) {
      quip.textContent = 'MAXIMUM HYPE. Shipping a Short to the moon.';
      flash('is-zoom', 900);
      hype = 0;
    } else if (pokes % 10 === 0) {
      quip.textContent = pokes + ' pokes. I am filing a complaint with Cecil.';
      flash('is-spin', 600);
    } else {
      quip.textContent = pick(QUIPS);
      flash('is-spin', 600);
    }
    render();
    napLater();
  });

  setInterval(function () { if (hype > 0) { hype = Math.max(0, hype - 1); render(); } }, 1500);
  render();
  napLater();
})();
