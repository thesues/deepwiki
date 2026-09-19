"use strict";
/* The HOME page: the project cards, deepwiki.com style, and nothing else.
 *
 * A card click is a NAVIGATION to /<key>/ — the chat page for that project.
 * No view switching, no state carried over: the two pages share a stylesheet
 * and a server, and that is all. The chat page reads its project off the URL
 * the same way any page reads its own address.
 */

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

async function boot() {
  // Version the assets like the chat page does: the server rewrites the URL
  // with ?v=, this page only needs to render what the server sent.
  // The two lists in PARALLEL, not one after the other. They are independent —
  // /api/status names the projects, /api/sessions counts the conversations —
  // and awaiting them in sequence made the page wait for the sum of two
  // round trips (the second one queries hermes' session store) to draw cards
  // that only need the first. Only /api/status is fatal: with no projects
  // there is nothing to render, whereas a missing count is a blank line on a
  // card that is otherwise correct.
  let j, sess = { sessions: [] };
  const [statusRes, sessRes] = await Promise.allSettled([
    fetch("/api/status").then((r) => r.json()),
    fetch("/api/sessions").then((r) => r.json()),
  ]);
  if (statusRes.status !== "fulfilled") {
    document.getElementById("home-error").hidden = false;
    return;
  }
  j = statusRes.value;
  if (sessRes.status === "fulfilled") sess = sessRes.value;   // counts stay blank otherwise
  const profiles = j.profiles || [];
  const grid = document.getElementById("cards");
  if (!profiles.length) {
    document.getElementById("home-error").textContent = "还没有配置任何项目";
    document.getElementById("home-error").hidden = false;
    return;
  }
  // Sessions per project: a conversation belongs to the project it was opened
  // under (recorded server-side at chat/start); sessions from before profiles
  // have no pin and file under the default. The count on a card is what says
  // "this project has history" before you click in.
  const def = j.defaultProfile || (profiles[0] || {}).key;
  const counts = {};
  (sess.sessions || []).forEach((s) => {
    const k = s.profile || def;
    counts[k] = (counts[k] || 0) + 1;
  });
  // Each card navigates. The link, not a click handler, is the whole
  // mechanism: middle-click / cmd-click / copy-link all work for free,
  // which is what makes a project page a real address and not a state.
  profiles.forEach((p) => {
    const card = el("a", "project-card");
    card.href = `/${encodeURIComponent(p.key)}/`;
    const n = counts[p.key];
    card.append(
      el("div", "pc-name", p.label || p.key),
      el("div", "pc-key", n ? `${n} 个会话` : p.key),
    );
    grid.appendChild(card);
  });

  // Theme toggle — the chat page's, verbatim. Dark is the original look and the
  // default; light is the same layout with a daylight palette (style.css
  // [data-theme="light"]). Persisted per browser; applied before first paint
  // via the inline script in <head> so a light reader never sees a dark flash.
  //
  // WHICH icon is up is CSS's business (both sun and moon are in the markup,
  // [data-theme] picks one). This is the whole of the behaviour: flip the
  // attribute, arm the cross-fade for the length of the switch, persist.
  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    const paint = () => {
      const light = document.documentElement.dataset.theme === "light";
      themeBtn.title = light ? "切换到暗色主题" : "切换到亮色主题";
    };
    let settle = 0;
    themeBtn.onclick = () => {
      const root = document.documentElement;
      const next = root.dataset.theme === "light" ? "dark" : "light";
      const flip = () => {
        if (next === "dark") delete root.dataset.theme;
        else root.dataset.theme = "light";
        try { localStorage.setItem("hermes.theme", next); } catch (_) {}
        paint();
      };
      // A View Transition turns the switch into a diagonal wipe: the browser
      // freezes the page, we flip the attribute, and the new palette is
      // revealed over the old snapshot (style.css, ::view-transition-new).
      // The whole animation lives in CSS — this only says WHEN the DOM
      // changes, which is the one thing CSS cannot know.
      //
      // Two reasons to fall back. An older browser has no startViewTransition
      // (Firefox shipped it well after Chrome), and a reader who asked for
      // less motion gets the cross-fade instead — checked HERE and not only
      // in the media query, because a transition that has already started
      // cannot be called off, it can only run unanimated, which lands as a
      // snap a quarter-second late.
      if (typeof document.startViewTransition === "function" &&
          !matchMedia("(prefers-reduced-motion: reduce)").matches) {
        document.startViewTransition(flip);
        return;
      }
      // The cross-fade is armed only around the switch — left standing, it
      // makes every hover and the streaming caret lag (style.css .theming).
      root.classList.add("theming");
      clearTimeout(settle);
      settle = setTimeout(() => root.classList.remove("theming"), 380);
      flip();
    };
    paint();
  }
}

boot();
