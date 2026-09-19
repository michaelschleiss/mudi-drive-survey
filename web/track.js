'use strict';
(() => {
 const M=TrackModel;
 let observations=[],samples=[],selectedCell='all',selected=null,layer=null,highlight=null,axis='time',lastOptions='';
 const definitions={rsrp:'RSRP · strength',sinr:'SINR · cleanliness',rsrq:'RSRQ · quality',stability:'Stability · RSRP variation',width:'Reported channel width',bands:'Serving band',hunt:'Scouting clues',upload:'Upload tests'};
 const legends={rsrp:'RSRP: red <−110 · amber −110 to −90 · green ≥−90 dBm.',sinr:'SINR: red <5 · amber 5–15 · green ≥15 dB.',rsrq:'RSRQ: red <−16 · amber −16 to −10 · green ≥−10 dB.',stability:'RSRP standard deviation over the preceding 10 s of this cell: green ≤2 · amber ≤5 · red >5 dB. At least 5 readings required. Stable does not mean strong.',width:'Reported channel width: red <10 · amber 10–40 · green ≥40 MHz. Not confirmed uplink bandwidth.',bands:'Colour identifies band. Select a cell to distinguish transmitters on the same band.',hunt:'Green: width ≥20 MHz + RSRP ≥−105 + SINR ≥10 together. Scouting clues, not predicted Mbps.',upload:'Completed upload footprints; cell traces are hidden.'};
 const metricSelect=$('radio-layer');metricSelect.replaceChildren(...Object.entries(definitions).map(([v,l])=>new Option(l,v)));metricSelect.value='rsrp';radioMapMode='rsrp';
 const controls=document.createElement('div');controls.className='track-controls';
 controls.innerHTML='<label>Track layer <select id="track-metric"></select></label><label>Follow a cell <select id="track-cell"><option value="all">All cells</option></select></label><button id="track-fit">Fit observations</button><p id="track-legend"></p>';
 document.querySelector('.map-wrap').before(controls);
 $('track-metric').replaceChildren(...Object.entries(definitions).map(([v,l])=>new Option(l,v)));$('track-metric').value='rsrp';
 const cellControl=document.createElement('label');cellControl.className='radio-layer-label';cellControl.textContent='Follow cell ';
 const mapCell=document.createElement('select');mapCell.id='map-cell';mapCell.add(new Option('All cells','all'));cellControl.append(mapCell);$('radio-layer-note').before(cellControl);
 const panel=document.createElement('section');panel.id='track-inspector';panel.hidden=true;
 panel.innerHTML='<div class="track-heading"><strong id="track-title">Cell observations</strong><button id="track-close" aria-label="Close cell inspector">×</button></div><div id="track-detail"></div><label class="track-axis">Chart axis <select id="track-axis"><option value="time">Time</option><option value="distance">Travelled distance</option></select></label><svg id="track-chart" viewBox="0 0 420 235" role="img" aria-label="Selected cell signal charts; click to select a GPS observation"></svg><p id="track-chart-note"></p><p class="track-limit">Timing advance: unavailable / unvalidated. No mast coordinates inferred.</p>';
 document.querySelector('.map-card').append(panel);
 // The backend now supplies one timestamp-qualified position per radio observation.
 // Disable the old GPS-triggered painter to avoid duplicated or differently matched points.
 paintRadioObservation=()=>{};
 const matched=s=>(selectedCell==='all'||s.identity===selectedCell)&&(huntBand==='all'||s.band===huntBand);
 function tint(s){if(radioMapMode==='hunt')return scoutColor(scoutPriority(s.width,s.rsrp,s.sinr));return M.color(s,radioMapMode);}
 function title(s){return `${s.prefix==='nr'?'5G':'LTE'} ${s.band} · ${s.plmn||'operator unknown'} · ${s.cell_id?'Cell '+s.cell_id:'PCI '+s.pci+' / channel '+(s.channel??'—')+' (partial identity)'}`;}
 function selectCell(id){selectedCell=id==='all'?'all':M.normalize(id);selected=null;for(const el of [$('track-cell'),mapCell])el.value=selectedCell;redraw();renderInspector();}
 window.selectTrackCell=selectCell;
 function inspect(s,pan=false){map?.closePopup();selectedCell=s.identity;selected=s;for(const el of [$('track-cell'),mapCell])el.value=selectedCell;redraw();renderInspector();setFollow(false);if(pan)map?.panTo([s.lat,s.lon]);}
 function redraw(){
  if(!map)return;if(!layer)layer=L.layerGroup().addTo(map);layer.clearLayers();
  if(highlight){map.removeLayer(highlight);highlight=null;}
  if(cellObservationLayer){if(selectedCell!=='all'||!['hunt','upload'].includes(radioMapMode))map.removeLayer(cellObservationLayer);else if(!map.hasLayer(cellObservationLayer))cellObservationLayer.addTo(map);}
  const note=(legends[radioMapMode]||'')+' Grey = unavailable. Gaps >3 s are not connected. Points are receiver locations. Dense map tracks are simplified; charts retain all loaded readings.';
  $('radio-layer-note').textContent=note;$('track-legend').textContent=note;
  if(radioMapMode==='upload')return;
  const previous={},paintCounts={lte:0,nr:0},visible=samples.filter(matched),stride=Math.max(1,Math.ceil(visible.length/2000));
  for(let index=0;index<visible.length;index++){
   const s=visible[index],ordinal=paintCounts[s.prefix]++;if(ordinal%stride&&index!==visible.length-1&&!s.change&&s.id!==selected?.id&&previous[s.prefix]?.segment===s.segment)continue;
   const active=matched(s),color=active?tint(s):'#9fa7a2',prior=previous[s.prefix];
   if(prior&&prior.identity===s.identity&&prior.segment===s.segment){
    L.polyline([[prior.lat,prior.lon],[s.lat,s.lon]],{color,weight:active?4:1,opacity:active?.6:.12,interactive:false}).addTo(layer);
   }
   previous[s.prefix]=s;
   if(!active)continue;
   const point=L.circleMarker([s.lat,s.lon],{radius:s.prefix==='lte'?6:4,color,weight:1,fillColor:color,fillOpacity:.8});
   const label=document.createElement('div');label.textContent=`${title(s)} · ${new Date(s.ts*1000).toLocaleTimeString()} · RSRP ${fmt(s.rsrp)} · SINR ${fmt(s.sinr)} · RSRQ ${fmt(s.rsrq)} · GPS ±${fmt(s.acc_m)} m · match Δ ${fmt(s.gps_delta_s,1)} s`;
   point.bindTooltip(label).on('click',()=>inspect(s)).addTo(layer);
   if(s.change){const change=L.circleMarker([s.lat,s.lon],{radius:10,color:'#e28134',weight:2,fillOpacity:0});change.bindTooltip('Observed serving-cell change · '+title(s)).on('click',()=>inspect(s)).addTo(layer);}
  }
  if(selected)highlight=L.circleMarker([selected.lat,selected.lon],{radius:12,color:'#182e28',weight:3,fillOpacity:0}).addTo(map);
 }
 function renderInspector(){
  for(const card of document.querySelectorAll('.cell-candidate'))card.classList.toggle('active-cell',card.dataset.identity===selectedCell);
  if(selectedCell==='all'){panel.hidden=true;return;}
  panel.hidden=false;
  const list=samples.filter(s=>s.identity===selectedCell&&(huntBand==='all'||s.band===huntBand));
  if(!list.length){$('track-title').textContent='No matching observations in loaded track';$('track-detail').textContent='Select another cell or clear the band filter. Raw history remains available in Export survey.';$('track-chart').replaceChildren();return;}
  const s=selected&&list.find(x=>x.id===selected.id)?selected:list.at(-1);
  $('track-title').textContent=title(s);
  const caAge=s.radio.ca_ts==null?'age unknown':`${fmt(Math.max(0,s.ts-s.radio.ca_ts),1)} s old`;
  $('track-detail').textContent=`${new Date(s.ts*1000).toLocaleString()} · ${s.lat.toFixed(6)}, ${s.lon.toFixed(6)}\nRSRP ${fmt(s.rsrp)} dBm · SINR ${fmt(s.sinr)} dB · RSRQ ${fmt(s.rsrq)} dB\nReported width ${fmt(s.width)} MHz · RSRP variation ${fmt(s.stability,1)} dB\nCA ${s.radio.ca||'unknown'} (${caAge}; uplink unconfirmed)\nGPS ±${fmt(s.acc_m)} m · GPS fix ${new Date(s.gps_ts*1000).toLocaleTimeString()} · radio–GPS Δ ${fmt(s.gps_delta_s,2)} s`;
  drawChart(list,s);
 }
 let chartSamples=[];
 function drawChart(list,s){
  chartSamples=list;const svg=$('track-chart');svg.replaceChildren();const width=Math.max(300,svg.clientWidth||420);svg.setAttribute('viewBox',`0 0 ${width} 235`);svg.setAttribute('preserveAspectRatio','none');
  const ns='http://www.w3.org/2000/svg',add=(tag,attrs,text)=>{const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!=null)el.textContent=text;svg.append(el);return el;};
  const xval=o=>axis==='time'?o.ts:o.distance,lo=xval(list[0]),hi=xval(list.at(-1)),x=o=>55+(xval(o)-lo)/(hi-lo||1)*(width-70);
  [['rsrp',-130,-60,'RSRP dBm'],['sinr',-10,35,'SINR dB'],['rsrq',-25,-3,'RSRQ dB']].forEach(([field,min,max,label],i)=>{
   const top=20+i*68,y=v=>top+48-(Math.max(min,Math.min(max,v))-min)/(max-min)*48;
   add('text',{x:4,y:top+9,'font-size':10,fill:'#53655b'},label);add('text',{x:4,y:top+24,'font-size':12,fill:'#17392f'},fmt(s[field],1));
   add('line',{x1:55,x2:width-15,y1:top+48,y2:top+48,stroke:'#d9e0d5'});
   let d='',prior=null;
   for(const o of list){if(o[field]==null){prior=null;continue;}d+=`${prior&&o.segment===prior.segment?'L':'M'}${x(o)},${y(o[field])} `;prior=o;}
   add('path',{d,fill:'none',stroke:'#25836b','stroke-width':1.8});
   if(s[field]!=null)add('circle',{cx:x(s),cy:y(s[field]),r:3.5,fill:'#17392f'});
  });
  add('line',{x1:x(s),x2:x(s),y1:12,y2:204,stroke:'#bd8135','stroke-dasharray':'3 3'});
  const axisLabel=axis==='time'?`${new Date(list[0].ts*1000).toLocaleTimeString()} — ${new Date(list.at(-1).ts*1000).toLocaleTimeString()}`:`${fmt((hi-lo)/1000,2)} km travelled across loaded, GPS-matched segments`;
  add('text',{x:55,y:228,'font-size':10,fill:'#53655b'},axisLabel);
  $('track-chart-note').textContent=`${list.length} readings · click chart to locate a sample. Line breaks show missing data. Display ranges: RSRP −130…−60, SINR −10…35, RSRQ −25…−3; exact values above. Last 12,000 located radio observations; export retains full history.`;
 }
 $('track-chart').onclick=e=>{if(!chartSamples.length)return;const rect=e.currentTarget.getBoundingClientRect(),f=Math.max(0,Math.min(1,((e.clientX-rect.left)/rect.width*e.currentTarget.viewBox.baseVal.width-55)/(e.currentTarget.viewBox.baseVal.width-70)));const value=o=>axis==='time'?o.ts:o.distance;const target=value(chartSamples[0])+f*(value(chartSamples.at(-1))-value(chartSamples[0]));const nearest=chartSamples.reduce((a,b)=>Math.abs(value(a)-target)<=Math.abs(value(b)-target)?a:b);inspect(nearest,true);};
 $('track-axis').onchange=e=>{axis=e.target.value;renderInspector();};
 $('track-close').onclick=()=>selectCell('all');
 for(const el of [$('track-cell'),mapCell])el.onchange=e=>selectCell(e.target.value);
 $('track-metric').onchange=e=>{metricSelect.value=e.target.value;metricSelect.dispatchEvent(new Event('change'));};
 metricSelect.addEventListener('change',()=>{$('track-metric').value=metricSelect.value;redraw();});
 for(const id of ['inventory-band','map-band'])$(id).addEventListener('change',()=>{redraw();renderInspector();});
 $('track-fit').onclick=()=>{if(!map)return;const list=samples.filter(matched);if(list.length){setFollow(false);map.fitBounds(L.latLngBounds(list.map(s=>[s.lat,s.lon])),{padding:[50,50],maxZoom:17});}};
 $('fit-route').onclick=$('track-fit').onclick;
 window.consumeTrack=(incoming,reset)=>{
  if(reset){observations=[];selected=null;}
  if(!incoming.length&&!reset)return;
  const merged=new Map(observations.map(o=>[o.id,o]));for(const o of incoming)merged.set(o.id,o);
  observations=[...merged.values()].sort((a,b)=>a.ts-b.ts||a.id-b.id).slice(-12000);samples=M.samples(observations);
  const cells=[...new Map(samples.map(s=>[s.identity,s])).values()];const signature=cells.map(s=>s.identity).join('|');
  if(signature!==lastOptions){lastOptions=signature;for(const el of [$('track-cell'),mapCell]){el.replaceChildren(new Option('All cells','all'),...cells.map(s=>new Option(title(s),s.identity)));el.value=selectedCell;}}
  redraw();renderInspector();
 };
 const renderHunt=window.renderCellHunt;window.renderCellHunt=st=>{renderHunt(st);if(map&&cellObservationLayer&&(selectedCell!=='all'||!['hunt','upload'].includes(radioMapMode)))map.removeLayer(cellObservationLayer);};
 window.TrackUI={get samples(){return samples;},get selected(){return selected;},get selectedCell(){return selectedCell;},selectCell,inspect,redraw,refresh:renderInspector};
})();
