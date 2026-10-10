const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.MINI_PLAYWRIGHT_MODULE||'C:/Users/Tim/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const loc=JSON.parse(fs.readFileSync(process.env.MINI_FRONTEND_TEST_INSTANCE,'utf8'));
assert(path.resolve(loc.state).includes(path.join('.local','webui','frontend-state-tests')));
const service=JSON.parse(fs.readFileSync(path.join(loc.state,'service.json'),'utf8'));

(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const checks=[],errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${service.port}/#${service.nonce}`);
  await page.waitForFunction(()=>connected&&!initialization);
  const api=(url,body)=>page.evaluate(({url,body})=>api(url,body?'POST':'GET',body),{url,body});
  async function check(name,fn){await fn();checks.push(name);console.log('PASS '+name);}
  async function holdResponse(routePath){
   let release,seen,finished,used=false;
   const gate=new Promise(resolve=>{release=resolve;}),ready=new Promise(resolve=>{seen=resolve;}),done=new Promise(resolve=>{finished=resolve;});
   const handler=async route=>{if(used){await route.continue();return;}used=true;const response=await route.fetch();seen();await gate;await route.fulfill({response});finished();};
   await page.route(routePath,handler);
   return {ready,release:async()=>{release();await done;await page.unroute(routePath,handler);}};
  }
  await check('history last page and first page retain business-disabled controls',async()=>{
   await page.locator('.nav-item[data-page="history"]').click();
   await page.waitForFunction(()=>historyTotal===25);
   await page.locator('#history-next').click();
   await page.waitForFunction(()=>!$('history-next').dataset.busy);
   assert(await page.locator('#history-next').isDisabled());assert.equal(await page.locator('#history-list .history-row').count(),5);
   await page.locator('#history-prev').click();await page.waitForFunction(()=>!$('history-prev').dataset.busy);
   assert(await page.locator('#history-prev').isDisabled());assert.equal(await page.locator('#history-list .history-row').count(),20);
   const gate=await holdResponse('**/api/history?*');await page.locator('#history-refresh').click();await gate.ready;
   assert(await page.locator('#history-refresh').evaluate(button=>button.inert));await gate.release();
   await page.waitForFunction(()=>!$('history-refresh').dataset.busy);assert(!(await page.locator('#history-refresh').evaluate(button=>button.inert)));
  });
  await check('batch edits discard delayed previews and changed snapshots cannot submit',async()=>{
   await page.locator('.nav-item[data-page="library"]').click();await page.locator('#library-list input').first().check();
   await page.locator('#batch-operation').selectOption('solve');await page.locator('#batch-profile').selectOption(loc.profile_id);
   for(const id of ['batch-data','batch-image','batch-auto-approve'])await page.locator('#'+id).check();
   const gate=await holdResponse('**/api/queues/preview');await page.locator('#batch-preview').click();await gate.ready;
   for(const id of ['batch-data','batch-image','batch-auto-approve'])await page.locator('#'+id).uncheck();
   await gate.release();await page.waitForFunction(()=>!$('batch-preview').dataset.busy);
   assert(await page.evaluate(()=>batchPlan===null&&$('batch-preview-panel').hidden&&$('batch-start').disabled));
   await page.locator('#batch-operation').selectOption('cache');await page.locator('#batch-preview').click();
   await page.waitForFunction(()=>batchPlan!==null&&!$('batch-preview').dataset.busy);
   assert((await page.locator('#batch-summary').textContent()).includes('题目资料下载：不允许'));
   assert((await page.locator('#batch-summary').textContent()).includes('运行镜像下载：不允许'));
   const message=await page.evaluate(async()=>{$('batch-data').checked=true;$('batch-ack').checked=true;try{await startBatch();return '';}catch(error){return error.message;}});
   assert(message.includes('批次条件已变化'));assert(await page.locator('#batch-start').isDisabled());assert.deepEqual(await api('/api/queues'),[]);
  });
  await check('experiment edits discard delayed authorization and submit rechecks its snapshot',async()=>{
   await page.locator('.nav-item[data-page="results"]').click();await page.locator('#experiment-new').click();
   await page.locator('#experiment-instances').fill('audit__fixture-1000');
   await page.locator(`#experiment-models input[value="${loc.profile_id}"]`).check();
   for(const id of ['experiment-data','experiment-images','experiment-auto'])await page.locator('#'+id).check();
   const gate=await holdResponse('**/api/experiments/preview');await page.locator('#experiment-preview').click();await gate.ready;
   for(const id of ['experiment-data','experiment-images','experiment-auto'])await page.locator('#'+id).uncheck();
   await gate.release();await page.waitForFunction(()=>!$('experiment-preview').dataset.busy);
   assert(await page.evaluate(()=>experimentPlan===null&&$('experiment-plan').hidden&&$('experiment-start').disabled));
   await page.locator('#experiment-preview').click();await page.waitForFunction(()=>experimentPlan!==null&&!$('experiment-preview').dataset.busy);
   const summary=await page.locator('#experiment-plan-summary').textContent();
   for(const phrase of ['题目资料下载：不允许','运行镜像下载：不允许','求解命令：逐批确认','自动评测：开启'])assert(summary.includes(phrase));
   const message=await page.evaluate(async()=>{$('experiment-budget').value='2';$('experiment-ack').checked=true;try{await startExperiment();return '';}catch(error){return error.message;}});
   assert(message.includes('实验条件已变化'));assert(await page.locator('#experiment-start').isDisabled());const experiments=await api('/api/experiments');assert.equal((experiments.items||experiments).length,0);
   await page.locator('#experiment-close').click();
  });
  const config=await api('/api/config'),ids=[];
  for(const name of ['State race A','State race B']){
   const plan=await api('/api/experiments/preview',{name,dataset:'verified',instance_ids:['audit__fixture-1000'],profile_ids:[loc.profile_id],
    settings_version:config.version,step_limit:8,cost_limit:1,timeout:30,total_budget:2,auto_evaluate:false,auto_approve:true,diagnostics:{'audit__fixture-1000':'success'}});
   assert(plan.ready);const row=await api('/api/experiments',{token:plan.token,request_id:crypto.randomUUID()});ids.push(row.id);
   const deadline=Date.now()+45000;
   while((await api('/api/experiments/'+row.id)).status!=='completed'){
    assert(Date.now()<deadline,'Diagnostic experiment did not finish');await new Promise(resolve=>setTimeout(resolve,100));
   }
  }
  await check('old experiment reads and controls cannot replace the latest selected experiment',async()=>{
   await page.locator('[data-results-tab="experiments"]').click();await page.locator('#results-refresh').click();
   await page.waitForFunction(()=>!$('results-refresh').dataset.busy);
   await page.evaluate(id=>loadExperiment(id),ids[0]);
   const readGate=await holdResponse('**/api/experiments/'+ids[0]);
   await page.evaluate(()=>{window.oldExperimentRead=loadExperiment(experimentWanted,{background:true});});await readGate.ready;
   await page.evaluate(id=>loadExperiment(id),ids[1]);await readGate.release();await page.evaluate(()=>window.oldExperimentRead);
   assert.equal(await page.evaluate(()=>selectedExperiment.id),ids[1]);assert((await page.locator('#experiment-title').textContent()).includes('State race B'));
   await page.evaluate(id=>loadExperiment(id),ids[0]);
   await page.evaluate(()=>{window.oldExperimentButton=[...$('experiment-items').querySelectorAll('button')].find(button=>button.textContent.includes('重新求解'));});
   const controlGate=await holdResponse('**/api/experiments/'+ids[0]+'/control');
   page.once('dialog',dialog=>dialog.accept());await page.locator('#experiment-items').getByRole('button',{name:'重新求解（保留首轮）',exact:true}).click();await controlGate.ready;
   assert(await page.evaluate(()=>[...$('experiment-controls').querySelectorAll('button')].every(button=>button.disabled)));
   await page.evaluate(id=>loadExperiment(id),ids[1]);await controlGate.release();
   await page.waitForFunction(id=>!experimentControlsPending.has(id),ids[0]);assert.equal(await page.evaluate(()=>selectedExperiment.id),ids[1]);
   const before=await api('/api/experiments/'+ids[1]);await page.evaluate(()=>window.oldExperimentButton.onclick());
   assert.deepEqual((await api('/api/experiments/'+ids[1])).items,before.items);
   await api('/api/experiments/'+ids[0]+'/control',{action:'finish'});
  });
  await check('returning from experiment comparison restores an empty history picker',async()=>{
   await page.locator('[data-results-tab="compare"]').click();await page.locator('#compare-experiment').selectOption(ids[1]);
   await page.waitForFunction(()=>comparisonData?.mode!=='history_common_cases'&&comparisonData!==null);
   assert(await page.locator('#compare-models').evaluate(element=>element.hidden));
   await page.evaluate(()=>{$('compare-model-options').querySelectorAll('input').forEach(input=>input.checked=false);});
   await page.locator('#compare-experiment').selectOption('');await page.waitForFunction(()=>$('compare-feedback').textContent!=='正在对齐题目和模型结果…');
   assert(await page.locator('#compare-models summary').isVisible());await page.locator('#compare-models summary').focus();await page.keyboard.press('Enter');
   assert(await page.locator('#compare-models').evaluate(element=>element.open));
  });
  await check('desktop and 390px layouts remain contained',async()=>{
   await page.locator('#experiment-new').click();await page.locator('#experiment-preview').click();await page.waitForFunction(()=>experimentPlan!==null);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(loc.state,'desktop.png'),fullPage:true});
   await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(loc.state,'mobile.png'),fullPage:true});
  });
  await check('unknown probe recovery requires confirmation and closes the old request without a model call',async()=>{
   const requestId=crypto.randomUUID();
   await page.evaluate(id=>sessionStorage.setItem('mini-profile-probe',JSON.stringify({profile_id:'legacy',request_id:id,started:Date.now()})),requestId);
   await page.reload();await page.waitForFunction(()=>connected&&!initialization);
   await page.locator('.nav-item[data-page="settings"]').click();await page.locator('#profile-test-recovery').waitFor({state:'visible'});
   await page.locator('#profile-test-end').click();assert((await page.locator('#profile-test-result').textContent()).includes('请先确认'));
   assert(await page.evaluate(()=>probePending!==null));
   await page.setViewportSize({width:1440,height:1000});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('#profile-test-recovery').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(loc.state,'probe-recovery-desktop.png')});
   await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('#profile-test-recovery').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(loc.state,'probe-recovery-mobile.png')});
   await page.locator('#profile-test-end-ack').check();await page.locator('#profile-test-end').click();
   await page.waitForFunction(()=>probePending===null);assert(await page.locator('#profile-test-retry').isVisible());
   const response=await api('/api/profiles/legacy/test',{token:'closed-request-no-provider-call',request_id:requestId});
   assert.equal(response.status,'incomplete');
  });
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(loc.state,'checks.json'),JSON.stringify({passed:true,checks,errors,method:'Isolated service, diagnostic-only experiments, delayed original HTTP responses; no model or external download requests.'},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
