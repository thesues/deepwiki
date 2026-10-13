import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ console });
for (const file of ['vendor/marked.min.js', 'vendor/katex-0.19.0/katex.min.js', 'math.js']) {
  vm.runInContext(fs.readFileSync(new URL(`../../static/${file}`, import.meta.url), 'utf8'), context);
}
const parse = (text) => context.marked.parse(text, { gfm: true, breaks: true });
const inline = String.raw`用恒等式把 $\pi_\theta$ 还回去，$A_t>0$ 时增大概率。`;
const block = String.raw`$$
\nabla_\theta J=\int \pi_\theta(a\mid s)\big[\nabla_\theta\log\pi_\theta(a\mid s)\big]A(a)\,da
=\mathbb{E}_{a\sim\pi_\theta}\big[A_t\nabla_\theta\log\pi_\theta(a_t\mid s_t)\big]
$$`;
assert.equal((parse(inline).match(/class="katex"/g) || []).length, 2);
assert.match(parse(block), /class="katex-display"/);
assert.ok(!parse(block).includes('katex-error'));
assert.ok(!parse(block).includes('<em>'));
for (const text of [String.raw`\(x_i\)`, String.raw`\[x_i\]`, '$x_i$', '$$x_i$$', '- $x_i$', '> $$x_i$$']) {
  assert.match(parse(text), /class="katex"/);
}
for (const text of ['`$x_i$`', '```tex\n$$x_i$$\n```', String.raw`\$5 and \$10`, '$5 and $10', '$unclosed', '$$x$']) {
  assert.ok(!parse(text).includes('class="katex"'), text);
}
// A stream may stop anywhere in a command or delimiter, then be replayed.
for (let i = 0; i <= block.length; i++) assert.doesNotThrow(() => parse(block.slice(0, i)));
assert.equal(parse(block), parse(block));
assert.match(parse(String.raw`$\frac{1}$`), /katex-error/);
assert.ok(!parse(String.raw`$\href{javascript:alert(1)}{click}$`).includes('href="javascript:'));
assert.ok(!parse(String.raw`$\includegraphics{https://example.com/x.png}$`).includes('<img'));
console.log('ok - math delimiters, screenshot equation, code/prices, streaming, invalid and untrusted TeX');
