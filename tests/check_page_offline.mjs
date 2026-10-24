// Native handlers over pipes, a disposable headless profile, all network blocked.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {readFile,mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname,resolve,join,delimiter} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import assert from 'node:assert/strict';

const require=createRequire(import.meta.url),{chromium}=require(process.env.PLAYWRIGHT_PACKAGE||'playwright');
if(!process.env.ASTRBOT_SOURCE)throw Error('Set ASTRBOT_SOURCE');
const runtime=await mkdtemp(join(tmpdir(),'tool-prompts-offline-'));
const parent=resolve(dirname(fileURLToPath(import.meta.url)),'..','..');
const python=spawn(process.env.ASTRBOT_PYTHON||'python3',['tests/ui_rpc.py'],{env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[process.env.ASTRBOT_SOURCE,parent].join(delimiter),ASTRBOT_ROOT:runtime,ASTRBOT_DISABLE_METRICS:'1'},stdio:['pipe','pipe','pipe']});
let sequence=0;const pending=new Map();let stderr='';python.stderr.on('data',data=>stderr+=data);
const lines=createInterface({input:python.stdout});
lines.on('line',line=>{if(!line.startsWith('TOOLS_RPC:'))return;const message=JSON.parse(line.slice(10)),task=pending.get(message.id);if(task){clearTimeout(task.timer);pending.delete(message.id);task.resolve(message.result);}});
function api(method,path,body){return new Promise((resolve,reject)=>{const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(Error('API pipe timed out: '+stderr.slice(-2000)));},25000);pending.set(id,{resolve,reject,timer});python.stdin.write(JSON.stringify({id,method,path,body})+'\n');});}
let browser;
try{
 const initial=await api('GET','tools');assert.ok(initial.tools.length>100,JSON.stringify(initial));assert.equal(initial.total,initial.tools.length);
 browser=await chromium.launch({headless:true,executablePath:process.env.TEST_CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',args:['--disable-background-networking']});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];page.on('pageerror',error=>errors.push(error.message));await page.route('**/*',route=>route.abort());await page.exposeFunction('toolFixtureAPI',api);
 const bridge=`<script>window.AstrBotPluginPage={apiGet:path=>window.toolFixtureAPI('GET',path),apiPost:(path,body)=>window.toolFixtureAPI('POST',path,body)};</script>`,html=await readFile('pages/tools/index.html','utf8');
 await page.setContent(html.replace('<head>','<head>'+bridge));
 await page.waitForFunction(()=>document.getElementById('status').textContent==='');
 const names=()=>page.locator('#list button strong').allTextContents();
 assert.equal(await page.locator('#list button').count(),initial.total);assert.equal(await page.locator('#toggle').isDisabled(),true);
 const enabled=initial.tools.filter(tool=>tool.active),disabled=initial.tools.filter(tool=>!tool.active);
 assert.deepEqual(await names(),[...enabled,...disabled].map(tool=>tool.name));
 assert.deepEqual(await page.locator('.tool-group').evaluateAll(groups=>groups.map(group=>group.getAttribute('aria-label'))),['已启用工具','已关闭工具']);
 assert.equal(await page.locator('#list button.is-inactive').count(),disabled.length);
 assert.equal(await page.locator('#list button.is-inactive').first().evaluate(button=>{const style=getComputedStyle(button);return style.borderTopStyle==='dashed'&&style.borderTopColor!=='rgba(0, 0, 0, 0)';}),true);
 await page.locator('#state').selectOption('disabled');assert.deepEqual(await names(),disabled.map(tool=>tool.name));
 await page.locator('#state').selectOption('enabled');assert.deepEqual(await names(),enabled.map(tool=>tool.name));
 await page.locator('#state').selectOption('');await page.locator('#source').selectOption(JSON.stringify(['plugin','bulk-fixture']));assert.equal(await page.locator('#list button').count(),125);await page.locator('#search').fill('bulk_124');assert.equal(await page.locator('#list button').count(),1);await page.locator('#search').fill('');await page.locator('#source').selectOption(JSON.stringify(['plugin','old-memes']));assert.equal(await page.locator('#list button').count(),1);assert.match(await page.locator('#list button').innerText(),/已关闭/);
 const lcmTools=initial.tools.filter(tool=>tool.source==='astrbot_plugin_lcm');
 if(lcmTools.length){
  assert.equal(lcmTools.length,3);await page.locator('#source').selectOption(JSON.stringify(['plugin','astrbot_plugin_lcm']));assert.equal(await page.locator('#list button').count(),3);
  await page.locator('#list button').filter({hasText:'lcm_search'}).click();await page.locator('#description').fill('需要查证原话时主动检索 LCM 历史。');await page.locator('textarea[data-path="/properties/query"]').fill('只影响查询关键词的说明');await page.locator('#save').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('已保存'));
  const payload=await api('GET','schema'),tool=payload.schema.find(item=>item.function.name==='lcm_search').function;assert.match(tool.description,/主动检索 LCM/);assert.equal(tool.parameters.properties.query.description,'只影响查询关键词的说明');assert.equal(tool.parameters.properties.sender.description,undefined);
  await page.locator('#reset').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('已恢复'));
 }
 await page.locator('#source').selectOption(JSON.stringify(['plugin','fixture-memes']));
 assert.equal(await page.locator('#list button').count(),5);
 assert.deepEqual(await names(),['meme_disable','meme_edit','meme_search','meme_view','meme_send']);
 await page.locator('#state').selectOption('enabled');assert.equal(await page.locator('#list button').count(),4);
 await page.locator('#state').selectOption('disabled');assert.deepEqual(await names(),['meme_send']);
 await page.locator('#search').fill('meme_search');assert.equal(await page.locator('#list button').count(),0);assert.match(await page.locator('#list').innerText(),/没有匹配/);
 await page.locator('#search').fill('');await page.locator('#state').selectOption('');
 await page.locator('#list button').filter({hasText:'meme_search'}).click();
 assert.equal(await page.locator('#description').inputValue(),'按语义查找表情包。');
 assert.equal(await page.locator('#save').isDisabled(),true);
 await page.locator('#description').fill('适合表达情绪时，主动搜索表情包，可选使用。');
 await page.locator('textarea[data-path="/properties/query"]').fill('结合最近聊天，用简短词描述情绪和场景。');
 assert.match(await page.locator('#dirty').innerText(),/未保存/);
 await page.locator('#state').selectOption('enabled');
 await page.locator('#toggle').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('工具已关闭'));assert.match(await page.locator('#description').inputValue(),/主动搜索/);assert.match(await page.locator('#dirty').innerText(),/未保存/);assert.equal(await page.locator('#toggle').getAttribute('aria-checked'),'false');
 assert.deepEqual(await names(),['meme_disable','meme_edit','meme_view']);assert.equal(await page.locator('#tool-name').innerText(),'meme_search');
 await page.locator('#state').selectOption('disabled');assert.deepEqual(await names(),['meme_search','meme_send']);assert.equal(await page.locator('#list button[aria-current="true"].is-inactive').count(),1);
 await page.locator('#save').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('已保存'));
 let captured=await api('GET','schema'),definition=captured.schema.find(item=>item.function.name==='meme_search').function;
 assert.match(definition.description,/主动搜索/);assert.match(definition.parameters.properties.query.description,/最近聊天/);assert.deepEqual(definition.parameters.required,['query']);assert.equal(captured.original,'按语义查找表情包。');
 await page.locator('#custom-only').check();assert.equal(await page.locator('#list button').count(),1);
 await api('POST','restart');await page.locator('#refresh').click();await page.waitForFunction(()=>document.getElementById('status').textContent==='');assert.match(await page.locator('#description').inputValue(),/主动搜索/);assert.equal(await page.locator('#toggle').getAttribute('aria-checked'),'false');await page.locator('#toggle').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('工具已开启'));assert.equal(await page.locator('#toggle').getAttribute('aria-checked'),'true');
 assert.equal(await page.locator('#state').inputValue(),'disabled');assert.equal(await page.locator('#list button').count(),0);assert.equal(await page.locator('#tool-name').innerText(),'meme_search');
 await page.locator('#state').selectOption('');assert.equal(await page.locator('#list button').count(),1);
 await page.locator('#description').fill('此修改基于旧版默认说明');await api('POST','definition_change');await page.locator('#save').click();await page.waitForFunction(()=>document.getElementById('status').textContent.includes('工具定义'));assert.equal(await page.locator('#description').inputValue(),'此修改基于旧版默认说明');
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh').click();await page.waitForFunction(()=>document.getElementById('status').textContent==='');
 await page.locator('#reset').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('已恢复'));
 assert.equal(await page.locator('#description').inputValue(),'插件升级后的默认说明。');assert.equal(await page.locator('#list button').count(),0);
 captured=await api('GET','schema');assert.equal(captured.schema.find(item=>item.function.name==='meme_search').function.description,'插件升级后的默认说明。');
 await page.locator('#custom-only').uncheck();await page.locator('#search').fill('meme_send');assert.equal(await page.locator('#list button').count(),1);await page.locator('#list button').click();assert.match(await page.locator('#tool-state').innerText(),/已关闭/);assert.equal(await page.evaluate(()=>window.injected),undefined);
 await page.locator('#search').fill('');await page.locator('#list button').filter({hasText:'meme_search'}).click();await page.screenshot({path:'/private/tmp/tool-prompts-desktop.png',fullPage:true});
 await page.emulateMedia({colorScheme:'dark'});await page.screenshot({path:'/private/tmp/tool-prompts-dark.png',fullPage:true});await page.emulateMedia({colorScheme:'light'});
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);await page.screenshot({path:'/private/tmp/tool-prompts-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({nativeTools:initial.tools.length,moreThan100Tools:true,fiveMemeToolsAndSeparateOldPlugin:true,nativeActivationPersistence:true,toggleKeepsUnsavedEdits:true,builtinsStayReadonly:true,registeredLCMTools:lcmTools.length,lcmDescriptionsEdited:lcmTools.length===3,aliasedParameterIsolation:true,sourceFilter:true,stateFilter:true,enabledFirstAndClosedGroupLast:true,stateFilterComposesWithSearchAndCustomFilter:true,toggleUpdatesFilteredList:true,search:true,saveReachedNativeSchema:true,originalsUnchanged:true,restartPersistence:true,staleEditorRejected:true,resetUsesLatestDefault:true,customFilter:true,disabledToolVisible:true,xssSafe:true,mobileNoOverflow:true,scriptErrors:errors,apiTransport:'native dashboard extension dispatcher via ASGI pipes'}));
}finally{
 if(browser)await browser.close();lines.close();python.stdin.end();if(python.exitCode===null&&python.signalCode===null){const exited=new Promise(resolve=>python.once('exit',resolve));python.kill('SIGTERM');await exited;}await rm(runtime,{recursive:true,force:true});for(const task of pending.values()){clearTimeout(task.timer);task.reject(Error('fixture closed'));}
}
