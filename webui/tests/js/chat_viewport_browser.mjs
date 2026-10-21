// NODE_PATH=<playwright node_modules> node this-file.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
const {chromium} = createRequire(import.meta.url)('playwright');
const root = fileURLToPath(new URL('../../static/', import.meta.url));
const browser = await chromium.launch({headless:true});
try {
  for (const width of [390, 768, 821, 1440, 1920, 2560]) {
    const page = await browser.newPage({viewport:{width,height:900}});
    await page.route('http://webui.test/**', async route => {
      const url = new URL(route.request().url());
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//,'');
      if (name.endsWith('.js')) return route.fulfill({contentType:'application/javascript',body:''});
      const body = process.env.BASELINE && name==='style.css'
        ? execFileSync('git',['show','HEAD:webui/static/style.css'])
        : await fs.readFile(path.join(root,name));
      return route.fulfill({body,contentType:{'.html':'text/html','.css':'text/css','.woff2':'font/woff2','.svg':'image/svg+xml'}[path.extname(name)]});
    });
    await page.goto('http://webui.test/');
    await page.evaluate(() => {
      const root=document.querySelector('#messages');
      for (let i=0;i<30;i++) {
        const message=document.createElement('div');message.className='msg bot';
        const bubble=document.createElement('div');bubble.className='bubble md';
        const p=document.createElement('p');p.textContent='数学证明与代码说明。'.repeat(50);
        const pre=document.createElement('pre');pre.textContent='wide_code_or_formula_'.repeat(100);
        bubble.append(p,pre);message.append(bubble);root.append(message);
      }
    });
    for (const collapsed of (width<=820 ? [true] : [false,true])) {
      await page.evaluate(collapsed=>document.querySelector('.grid').classList.toggle('sessions-collapsed',collapsed),collapsed);
      const dimensions=await page.evaluate(()=>{
        const rect=s=>document.querySelector(s).getBoundingClientRect();
        const shell=rect('.shell'),chat=rect('.chat'),msg=rect('.msg'),messages=rect('#messages'),composer=rect('#composer');
        return {shell:shell.width,chat:chat.width,message:msg.width,messageHeight:messages.height,
          composer:composer.width,composerBottom:composer.bottom,pageWidth:document.documentElement.scrollWidth,
          viewportWidth:innerWidth,viewportHeight:innerHeight,
          codeScrollable:document.querySelector('.md pre').scrollWidth>document.querySelector('.md pre').clientWidth,
          messagesScrollable:document.querySelector('#messages').scrollHeight>document.querySelector('#messages').clientHeight};
      });
      console.log(JSON.stringify({width,collapsed,...dimensions}));
      if (!process.env.BASELINE) {
        assert.equal(dimensions.shell,width);
        assert.ok(dimensions.pageWidth<=width,'long code must not widen the viewport');
        assert.ok(dimensions.composerBottom<=900,'composer must stay in the viewport');
        assert.ok(dimensions.messageHeight>640,'chrome must leave room for messages');
        assert.ok(dimensions.codeScrollable&&dimensions.messagesScrollable);
        if (width>=1440) assert.ok(dimensions.message>=950,'desktop prose should use the shared wider column');
        if (width===390) assert.ok(dimensions.message>=345,'mobile padding must not eat the column');
      }
    }
    await page.close();
  }
} finally {await browser.close();}
