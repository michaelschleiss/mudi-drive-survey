'use strict';
(() => {
 const equal=(a,b)=>a!=null&&b!=null&&String(a)===String(b);
 function selectedCells(st,live,selection,now=Date.now()/1000){
  const r=st.latest?.radio||{};
  if(!live){const cell=(st.cells||[]).find(c=>c.identity===selection);return cell?[{...cell,rat:cell.identity.split(':')[0]}]:[];}
  if(!Number.isFinite(r.ts)||now-r.ts<0||now-r.ts>Math.max(3,(r.interval_s||.25)*3))return [];
  return ['lte','nr'].filter(p=>r[p+'_band']).map(p=>({rat:p,band:r[p+'_band'],pci:r[p+'_pci'],channel:r[p+'_arfcn'],plmn:r[p+'_plmn'],cell_id:r[p+'_cell_id'],subscription:r.subscription}));
 }
 function matches(row,cell,ta,live){
  if(row.session_id==null||row.association_epoch==null||!equal(row.rat,cell.rat)||!equal(row.plmn,cell.plmn))return false;
  if(!equal(row.band,cell.band)||!equal(row.pci,cell.pci)||!equal(row.channel,cell.channel))return false;
  if(!equal(row.subscription,cell.subscription))return false;
  if(row.cell_id!=null&&cell.cell_id!=null&&!equal(row.cell_id,cell.cell_id))return false;
  if(live)return equal(row.session_id,ta.session_id)&&equal(row.association_epoch,ta.association_epoch);
  // Review uses the recorded selection, never the unrelated live capture epoch.
  // Local signatures can repeat, so retain the recorded session/epoch boundary.
  return equal(row.session_id,cell.session_id)&&equal(row.association_epoch,cell.association_epoch);
 }
 const drawable=e=>e.map_eligible!==false&&Number.isFinite(e.lat)&&Number.isFinite(e.lon)&&Number.isFinite(e.range_m)&&e.range_m>=0;
 if(typeof module!=='undefined'&&module.exports){module.exports={selectedCells,matches,drawable};return;}
 const box=document.querySelector('.live-ta');
 if(!box)return;
 box.replaceChildren();
 const glance=document.createElement('div');glance.id='mast-range-glance';glance.setAttribute('role','status');
 $('live-radio-slot').after(glance);
 const title=document.createElement('strong');title.textContent='Experimental mast ranges';
 const status=document.createElement('span');status.id='ta-status';
 const detail=document.createElement('p');detail.id='ta-detail';
 const toggle=document.createElement('button');toggle.id='ta-toggle';
 box.append(title,toggle,status,detail);
 const layer=map?L.layerGroup().addTo(map):null;
 let signature='',pending=false;
 toggle.onclick=async()=>{pending=true;toggle.disabled=true;try{await post('/api/experimental-ta',{enabled:!state?.experimental_ta?.enabled});}catch(e){status.textContent=e.message;}finally{pending=false;toggle.disabled=false;}};
 function render(st){
  const ta=st.experimental_ta||{};
  toggle.textContent=ta.enabled?'Pause experimental capture':'Try experimental capture';toggle.disabled=pending;
  toggle.setAttribute('aria-pressed',String(!!ta.enabled));
  const live=document.body.classList.contains('live-hunting');
  const cells=selectedCells(st,live,window.TrackUI?.selectedCell);
  const rows=(ta.ranges||[]).filter(e=>cells.some(c=>matches(e,c,ta,live))).sort((a,b)=>a.ts-b.ts);
  const located=rows.filter(drawable).slice(-12);
  // Show unassociated NR initial events as a deliberately broad temporary band.
  // The candidate index can correspond to either µ=1 or µ=0 here; it is never
  // promoted to a cell-specific or validated range.
  const rough=(ta.ranges||[]).filter(e=>e.session_id===ta.session_id&&e.rat==='nr'&&
    Number.isFinite(e.lat)&&Number.isFinite(e.lon)&&Number.isFinite(e.ta_index_candidate)&&
    !Number.isFinite(e.range_m)).slice(-6);
  const temporaryLte=(ta.ranges||[]).filter(e=>e.kind==='lte_timing_snapshot'&&Number.isFinite(e.range_m)&&
    Number.isFinite(e.temporary_lat)&&Number.isFinite(e.temporary_lon)).slice(-180);
  const band=cells.map(c=>c.band).join(' + '),pci=cells.map(c=>c.pci).join(', ');
  const latest=rows.at(-1),lastLocated=located.at(-1);
  const timing=ta.timing_snapshot;
  const matchedTiming=timing&&cells.some(c=>matches(timing,c,ta,live))&&Number.isFinite(timing.timing_advance_us)?timing:null;
  const areas=(ta.areas||[]).filter(e=>cells.some(c=>matches(e,c,ta,live)));
  const regions=areas.flatMap(a=>a.regions||[]);
  glance.textContent=lastLocated?`${lastLocated.rat.toUpperCase()} range ≈ ${fmt(lastLocated.range_m)} m · experimental · ${fmt(age(lastLocated))} s ago`:!cells.length?(live?'Mast range · fresh radio needed':'Select a cell to inspect ranges'):!ta.enabled?'Mast range · capture paused':!cells.some(c=>c.rat==='nr')?(cells.some(c=>c.rat==='lte')?'LTE range · timing advance unavailable':'Mast range · no serving cell'):/unavailable|error|failed|timeout/i.test(ta.status||'')?'Mast range · capture unavailable':'Mast range · waiting for a located measurement';
  if(matchedTiming&&!lastLocated)glance.textContent=`LTE timing ${fmt(matchedTiming.timing_advance_us)} µs · age unknown`;
  if(rough.length&&!lastLocated)glance.textContent=`5G rough distance bands · ${rough.length} temporary`;
  if(temporaryLte.length&&!lastLocated)glance.textContent=`LTE temporary radii · ${temporaryLte.length} samples`;
  if(regions.length)glance.textContent='Mast search area · experimental';
  status.textContent=/unavailable|error|failed|timeout/i.test(ta.status||'')?'Capture unavailable · check router connection':(ta.enabled?'Capture enabled':'Capture paused');
  detail.textContent=located.length?`${located.length} experimental ranges for ${band}. Last candidate ${fmt(lastLocated.range_m)} m · ${fmt(age(lastLocated))} s ago. Unvalidated decoding; error bound unknown. Ring overlaps are search clues, not a mast fix.`:latest?`${band} · PCI ${pci}: ${latest.reason}. No ring placed.`:`No matched distance measurement${band?' for '+band:''}.${ta.enabled?' Capture continues automatically':''}`;
  if(!matchedTiming&&cells.some(c=>c.rat==='lte')&&ta.lte_status)detail.textContent+=' LTE timing: '+ta.lte_status;
  if(matchedTiming)detail.textContent+=` LTE modem timing snapshot: ${fmt(matchedTiming.timing_advance_us)} µs${Number.isFinite(matchedTiming.range_m)?' · nominal distance '+fmt(matchedTiming.range_m)+' m':''}. Measurement time unknown; unverified and not mapped.`;
  if(rough.length)detail.textContent+=` ${rough.length} unmatched 5G timing events are drawn as broad purple temporary bands. They assume either 30 or 15 kHz numerology and may belong to another cell; they are not mast ranges.`;
  if(temporaryLte.length)detail.textContent+=` ${temporaryLte.length} orange LTE radii are drawn from the modem's repeated timing value and the GPS position when it was retrieved. Their intersection is a useful visual clue only: the modem does not report the timing measurement age.`;
  if(areas.length)detail.textContent+=' Search model: '+areas.map(a=>a.status).filter(Boolean).join(', ')+'. Assumed error bounds; not a confirmed mast location.';
  if(ta.capture?.adjustments)detail.textContent+=` ${ta.capture.adjustments} relative corrections received this session; not applied without a matched initial range.`;
  const key=JSON.stringify([live,band,pci,located.map(e=>[e.id,e.range_m,e.uncertainty_m]),rough.map(e=>[e.id,e.ts,e.ta_index_candidate]),temporaryLte.map(e=>[e.id,e.temporary_lat,e.temporary_lon,e.range_m]),areas]);
  if(key===signature)return;signature=key;layer?.clearLayers();
  for(const region of regions){
   const polygons=(region.polygons||[]).filter(p=>Array.isArray(p)&&p.length>=4&&p.every(v=>Array.isArray(v)&&v.length===2&&v.every(Number.isFinite)));
   if(layer&&polygons.length)L.polygon(polygons.map(p=>[p]),{color:'#d18c39',weight:0,fillColor:'#d18c39',fillOpacity:.18}).bindTooltip('Experimental mast search area · conditional on unvalidated timing measurements and assumed error bounds').addTo(layer);
  }
  located.forEach((e,i)=>{
   const colour='#d18c39',label=document.createElement('div');
   label.textContent=`EXPERIMENTAL · ${e.band} · PCI ${e.pci} · channel ${e.channel} · candidate TA ${e.ta_index_candidate} → ${fmt(e.range_m)} m. ${new Date(e.ts*1000).toLocaleTimeString()} · GPS ±${fmt(e.acc_m)} m. TA decoding and cell association unvalidated; range error unknown.`;
   if(layer){if(Number.isFinite(e.uncertainty_m)&&e.uncertainty_m>0){for(const radius of [Math.max(0,e.range_m-e.uncertainty_m),e.range_m+e.uncertainty_m])L.circle([e.lat,e.lon],{radius,color:colour,weight:1,dashArray:'3 5',fill:false,opacity:.25}).addTo(layer);}
    L.circle([e.lat,e.lon],{radius:e.range_m,color:colour,weight:2,dashArray:'7 7',fill:false,opacity:.3+.6*(i+1)/located.length}).bindTooltip(label).addTo(layer);
    L.circleMarker([e.lat,e.lon],{radius:4,color:colour,fillOpacity:.7}).bindTooltip('Receiver position at experimental TA event').addTo(layer);}
  });
  rough.forEach(e=>{
   const low=e.ta_index_candidate*299792458/(2*1920000*2),high=low*2;
   const label=`TEMPORARY 5G TA band · candidate ${e.ta_index_candidate} · roughly ${fmt(low)}–${fmt(high)} m. No confirmed band, PCI, cell identity or numerology; do not treat this as a mast location.`;
   if(layer){
    L.circle([e.lat,e.lon],{radius:low,color:'#6d62a6',weight:1,dashArray:'2 7',fill:false,opacity:.4}).bindTooltip(label).addTo(layer);
    L.circle([e.lat,e.lon],{radius:high,color:'#6d62a6',weight:1,dashArray:'2 7',fill:false,opacity:.4}).bindTooltip(label).addTo(layer);
    L.circleMarker([e.lat,e.lon],{radius:3,color:'#6d62a6',fillOpacity:.8}).bindTooltip(label).addTo(layer);
   }
  });
  temporaryLte.forEach(e=>{
   const label=`TEMPORARY LTE radius · ${fmt(e.range_m)} m from ${fmt(e.timing_advance_us)} µs. Centre is GPS at QMI retrieval, not measurement time; age unknown, cell match only, not a validated mast range.`;
   if(layer)L.circle([e.temporary_lat,e.temporary_lon],{radius:e.range_m,color:'#c77d32',weight:1,dashArray:'5 8',fill:false,opacity:.18}).bindTooltip(label).addTo(layer);
  });
 }
 const previous=window.renderDrive;window.renderDrive=st=>{previous(st);render(st);};
 window.ExperimentalRanges={render,layer};
 if(state)render(state);
})();
