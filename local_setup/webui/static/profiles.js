'use strict';
let modelRegistry={connections:[],profiles:[],default_profile_id:''},editingConnection=null,editingProfile=null,profileTestPlan=null,profilesEpoch=0;
const providerTemplates=[
 {id:'custom',name:'自定义 OpenAI 兼容服务',api_base:''},
 {id:'openai',name:'OpenAI',api_base:'https://api.openai.com/v1'},
 {id:'deepseek',name:'DeepSeek',api_base:'https://api.deepseek.com'},
 {id:'anthropic',name:'Claude（OpenAI 兼容）',api_base:'https://api.anthropic.com/v1/'},
 {id:'xai',name:'Grok',api_base:'https://api.x.ai/v1'},
 {id:'zhipu',name:'GLM',api_base:'https://open.bigmodel.cn/api/paas/v4/'},
 {id:'moonshot',name:'Kimi',api_base:'https://api.moonshot.ai/v1'}
];
const profileById=id=>modelRegistry.profiles.find(x=>x.id===id);
const activeProfiles=()=>modelRegistry.profiles.filter(x=>x.enabled!==false&&modelRegistry.connections.find(c=>c.id===x.connection_id)?.enabled!==false);
function profileLabel(profile){const connection=modelRegistry.connections.find(x=>x.id===profile.connection_id);return `${profile.name} · ${connection?.name||'原连接'} · ${profile.api_model_id||profile.model_name.replace(/^openai\//,'')}`;}
function registryButton(title,action,cls='secondary'){const button=node('button',title,cls);button.type='button';button.onclick=()=>busy(button,action);return button;}
async function loadProfiles(){const epoch=++profilesEpoch,data=await api('/api/profiles');if(epoch!==profilesEpoch)return;modelRegistry=data;renderProfiles();renderProfileChoices();if(probePending&&$('profile-test-panel').hidden)restoreProbePanel();}
function profileTestLabel(profile){
 const test=profile.last_test;if(!test)return '尚未验证';
 const label=test.status==='running'?'验证中':test.fingerprint&&!profile.test_current?'配置变化，需重新验证':({passed:profile.test_current?'已验证':'旧记录未绑定配置，需重验',compatible:'已验证',failed:'验证未通过',incompatible:'工具调用不兼容',incomplete:'未完成验证'})[test.status]||'未完成验证';
 return label+(test.checked_at?' · '+new Date(test.checked_at).toLocaleString():'');
}
function renderProfiles(){
 $('profiles-list').replaceChildren();
 for(const profile of modelRegistry.profiles){const row=node('article',undefined,'profile-row'),info=node('div');
  info.append(node('strong',profile.name),node('p',profileLabel(profile),'help'),node('p',`${profile.protocol==='responses'?'Responses':'Chat Completions'} · ${profileTestLabel(profile)} · ${profile.pricing?.mode==='free'?'明确免费':profile.pricing_available?'价格可用':'价格待确认'}${profile.enabled===false?' · 已停用':''}`,'help'));if(profile.issue)info.append(node('p',profile.issue,'field-error'));
  const actions=node('div',undefined,'profile-actions');if(profile.id===modelRegistry.default_profile_id)actions.append(node('span','网页默认','pill'));else if(profile.enabled!==false)actions.append(registryButton('设为默认',async()=>{await api('/api/profiles/default','PUT',{profile_id:profile.id});await loadProfiles();$('task-profile').value=profile.id;$('batch-profile').value=profile.id;updateProfileSummary();invalidateBatch();saveDraft();notice('网页默认模型已更新，原命令行配置未改变。',true);},'text-button'));
  if(profile.id==='legacy'||profile.readonly)actions.append(registryButton('查看原配置',async()=>{$('legacy-settings').open=true;$('legacy-settings').scrollIntoView({block:'start'});},'text-button'));else actions.append(registryButton('编辑',async()=>editProfile(profile)));
  actions.append(registryButton('复制',async()=>editProfile({...profile,id:null,name:profile.name+' · 副本'}),'text-button'));
  if(profile.enabled!==false)actions.append(registryButton('验证工具调用',()=>previewProfileTest(profile),'text-button'));
  row.append(info,actions);$('profiles-list').append(row);
 }
 if(!modelRegistry.profiles.length)$('profiles-list').append(node('p','先添加一个服务连接，再配置要使用的模型。','empty'));
 $('connections-list').replaceChildren();for(const connection of modelRegistry.connections){const row=node('div',undefined,'history-row'),info=node('div');info.append(node('strong',connection.name),node('p',`${connection.api_base} · ${connection.key_configured?'密钥已保存':'尚未配置密钥'}${connection.enabled===false?' · 已停用':''}`,'help'));row.append(info,registryButton(connection.id==='legacy'?'查看原连接':'管理',async()=>{if(connection.id==='legacy'){$('legacy-settings').open=true;$('legacy-settings').scrollIntoView({block:'start'});}else editConnection(connection);}));$('connections-list').append(row);}
 const templates=(modelRegistry.presets||[]).some(x=>x.api_base)?modelRegistry.presets:providerTemplates;
 const old=$('connection-preset').value;$('connection-preset').replaceChildren();for(const p of templates){const option=node('option',p.name);option.value=p.id;$('connection-preset').append(option);}if(!templates.some(x=>x.id==='custom')){const option=node('option','自定义 OpenAI 兼容服务');option.value='custom';$('connection-preset').append(option);}$('connection-preset').value=old||'custom';
}
function renderProfileChoices(){
 for(const id of ['task-profile','batch-profile']){const select=$(id),old=select.dataset.preferred||select.value,initialized=select.dataset.loaded==='1'||Boolean(select.dataset.preferred);select.replaceChildren();for(const p of activeProfiles()){const option=node('option',profileLabel(p));option.value=p.id;select.append(option);}if(!select.options.length){const option=node('option','原配置');option.value='';select.append(option);}select.value=initialized&&[...select.options].some(x=>x.value===old)?old:modelRegistry.default_profile_id||select.options[0]?.value||'';select.dataset.loaded='1';delete select.dataset.preferred;}
 updateProfileSummary();if(typeof renderExperimentProfiles==='function')renderExperimentProfiles();
}
function updateProfileSummary(){const p=profileById($('task-profile').value);if(!p)return;$('model-summary').textContent=p.model_name;const limits=p.limits||settings||{};$('limits-summary').textContent=`${limits.step_limit??'—'} 步 · 约 $${limits.cost_limit??'—'} 上限`;}
function editConnection(connection=null){editingConnection=connection; $('connection-form').hidden=false;$('connection-key-form').hidden=!connection?.id;$('connection-editor-title').textContent=connection?'编辑服务连接':'添加服务连接';$('connection-name').value=connection?.name||'';$('connection-base').value=connection?.api_base||'';$('connection-preset').value=connection?.provider||'custom';$('connection-models').hidden=!connection?.id;$('connection-disable').hidden=!connection?.id;$('connection-disable').textContent=connection?.enabled===false?'启用连接':'停用连接';$('connection-key').value='';$('connection-key-state').textContent=connection?.key_configured?'已有密钥；替换不会显示旧值。':'尚无密钥；先保存连接，再加密保存密钥。';$('connection-message').textContent='';$('connection-model-list').hidden=true;$('connection-form').scrollIntoView({block:'start'});$('connection-name').focus({preventScroll:true});}
function editProfile(profile=null){
 editingProfile=profile;clearErrors();for(const hint of $('profile-form').querySelectorAll('.action-feedback'))hint.hidden=true;$('profile-form').hidden=false;$('profile-editor-title').textContent=profile?.id?'编辑模型档案':'添加模型档案';$('profile-name').value=profile?.name||'';$('profile-model').value=profile?.model_name||'';$('profile-protocol').value=profile?.protocol==='responses'?'responses':'chat';$('profile-disable').hidden=!profile?.id;$('profile-disable').textContent=profile?.enabled===false?'启用档案':'停用档案';
 $('profile-connection').replaceChildren();for(const c of modelRegistry.connections.filter(x=>x.enabled!==false||x.id===profile?.connection_id)){const option=node('option',c.name);option.value=c.id;$('profile-connection').append(option);}$('profile-connection').value=profile?.connection_id||modelRegistry.connections.find(x=>x.enabled!==false)?.id||'';
 const params=profile?.parameters||{},pricing=profile?.pricing||{},limits=profile?.limits||settings||{};
 for(const option of $('profile-reasoning').querySelectorAll('[data-preserved]'))option.remove();
 const reasoning=params.reasoning_effort,unknown=reasoning!==undefined&&![...$('profile-reasoning').options].some(option=>option.value===reasoning);
 if(unknown){const option=node('option',`${reasoning}（保留已有值）`);option.value=reasoning;option.dataset.preserved='1';$('profile-reasoning').append(option);}
 $('profile-reasoning-note').hidden=!unknown;$('profile-reasoning-note').textContent=unknown?'此档案包含当前页面未列出的推理参数，已保留。仅修改名称不会清除它；变更前请确认服务商支持情况。':'';
 $('profile-top-p').value=params.top_p??'';$('profile-extra').value=params.extra_body?JSON.stringify(params.extra_body,null,2):'';
 $('profile-temperature').value=params.temperature??'';$('profile-output').value=params.max_output_tokens??params.max_completion_tokens??params.max_tokens??'';$('profile-reasoning').value=params.reasoning_effort??'';$('profile-pricing').value=pricing.mode||'builtin';$('profile-input-price').value=pricing.input_per_million??'';$('profile-output-price').value=pricing.output_per_million??'';$('profile-cache-price').value=pricing.cached_input_per_million??'';
 $('profile-steps').value=limits.step_limit||30;$('profile-cost').value=limits.cost_limit||3;$('profile-timeout').value=limits.timeout||120;profilePricingVisibility();$('profile-form').scrollIntoView({block:'start'});$('profile-name').focus({preventScroll:true});
}
function profilePricingVisibility(){const custom=$('profile-pricing').value==='custom';$('profile-prices').hidden=!custom;$('profile-input-price').required=custom;$('profile-output-price').required=custom;}
function profileBody(){const parameters={...(editingProfile?.parameters||{})};delete parameters.temperature;delete parameters.max_tokens;delete parameters.max_completion_tokens;delete parameters.max_output_tokens;delete parameters.reasoning_effort;
 delete parameters.top_p;delete parameters.extra_body;if($('profile-top-p').value!=='')parameters.top_p=Number($('profile-top-p').value);if($('profile-extra').value.trim()){let value;try{value=JSON.parse($('profile-extra').value);}catch{throw inputError('额外服务参数不是有效的 JSON，请检查引号、逗号和括号。','profile-extra');}if(!value||typeof value!=='object'||Array.isArray(value))throw inputError('额外服务参数需要是 JSON 对象，例如 {"enable_thinking":true}。','profile-extra');parameters.extra_body=value;}
 if($('profile-temperature').value!=='')parameters.temperature=Number($('profile-temperature').value);if($('profile-output').value!=='')parameters[$('profile-protocol').value==='responses'?'max_output_tokens':editingProfile?.parameters?.max_completion_tokens!==undefined?'max_completion_tokens':'max_tokens']=Number($('profile-output').value);if($('profile-reasoning').value)parameters.reasoning_effort=$('profile-reasoning').value;
 const pricing={mode:$('profile-pricing').value};if(pricing.mode==='custom'){pricing.input_per_million=Number($('profile-input-price').value);pricing.output_per_million=Number($('profile-output-price').value);if($('profile-cache-price').value!=='')pricing.cached_input_per_million=Number($('profile-cache-price').value);}
 return {name:$('profile-name').value.trim(),connection_id:$('profile-connection').value,model_name:$('profile-model').value.trim(),protocol:$('profile-protocol').value,parameters,pricing,limits:{step_limit:Number($('profile-steps').value),cost_limit:Number($('profile-cost').value),timeout:Number($('profile-timeout').value)},enabled:editingProfile?.enabled!==false,...(editingProfile?.id?{version:editingProfile.version}:{})};
}
let probePending=null,probePolling=false,probePreparing=false,probeSelected=null,probeUncertain=false,probeClosing=false;
try{probePending=JSON.parse(sessionStorage.getItem('mini-profile-probe')||'null');}catch{}
function probeMessage(text,error=false){const el=$('profile-test-result');el.textContent=text;el.className=error?'field-error':'help';el.setAttribute('role',error?'alert':'status');}
function probeControls(){
 $('profile-test-start').disabled=Boolean(probePending||probePreparing);
 $('profile-test-start').hidden=!profileTestPlan&&!probePending&&!probePreparing;
 $('profile-test-start').textContent=probePending?(probeUncertain?'等待确认结果…':'验证中…'):'确认并验证';
 $('profile-test-retry').hidden=!probeSelected||Boolean(profileTestPlan)||Boolean(probePending||probePreparing);
 $('profile-test-check').hidden=!probePending;
 $('profile-test-recovery').hidden=!probePending||!probeUncertain;
 $('profile-test-end').disabled=probeClosing;
 $('profile-test-ack').disabled=Boolean(probePending||probePreparing);
 $('profile-test-panel').setAttribute('aria-busy',String(Boolean(probePending||probePreparing)));
}
function rememberProbe(value){probePending=value;probeUncertain=false;$('profile-test-end-ack').checked=false;if(value)sessionStorage.setItem('mini-profile-probe',JSON.stringify(value));else sessionStorage.removeItem('mini-profile-probe');probeControls();}
async function finishProbe(data){
 if(data.status==='running'){probeUncertain=false;probeControls();return;}
 if(data.persistence_error){probeUncertain=true;probeMessage((data.result?.message||'验证已结束。')+' '+data.persistence_error,true);probeControls();return;}
 const result=data.result||data;rememberProbe(null);profileTestPlan=null;
 $('profile-test-elapsed').textContent='';
 probeMessage((result.message||result.error||'验证未完成，请重新预览后确认。')+(result.provider_code?' 服务错误码：'+result.provider_code:''),result.status!=='passed');
 try{await loadProfiles();}catch{probeMessage((result.message||result.error||'验证已结束。')+' 模型列表刷新失败，可点击刷新模型配置。',true);}
 probeControls();
}
async function checkProbe(){
 if(!probePending||probePolling)return;probePolling=true;const pending=probePending;
 try{const data=await api(`/api/profiles/${encodeURIComponent(pending.profile_id)}/test/${encodeURIComponent(pending.request_id)}`);if(probePending?.request_id===pending.request_id)await finishProbe(data);}
 catch{if(probePending?.request_id===pending.request_id){probeUncertain=true;probeMessage('暂时无法确认上次验证结果。可查询上次结果，或确认后结束本次等待；不会自动重复调用。',true);probeControls();}}
 finally{probePolling=false;}
}
async function previewProfileTest(profile){
 $('profile-test-panel').hidden=false;$('profile-test-panel').scrollIntoView({block:'start'});
 if(probePending){probeMessage('上次验证结果尚未确认，请先查询上次结果。',true);await checkProbe();return;}
 if(probePreparing)return;probeSelected=profile;profileTestPlan=null;probePreparing=true;
 $('profile-test-ack').checked=false;$('profile-test-ack').removeAttribute('aria-invalid');
 $('profile-test-description').textContent=profileLabel(profile);probeMessage('正在准备验证范围，此步骤不会调用模型。');probeControls();
 try{const data=await api(`/api/profiles/${encodeURIComponent(profile.id)}/test/preview`,'POST',{});
 profileTestPlan={...data,profile_id:profile.id,request_id:crypto.randomUUID()};
 $('profile-test-description').textContent=`实际模型 ID：${data.api_model_id||profile.api_model_id||profile.model_name.replace(/^openai\//,'')}\n协议：${data.protocol==='responses'?'Responses':'Chat Completions'}\n请求地址：${data.endpoint||data.api_base}\n内部路由名称：${data.model_name}（openai/ 用于内部路由，不额外发送给服务商）\n生成参数：${JSON.stringify(data.parameters||{})}；本次输出上限固定为 256 token。`;
 probeMessage(`估算费用检查上限 $${data.budget_limit??0.05}。单次请求仍可能超限。`);
 }catch(error){probeMessage(error.message,true);}
 finally{probePreparing=false;probeControls();}
}
$('profile-test-retry').onclick=()=>previewProfileTest(profileById(probeSelected.id)||probeSelected);
$('profile-test-check').onclick=checkProbe;
$('profile-test-end').onclick=async()=>{
 if(!probePending||probeClosing)return;
 if(!$('profile-test-end-ack').checked){probeMessage('请先确认下方结束等待的说明。',true);$('profile-test-end-ack').focus();return;}
 const pending=probePending;probeClosing=true;probeControls();
 try{const data=await api(`/api/profiles/${encodeURIComponent(pending.profile_id)}/test/${encodeURIComponent(pending.request_id)}/close`,'POST',{confirmed:true});
 if(probePending?.request_id===pending.request_id)await finishProbe(data);
 }catch(error){if(probePending?.request_id===pending.request_id)probeMessage(error.message,true);}
 finally{probeClosing=false;probeControls();}
};
setInterval(()=>{if(!probePending)return;
 $('profile-test-elapsed').textContent=`已等待 ${Math.max(0,Math.floor((Date.now()-probePending.started)/1000))} 秒；关闭面板不会取消已发送的请求。`;
 if(connected)checkProbe();
},3000);

