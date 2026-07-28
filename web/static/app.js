// Forms that add legs/parlays to the caller's own parlay slip: intercept the
// submit so the page doesn't navigate to /parlay, and surface the result as a
// small popup next to the button that was clicked instead.
const SLIP_FORM_PATTERNS = [
  /^\/parlay\/add\/\d+$/,
  /^\/tail\/\d+\/add-to-slip$/,
  /^\/tail\/parlay\/\d+\/add-to-slip$/,
];

// Popup anchored to a specific element (the button just clicked) — appended to
// <body> and positioned via JS so it's never clipped by a scrolling or
// overflow-hidden ancestor (e.g. the nav bar).
function showInlinePopup(anchorEl, message, isError) {
  if (!anchorEl || !anchorEl.getBoundingClientRect) return;
  const bubble = document.createElement('div');
  bubble.className = 'inline-popup' + (isError ? ' error' : '');
  bubble.textContent = message;
  document.body.appendChild(bubble);

  const anchorRect = anchorEl.getBoundingClientRect();
  const bubbleRect = bubble.getBoundingClientRect();
  let left = anchorRect.left + anchorRect.width / 2 - bubbleRect.width / 2;
  left = Math.max(6, Math.min(left, window.innerWidth - bubbleRect.width - 6));
  let top = anchorRect.bottom + 6;
  if (top + bubbleRect.height > window.innerHeight - 6) {
    top = anchorRect.top - bubbleRect.height - 6;
  }
  bubble.style.left = `${left}px`;
  bubble.style.top = `${top}px`;

  requestAnimationFrame(() => bubble.classList.add('show'));
  setTimeout(() => {
    bubble.classList.remove('show');
    setTimeout(() => bubble.remove(), 200);
  }, 2600);
}

async function submitSlipForm(form, anchorEl) {
  let data = {};
  try {
    const res = await fetch(form.getAttribute('action'), {
      method: 'POST',
      headers: { Accept: 'application/json' },
      body: new URLSearchParams(new FormData(form)),
    });
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      showInlinePopup(anchorEl, data.detail || 'Something went wrong.', true);
      return;
    }
  } catch (_) {
    showInlinePopup(anchorEl, 'Network error — try again.', true);
    return;
  }
  showInlinePopup(anchorEl, data.message || 'Added to your parlay slip.', false);
}

document.addEventListener('submit', (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  const action = form.getAttribute('action') || '';
  if (SLIP_FORM_PATTERNS.some((re) => re.test(action))) {
    e.preventDefault();
    const anchorEl = e.submitter || form.querySelector('button[type="submit"], button:not([type])') || form;
    submitSlipForm(form, anchorEl);
  }
});

document.addEventListener('DOMContentLoaded', () => {
  // Auto-dismiss flash messages after 5 seconds
  document.querySelectorAll('.flash').forEach(el => {
    setTimeout(() => {
      el.style.transition = 'opacity 0.5s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 500);
    }, 5000);
  });

  // Hamburger menu toggle
  const navToggle = document.getElementById('nav-toggle');
  const navbar = document.querySelector('.navbar');
  if (navToggle && navbar) {
    navToggle.addEventListener('click', () => {
      const isOpen = navbar.classList.toggle('nav-open');
      navToggle.classList.toggle('open', isOpen);
      navToggle.setAttribute('aria-expanded', String(isOpen));
    });
    // Close menu when a nav link is clicked
    navbar.querySelectorAll('.nav-links a').forEach(a => {
      a.addEventListener('click', () => {
        navbar.classList.remove('nav-open');
        navToggle.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      });
    });
  }
});
