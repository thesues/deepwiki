import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
for (const name of ['app.js', 'home.js']) {
  const source = fs.readFileSync(new URL('../../static/' + name, import.meta.url), 'utf8');
  const start = source.indexOf('async function authFetch(');
  const helper = source.slice(start, source.indexOf('\n}', start) + 2);
  let response = {status: 200};
  const redirects = [];
  const context = vm.createContext({
    fetch: async () => response,
    window: {location: {pathname: '/buda/', search: '?x=1&y=2', assign: (url) => redirects.push(url)}},
  });
  vm.runInContext(helper, context);
  assert.equal(await context.authFetch('/api/status'), response);
  response = {status: 503};
  assert.equal(await context.authFetch('/api/status'), response);
  assert.equal(redirects.length, 0);
  response = {status: 401};
  await assert.rejects(context.authFetch('/api/status'));
  assert.equal(redirects[0], '/auth/login?return=%2Fbuda%2F%3Fx%3D1%26y%3D2');
}
console.log('auth login handling passed for both pages');