$('profiles-refresh').onclick=()=>busy($('profiles-refresh'),loadProfiles);
$('connection-new').onclick=()=>editConnection();$('profile-new').onclick=()=>editProfile();
$('connection-close').onclick=()=>{$('connection-form').hidden=true;$('connection-key-form').hidden=true;$('connection-key').value='';};$('profile-close').onclick=()=>{$('profile-form').hidden=true;};
$('connection-preset').onchange=()=>{const templates=(modelRegistry.presets||[]).some(x=>x.api_base)?modelRegistry.presets:providerTemplates;const template=templates.find(x=>x.id===$('connection-preset').value);if(template?.api_base)$('connection-base').value=template.api_base;if(!$('connection-name').value&&template)$('connection-name').value=template.name;};
$('connection-form').onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{const body={name:$('connection-name').value.trim(),api_base:$('connection-base').value.trim(),provider:$('connection-preset').value,enabled:editingConnection?.enabled!==false,...(editingConnection?{version:editingConnection.version}:{})};const data=await api(editingConnection?'/api/connections/'+encodeURIComponent(editingConnection.id):'/api/connections',editingConnection?'PUT':'POST',body);await loadProfiles();editConnection(data.connection||data);$('connection-message').textContent='连接已保存。现在可以在下方保存密钥，再添加模型档案。';});};
$('connection-key-form').onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{if(!editingConnection?.id)throw Error('请先保存服务连接。');const key=$('connection-key').value;try{await api(`/api/connections/${encodeURIComponent(editingConnection.id)}/key`,'PUT',{key,version:editingConnection.version});}finally{$('connection-key').value='';}const id=editingConnection.id;await loadProfiles();editingConnection=modelRegistry.connections.find(x=>x.id===id);$('connection-key-state').textContent='密钥已加密保存。正在运行的任务不会切换凭据。';});};
$('connection-models').onclick=()=>busy($('connection-models'),async()=>{if(!editingConnection)return;const data=await api(`/api/connections/${encodeURIComponent(editingConnection.id)}/models`);const models=data.models||data.data||data.items||data;$('connection-model-list').replaceChildren();$('profile-model-options').replaceChildren();for(const item of models){const id=typeof item==='string'?item:item.id||item.model_name;if(!id)continue;const option=node('option');option.value=id;$('profile-model-options').append(option);$('connection-model-list').append(registryButton(id,async()=>{editProfile({name:id,model_name:id,connection_id:editingConnection.id});},'text-button'));}$('connection-model-list').hidden=false;$('connection-message').textContent=`获取到 ${models.length} 个模型。点选可建立档案；这不代表工具调用已经验证。`;});
$('connection-disable').onclick=()=>busy($('connection-disable'),async()=>{if(!editingConnection)return;await api('/api/connections/'+encodeURIComponent(editingConnection.id),'PUT',{...editingConnection,enabled:editingConnection.enabled===false});const id=editingConnection.id;await loadProfiles();editConnection(modelRegistry.connections.find(x=>x.id===id));});
$('profile-form').onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{if(!$('profile-connection').value)throw Error('请先添加一个服务连接。');await api(editingProfile?.id?'/api/profiles/'+encodeURIComponent(editingProfile.id):'/api/profiles',editingProfile?.id?'PUT':'POST',profileBody());$('profile-form').hidden=true;await loadProfiles();notice('模型档案已保存，没有发起模型调用。',true);});};
$('profile-disable').onclick=()=>busy($('profile-disable'),async()=>{if(!editingProfile?.id)return;await api('/api/profiles/'+encodeURIComponent(editingProfile.id),'PUT',{...profileBody(),enabled:editingProfile.enabled===false});$('profile-form').hidden=true;await loadProfiles();notice('档案状态已更新，历史记录和结果快照保留。',true);});
$('profile-pricing').onchange=profilePricingVisibility;
$('profile-test-close').onclick=()=>{$('profile-test-panel').hidden=true;if(!probePending){profileTestPlan=null;$('profile-test-ack').checked=false;}};
$('profile-test-start').onclick=async()=>{
 if(probePending||probePreparing)return;
 if(!profileTestPlan){probeMessage('请点击“重新验证”准备新的验证范围，再勾选授权。',true);return;}
 if(!$('profile-test-ack').checked){probeMessage('请先勾选“允许对这个模型发起上述付费验证”，再点击确认。',true);$('profile-test-ack').setAttribute('aria-invalid','true');$('profile-test-ack').focus();return;}
 $('profile-test-ack').removeAttribute('aria-invalid');const plan=profileTestPlan;profileTestPlan=null;
 $('profile-test-ack').checked=false;rememberProbe({profile_id:plan.profile_id,request_id:plan.request_id,started:Date.now()});
 probeMessage('正在验证工具调用往返，请勿重复提交…');
 try{const data=await api(`/api/profiles/${encodeURIComponent(plan.profile_id)}/test`,'POST',{token:plan.token,request_id:plan.request_id},{signal:AbortSignal.timeout(180000)});
 if(probePending?.request_id===plan.request_id)await finishProbe(data);
 }catch(error){if(probePending?.request_id!==plan.request_id)return;
 if(error.status&&error.status<500){rememberProbe(null);probeMessage(error.message,true);}
 else{probeMessage('连接中断或等待超时，正在查询原请求结果；不会自动重新调用模型。',true);await checkProbe();}}
};
function restoreProbePanel(){if(!probePending)return;probeSelected=profileById(probePending.profile_id)||{id:probePending.profile_id};$('profile-test-panel').hidden=false;$('profile-test-description').textContent='恢复上次验证状态';probeControls();checkProbe();}

$('task-profile').onchange=()=>{updateProfileSummary();saveDraft();};
$('batch-profile').onchange=()=>{const p=profileById($('batch-profile').value);if(p?.limits)$('batch-budget').value=p.limits.cost_limit;invalidateBatch();};
