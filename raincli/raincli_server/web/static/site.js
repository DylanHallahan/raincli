// The homepage header (website Phase B): a 1 px bottom border once the page has scrolled.
(function () {
  "use strict";
  var header = document.querySelector("[data-site-header]");
  if (!header) return;
  function update() { header.classList.toggle("scrolled", window.scrollY > 0); }
  window.addEventListener("scroll", update, {passive: true});
  update();
  // The phone menu closes after a link is chosen.
  var menu = header.querySelector(".site-menu");
  if (menu) menu.addEventListener("click", function (e) { if (e.target.closest("a")) menu.open = false; });
}());
