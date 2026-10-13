/* Recognize TeX before Markdown consumes its underscores and backslashes.
   Marked's code tokens keep backticks/fences literal. Unclosed delimiters
   remain text until a subsequent streaming render receives the closing one. */
(() => {
  const render = (token) => katex.renderToString(token.text, {
    displayMode: token.display,
    throwOnError: false,
    trust: false,
    strict: "ignore",
    maxExpand: 1000,
    maxSize: 20,
  }) + (token.type === "mathBlock" ? "\n" : "");

  marked.use({ extensions: [
    {
      name: "mathBlock",
      level: "block",
      start(src) { return src.match(/^(?: {0,3})(?:\$\$|\\\[)/m)?.index; },
      tokenizer(src) {
        const match = /^(?: {0,3})(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])[ \t]*(?:\n|$)/.exec(src);
        if (match) return { type: "mathBlock", raw: match[0], text: (match[1] ?? match[2]).trim(), display: true };
      },
      renderer: render,
    },
    {
      name: "mathInline",
      level: "inline",
      start(src) { return src.search(/\$|\\\(/); },
      tokenizer(src) {
        // Dollar math cannot open/close against whitespace, or close before
        // a digit: ordinary prices such as "$5 and $10" stay ordinary text.
        const match = /^(?:\$\$((?:\\[\s\S]|[^\\])+?)\$\$|\$(?![\s$])((?:\\[^\n]|[^\\$\n])+?)(?<!\s)\$(?!\d)|\\\(([^\n]+?)\\\))/.exec(src);
        if (match) return { type: "mathInline", raw: match[0], text: match[1] ?? match[2] ?? match[3], display: match[1] !== undefined };
        // Do not reinterpret the second dollar of an unfinished $$ block
        // as the start of a single-dollar formula during streaming.
        if (src.startsWith("$$")) return { type: "text", raw: "$$", text: "$$" };
      },
      renderer: render,
    },
  ] });
})();
