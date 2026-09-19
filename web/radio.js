'use strict';
const radioSection=document.createElement('section');radioSection.className='radio-panel';radioSection.id='radio-panel';
radioSection.innerHTML=`<div class="radio-title"><div><p class="eyebrow">MODEM TELEMETRY · CONTINUOUS RECORDING</p><h2>Live bands & cells</h2></div><span id="radio-live-status">Connecting</span></div>
<div class="radio-columns"><div><div class="radio-ca" id="radio-ca">Waiting for carriers</div><p class="radio-caption">Reported carrier aggregation · not a guarantee of which carriers carry uploads</p><div class="radio-serving" id="radio-serving"></div></div>
<div><h3>Cells heard here</h3><div class="radio-table-wrap"><table><thead><tr><th>Band</th><th>PCI</th><th>RSRP</th><th>RSRQ</th><th>Channel</th></tr></thead><tbody id="radio-neighbours"></tbody></table></div><p class="radio-caption">Serving cell every ~1 s; neighbour list and aggregation every fifth poll.</p></div>
<div><h3>Recent band / cell changes</h3><div id="radio-changes"></div><p class="radio-caption">Changes observed in the loaded recording. Recorded even when GPS or upload tests are off.</p></div></div>`;
document.querySelector('.metrics').after(radioSection);
const radioDock=document.createElement('aside');radioDock.id='radio-dock';radioDock.innerHTML=`<div class="dock-heading"><b>LIVE RADIO</b><span id="dock-radio-age">Waiting</span></div><div id="dock-ca">—</div><div id="dock-serving"></div><div class="dock-neighbour-title">Neighbour cells <span id="dock-neighbour-count">0</span></div><div id="dock-neighbours"></div><div class="dock-controls"><button id="show-radio">All radio details</button><button id="fit-route">Fit recorded route</button></div><label class="radio-layer-label">Map layer <select id="radio-layer"><option value="hunt">All-band hunt</option><option value="upload">Upload tests</option><option value="sinr">Radio SINR</option><option value="bands">Serving bands</option></select></label><label class="radio-layer-label">Band <select id="map-band"><option value="all">All bands</option></select></label><div id="radio-layer-note">Uploads need GPS during each test.</div>`;
document.getElementById('drive-display').append(radioDock);
let radioPrevious=null,radioChanges=[],radioObservations=[],radioMapLayer=null,radioMapMode='hunt',radioChangeVersion=0,radioRenderedVersion=-1;
let radioLayerChosen=false;
let huntBand='all';
const scoutPriority=(w,r,s)=>w!=null&&w>=20&&r!=null&&r>=-105&&s!=null&&s>=10?2:(w!=null&&w>=20)||(r!=null&&r>=-105&&s!=null&&s>=10)?1:0;
const scoutColor=p=>p===2?'#197968':p===1?'#c28e31':'#95a99c';
const radioText=(id,value)=>{document.getElementById(id).textContent=value;};
function radioCellId(r){return [r.rat,r.lte_plmn,r.lte_cell_id,r.lte_arfcn,r.lte_band,r.lte_pci,r.nr_cell_id,r.nr_arfcn,r.nr_band,r.nr_pci,r.ca].join('|');}
function bandColor(r){const s=r.nr_band||r.lte_band||'';let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))%360;return `hsl(${h},65%,40%)`;}
function paintRadioObservation(obs){
 if(!map||radioMapMode==='upload')return;
 if(!radioMapLayer)radioMapLayer=L.layerGroup().addTo(map);
 const r=obs.radio,si=r.nr_sinr??r.lte_sinr;
 if(huntBand!=='all'&&r.nr_band!==huntBand&&r.lte_band!==huntBand)return;
 const priority=Math.max(...['lte','nr'].filter(p=>r[p+'_band']&&(huntBand==='all'||r[p+'_band']===huntBand)).map(p=>scoutPriority(r[p+'_bw'],r[p+'_rsrp'],r[p+'_sinr'])),0);
 const tint=radioMapMode==='hunt'?scoutColor(priority):radioMapMode==='bands'?bandColor(r):si==null?'#899588':si<5?'#c4664d':si<15?'#c49c38':'#25866c';
 const label=document.createElement('div');label.textContent=`${new Date(obs.ts*1000).toLocaleTimeString()} · ${r.ca||r.nr_band||r.lte_band||'—'} · SINR ${fmt(si)} dB · LTE PCI ${fmt(r.lte_pci)} · 5G PCI ${fmt(r.nr_pci)} · Cell ${r.nr_cell_id||r.lte_cell_id||'ID unavailable'} · Channel ${fmt(r.nr_arfcn??r.lte_arfcn)} · GPS ±${fmt(obs.acc_m)} m · receiver position, not mast`;
 L.circleMarker([obs.lat,obs.lon],{radius:radioMapMode==='hunt'&&priority===2?7:4,color:tint,weight:0,fillColor:tint,fillOpacity:.85}).bindTooltip(label).addTo(radioMapLayer);
 const layers=radioMapLayer.getLayers();if(layers.length>6000)radioMapLayer.removeLayer(layers[0]);
}
window.consumeRadio=(events,reset)=>{
 if(reset){radioPrevious=null;radioChanges=[];radioObservations=[];radioMapLayer?.clearLayers();radioChangeVersion++;}
 for(const e of events){
  if(e.kind==='radio'&&e.rat){
   if(!radioPrevious||radioCellId(e)!==radioCellId(radioPrevious)){radioChanges.push(e);if(radioChanges.length>100)radioChanges.shift();radioChangeVersion++;}
   radioPrevious=e;
  }
  if(e.kind==='gps'&&e.acc_m<=30&&radioPrevious&&Math.abs(e.ts-radioPrevious.ts)<=3){const obs={...e,radio:radioPrevious};radioObservations.push(obs);if(radioObservations.length>6000)radioObservations.shift();paintRadioObservation(obs);}
 }
 if(!radioLayerChosen&&radioObservations.length&&!recordedTests.some(e=>e.lat!=null)){
  const select=document.getElementById('radio-layer');select.value='hunt';select.dispatchEvent(new Event('change'));
 }
};
function servingCard(c){
 const card=document.createElement('div');card.className='radio-carrier';
 const title=document.createElement('strong');title.textContent=`${c.label} · ${c.band||'not reported'}`;
 const values=document.createElement('span');values.textContent=`PCI ${fmt(c.pci)} · ${fmt(c.bw_mhz)} MHz`;
 const signal=document.createElement('b');signal.textContent=`RSRP ${fmt(c.rsrp)} dBm · SINR ${fmt(c.sinr)} dB`;
 const quality=document.createElement('small');quality.textContent=`RSRQ ${fmt(c.rsrq)} dB · ${c.plmn||'PLMN —'} · Cell ${c.cell_id||'ID not reported'} · Channel ${fmt(c.arfcn)} · TAC ${c.tac||'—'}`;
 card.append(title,values,signal,quality);return card;
}
// QENG names only the serving carriers; QCAINFO lists every aggregated one.
// Identified carriers keep their QENG values, secondaries show what QCAINFO
// reports and leave the rest blank rather than borrowing the serving cell's.
function carrierCards(r){
 const serving=['lte','nr'].filter(p=>r[p+'_band']).map(p=>({
  label:p==='lte'?'LTE':r.rat==='NR5G-SA'?'5G SA':'5G',band:r[p+'_band'],pci:r[p+'_pci'],bw_mhz:r[p+'_bw'],
  rsrp:r[p+'_rsrp'],sinr:r[p+'_sinr'],rsrq:r[p+'_rsrq'],plmn:r[p+'_plmn'],cell_id:r[p+'_cell_id'],
  arfcn:r[p+'_arfcn'],tac:r[p+'_tac']}));
 if(!r.carriers?.length)return serving;
 const byBand=new Map(serving.map(c=>[c.band,c]));
 return r.carriers.map(c=>byBand.get(c.band)||{
  label:(c.rat==='nr'?'5G':'LTE')+' '+(c.role||'SCC'),band:c.band,pci:c.pci,bw_mhz:c.bw_mhz,
  rsrp:c.rsrp,sinr:null,rsrq:c.rsrq,plmn:null,cell_id:null,arfcn:c.arfcn,tac:null});
}
window.renderRadio=st=>{
 const r=st.latest.radio||{},fresh=!!r.rat&&age(r)<=Math.max(3,st.config.radio_interval*3);
 radioText('radio-live-status',fresh?`${r.rat.replace('NR5G-','5G ')} · ${fmt(age(r),1)} s ago · ${fmt(r.poll_ms)} ms poll`:'Modem unavailable / reading stale');
 radioText('dock-radio-age',fresh?`${fmt(age(r),1)} s ago`:'STALE / NO MODEM');radioDock.classList.toggle('radio-stale',!fresh);
 radioText('radio-ca',r.ca?r.ca.replaceAll('+',' + '):'No aggregation reported');radioText('dock-ca',r.ca?r.ca.replaceAll('+',' + '):'No aggregation reported');
 const cards=carrierCards(r);
 for(const id of ['radio-serving','dock-serving']){const target=document.getElementById(id);target.replaceChildren(...cards.map(servingCard));}
 const neighbours=r.neighbours||[];radioText('dock-neighbour-count',neighbours.length);
 const tbody=document.getElementById('radio-neighbours');tbody.replaceChildren();
 const rows=[...(r.lte_band?[{band:r.lte_band,pci:r.lte_pci,rsrp:r.lte_rsrp,rsrq:r.lte_rsrq,serving:true}]:[]),...neighbours.filter(n=>!(n.band===r.lte_band&&n.pci===r.lte_pci))].sort((a,b)=>(b.rsrp??-999)-(a.rsrp??-999));
 for(const n of rows){const tr=document.createElement('tr');for(const value of [n.band+(n.serving?' ●':''),fmt(n.pci),fmt(n.rsrp)+' dBm',fmt(n.rsrq)+' dB',fmt(n.earfcn)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}tbody.append(tr);}
 const small=document.getElementById('dock-neighbours');small.replaceChildren();
 for(const n of rows.filter(n=>!n.serving).slice(0,4)){const line=document.createElement('div');const label=document.createElement('span');label.textContent=`${n.band} · PCI ${fmt(n.pci)}`;const value=document.createElement('b');value.textContent=`${fmt(n.rsrp)} dBm`;line.append(label,value);small.append(line);}
 small.hidden=!small.children.length;radioDock.querySelector('.dock-neighbour-title').hidden=small.hidden;
 if(radioRenderedVersion!==radioChangeVersion){
  const target=document.getElementById('radio-changes');target.replaceChildren();
  for(const e of radioChanges.slice(-15).reverse()){
   const entry=document.createElement('div');entry.className='radio-change';
   const time=document.createElement('time');time.textContent=new Date(e.ts*1000).toLocaleTimeString();
   const band=document.createElement('strong');band.textContent=e.ca||[e.lte_band,e.nr_band].filter(Boolean).join(' + ');
   const cells=document.createElement('small');cells.textContent=`LTE PCI ${fmt(e.lte_pci)} · 5G PCI ${fmt(e.nr_pci)}`;entry.append(time,band,cells);target.append(entry);
  }
  radioRenderedVersion=radioChangeVersion;
 }
};
document.getElementById('show-radio').onclick=()=>{setViewMode('dashboard');radioSection.scrollIntoView({behavior:'smooth',block:'start'});};
document.getElementById('fit-route').onclick=()=>{
 if(!map)return;const group=L.featureGroup([...track.getLayers(),...(radioMapLayer?.getLayers()||[]),...(cellObservationLayer?.getLayers()||[])]);if(me)group.addLayer(L.circleMarker(me.getLatLng()));
 const bounds=group.getBounds();if(bounds.isValid()){setFollow(false);map.fitBounds(bounds,{padding:[60,60],maxZoom:17});}
};
document.getElementById('radio-layer').onchange=e=>{
 radioLayerChosen=true;
 radioMapMode=e.target.value;radioMapLayer?.clearLayers();
 if(map){if(radioMapMode==='upload'){if(!map.hasLayer(tests))tests.addTo(map);if(!map.hasLayer(spots))spots.addTo(map);}else {map.removeLayer(tests);map.removeLayer(spots);}}
 for(const obs of radioObservations)paintRadioObservation(obs);
 document.querySelector('.legend').style.display=radioMapMode==='upload'?'':'none';
 radioText('radio-layer-note',radioMapMode==='hunt'?'Green: ≥20 MHz, RSRP ≥−105 and SINR ≥10 together. Amber: width or signal criterion. Grey: other/unknown. Scouting clues, not Mbps or mast locations.':radioMapMode==='upload'?'Uploads need GPS during each test.':radioMapMode==='sinr'?'SINR: red <5 · amber 5–15 · green ≥15 dB. Hover for bands.':'Colour groups serving bands. Hover a point for bands, PCI and signal.');
};

let cellInventorySignature='',cellObservationLayer=null,cellPositionSignature='';
window.renderCellHunt=st=>{
 const allCells=st.cells||[];
 for(const id of ['inventory-band','map-band']){const select=document.getElementById(id),bands=[...new Set(allCells.map(c=>c.band))].sort();if(select.dataset.bands!==bands.join(',')){select.replaceChildren(new Option('All bands','all'),...bands.map(b=>new Option(b,b)));select.dataset.bands=bands.join(',');}select.value=huntBand;}
 const cells=allCells.filter(c=>huntBand==='all'||c.band===huntBand), signature=huntBand+JSON.stringify(cells);if(signature===cellInventorySignature)return;cellInventorySignature=signature;
 radioText('cell-count',cells.length);
 const positionSignature=JSON.stringify(cells.map(c=>[c.identity,c.candidate_position||c.best_position,c.hunt_priority]));
 if(map&&positionSignature!==cellPositionSignature){
  cellPositionSignature=positionSignature;
  if(!cellObservationLayer)cellObservationLayer=L.layerGroup().addTo(map);
  cellObservationLayer.clearLayers();
  for(const c of cells){const p=c.candidate_position||c.best_position;if(!p)continue;
   const label=document.createElement('div');label.textContent=`${c.band} · PCI ${fmt(c.pci)} · candidate GPS observation · RSRP ${fmt(p.rsrp)} dBm · GPS ±${fmt(p.acc_m)} m · not a mast location`;
   L.circleMarker([p.lat,p.lon],{radius:c.hunt_priority===2?10:6,color:scoutColor(c.hunt_priority),weight:3,fillOpacity:.25}).bindTooltip(label).addTo(cellObservationLayer);
  }
 }
 const list=document.getElementById('cell-inventory');list.replaceChildren();
 if(!cells.length){list.textContent='No cell observations yet. Connect the Mudi; GPS and radio recording are automatic.';return;}
 for(const c of cells){
  const item=document.createElement('button');item.dataset.identity=window.TrackModel?TrackModel.normalize(c.identity):c.identity;item.className='cell-candidate'+(c.hunt_priority===2?' target-cell':'');
  const title=document.createElement('strong');title.textContent=`${c.band} · ${fmt(c.bandwidth_mhz)} MHz · PCI ${fmt(c.pci)}`;
  const identity=document.createElement('small');identity.textContent=`${c.plmn||'PLMN unavailable'} · ${c.cell_id?'Cell '+c.cell_id:c.identity_quality} · channel ${fmt(c.channel)}`;
  const detail=document.createElement('span');detail.textContent=`Best RSRP ${fmt(c.best_rsrp)} dBm · best SINR ${fmt(c.best_sinr)} dB · ${c.located}/${c.observations} located`;
  const location=document.createElement('small');const p=c.candidate_position||c.best_position;
  location.textContent=p?`Candidate observation: ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} · ±${fmt(p.acc_m)} m · ${new Date(p.radio_ts*1000).toLocaleString()}`:'No accurate GPS match yet';
  const evidence=document.createElement('small');evidence.textContent=p?`${c.hunt_priority===2?'Width + signal':c.hunt_priority===1?'Width or signal':'Other / incomplete evidence'} · ${fmt(p.bandwidth_mhz)} MHz · RSRP ${fmt(p.rsrp)} · SINR ${fmt(p.sinr)} at this fix. Reported CA: ${p.ca||'unknown'}; LTE anchor: ${p.lte_anchor||p.lte_anchor_band||'unknown'}. Uplink aggregation unconfirmed.`:'No located candidate yet';
  item.classList.toggle('active-cell',window.TrackUI?.selectedCell===item.dataset.identity);
  item.append(title,identity,detail,evidence,location);item.disabled=!p;
  if(p)item.onclick=()=>{const sample=window.TrackUI?.samples.find(s=>s.identity===TrackModel.normalize(c.identity)&&s.ts===p.radio_ts);if(sample)window.TrackUI.inspect(sample);else window.selectTrackCell?.(c.identity);setViewMode('map');setFollow(false);map?.setView([p.lat,p.lon],17);if(map){const note=document.createElement('div');note.textContent=`${c.band} · PCI ${fmt(c.pci)} · candidate GPS observation, not mast location · RSRP ${fmt(p.rsrp)} dBm`;L.popup().setLatLng([p.lat,p.lon]).setContent(note).openOn(map);}};
  list.append(item);
 }
};

for(const id of ['inventory-band','map-band'])document.getElementById(id).onchange=e=>{huntBand=e.target.value;cellInventorySignature='';cellPositionSignature='';radioMapLayer?.clearLayers();for(const obs of radioObservations)paintRadioObservation(obs);if(state)window.renderCellHunt(state);};
