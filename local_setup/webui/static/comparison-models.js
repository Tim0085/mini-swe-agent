'use strict';
function comparisonModelName(group){return String(group.model||group.model_name||group.profile_name||group.name||'来源未知').trim().replace(/^openai\//,'');}
function comparisonFamilies(groups,preferred=[]){
 const families=new Map();
 for(const group of groups){const name=comparisonModelName(group);if(!families.has(name))families.set(name,{name,variants:[]});families.get(name).variants.push(group);}
 for(const family of families.values()){
  family.variants.sort((a,b)=>Number(Boolean(b.conditions_known))-Number(Boolean(a.conditions_known))||(b.summary?.evaluated||0)-(a.summary?.evaluated||0)||String(a.group_id||a.id).localeCompare(String(b.group_id||b.id)));
  const choice=family.variants.find(g=>preferred.includes(g.group_id||g.id))||family.variants[0];family.defaultId=choice.group_id||choice.id;
 }
 return [...families.values()];
}
if(typeof module!=='undefined')module.exports={comparisonModelName,comparisonFamilies};
