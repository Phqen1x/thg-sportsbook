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
