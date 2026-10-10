'use strict';
const $=id=>document.getElementById(id);
const terminal=new Set(['completed','failed','cancelled','interrupted','cleanup_required']);
const labels={preparing:'准备中',running:'运行中',awaiting_confirmation:'等待确认',evaluating:'评测中',stopping:'停止中',cleaning:'清理中',cleanup_required:'资源待清理',completed:'已完成',failed:'未完成',cancelled:'已停止',interrupted:'已中断'};
const configFields={'model-name':'model_name','api-base':'api_base','model-class':'model_class','step-limit':'step_limit','cost-limit':'cost_limit','timeout':'timeout'};
let settings=null,environment=null,connected=false,legacy=false,exited=false,pageName='new',kind='local';
let selectedId=null,currentRun=null,generation=0,stream=null,runAbort=null,metaTimer=null,loadedProblem=null;
let allRuns=[],historyOffset=0,historyTotal=0,historyLimit=20,browsingFolder=null,storagePlan=null,storageData=null;
let lifecycleAction='exit',originalBoot=null,restartRequested=false,exitAcknowledged=false,exitPendingResources=false,settingsDirty=false,parentId=null;
let initialization=null;
let benchmarks=[];
let followingWorkflow=null;
let patchGeneration=0,patchChoices=[],checkedPatch=null,patchTimer=null;
let cacheGeneration=0,cacheTimer=null,settingsMerge=null;
let messageWindow=[],messageBefore=null,messageOlder=false,messageTrail=[],messagePageBefore=null,messageLatest=true,messagePending=0;
const sessionRead=key=>{try{return JSON.parse(sessionStorage.getItem(key));}catch{return null;}};
const pendingRead=()=>sessionRead('mini-pending-v2');
const tabId=sessionStorage.getItem('mini-tab-v2')||crypto.randomUUID();
sessionStorage.setItem('mini-tab-v2',tabId);
const draftKey='mini-draft-v2-'+tabId;
const sensitive=value=>/\bsk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9_.-]{12,}/i.test(JSON.stringify(value));
const text=value=>typeof value==='string'?value:JSON.stringify(value,null,2);
const fileSize=bytes=>bytes<1024?`${bytes} B`:`${(bytes/1024).toFixed(1)} KB`;
function node(tag,value,cls){const el=document.createElement(tag);if(value!==undefined)el.textContent=value;if(cls)el.className=cls;return el;}
function notice(message,success=false){$('notice').textContent=message;$('notice').classList.toggle('success',success);$('notice').hidden=false;}
function connection(ok){connected=ok;$('connection').textContent=exited?'服务已退出':ok?'已连接 · 本机':'已断开 · 后台任务可能仍在运行';updateControls();}
function updateControls(){
 for(const id of ['start','offline','preview','approve','reject','cancel','resolve-submit','retry-submit']){
  const button=$(id);button.disabled=!connected||legacy||exited||button.dataset.busy==='1';
 }
 if(settings&&!settings.valid&&kind!=='evaluate'&&(!$('task-profile').value||$('task-profile').value==='legacy'))$('start').disabled=true;
 if(environment&&!environment.bash){$('offline').disabled=true;if(kind==='local')$('start').disabled=true;}
 if(kind==='local'&&!$('task').value.trim())$('start').disabled=true;
 if(kind==='evaluate'&&(!checkedPatch?.usable||checkedPatch.path!==$('prediction').value.trim()||checkedPatch.checkedInstance!==$('instance').value.trim()))$('start').disabled=true;
 if(['swebench','evaluate'].includes(kind)&&(!loadedProblem||loadedProblem.instance_id!==$('instance').value.trim()||loadedProblem.dataset!==$('dataset').value))$('start').disabled=true;
 $('pending-submit').hidden=!pendingRead();if(pendingRead())$('start').disabled=true;
 $('restart-workbench').disabled=!connected||legacy||exited;
 $('exit-workbench').disabled=!connected||legacy||exited;
 if(currentRun?.status==='stopping')$('cancel').disabled=true;
 messageButtons();
 if(typeof updateLibraryControls==='function')updateLibraryControls();
}
async function api(path,method='GET',body,options={}){
 let response;
 try{response=await fetch(path,{method,headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:options.signal||AbortSignal.timeout(15000)});}
 catch(error){if(error.name==='AbortError'&&options.signal?.aborted)throw error;connection(false);const e=Error('无法连接本地服务。后台任务可能仍在运行；请重新连接后核对状态，不要重复提交。');e.network=true;throw e;}
 let data;try{data=await response.json();}catch{throw Error('服务返回了无法读取的数据，请查看诊断提示。');}
 if(!response.ok){const error=Error(typeof data.detail==='string'?data.detail:'操作失败，请检查输入。');error.status=response.status;error.fields=data.fields||[];if(response.status===401)connection(false);throw error;}
 return data;
}
function actionFeedback(button){
 if(button.classList.contains('nav-item'))return null;
 const host=button.closest('.form-actions,.artifact-actions')||button;
 let hint=button.id?$(button.id+'-feedback'):host.nextElementSibling;
 if(!hint?.classList.contains('action-feedback')){hint=node('p',undefined,'action-feedback');if(button.id)hint.id=button.id+'-feedback';host.after(hint);}
 hint.setAttribute('role','alert');hint.setAttribute('aria-live','assertive');hint.setAttribute('aria-atomic','true');hint.hidden=true;return hint;
}
function inputError(message,field){const error=Error(message);error.fields=[{field,message}];return error;}
function showError(error,feedback){
 if(error.name==='AbortError')return;
 if(feedback?.isConnected){feedback.replaceChildren(document.createTextNode(error.message));feedback.hidden=false;}else notice(error.message);
 for(const item of error.fields||[]){const id=Object.keys(configFields).find(k=>configFields[k]===item.field)||({key:'api-key',instance_id:'instance'}[item.field])||item.field;const input=$(id);if(!input)continue;input.setAttribute('aria-invalid','true');let hint=$(id+'-error');if(!hint){hint=node('p',undefined,'field-error validation-error');hint.id=id+'-error';(input.closest('label.check')||input).after(hint);}hint.textContent=item.message;input.setAttribute('aria-describedby',[...new Set([...(input.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean),hint.id])].join(' '));}
 const first=(error.fields||[]).map(item=>$(Object.keys(configFields).find(k=>configFields[k]===item.field)||({key:'api-key',instance_id:'instance'}[item.field])||item.field)).find(Boolean);
 if(first){if(feedback?.isConnected){const locate=node('button','定位需要修改的选项','text-button');locate.type='button';locate.onclick=()=>{for(let el=first.parentElement;el;el=el.parentElement)if(el.tagName==='DETAILS')el.open=true;first.scrollIntoView({block:'center'});first.focus({preventScroll:true});};feedback.append(locate);first.focus({preventScroll:true});}else first.focus();}
 if(feedback?.isConnected)feedback.scrollIntoView({block:'nearest'});
}
async function busy(button,fn){
 if(button.dataset.busy==='1')return;clearErrors();const inert=button.inert,feedback=actionFeedback(button);button.dataset.busy='1';button.inert=true;button.setAttribute('aria-busy','true');updateControls();
 try{await fn();}catch(error){showError(error,feedback?.closest('.page')?.hidden?null:feedback);}finally{delete button.dataset.busy;button.inert=inert;button.removeAttribute('aria-busy');updateControls();}
}
function authorizationSummary(body){return `题目资料下载：${body.allow_data_download?'允许':'不允许'}；运行镜像下载：${body.allow_image_download?'允许':'不允许'}。\n`+(body.operation&&body.operation!=='solve'?'本批不调用模型、不执行求解命令。':`求解命令：${body.auto_approve?'自动执行，不逐条确认':'逐批确认'}；${autoEvaluationLabel(body.dataset)}：${body.auto_evaluate?'开启':'关闭'}。`);}
function clearErrors(){const hints=[...document.querySelectorAll('.validation-error')],ids=new Set(hints.map(el=>el.id));hints.forEach(el=>el.remove());document.querySelectorAll('[aria-invalid]').forEach(el=>{el.removeAttribute('aria-invalid');const descriptions=(el.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&!ids.has(id));if(descriptions.length)el.setAttribute('aria-describedby',descriptions.join(' '));else el.removeAttribute('aria-describedby');});}
function resetConsent(){$('auto').checked=false;$('downloads').checked=false;$('image-downloads').checked=false;}
function benchmarkInfo(id){return benchmarks.find(item=>item.id===id);}
function autoEvaluationLabel(dataset){return benchmarkInfo(dataset)?.capture_partial?'结束后评测本次改动（含达到限制时的部分补丁）':'成功提交后自动评测';}
function updateAutoEvaluationLabels(){for(const [dataset,input] of [['dataset','task-auto-evaluate'],['library-dataset','batch-auto-eval'],['experiment-dataset','experiment-auto-evaluate']]){const label=$(input+'-label');if(label)label.textContent=autoEvaluationLabel($(dataset).value);}const note=$('batch-operation-note');if(note&&$('batch-operation').value==='solve')note.textContent=benchmarkInfo($('library-dataset').value)?.capture_partial?'求解会调用所选模型；结束后可评测本次保留的改动。主动停止任务不自动评测。':'求解会调用当前模型；自动评测只使用本次成功提交的补丁。';}
function patchCaptureNote(capture){if(!capture?.partial)return '';const reasons={LimitsExceeded:'达到步数或费用限制',TimeExceeded:'达到本题时间限制',TimeLimitExceeded:'达到本题时间限制',CostLimitExceeded:'达到费用限制',StepLimitExceeded:'达到步数限制',BillingUnavailable:'调用费用无法确认',Cancelled:'任务已停止'};return `部分补丁 · ${capture.captured?'已保留本次运行中的代码改动':'未取得可评测的代码改动'}。求解停止原因：${reasons[capture.reason]||capture.reason||'未知'}。这不代表模型已成功提交。`;}
function evaluationTimeoutNote(run){const recorded=run.evaluation_timeout_seconds,evaluator=run.benchmark_snapshot?.evaluator;const seconds=Number.isFinite(recorded)&&recorded>0?recorded:evaluator==='swe-bench-pro-v2-verifier'?3000:['verified','lite'].includes(run.dataset)||evaluator==='swe-rebench-fork'?1800:null;return seconds?`单题测试超时 ${seconds} 秒；准备与清理另计。`:'此记录未保存单题测试时限。';}
function benchmarkLabel(id){return benchmarkInfo(id)?.label||({verified:'SWE-bench Verified',lite:'SWE-bench Lite'}[id])||id||'数据集未知';}
function benchmarkSummary(id){const item=benchmarkInfo(id);return benchmarkLabel(id)+(item?.source_revision?` · 固定版本 ${item.source_revision.slice(0,12)}`:'')+(item?.split?` · ${item.split}`:'');}
function selectBenchmark(element,id){if(id&&![...element.options].some(option=>option.value===id)){const option=node('option',`${id}（题集信息待读取）`);option.value=id;element.append(option);}element.value=id;updateBenchmarkNote(element);}
function updateBenchmarkNote(element){const hint=$(element.id+'-note');if(!hint)return;const item=benchmarkInfo(element.value);hint.replaceChildren();const details=node('details',undefined,'benchmark-info'),summary=node('summary','题集信息'+(item?' · '+benchmarkLabel(element.value):''));details.append(summary);const rules=item?.capture_partial?`求解容器不联网；本题总时限 ${Math.round(item.task_time_limit/60)} 分钟。评测使用独立环境；达到限制时保留本次部分补丁，主动停止不自动评测。`:'';const description=item?[benchmarkSummary(element.value),item.description?.replace(/[。；\s]+$/,''),rules,item.license?`许可：${item.license}`:''].filter(Boolean).join('\n'):element.value?`${benchmarkLabel(element.value)} · 题集信息尚未加载；可重新连接后读取。`:'不同题集与固定版本分别统计，比较只采用共同题目。';details.append(node('p',description,'help'));hint.append(details);hint.title=item?.source_revision||'';updateAutoEvaluationLabels();}
async function loadBenchmarks(){
 const data=await api('/api/benchmarks');if(!Array.isArray(data.items)||!data.items.length)throw Error('题集目录暂时不可用，请重新连接后重试。');benchmarks=data.items;
 for(const id of ['dataset','library-dataset','results-dataset','experiment-dataset']){const element=$(id),selected=element.value;element.replaceChildren();if(id==='results-dataset'){const option=node('option','全部数据集');option.value='';element.append(option);}for(const item of benchmarks){const option=node('option',item.label);option.value=item.id;option.title=benchmarkSummary(item.id);element.append(option);}selectBenchmark(element,selected|| (id==='results-dataset'?'':'verified'));}
}
function page(name){
 if(pageName!==name&&typeof invalidateInlineEvaluation==='function')invalidateInlineEvaluation();
 if(pageName==='library'&&name!=='library'){$('library-detail').hidden=true;libraryDetailEpoch++;}
 pageName=name;sessionStorage.setItem('mini-page-v2',name);
 for(const el of document.querySelectorAll('.page'))el.hidden=el.id!=='page-'+name;
 for(const el of document.querySelectorAll('.nav-item'))el.classList.toggle('selected',el.dataset.page===name);
 $('breadcrumb').textContent='工作区 / '+({new:'新建任务',library:'实例库',results:'评测中心',settings:'模型与设置',history:'历史记录',run:'任务详情',storage:'存储管理'}[name]);
 $('notice').hidden=true;if(name==='new')resetConsent();updateControls();
}
function setKind(value,{save=true}={}){
 kind=value;loadedProblem=null;cacheGeneration++;resetConsent();$('problem').hidden=true;
 for(const tab of document.querySelectorAll('[data-kind]')){const on=tab.dataset.kind===value;tab.setAttribute('aria-selected',String(on));tab.tabIndex=on?0:-1;if(on)$('task-form').setAttribute('aria-labelledby',tab.id);}
 $('local-fields').hidden=value!=='local';$('bench-fields').hidden=value==='local';$('prediction-fields').hidden=value!=='evaluate';$('cwd').required=value==='local';$('auto-row').hidden=value==='evaluate';$('offline').hidden=value!=='local';
 $('auto-evaluate-row').hidden=value!=='swebench';$('task-auto-evaluate').checked=true;
 $('billing').textContent=value==='evaluate'?'评测复用已有补丁，不调用模型。':'点击开始后会调用模型；费用以服务商账单为准。';$('start').textContent=value==='evaluate'?'开始评测 →':'开始任务 →';document.querySelector('.run-options').hidden=value==='evaluate';if(save)saveDraft();updateControls();if(value==='evaluate'&&settings&&connected)refreshPatchChoices().catch(showError);if(value!=='local'&&connected)checkCachedProblem();
}
function draftValue(){return {kind,task:$('task').value,cwd:$('cwd').value,dataset:$('dataset').value,instance_id:$('instance').value,prediction_path:$('prediction').value,profile_id:$('task-profile').value,parent_id:parentId,updated:Date.now()};}
function saveDraft(){
 const draft=draftValue();if(sensitive(draft)){$('draft-status').textContent='草稿疑似包含凭据，未自动保存；请先移除敏感内容。';return;}
 try{localStorage.setItem(draftKey,JSON.stringify(draft));localStorage.setItem('mini-last-draft-v2',draftKey);const keys=Object.keys(localStorage).filter(k=>k.startsWith('mini-draft-v2-')).sort((a,b)=>{try{return JSON.parse(localStorage.getItem(b)).updated-JSON.parse(localStorage.getItem(a)).updated;}catch{return 0;}});for(const key of keys.slice(5))localStorage.removeItem(key);$('draft-status').textContent='草稿已保存到本机；执行和下载授权仅对当前任务有效。';}catch{$('draft-status').textContent='浏览器无法保存草稿，请手动复制重要内容后再关闭页面。';}
}
function restoreDraft(){
 let draft;try{draft=JSON.parse(localStorage.getItem(draftKey)||localStorage.getItem(localStorage.getItem('mini-last-draft-v2'))||'null');}catch{}
 if(!draft)return false;setKind(['local','swebench','evaluate'].includes(draft.kind)?draft.kind:'local',{save:false});
 for(const [id,key] of [['task','task'],['cwd','cwd'],['dataset','dataset'],['instance','instance_id'],['prediction','prediction_path']])if(draft[key]!==undefined){if(id==='dataset')selectBenchmark($(id),draft[key]);else $(id).value=draft[key];}if(draft.profile_id)$('task-profile').dataset.preferred=draft.profile_id;parentId=draft.parent_id||null;resetConsent();return true;
}
function configValue(){const result={};for(const [id,key] of Object.entries(configFields))result[key]=['step_limit','cost_limit','timeout'].includes(key)?Number($(id).value):$(id).value;return result;}
function saveSettingsDraft(){settingsDirty=true;const old=sessionRead('mini-settings-draft-v2');const value={settings:configValue(),version:old?.version||settings?.version,base:old?.base||Object.fromEntries(Object.values(configFields).map(k=>[k,settings?.[k]]))};if(!sensitive(value))sessionStorage.setItem('mini-settings-draft-v2',JSON.stringify(value));renderSettingsConflict();}
function renderSettingsConflict(){
 const draft=sessionRead('mini-settings-draft-v2');settingsMerge=null;$('settings-conflict').hidden=!draft||!settings||draft.version===settings.version;$('settings-conflict-fields').replaceChildren();if($('settings-conflict').hidden)return;
 settingsMerge={...settings};
 for(const [id,key] of Object.entries(configFields)){const mine=draft.settings[key],theirs=settings[key],base=draft.base?.[key];if(mine===theirs||mine===base)continue;if(draft.base&&theirs===base){settingsMerge[key]=mine;continue;}
  const label=node('label',document.querySelector(`label[for="${id}"]`)?.textContent||key),select=node('select');select.dataset.conflictKey=key;select.id='merge-'+id;label.htmlFor=select.id;for(const [value,title] of [['server',`最新保存：${theirs}`],['mine',`我的编辑：${mine}`]]){const option=node('option',title);option.value=value;select.append(option);}$('settings-conflict-fields').append(label,select);
 }
}
function fillSettings({preserve=true}={}){
 if(!settings)return;const draft=preserve?sessionRead('mini-settings-draft-v2'):null;const values=draft?.settings||settings;
 for(const [id,key] of Object.entries(configFields))$(id).value=values[key]??'';
 settingsDirty=Boolean(draft);$('model-summary').textContent=settings.model_name;$('limits-summary').textContent=`${settings.step_limit} 步 · 约 $${settings.cost_limit} 上限`;
 if(typeof updateProfileSummary==='function')updateProfileSummary();
 $('key-status').textContent=({ready:'已配置且可解密',missing:'未配置',unreadable:'需要重新配置'}[settings.key_state])||(settings.key_configured?'已配置':'未配置');
 $('config-issues').hidden=settings.valid&&!settings.key_note&&!settings.runtime_limit_note;
 $('config-issues').textContent=[...(settings.issues||[]),settings.key_note||'',settings.runtime_limit_note||''].filter(Boolean).join('\n');$('repair-config').hidden=settings.valid;renderSettingsConflict();updateControls();
}
async function loadSettings(){settings=await api('/api/config');fillSettings();const backups=await api('/api/config/backups');$('backup-list').replaceChildren();for(const backup of backups){const option=node('option',`${backup.id} · ${backup.files.join('、')}`);option.value=backup.id;$('backup-list').append(option);}if(!backups.length){const option=node('option','暂无可恢复备份');option.value='';$('backup-list').append(option);}$('restore-config').disabled=!backups.length;}
function historyRow(run){const row=node('div',undefined,'history-row'),button=node('button');button.type='button';button.append(node('strong',run.title),node('small',`${run.id} · ${run.model} · ${run.kind==='evaluate'?'补丁来源模型':'任务模型'}`));button.onclick=()=>openRun(run.id).catch(showError);row.append(button,node('span',labels[run.status]||run.status,'pill'));return row;}
async function refreshRuns(){
 allRuns=await api('/api/runs');$('run-count').textContent=allRuns.length>=50?'50+':allRuns.length;$('recent').replaceChildren();for(const run of allRuns.slice(0,12)){const button=node('button');button.type='button';button.append(node('span',run.title),node('small',labels[run.status]||run.status));button.onclick=()=>openRun(run.id).catch(showError);$('recent').append(button);}if(!allRuns.length)$('recent').append(node('div','任务开始后会显示在这里','empty'));
}
async function loadHistory(){const data=await api(`/api/history?offset=${historyOffset}&limit=${historyLimit}&archived=${$('history-archived').checked}&search=${encodeURIComponent($('history-search').value)}`);historyTotal=data.total;$('history-list').replaceChildren(...data.items.map(historyRow));if(!data.items.length)$('history-list').append(node('p','没有匹配记录。','empty'));$('history-page').textContent=`${data.total} 条 · 第 ${Math.floor(historyOffset/historyLimit)+1} 页`;$('history-prev').disabled=historyOffset===0;$('history-next').disabled=historyOffset+historyLimit>=historyTotal;}
async function loadLegacy(){const files=await api('/api/legacy');$('legacy-list').replaceChildren();for(const file of files){const row=node('div',undefined,'history-row'),button=node('button');button.append(node('strong',file.name),node('small',`${Math.round(file.bytes/1024)} KB · 保留原位置`));button.onclick=()=>busy(button,async()=>{const data=await api('/api/legacy/read','POST',{path:file.path});$('legacy-content').textContent=text(data);$('legacy-content').hidden=false;});row.append(button);$('legacy-list').append(row);}}
function describePatch(item){
 checkedPatch=item?{...item,checkedInstance:$('instance').value.trim()}:null;
 $('prediction-summary').textContent=!item?'请先选择或检查一份本题补丁。':!item.usable?item.reason:`${item.note} · 来源模型 ${item.model} · ${item.files.length} 个文件：${item.files.join('、')}\n${item.copies>1?`${item.copies} 个文件保存同一份补丁，已合并。`:'已找到代码补丁。'}${item.has_conversation?' 可在评测详情回看匹配的生成对话。':''} 修改时间 ${new Date(item.modified).toLocaleString('zh-CN')}。尚不能据此判断测试是否通过。`;
 updateControls();
}
async function inspectSelectedPatch(){const path=$('prediction').value.trim(),instance=$('instance').value.trim(),epoch=++patchGeneration;describePatch(null);if(!path)return;const item=await api('/api/patches/inspect','POST',{path,instance_id:instance,dataset:$('dataset').value});if(epoch!==patchGeneration||path!==$('prediction').value.trim()||instance!==$('instance').value.trim())return;describePatch(item);}
async function refreshPatchChoices(){
 const epoch=++patchGeneration,instance=$('instance').value.trim(),original=$('prediction').value.trim();describePatch(null);if(!instance)return;
 let data;try{data=await api('/api/patches?'+new URLSearchParams({instance_id:instance,dataset:$('dataset').value}));}catch(error){if(epoch!==patchGeneration)return;$('prediction-summary').textContent=error.status===404?'后台尚未载入补丁选择功能；请在任务结束后重启工作台。':error.message;return;}
 if(epoch!==patchGeneration||instance!==$('instance').value.trim())return;patchChoices=data.items;$('prediction-list').replaceChildren(node('option',data.items.length?'选择要评测的补丁…':'未找到本题可用补丁'));$('prediction-list').firstChild.value='';
 for(const item of data.items){const option=node('option',`${data.recommended===item.path?'推荐 · ':''}${instance} · ${item.model} · ${item.files.length} 个改动文件 · ${item.format==='prediction'?'评测文件':'含对话轨迹'}${item.copies>1?`（${item.copies} 份同内容）`:''} · ${new Date(item.modified).toLocaleString('zh-CN')}`);option.value=item.path;$('prediction-list').append(option);}
 $('prediction-reasons').replaceChildren(...data.rejected.map(x=>node('p',x.name+'：'+x.reason)));$('prediction-excluded').hidden=!data.rejected.length;
 const selected=data.items.find(x=>x.path===original||x.alternatives.some(a=>a.path===original))||(!original?data.items.find(x=>x.path===data.recommended):null);
 if(selected){$('prediction').value=selected.path;$('prediction-list').value=selected.path;describePatch(selected);saveDraft();}else if(original){await inspectSelectedPatch();}else{$('prediction-summary').textContent=data.items.length>1?'存在多份不同内容的补丁，请按来源模型、修改时间和文件列表选择；不会替你猜选。':'没有可直接评测的本题补丁。先求解本题，或填写已有补丁路径后检查。';}
}
function setView(view){$('conversation-tools').hidden=view!=='conversation';for(const button of document.querySelectorAll('[data-view]')){const on=button.dataset.view===view;button.setAttribute('aria-selected',String(on));button.tabIndex=on?0:-1;$(button.dataset.view==='logs'?'logs-panel':button.dataset.view).hidden=!on;}}
function renderPatch(value){const pre=$('patch-content');pre.replaceChildren();for(const line of String(value).split('\n')){const cls=line.startsWith('@@')?'diff-meta':line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':'';pre.append(node('span',line+'\n',cls));}}
function renderMeta(run,epoch=generation){
 if(run.id!==selectedId||epoch!==generation)return;currentRun=run;
 $('run-title').textContent=run.title;$('run-context').textContent=[run.model,run.dataset?benchmarkSummary(run.dataset):'',run.cwd||run.instance_id,run.id].filter(Boolean).join(' · ');$('status').textContent=labels[run.status]||run.status;$('phase').textContent=terminal.has(run.status)?'':run.phase||'';
 const recentIndex=allRuns.findIndex(x=>x.id===run.id);if(recentIndex>=0){allRuns[recentIndex]=run;const recent=$('recent').children[recentIndex]?.querySelector('small');if(recent)recent.textContent=labels[run.status]||run.status;}
 if($('conversation-empty')&&run.kind!=='evaluate')$('conversation-empty').textContent=run.kind==='prepare'?'加载题目不调用模型。题目内容和下一步入口显示在上方。':terminal.has(run.status)?'此任务没有保存模型回复。请查看上方结果说明及命令日志。':run.phase==='等待模型回复'?'正在等待模型回复；收到完整回复后会自动显示在这里。':'任务正在准备，模型回复和执行结果会在这里依次显示。';
 if(!$('logs').textContent)$('logs-empty').textContent=terminal.has(run.status)?'此任务没有日志输出，请查看上方结果说明。':'正在等待测试或命令输出；暂无新日志不代表任务已经停止。';
 const costUnknown=run.billing?.unknown||run.billing?.pending,knownCost=Math.max(run.stats?.cost||0,run.billing?.cost_floor||0);
 $('stats').textContent=run.stats?`${run.stats.calls} 次调用 · 估算 ${costUnknown?'未知'+(knownCost>0?`（已知至少 $${knownCost.toFixed(4)}）`:''):Number.isFinite(run.stats.cost)?'$'+run.stats.cost.toFixed(4):'未知'}`:'';$('cancel').hidden=terminal.has(run.status)||run.status==='cleaning';$('cancel').textContent=run.status==='stopping'?'正在停止…':'停止任务';if(run.status==='stopping')$('cancel').disabled=true;
 $('approval').hidden=!run.pending;if(run.pending){$('commands').textContent=run.pending.actions.map(a=>a.command).join('\n\n');$('approval-cwd').textContent='执行目录：'+run.pending.cwd;}
 $('cleanup-run').hidden=!run.cleanup_pending?.length&&!run.recovery_pending;$('cleanup-run').textContent=run.recovery_pending?'重试状态恢复与资源清理':'重试清理所属资源';$('archive-run').hidden=!terminal.has(run.status);$('archive-run').textContent=run.archived?'取消归档':'归档此记录';
 let result=run.error||'';if(run.evaluation&&!run.result_summary)result+=`${result?'\n':''}${run.evaluation.verdict}\n完成 ${run.evaluation.completed} · 解决 ${run.evaluation.resolved} · 未解决 ${run.evaluation.unresolved} · 错误 ${run.evaluation.errors}`;else if(run.result&&!run.result_summary)result+=(result?'\n':'')+(run.result.exit_status==='Submitted'?'任务已提交。请查看下方结果和产物。':'任务已结束：'+run.result.exit_status);
 if(run.result?.patch_capture?.partial&&!run.result_summary)result+='\n'+patchCaptureNote(run.result.patch_capture);
 if(run.evaluation&&(run.evaluation.infrastructure||run.evaluation.ambiguous||Object.keys(run.evaluation.failure_reasons||{}).length)){result+=`\n疑似环境失败 ${run.evaluation.infrastructure||0} · 原因不明确 ${run.evaluation.ambiguous||0}`;for(const [instance,reason] of Object.entries(run.evaluation.failure_reasons||{}))result+=`\n${instance}：${reason}`;}
 $('evaluation-progress').textContent=run.kind==='evaluate'?(run.evaluation_progress?`${run.evaluation_progress.stage} · 最近阶段日志：${run.evaluation_progress.last_output_at?new Date(run.evaluation_progress.last_output_at).toLocaleTimeString('zh-CN'):'尚未收到'}。${run.evaluation_progress.note}`:evaluationTimeoutNote(run)+'阶段日志到达后会显示，测试正文可能在结束后才写入。'):'';
 if(run.cleanup_pending?.length)result+='\n仍待清理的所属资源：\n'+run.cleanup_pending.join('\n');
 if(run.recovery_pending)result+='\n任务状态尚未完成恢复，请重试状态恢复与资源清理后再继续。';
 if(run.prepared)result=`${run.prepared.instance_id}\n${run.prepared.note||(run.prepared.docker_ready?(run.prepared.image_cached?'Docker 已就绪，题目镜像已缓存。':'Docker 已就绪，镜像尚未缓存；可能需数 GB 存储。'):'Docker 尚未就绪。题目已加载，求解／评测前请检查 Docker。')}\n\n${run.prepared.problem_statement}`;
 if(run.catalog_result)result=`目录同步完成：${run.catalog_result.count} 题。请返回实例库查看。没有下载镜像或调用模型。`;
 $('result-panel').textContent=result;$('result-panel').hidden=!result;$('prepared-actions').hidden=!run.prepared;
 renderPatch(run.result?.submission||(run.kind==='evaluate'?'当前显示评测任务；补丁与完整测试报告可从产物列表下载。':'任务尚未提交；可查看已保存的过程文件。'));
 if(typeof renderWorkflow==='function')renderWorkflow(run);
 if(typeof restoreInlineEvaluation==='function')restoreInlineEvaluation(run);
 if(typeof refreshRunResult==='function')refreshRunResult(run);
 $('artifact-list').replaceChildren();for(const file of run.artifacts||[]){const a=node('a',`${file.name} · ${fileSize(file.bytes)}`,'text-button');a.href=`/api/runs/${run.id}/artifact/${file.id}`;a.onclick=event=>{event.preventDefault();downloadArtifact(run.id,file).catch(showError);};$('artifact-list').append(a);}
 if(!run.artifacts?.length)$('artifact-list').append(node('p','尚未生成可下载产物。','help'));$('evaluate-existing').hidden=!run.prediction;
 if($('changes-summary').dataset.run!==run.id||$('changes-summary').dataset.value!==JSON.stringify(run.changes)){$('changes-summary').dataset.run=run.id;$('changes-summary').dataset.value=JSON.stringify(run.changes);$('changes-summary').replaceChildren();if(run.changes){$('changes-summary').append(node('h2','实际改动文件'),node('p',run.changes.note||'','help'));for(const file of run.changes.files||[]){const details=node('details');details.append(node('summary',`${file.change} · ${file.path}`),node('p',file.preview_note||'此旧记录只保存文件清单，请在本机检查。','help'));if(file.preview)details.append(node('pre',file.preview,'output'));$('changes-summary').append(details);}}}
}
async function downloadArtifact(id,file){const response=await fetch(`/api/runs/${id}/artifact/${file.id}`);if(!response.ok){const data=await response.json();throw Error(data.detail||'下载失败，请刷新文件列表。');}const url=URL.createObjectURL(await response.blob()),a=node('a');a.href=url;a.download=file.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function appendLog(value){$('logs-empty').hidden=true;$('logs').textContent=($('logs').textContent+'\n'+value).slice(-200000);}
function messageText(value){
 if(Array.isArray(value))return value.map(messageText).filter(Boolean).join('\n');
 if(value&&typeof value==='object'){if(value.type==='function_call')return '';return messageText(value.text??value.content??value.output??value.summary??'');}
 if(typeof value==='string'&&/^[\s]*[\[{]/.test(value)){try{const parsed=JSON.parse(value);if(Array.isArray(parsed))return messageText(parsed);}catch{}}
 return text(value??'');
}
function renderMessage(message,{historical=false}={}){const role=message.role||(message.object==='response'||message.type==='message'?'assistant':message.type)||'message';if(role==='system')return;$('conversation-empty')?.remove();const article=node('article'),content=messageText(message.content??message.output??'');article.append(node('div',({assistant:'Mini',user:historical?'原始任务 / 用户':'你',exit:'提交结果',tool:'执行结果',function_call_output:'执行结果',function_call:'执行命令'}[role]||role),'role'));if(role==='tool'||role==='function_call_output'){const details=node('details');details.append(node('summary','查看执行输出'),node('pre',content));article.append(details);}else{article.append(node('pre',content));}if(message.extra?.actions?.length){const details=node('details');details.open=true;details.append(node('summary',`${message.extra.actions.length} 条命令`),node('pre',message.extra.actions.map(x=>x.command).join('\n\n')));article.append(details);}else if(message.tool_calls?.length||role==='function_call'){const details=node('details');details.append(node('summary','查看工具调用'),node('pre',text(message.tool_calls||message.arguments||'')));article.append(details);}if(message.display_truncated)article.append(node('p','此消息只展示节选；完整内容保留在原轨迹或事件日志中。','help'));$('conversation').append(article);}
function messageButtons(){ $('messages-older').disabled=!messageOlder||$('messages-older').dataset.busy==='1';$('messages-newer').disabled=!messageTrail.length||$('messages-newer').dataset.busy==='1';$('messages-latest').textContent=messagePending?`回到最新（${messagePending} 条新消息）`:'回到最新';$('messages-position').textContent=`当前显示 ${messageWindow.length} 条${messageOlder?'，还有较早消息':''}。每页最多 50 条。`;}
async function loadMessagePage(before=null,id=selectedId,epoch=generation,prepared=null){
 const source=currentRun?.kind==='evaluate',endpoint=source?'conversation-source':'timeline';
 const data=prepared||await api(`/api/runs/${id}/${endpoint}${before===null?'':'?before='+before}`,'GET',undefined,{signal:runAbort.signal});
 if(epoch!==generation||id!==selectedId)return null;
 messageWindow=source?(data.messages||[]).map((message,i)=>({id:(data.before||0)+i,message})):data.items;
 messageBefore=data.before;messageOlder=Boolean(data.has_older);messagePageBefore=before;messageLatest=before===null;messagePending=0;
 const note=data.note?(data.note+(data.source?' 来源：'+data.source:'')):'';$('conversation').replaceChildren();if(note)$('conversation').append(node('p',note,'help'));
 for(const item of messageWindow)renderMessage(item.message,{historical:source});
 if(!messageWindow.length&&!note){const empty=node('p','此任务暂时没有保存模型消息。','help');empty.id='conversation-empty';$('conversation').append(empty);}
 messageButtons();return data;
}
function appendMessage(message,eventId){
 if(message.role==='system')return;
 if(message.role==='tool'||message.type==='function_call_output')appendLog(messageText(message.content??message.output));
 const last=$('conversation').lastElementChild;const nearBottom=!last||last.getBoundingClientRect().bottom<innerHeight+120;
 if(!messageLatest||(messageWindow.length>=50&&pageName==='run'&&!$('conversation').hidden&&!nearBottom)){messagePending++;messageButtons();return;}
 if(!nearBottom)messagePending++;
 messageWindow.push({id:eventId,message});if(messageWindow.length>50){messageWindow.shift();$('conversation').querySelector('article')?.remove();messageOlder=true;}messageBefore=messageWindow[0]?.id;
 renderMessage(message);messageButtons();if(nearBottom&&pageName==='run'&&!$('conversation').hidden)requestAnimationFrame(()=>$('conversation').lastElementChild?.scrollIntoView({block:'end'}));
}
async function latestMessages(){messageTrail=[];await loadMessagePage();$('conversation').lastElementChild?.scrollIntoView({block:'end'});}
async function refreshSelected(id=selectedId,epoch=generation){if(!id||epoch!==generation)return;try{const run=await api('/api/runs/'+id,'GET',undefined,{signal:runAbort?.signal});if(epoch!==generation)return;renderMeta(run,epoch);const next=run.workflow?.current_run||(run.workflow?.items||[]).flatMap(item=>item.attempts||[]).filter(attempt=>attempt.run_id).at(-1)?.run_id;if(pageName==='run'&&followingWorkflow&&run.workflow?.id===followingWorkflow&&next&&next!==id&&terminal.has(run.status)){await openRun(next,{followWorkflow:true});}}catch(error){if(epoch!==generation)return;if(error.status===404){stream?.close();clearInterval(metaTimer);selectedId=null;currentRun=null;sessionStorage.removeItem('mini-last-run-v2');page('history');await loadHistory();notice('该记录已删除，已返回历史列表。');}else if(error.name!=='AbortError')showError(error);}}
async function openRun(id,{followWorkflow=false}={}){
 if(!followWorkflow){followingWorkflow=null;sessionStorage.removeItem('mini-follow-workflow');}
 if(typeof invalidateInlineEvaluation==='function')invalidateInlineEvaluation();
 const epoch=++generation;selectedId=id;currentRun=null;stream?.close();runAbort?.abort();runAbort=new AbortController();clearInterval(metaTimer);messageWindow=[];messageTrail=[];messageBefore=null;messageOlder=false;messageLatest=true;messagePending=0;messageButtons();const empty=node('p','正在读取任务过程…','help');empty.id='conversation-empty';$('conversation').replaceChildren(empty);$('logs').textContent='';$('logs-empty').hidden=false;$('feedback').value='';$('approval').hidden=true;$('run-title').textContent='正在加载任务…';$('cancel').hidden=true;setView('conversation');page('run');sessionStorage.setItem('mini-last-run-v2',id);
 let run;try{run=await api('/api/runs/'+id,'GET',undefined,{signal:runAbort.signal});}catch(error){if(error.status===404&&epoch===generation){selectedId=null;sessionStorage.removeItem('mini-last-run-v2');page('history');await loadHistory();notice('上次查看的记录已不存在，请选择其他任务。');return;}throw error;}if(epoch!==generation)return;if(followWorkflow&&run.workflow?.id){followingWorkflow=run.workflow.id;sessionStorage.setItem('mini-follow-workflow',followingWorkflow);}renderMeta(run,epoch);
 document.querySelector('[data-view="conversation"]').textContent=run.kind==='evaluate'?'补丁生成对话':'对话过程';document.querySelector('[data-view="logs"]').textContent=run.kind==='evaluate'?'评测进度':'命令与日志';$('run-explanation').textContent=run.kind==='evaluate'?'当前在测试已有补丁，不调用模型。这里默认显示评测进度；“补丁生成对话”可回看与本补丁匹配的历史记录。':run.kind==='prepare'?'当前仅加载题目。加载完成后，选择求解本题或评测已有补丁。':'模型回复、执行命令和执行结果会依次显示；回复到达前会显示等待状态。';if(run.kind==='evaluate')setView('logs');else if(run.kind==='prepare')setView('patch');
 if(run.kind==='swebench'&&run.status==='failed'&&!run.stats?.calls){setView('logs');$('run-explanation').textContent='任务在准备或首次模型回复前结束，请先查看命令日志和错误信息。没有保存的对话不能补造；重试需要你明确操作。';}
 const timeline=await api(`/api/runs/${id}/timeline`,'GET',undefined,{signal:runAbort.signal});if(epoch!==generation)return;
 await loadMessagePage(null,id,epoch,run.kind==='evaluate'?null:timeline);if(epoch!==generation)return;if(timeline.logs)appendLog(timeline.logs);
 let sequence=timeline.last_event_id;const source=new EventSource(`/api/runs/${id}/events?after=${sequence}`);stream=source;
 const mine=()=>epoch===generation&&selectedId===id;
 source.onmessage=event=>{if(!mine())return;const item=JSON.parse(event.data);if(item.id<=sequence)return;sequence=item.id;if(item.kind==='message'&&!messageWindow.some(x=>x.id===item.id))appendMessage(item.data,item.id);else if(['log','error'].includes(item.kind))appendLog(text(item.data));if(['approval','state','result','evaluation','prepared','evaluation_progress'].includes(item.kind))refreshSelected(id,epoch);};
 source.onerror=()=>{if(mine()&&!exited)connection(false);};
 source.addEventListener('done',()=>{source.close();if(!mine())return;if(!currentRun?.workflow_id&&!currentRun?.workflow)clearInterval(metaTimer);refreshSelected(id,epoch);refreshRuns().catch(showError);});
 source.addEventListener('service-exit',()=>source.close());
 metaTimer=setInterval(()=>{if(mine()&&connected)refreshSelected(id,epoch);},1500);
}
function requestBody(runKind){return {kind:runKind,task:runKind==='offline'?'离线连接检查':runKind==='local'?$('task').value:'',cwd:['local','offline'].includes(runKind)?$('cwd').value:'',dataset:$('dataset').value,instance_id:['local','offline'].includes(runKind)?'':$('instance').value.trim(),prediction_path:runKind==='evaluate'?$('prediction').value.trim():'',profile_id:['local','swebench'].includes(runKind)?$('task-profile').value:undefined,profile_fingerprint:['local','swebench'].includes(runKind)?profileById($('task-profile').value)?.execution_fingerprint:undefined,auto_evaluate:runKind==='swebench'&&$('task-auto-evaluate').checked,auto_approve:runKind==='offline'||runKind==='prepare'||runKind==='evaluate'?false:$('auto').checked,allow_download:false,allow_data_download:['prepare','swebench','evaluate'].includes(runKind)&&$('downloads').checked,allow_image_download:['swebench','evaluate'].includes(runKind)&&$('image-downloads').checked,request_id:crypto.randomUUID(),settings_version:settings.version,parent_id:parentId};}
async function submitBody(body){
 const run=await api('/api/runs','POST',body);sessionStorage.removeItem('mini-pending-v2');$('retry-submit').hidden=true;resetConsent();if(body.kind==='local'){$('task').value='';saveDraft();}await refreshRuns();await openRun(run.id,{followWorkflow:Boolean(run.workflow_id)});
}
async function startRun(runKind){
 clearErrors();if(pendingRead())throw Error('请先核对上次提交结果，避免重复运行。');if(!settings)throw Error('设置尚未加载。');
 const body=requestBody(runKind);if(['swebench','evaluate'].includes(runKind)&&(!loadedProblem||loadedProblem.instance_id!==body.instance_id||loadedProblem.dataset!==body.dataset))throw Error('请先加载题目，并从加载结果选择后续操作。');
 if(sensitive(body))throw Error('任务文本疑似包含凭据。请先移除敏感内容；任务未提交，也未保存到浏览器。');
 sessionStorage.setItem('mini-pending-v2',JSON.stringify(body));resetConsent();updateControls();
 try{await submitBody(body);}catch(error){if(!error.network){sessionStorage.removeItem('mini-pending-v2');updateControls();}throw error;}
}
async function resolvePending(){const body=pendingRead();if(!body)return;try{const run=await api('/api/requests/'+body.request_id);sessionStorage.removeItem('mini-pending-v2');await openRun(run.id);notice('已找到上次提交的任务，没有重复创建。',true);}catch(error){if(error.status===404){$('retry-submit').hidden=false;notice('尚未找到任务。可使用原编号重试；不会换新编号重复创建。');}else if(error.status===410){sessionStorage.removeItem('mini-pending-v2');notice('该任务记录已被删除；未重新执行。需要重做时请明确新建任务。');}else throw error;}}
function usePrepared(target){if(!currentRun?.prepared)return;const data=currentRun.prepared;setKind(target,{save:false});selectBenchmark($('dataset'),data.dataset);$('instance').value=data.instance_id;loadedProblem=data;page('new');renderProblem(data);saveDraft();if(target==='evaluate')refreshPatchChoices().catch(showError);}
function renderProblem(data){$('problem').replaceChildren(node('h3',data.instance_id),node('p',benchmarkSummary(data.dataset||$('dataset').value),'help'),node('p',data.note||(data.docker_ready?(data.image_cached?'镜像已缓存，无需重新下载。':'镜像未缓存，请确认空间后单独允许下载。'):'Docker 未就绪，开始求解或评测前请检查引擎。')),node('pre',data.problem_statement));$('problem').hidden=false;}
async function checkCachedProblem(){
 if(kind==='local')return;const epoch=++cacheGeneration,instance=$('instance').value.trim(),dataset=$('dataset').value;loadedProblem=null;updateControls();
 if(!instance){$('cache-status').textContent='输入题号、仓库或关键词，再从下拉列表选择题目。';return;}
 $('cache-status').textContent='正在核对本机题目缓存…';
 try{const data=await api(`/api/bench/cached?dataset=${encodeURIComponent(dataset)}&instance_id=${encodeURIComponent(instance)}`);if(epoch!==cacheGeneration||instance!==$('instance').value.trim()||dataset!==$('dataset').value||kind==='local')return;$('cache-status').textContent=data.note;if(data.ready){loadedProblem=data;renderProblem(data);}else $('problem').hidden=true;}
 catch(error){if(epoch===cacheGeneration)$('cache-status').textContent=error.message;}
 updateControls();
}
async function browseFolder(path){browsingFolder=null;$('folder-select').disabled=true;const data=await api('/api/directories?path='+encodeURIComponent(path));browsingFolder=data;$('folder-path').textContent=data.path;$('folder-list').replaceChildren();for(const child of data.children){const b=node('button','▱ '+child.name,'folder-item');b.type='button';b.onclick=()=>busy(b,()=>browseFolder(child.path));$('folder-list').append(b);}if(!data.children.length)$('folder-list').append(node('p','此目录没有子文件夹。','help'));$('folder-select').disabled=false;}
function showLifecycle(action){lifecycleAction=action;$('lifecycle-panel').hidden=false;$('lifecycle-title').textContent=action==='exit'?'退出工作台':'重启工作台';$('lifecycle-explanation').textContent='可先停止本工作台的任务并清理资源，或等任务结束后处理。不会关闭其他 Python 或 Docker Desktop。未保存的非敏感草稿会保留；密钥输入不会保存。';$('lifecycle-stop').textContent=action==='exit'?'停止任务并退出':'停止任务并重启';$('lifecycle-wait').textContent=action==='exit'?'任务结束后退出':'任务结束后重启';$('lifecycle-progress').textContent='';$('lifecycle-title').scrollIntoView({block:'nearest'});}
async function lifecycle(mode,allowPending=false){saveDraft();if(settingsDirty)saveSettingsDraft();originalBoot=environment?.boot_id;restartRequested=lifecycleAction==='restart';const result=await api('/api/lifecycle','POST',{action:lifecycleAction,mode,allow_pending_cleanup:allowPending});environment.lifecycle=result;$('lifecycle-progress').textContent=result.status==='waiting'?'会在任务结束并清理完成后处理；此时不再接受新任务。':'正在停止所属任务并清理资源…';$('api-key').value='';}
async function pollHealth(force=false){
 if(exited||legacy||(initialization&&!force))return;
 try{const health=await api('/health');if(restartRequested&&originalBoot&&health.boot_id!==originalBoot){location.reload();return;}connection(true);environment=await api('/api/environment');
 const messages=[environment.bash_issue,...(environment.issues||[])].filter(Boolean);if(environment.update_available)messages.unshift('检测到代码更新，请在方便时使用“重启服务”使新版生效。');if(environment.cleanup_pending?.length)messages.push(`${environment.cleanup_pending.length} 项记录仍有资源待清理，可在任务详情重试。`);$('service-notice').hidden=!messages.length;$('service-notice').textContent=messages.join('\n');
 const life=environment.lifecycle||{};$('lifecycle-pending').hidden=!life.can_exit_with_pending;$('lifecycle-pending').textContent=life.action==='restart'?'保留待清理清单并重启后台':'保留待清理清单并退出后台';if(life.status==='blocked'){$('lifecycle-progress').textContent=life.error;restartRequested=false;exitAcknowledged=false;}
 if(life.status==='ready'){const ack=await api('/api/lifecycle/ack','POST',{id:life.id});exitAcknowledged=ack.action==='exit';restartRequested=ack.action==='restart';exitPendingResources=Boolean(ack.pending_resources?.length);$('lifecycle-progress').textContent=(exitPendingResources?'待清理资源已记录，':'资源已清理，')+(restartRequested?'正在重启…':'正在关闭后台服务…');}
 }catch(error){if(exitAcknowledged&&navigator.onLine){exited=true;stream?.close();clearInterval(metaTimer);connection(false);$('lifecycle-progress').textContent='工作台 Python 服务已退出。'+(exitPendingResources?'仍有待清理容器记录，下次启动可重试清理。':'')+'可以关闭此页；下次双击桌面入口重新启动。';}else if(!restartRequested&&error.status===401)notice('会话已失效，请通过桌面入口重新打开。草稿已保留。');}
}
async function initialize(){
 const restored=restoreDraft();
 document.querySelectorAll('input,textarea,select').forEach(el=>el.disabled=true);
 try{
 const health=await api('/health');if(!health.version){legacy=true;connection(false);$('service-notice').hidden=false;$('service-notice').textContent='检测到旧版后台。请双击桌面“Mini 工作台”，按提示确认升级；这里不会自动结束你正在使用的服务。';return;}
 if(location.hash.length>1){const nonce=location.hash.slice(1);history.replaceState(null,'',location.pathname);await api('/session','POST',{nonce});}
 const results=await Promise.allSettled([loadSettings(),api('/api/environment'),refreshRuns(),loadLegacy(),loadProfiles(),loadBenchmarks()]);if(results[1].status==='fulfilled')environment=results[1].value;for(const result of results)if(result.status==='rejected')showError(result.reason);
 if(!restored&&environment)$('cwd').value=environment.root;
 const savedPage=sessionStorage.getItem('mini-page-v2');const last=sessionStorage.getItem('mini-last-run-v2');if(savedPage==='run'&&last)await openRun(last,{followWorkflow:Boolean(sessionStorage.getItem('mini-follow-workflow'))});else if(savedPage&&$('page-'+savedPage)){page(savedPage);if(savedPage==='history')await loadHistory();if(savedPage==='results')await loadResults();if(savedPage==='library'){await loadLibrary();await refreshQueues();}}else if(environment?.active_run)await openRun(environment.active_run);
 await pollHealth(true);if(kind==='evaluate')await refreshPatchChoices();if(kind!=='local')await checkCachedProblem();updateControls();
 }finally{document.querySelectorAll('input,textarea,select').forEach(el=>el.disabled=false);}
}
async function bootstrap(){if(initialization)return initialization;initialization=initialize().finally(()=>{initialization=null;});return initialization;}
function wireTabs(selector){const tabs=[...document.querySelectorAll(selector)];tabs.forEach((tab,index)=>{tab.id=tab.id||'mini-tab-'+selector.replace(/\W/g,'')+'-'+index;const panel=tab.dataset.kind?'task-form':tab.dataset.view==='logs'?'logs-panel':tab.dataset.view;tab.setAttribute('aria-controls',panel);tab.tabIndex=tab.getAttribute('aria-selected')==='true'?0:-1;$(panel).setAttribute('role','tabpanel');if(!tab.dataset.kind||tab.tabIndex===0)$(panel).setAttribute('aria-labelledby',tab.id);tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[next].focus();tabs[next].click();};});}
document.querySelectorAll('[data-page]').forEach(button=>button.onclick=()=>busy(button,async()=>{page(button.dataset.page);if(button.dataset.page==='settings'){await loadSettings();await loadProfiles();}if(button.dataset.page==='results')await loadResults();if(button.dataset.page==='library'){await loadLibrary();await refreshQueues();}if(button.dataset.page==='history'){await loadHistory();await loadLegacy();}if(button.dataset.page==='storage')await loadStorage();}));
$('brand').onclick=event=>{event.preventDefault();page('new');};
document.querySelectorAll('[data-kind]').forEach(button=>button.onclick=()=>setKind(button.dataset.kind));
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>setView(button.dataset.view));wireTabs('[data-kind]');wireTabs('[data-view]');
for(const id of ['task','cwd','dataset','instance','prediction'])$(id).addEventListener('input',()=>{if(['dataset','instance'].includes(id)){loadedProblem=null;cacheGeneration++;clearTimeout(cacheTimer);cacheTimer=setTimeout(checkCachedProblem,400);$('problem').hidden=true;resetConsent();}if(['dataset','instance','prediction'].includes(id)){patchGeneration++;describePatch(null);clearTimeout(patchTimer);if(kind==='evaluate'&&id!=='prediction')patchTimer=setTimeout(()=>refreshPatchChoices().catch(showError),400);}saveDraft();updateControls();});
for(const id of ['dataset','library-dataset','results-dataset','experiment-dataset'])$(id).addEventListener('change',()=>updateBenchmarkNote($(id)));
$('dataset').addEventListener('change',()=>{$('instance').value='';$('prediction').value='';$('prediction-list').replaceChildren();$('dataset').dispatchEvent(new Event('input',{bubbles:true}));});
for(const id of Object.keys(configFields))$(id).addEventListener('input',saveSettingsDraft);
$('clear-draft').onclick=()=>{if(!confirm('清空当前草稿？已运行任务及其他标签页草稿不受影响。'))return;$('task').value='';$('prediction').value='';parentId=null;resetConsent();saveDraft();};
$('task-form').onsubmit=e=>{e.preventDefault();busy($('start'),()=>startRun(kind));};$('offline').onclick=()=>busy($('offline'),()=>startRun('offline'));$('preview').onclick=()=>busy($('preview'),()=>startRun('prepare'));
$('resolve-submit').onclick=()=>busy($('resolve-submit'),resolvePending);$('retry-submit').onclick=()=>busy($('retry-submit'),async()=>{const body=pendingRead();if(body)await submitBody(body);});
$('use-for-solve').onclick=()=>usePrepared('swebench');$('use-for-eval').onclick=()=>usePrepared('evaluate');
$('prediction-list').onchange=()=>{patchGeneration++;$('prediction').value=$('prediction-list').value;describePatch(patchChoices.find(x=>x.path===$('prediction-list').value));saveDraft();};$('inspect-prediction').onclick=()=>busy($('inspect-prediction'),inspectSelectedPatch);
$('folder-browser').ontoggle=()=>{if($('folder-browser').open)browseFolder($('cwd').value).catch(showError);};$('folder-up').onclick=()=>busy($('folder-up'),()=>browseFolder(browsingFolder?.parent||environment.root));$('folder-select').onclick=()=>{if(browsingFolder){$('cwd').value=browsingFolder.path;$('folder-browser').open=false;saveDraft();}};
async function persistSettings(repair=false){clearErrors();const draft=sessionRead('mini-settings-draft-v2');try{settings=await api('/api/config','PUT',{version:draft?.version||settings.version,settings:configValue(),repair});}catch(error){if(error.status===400||error.status===409){await loadSettings();if(!$('settings-conflict').hidden)throw Error('配置已变化。你的编辑已保留，请在冲突面板选择要保留的值，合并后再保存。');}throw error;}sessionStorage.removeItem('mini-settings-draft-v2');settingsDirty=false;fillSettings({preserve:false});await loadSettings();await loadProfiles();notice('配置已保存并读回验证；没有调用模型。',true);}
 $('merge-settings').onclick=()=>busy($('merge-settings'),async()=>{const current=await api('/api/config');if(current.version!==settings.version){settings=current;fillSettings();throw Error('配置再次变化，已刷新对比，请重新检查冲突项。');}const draft=sessionRead('mini-settings-draft-v2');if(!draft||!settingsMerge)return;const merged={...settingsMerge};for(const select of $('settings-conflict-fields').querySelectorAll('select'))if(select.value==='mine')merged[select.dataset.conflictKey]=draft.settings[select.dataset.conflictKey];sessionStorage.setItem('mini-settings-draft-v2',JSON.stringify({settings:Object.fromEntries(Object.values(configFields).map(k=>[k,merged[k]])),version:settings.version,base:Object.fromEntries(Object.values(configFields).map(k=>[k,settings[k]]))}));fillSettings();notice('已合并到最新版本，尚未保存。请核对后点击“保存设置”。',true);});
 $('messages-older').onclick=()=>busy($('messages-older'),async()=>{const previous=messagePageBefore;if(await loadMessagePage(messageBefore)){messageTrail.push(previous);messageButtons();$('conversation-tools').scrollIntoView({block:'start'});}});
 $('messages-newer').onclick=()=>busy($('messages-newer'),async()=>{if(!messageTrail.length)return;const before=messageTrail[messageTrail.length-1];if(await loadMessagePage(before)){messageTrail.pop();messageButtons();$('conversation-tools').scrollIntoView({block:'start'});}});
 $('messages-latest').onclick=()=>busy($('messages-latest'),latestMessages);
$('settings-form').onsubmit=e=>{e.preventDefault();busy(e.submitter,()=>persistSettings());};$('repair-config').onclick=()=>busy($('repair-config'),async()=>{if(confirm('将备份损坏文件，并用当前填写的值重建 Windows 配置模板。继续？'))await persistSettings(true);});
$('discard-settings').onclick=()=>{if(!confirm('放弃尚未保存的设置修改？'))return;sessionStorage.removeItem('mini-settings-draft-v2');settingsDirty=false;fillSettings({preserve:false});};
$('key-form').onsubmit=e=>{e.preventDefault();busy(e.submitter,async()=>{clearErrors();const key=$('api-key').value,previousVersion=settings.version;const updated=await api('/api/key','PUT',{key,version:previousVersion});$('api-key').value='';settings=updated;const draft=sessionRead('mini-settings-draft-v2');if(draft&&draft.version===previousVersion){draft.version=updated.version;sessionStorage.setItem('mini-settings-draft-v2',JSON.stringify(draft));}fillSettings();await loadProfiles();notice('密钥已加密保存；未覆盖其他尚未保存的设置。',true);});};
$('restore-config').onclick=()=>busy($('restore-config'),async()=>{if(!$('backup-list').value||!confirm('恢复此配置备份？当前文件会再次备份，运行中的任务仍使用原设置。'))return;settings=await api('/api/config/restore','POST',{backup_id:$('backup-list').value,version:settings.version});sessionStorage.removeItem('mini-settings-draft-v2');settingsDirty=false;await loadSettings();await loadProfiles();notice('配置已恢复。',true);});
for(const [id,approve] of [['approve',true],['reject',false]])$(id).onclick=()=>busy($(id),async()=>{const snapshot=currentRun,epoch=generation;if(!snapshot?.pending)return;await api(`/api/runs/${snapshot.id}/approve`,'POST',{ticket:snapshot.pending.ticket,approve,feedback:$('feedback').value});if(epoch===generation){$('feedback').value='';await refreshSelected(snapshot.id,epoch);}});
$('cancel').onclick=()=>busy($('cancel'),async()=>{const id=selectedId,epoch=generation;if(!id)return;await api(`/api/runs/${id}/cancel`,'POST',{});await refreshSelected(id,epoch);notice('停止已请求。请等待进程结束和资源清理；已有文件修改不会撤销。');});
$('cleanup-run').onclick=()=>busy($('cleanup-run'),async()=>{const id=selectedId;await api(`/api/runs/${id}/cleanup`,'POST',{});await refreshSelected(id);});
$('archive-run').onclick=()=>busy($('archive-run'),async()=>{const run=currentRun;if(!run)return;await api(`/api/runs/${run.id}/archive`,'POST',{archived:!run.archived});await refreshSelected(run.id);await refreshRuns();});
$('reuse-run').onclick=()=>busy($('reuse-run'),async()=>{if(!currentRun)return;const originalId=currentRun.id;const data=await api(`/api/runs/${originalId}/reuse`);setKind(['local','swebench','evaluate'].includes(data.kind)?data.kind:data.kind==='prepare'?'swebench':'local',{save:false});for(const [id,key] of [['task','task'],['cwd','cwd'],['dataset','dataset'],['instance','instance_id'],['prediction','prediction_path']]){if(id==='dataset')selectBenchmark($(id),data[key]||'verified');else $(id).value=data[key]||'';}if(data.kind==='offline')$('task').value='';parentId=originalId;page('new');saveDraft();if(kind==='evaluate')refreshPatchChoices().catch(showError);notice(data.kind==='offline'?'已复制检查目录，请点击“先做离线连接检查”，不会调用模型。':data.kind==='evaluate'?'已复制评测参数；重新加载题目后可再次评测，不调用模型。':'已复制参数为新任务；没有恢复旧代理上下文。开始时会重新调用模型，单次授权已重置。');});
$('evaluate-existing').onclick=()=>busy($('evaluate-existing'),()=>beginInlineEvaluation(currentRun?.result_summary?.solve_run_id||currentRun?.id));
$('exit-workbench').onclick=()=>showLifecycle('exit');$('restart-workbench').onclick=()=>showLifecycle('restart');$('lifecycle-stop').onclick=()=>busy($('lifecycle-stop'),()=>lifecycle('stop'));$('lifecycle-wait').onclick=()=>busy($('lifecycle-wait'),()=>lifecycle('when_idle'));$('lifecycle-keep').onclick=()=>busy($('lifecycle-keep'),async()=>{const life=environment?.lifecycle;if(life?.id&&['waiting','stopping','ready'].includes(life.status)){await api('/api/lifecycle/cancel','POST',{id:life.id});restartRequested=false;exitAcknowledged=false;}$('lifecycle-panel').hidden=true;notice('后台继续运行，已取消可取消的服务退出。此前已经停止的任务不会自动恢复。');});
$('lifecycle-pending').onclick=()=>busy($('lifecycle-pending'),async()=>{if(confirm('仅关闭本工作台 Python 后台，保留无法确认清理的容器记录。相关容器可能仍存在，下次启动需重试清理。确认继续？'))await lifecycle('stop',true);});
$('reconnect').onclick=()=>busy($('reconnect'),async()=>{exited=false;await bootstrap();if(selectedId)await openRun(selectedId);});
$('history-refresh').onclick=()=>busy($('history-refresh'),async()=>{historyOffset=0;await loadHistory();});$('history-prev').onclick=()=>busy($('history-prev'),async()=>{historyOffset=Math.max(0,historyOffset-historyLimit);await loadHistory();});$('history-next').onclick=()=>busy($('history-next'),async()=>{historyOffset+=historyLimit;await loadHistory();});
$('history-search').onkeydown=e=>{if(e.key==='Enter')$('history-refresh').click();};
let storageEpoch=0,storageTimer=null,storageSignature='';
async function loadStorage({refresh=false}={}){
 const epoch=++storageEpoch;clearTimeout(storageTimer);
 let status=$('storage-status');if(!status){status=node('p',undefined,'help');status.id='storage-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');$('storage-summary').before(status);}
 status.textContent='正在读取存储统计…';
 try{const data=await api('/api/storage'+(refresh?'?refresh=true':''));if(epoch!==storageEpoch)return;storageData=data;
 const signature=JSON.stringify([data.categories,data.items]);if(signature!==storageSignature){storageSignature=signature;
  $('storage-summary').replaceChildren(...data.categories.map(x=>node('p',`${x.name}：${x.available===false?'未能安全统计':Number.isFinite(x.bytes)?(x.bytes/1024**2).toFixed(2)+' MB':'尚未统计'}`)));
  const selected=new Set([...$('storage-items').querySelectorAll('input:checked')].map(x=>x.value));$('storage-items').replaceChildren();for(const item of data.items){const label=node('label',undefined,'check'),check=node('input');check.type='checkbox';check.value=item.id;check.checked=selected.has(item.id);label.append(check,node('span',`${item.label} · ${(item.bytes/1024**2).toFixed(2)} MB`));$('storage-items').append(label);}
 }
 const ready=Boolean(data.scanned_at||!('scanning' in data));status.className=data.error?'field-error':'help';status.setAttribute('role',data.error?'alert':'status');status.textContent=data.error?`统计未完成：${data.error}。可点击“刷新占用”重试；已有记录未改动。`:data.scanning?(ready?'正在更新，先显示上次统计。':'正在统计目录占用，完成后会自动显示；可继续使用其他页面。'):(data.scanned_at?'统计于 '+new Date(data.scanned_at).toLocaleString('zh-CN')+'。':'')+'占用为参考值，清理前会重新核对所选文件。';
 if(!data.items.length)$('storage-items').replaceChildren(node('p',data.scanning?'正在查找可管理记录…':data.error?'统计暂不可用，请刷新重试。':'暂无可清理的已结束工作台记录。','help'));
 $('storage-preview').disabled=!ready||!data.items.length;
 if(data.scanning&&pageName==='storage')storageTimer=setTimeout(()=>{if(pageName==='storage')loadStorage().catch(error=>showError(error,status));},2000);
 }catch(error){if(epoch!==storageEpoch)return;status.textContent='读取存储统计失败。'+error.message;status.className='field-error';status.setAttribute('role','alert');}
}
$('storage-refresh').onclick=()=>busy($('storage-refresh'),()=>loadStorage({refresh:true}));$('storage-preview').onclick=()=>busy($('storage-preview'),async()=>{const ids=[...$('storage-items').querySelectorAll('input:checked')].map(x=>x.value);storagePlan=await api('/api/storage/preview','POST',{ids});$('storage-plan').textContent=`将删除 ${storagePlan.count} 项，共 ${(storagePlan.bytes/1024**2).toFixed(2)} MB：\n`+storagePlan.paths.join('\n');$('storage-ack').checked=false;$('storage-confirm').hidden=false;});$('storage-delete').onclick=()=>busy($('storage-delete'),async()=>{if(!storagePlan||!$('storage-ack').checked)throw Error('请先检查精确清理范围并勾选确认。');await api('/api/storage','DELETE',{token:storagePlan.token});storagePlan=null;$('storage-confirm').hidden=true;await loadStorage();await refreshRuns();notice('所选工作台文件已删除；原配置、密钥和旧评测未包含在清理范围。',true);});
window.addEventListener('offline',()=>connection(false));window.addEventListener('online',()=>pollHealth());window.addEventListener('beforeunload',event=>{saveDraft();if(settingsDirty||$('api-key').value){event.preventDefault();event.returnValue='';}});
setInterval(()=>{if(currentRun){const end=terminal.has(currentRun.status)?Date.parse(currentRun.finished||currentRun.updated):Date.now();const seconds=Math.max(0,Math.floor((end-Date.parse(currentRun.created))/1000));$('elapsed').textContent=`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;}} ,1000);
setInterval(pollHealth,2500);
bootstrap().catch(showError);
