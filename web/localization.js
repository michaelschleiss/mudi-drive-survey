'use strict';
(() => {
 const box=document.querySelector('.live-ta');
 if(!box)return;
 box.replaceChildren();
 const title=document.createElement('strong');title.textContent='Experimental mast ranges';
 const status=document.createElement('span');status.id='ta-status';
 const detail=document.createElement('p');detail.id='ta-detail';
 const toggle=document.createElement('button');toggle.id='ta-toggle';
 box.append(title,toggle,status,detail);
 const layer=map?L.layerGroup().addTo(map):null;
 let signature='',pending=false;
 toggle.onclick=async()=>{pending=true;toggle.disabled=true;try{await post('/api/experimental-ta',{enabled:!state?.experimental_ta?.enabled});}catch(e){status.textContent=e.message;}finally{pending=false;toggle.disabled=false;}};
 function render(st){
  const ta=st.experimental_ta||{},r=st.latest.radio||{};
  toggle.textContent=ta.enabled?'Pause experimental capture':'Try experimental capture';toggle.disabled=pending;
  toggle.setAttribute('aria-pressed',String(!!ta.enabled));
  const target=st.cells?.find(c=>c.identity===st.target);
  const pci=target?target.pci:r.nr_pci,channel=target?target.channel:r.nr_arfcn,band=target?target.band:r.nr_band;
  // No cross-band or cross-PCI rings; radio signatures may still repeat geographically.
  const rows=(ta.ranges||[]).filter(e=>e.band===band&&e.pci===pci&&e.channel===channel);
  const located=rows.filter(e=>e.lat!=null&&e.lon!=null&&e.range_m!=null).slice(-30);
  const latest=rows.at(-1),lastLocated=located.at(-1);
  status.textContent=(ta.status||'Experimental capture off')+(ta.capture?` · stream heartbeat ${fmt(age(ta.capture),1)} s ago`: '');
  detail.textContent=located.length?`${located.length} dashed rings for ${band} · PCI ${pci}. Last candidate ${fmt(lastLocated.range_m)} m · ${fmt(age(lastLocated))} s ago. Unvalidated decoding; error bound unknown. Ring overlaps are search clues, not a mast fix.`:latest?`${band} · PCI ${pci}: ${latest.reason}. No ring placed.`:`Waiting for ${band||'an NR cell'} initial timing event and matching GPS. Events stream as written; modem events may be sparse. Dashed rings are unvalidated hypotheses.`;
  if(ta.capture?.adjustments)detail.textContent+=` ${ta.capture.adjustments} relative corrections received this session; not applied without a matched initial range.`;
  const key=JSON.stringify([band,pci,channel,located.map(e=>e.id)]);
  if(key===signature)return;signature=key;layer?.clearLayers();
  located.forEach((e,i)=>{
   const colour='#d18c39',label=document.createElement('div');
   label.textContent=`EXPERIMENTAL · ${e.band} · PCI ${e.pci} · channel ${e.channel} · candidate TA ${e.ta_index_candidate} → ${fmt(e.range_m)} m. ${new Date(e.ts*1000).toLocaleTimeString()} · GPS ±${fmt(e.acc_m)} m. TA decoding and cell association unvalidated; range error unknown.`;
   if(layer){L.circle([e.lat,e.lon],{radius:e.range_m,color:colour,weight:2,dashArray:'7 7',fill:false,opacity:.3+.6*(i+1)/located.length}).bindTooltip(label).addTo(layer);
    L.circleMarker([e.lat,e.lon],{radius:4,color:colour,fillOpacity:.7}).bindTooltip('Receiver position at experimental TA event').addTo(layer);}
  });
 }
 const previous=window.renderDrive;window.renderDrive=st=>{previous(st);render(st);};
 window.ExperimentalRanges={render,layer};
 if(state)render(state);
})();
