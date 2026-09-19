'use strict';
(() => {
 const panel=document.createElement('details');panel.id='evidence-panel';
 panel.innerHTML='<summary>Timing evidence <span id="evidence-count"></span></summary><p id="evidence-status">Select a full cell identity.</p><label><input id="evidence-rings" type="checkbox" checked> Show range rings</label><label class="evidence-replay">Review timing history <input id="evidence-at" type="range" min="0" max="0" value="0" disabled></label><div class="evidence-buttons"><button id="evidence-live">Latest</button><button id="evidence-play">Replay</button><button id="evidence-fit">Fit ranges</button></div><p id="evidence-time"></p><div id="evidence-sites"></div><small>Ranges are centred on receiver GPS. Estimated emitters and mast matches remain tentative. Live TA needs a validated diagnostic feeder.</small>';
 $('hunt-sidebar').insertBefore(panel,$('hunt-live-details'));
 const unresolved=document.createElement('div');unresolved.id='unresolved-sightings';unresolved.hidden=true;$('cell-inventory').before(unresolved);
 let activeKey=null,instance=null,detail=null,fullRings=[],request=0,lastFetch=0,inFlight=false,asOf=null,playing=null;
 const ranges=map?L.layerGroup().addTo(map):null,locations=map?L.layerGroup().addTo(map):null;
 const circle=(r,d)=>Array.from({length:73},(_,i)=>{const a=i/72*Math.PI*2;return [r.lat+Math.sin(a)*d/111320,r.lon+Math.cos(a)*d/(111320*Math.cos(r.lat*Math.PI/180))];});
 function draw(){
  ranges?.clearLayers();locations?.clearLayers();$('evidence-sites').replaceChildren();
  $('evidence-count').textContent=detail?`${detail.rings.length} rings`:'';
  if(!detail){$('evidence-status').textContent='Select a full cell identity to inspect attributed timing data.';return;}
  const estimate=detail.estimate;
  $('evidence-status').textContent=estimate?.status==='tentative'?`Tentative emitter fit · ${fmt(estimate.radius)} m plausible spread · ${fmt(estimate.residual_m)} m residual. Not a confirmed mast.`:estimate?.reason||'No attributed timing data for this cell. GPS and signal still record.';
  $('evidence-time').textContent=detail.rings.length?`${asOf==null?'Latest':'Review'} · ${new Date(detail.rings.at(-1).ts*1000).toLocaleTimeString()} · ${detail.rings.at(-1).source}`:'No rings; signal strength is not converted to distance.';
  if($('evidence-rings').checked&&ranges)for(const r of detail.rings.slice(-8)){
   const paths=[circle(r,r.distance_m+r.uncertainty_m)],inner=Math.max(0,r.distance_m-r.uncertainty_m);if(inner)paths.push(circle(r,inner).reverse());
   const note=document.createElement('span');note.textContent=`${r.key}: ${fmt(r.distance_m)} ±${fmt(r.uncertainty_m)} m · ${r.source}`;
   L.polygon(paths,{color:'#397d69',weight:1,opacity:.5,fillOpacity:.04}).bindTooltip(note).addTo(ranges);
  }
  if(estimate?.status==='tentative'&&locations){
   L.circle([estimate.lat,estimate.lon],{radius:estimate.radius,color:'#6e4ea1',weight:2,dashArray:'5 5',fillOpacity:.08}).bindTooltip('Tentative emitter position; not a surveyed mast').addTo(locations);
   const trail=detail.trail||[];if(trail.length>1)L.polyline(trail.map(p=>[p.lat,p.lon]),{color:'#6e4ea1',weight:2,dashArray:'2 5'}).bindTooltip('Recent fit convergence; not an accuracy guarantee').addTo(locations);
  }
  for(const site of detail.matches||[]){
   const row=document.createElement('p');row.textContent=`Candidate ${site.id} · ${site.source} · site uncertainty ±${fmt(site.uncertainty_m)} m`;$('evidence-sites').append(row);
   if(locations){const note=document.createElement('span');note.textContent=row.textContent;L.circleMarker([site.lat,site.lon],{radius:7,color:'#bd8c31',weight:2,fillOpacity:.3}).bindTooltip(note).addTo(locations);}
  }
 }
 async function refresh(force=false){
  if(!activeKey||activeKey.startsWith('unknown:')){detail=null;draw();return;}
  if(inFlight||(!force&&Date.now()-lastFetch<1800))return;
  const generation=++request,key=activeKey;inFlight=true;lastFetch=Date.now();
  try{
   const r=await fetch('/api/cell?key='+encodeURIComponent(key)+(asOf==null?'':'&at='+asOf),{signal:AbortSignal.timeout(3000)});
   if(!r.ok)throw Error('Timing details unavailable');const data=await r.json();
   if(generation!==request||key!==activeKey)return;
   detail=data;if(asOf==null){fullRings=data.rings;$('evidence-at').max=Math.max(0,fullRings.length-1);$('evidence-at').value=fullRings.length-1;$('evidence-at').disabled=fullRings.length<2;}draw();
  }catch(e){if(generation===request){detail=null;draw();$('evidence-status').textContent='Timing details unavailable; reconnecting…';}}
  finally{inFlight=false;}
 }
 function stopReplay(){if(playing)clearInterval(playing);playing=null;$('evidence-play').textContent='Replay';}
 $('evidence-rings').onchange=draw;
 $('evidence-at').oninput=e=>{stopReplay();asOf=fullRings[Number(e.target.value)]?.ts??null;lastFetch=0;refresh(true);};
 $('evidence-live').onclick=()=>{stopReplay();asOf=null;refresh(true);};
 $('evidence-play').onclick=()=>{
  if(playing){stopReplay();return;}if(fullRings.length<2)return;
  let index=0;$('evidence-play').textContent='Pause';
  const step=()=>{if(inFlight)return;if(index>=fullRings.length){stopReplay();return;}$('evidence-at').value=index;asOf=fullRings[index++].ts;refresh(true);};step();playing=setInterval(step,900);
 };
 $('evidence-fit').onclick=()=>{if(!map||!detail?.rings.length)return;const pts=detail.rings.flatMap(r=>circle(r,r.distance_m+r.uncertainty_m));setFollow(false);map.fitBounds(pts,{padding:[24,24],maxZoom:16});};
 window.renderEvidence=st=>{
  const target=driveView==='map'&&innerWidth>650?$('hunt-sidebar'):$('track-inspector');if(panel.parentElement!==target){if(target===$('hunt-sidebar'))target.insertBefore(panel,$('hunt-live-details'));else target.append(panel);}
  const key=window.TrackUI?.selectedCell==='all'?null:window.TrackUI?.selectedCell;
  if(key!==activeKey||instance!==st.instance){stopReplay();instance=st.instance;activeKey=key;request++;detail=null;fullRings=[];asOf=null;lastFetch=0;$('evidence-at').disabled=true;draw();}
  unresolved.hidden=!(st.unresolved||[]).length;unresolved.textContent=(st.unresolved||[]).map(o=>`${o.band||o.radio} · PCI ${fmt(o.pci)}: full identity unavailable; this sighting is not merged into a cell.`).join(' ');
  const slot=driveView==='map'?$('hunt-inventory-slot'):$('cell-inventory').parentElement;if(unresolved.parentElement!==slot)slot.insertBefore(unresolved,$('cell-inventory'));
  if(st.storage?.error){$('drive-health').textContent=st.storage.error;$('drive-health').classList.add('warning');}
  if(panel.open)refresh();
 };
 panel.ontoggle=()=>{if(panel.open)refresh(true);};
})();
