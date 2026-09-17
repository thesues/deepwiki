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
  let j;
  try {
    j = await (await fetch("/api/status")).json();
  } catch (_) {
    document.getElementById("home-error").hidden = false;
    return;
  }
  const profiles = j.profiles || [];
  const grid = document.getElementById("cards");
  if (!profiles.length) {
    document.getElementById("home-error").textContent = "还没有配置任何项目";
    document.getElementById("home-error").hidden = false;
    return;
  }
  // Each card navigates. The link, not a click handler, is the whole
  // mechanism: middle-click / cmd-click / copy-link all work for free,
  // which is what makes a project page a real address and not a state.
  profiles.forEach((p) => {
    const card = el("a", "project-card");
    card.href = `/${encodeURIComponent(p.key)}/`;
    card.append(el("div", "pc-name", p.label || p.key), el("div", "pc-key", p.key));
    grid.appendChild(card);
  });
}

boot();
