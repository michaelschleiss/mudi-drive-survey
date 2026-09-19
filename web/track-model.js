(function(root){
'use strict';
const key=(r,p)=>r[p+'_cell_id']&&r[p+'_plmn']?`${p}:${r[p+'_plmn']}:${r[p+'_cell_id']}`:`${p}:${r[p+'_plmn']??'None'}:${r[p+'_band']}:${r[p+'_arfcn']??'None'}:${r[p+'_pci']==null?'None':Number(r[p+'_pci']).toFixed(1)}`;
// Backend JSON numbers have no preserved .0: normalize both sides when comparing signatures.
const normalize=k=>k.replace(/:(\d+)\.0(?=:|$)/g,':$1');
const meters=(a,b)=>Math.hypot((a.lat-b.lat)*111320,(a.lon-b.lon)*111320*Math.cos(a.lat*Math.PI/180));
function samples(observations){
 const windows=new Map(),prev={},result=[];let last=null,distance=0;
 for(const o of observations){
  if(last&&o.ts-last.ts<=3&&o.ts>=last.ts)distance+=meters(last,o);last=o;
  for(const p of ['lte','nr']){
   const r=o.radio;if(!r[p+'_band']||r[p+'_pci']==null)continue;
   const id=normalize(key(r,p)),prior=prev[p],continuous=prior&&o.ts-prior.ts<=3&&o.ts>=prior.ts;
   let window=windows.get(id)||[];if(!continuous||prior.identity!==id)window=[];
   window=window.filter(s=>o.ts-s.ts<=10);
   if(r[p+'_rsrp']!=null)window.push({ts:o.ts,value:r[p+'_rsrp']});windows.set(id,window);
   const mean=window.length?window.reduce((a,b)=>a+b.value,0)/window.length:0;
   const stability=window.length>=5?Math.sqrt(window.reduce((a,b)=>a+(b.value-mean)**2,0)/window.length):null;
   const s={...o,segment:continuous&&prior.identity===id?prior.segment:`${p}:${o.id}`,prefix:p,identity:id,band:r[p+'_band'],pci:r[p+'_pci'],cell_id:r[p+'_cell_id'],plmn:r[p+'_plmn'],channel:r[p+'_arfcn'],rsrp:r[p+'_rsrp']??null,sinr:r[p+'_sinr']??null,rsrq:r[p+'_rsrq']??null,width:r[p+'_bw']??null,stability,distance,change:!!(continuous&&prior.identity!==id),from:continuous?prior.identity:null};
   result.push(s);prev[p]=s;
  }
 }
 return result;
}
function color(s,metric){
 const value=s[metric];if(metric==='bands'){let hue=0;for(const c of s.band)hue=(hue*31+c.charCodeAt(0))%360;return `hsl(${hue},60%,40%)`;}
 if(value==null)return '#9ba59e';
 const limits={rsrp:[-110,-90],sinr:[5,15],rsrq:[-16,-10],width:[10,40],stability:[5,2]}[metric];
 if(!limits)return '#587568';
 return metric==='stability'?(value>limits[0]?'#bd654d':value>limits[1]?'#c49a39':'#21836b'):(value<limits[0]?'#bd654d':value<limits[1]?'#c49a39':'#21836b');
}
function trend(list,identity,now){
 const same=list.filter(s=>s.identity===normalize(identity));const last=same.at(-1);
 if(!last||now-last.ts>3||now-last.ts<0)return null;
 const recent=same.filter(s=>s.segment===last.segment&&last.ts-s.ts<=10&&s.rsrp!=null);
 const before=recent.filter(s=>last.ts-s.ts>=5).map(s=>s.rsrp),after=recent.filter(s=>last.ts-s.ts<5).map(s=>s.rsrp);
 if(before.length<3||after.length<3)return null;
 const median=a=>{a.sort((a,b)=>a-b);const i=(a.length-1)/2;return(a[Math.floor(i)]+a[Math.ceil(i)])/2;};
 const change=median(after)-median(before);
 return {change,label:change>=2?'Strengthening':change<=-2?'Weakening':'Steady',seconds:10};
}
const api={key,normalize,meters,samples,color,trend};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.TrackModel=api;
})(globalThis);
