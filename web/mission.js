'use strict';
(() => {
 const rail=$('hunt-sidebar'),heading=rail.querySelector('.hunt-heading');
 heading.innerHTML='<div class="mission-wordmark">Field hunt</div><h2>Find a place worth returning to.</h2>';
 const mission=document.createElement('section');mission.id='hunt-mission';
 mission.innerHTML=`<div class="mission-top"><span id="mission-phase">Discover</span><button id="mission-end" hidden>End hunt</button></div><h3 id="mission-name">Choose your target</h3><p id="mission-identity">Select a recorded cell below, then start a hunt.</p><div id="mission-contact">No active target</div><div id="mission-signals"><div><span>Strength</span><strong id="mission-rsrp">—</strong><small>dBm RSRP</small></div><div><span>Cleanliness</span><strong id="mission-sinr">—</strong><small>dB SINR</small></div></div><div id="mission-trend">Signal trend needs a target.</div><div id="mission-evidence"><div><span>Located readings</span><b id="mission-count">—</b></div><div><span>Mast position</span><b>Unresolved</b></div></div><p id="mission-guidance">Find a promising cell using its width and signal. Upload capacity remains unverified.</p><button id="mission-start" class="mission-primary">Start hunt on selected cell</button><div id="mission-actions" hidden><button id="mission-spot" class="mission-primary">Show promising observation</button><div class="mission-secondary"><button id="mission-fit">Fit target trail</button><button id="mission-return">Return to target</button></div><p id="mission-distance">GPS needed for distance to the observed spot.</p></div><p id="mission-error" role="status"></p>`;
 heading.after(mission);
 const empty=$('hunt-analysis-empty');empty.innerHTML='<div><strong id="mission-analysis-title">Inspect the evidence</strong><span>Strength, cleanliness and quality on the same GPS track.</span></div><button id="mission-analysis-toggle">Open signal charts</button>';
 let target=null,lastTarget=null,waypoint=null,waypointIdentity=null,pending=false,revision=0;
 function setExpanded(open){if(document.body.classList.contains('analysis-expanded')===open)return;document.body.classList.toggle('analysis-expanded',open);$('mission-analysis-toggle').textContent=open?'Close signal charts':'Open signal charts';requestAnimationFrame(()=>{map?.invalidateSize();TrackUI?.refresh?.();});}
 $('mission-analysis-toggle').onclick=()=>{if(TrackUI.selectedCell==='all'&&target)TrackUI.selectCell(target.identity);setExpanded(!document.body.classList.contains('analysis-expanded'));};
 const chartClose=$('track-close').onclick;$('track-close').onclick=()=>{setExpanded(false);chartClose();};
 function elapsed(ts){const seconds=Math.max(0,Math.floor(Date.now()/1000-ts));return seconds<60?`${seconds} s`:seconds<3600?`${Math.floor(seconds/60)} min`:`${Math.floor(seconds/3600)} h ${Math.floor(seconds%3600/60)} min`;}
 function selectedCandidate(){return state?.cells?.find(c=>TrackModel.normalize(c.identity)===TrackUI.selectedCell);}
 async function commit(identity){
  if(pending)return;pending=true;$('mission-error').textContent='';$('mission-start').disabled=true;
  try{const result=await post('/api/target',{identity});state.target=result.target;renderMission(state);}
  catch(e){$('mission-error').textContent=`Could not update target: ${e.message}`;}
  finally{pending=false;if(state)renderMission(state);}
 }
 $('mission-start').onclick=()=>{const chosen=selectedCandidate();if(chosen)commit(chosen.identity);};
 $('mission-end').onclick=()=>commit(null);
 $('mission-return').onclick=()=>{if(target)TrackUI.selectCell(target.identity);};
 $('mission-fit').onclick=()=>{if(!target)return;TrackUI.selectCell(target.identity);const points=TrackUI.samples.filter(s=>s.identity===TrackModel.normalize(target.identity));if(map&&points.length){setFollow(false);map.fitBounds(L.latLngBounds(points.map(s=>[s.lat,s.lon])),{padding:[45,45],maxZoom:17});}};
 $('mission-spot').onclick=()=>{
  const p=target?.candidate_position||target?.best_position;if(!p||!map)return;
  TrackUI.selectCell(target.identity);setFollow(false);
  if(waypoint)map.removeLayer(waypoint);waypoint=L.layerGroup().addTo(map);waypointIdentity=target.identity;
  const text=document.createElement('div');text.className='mission-waypoint-label';text.textContent='Promising observation';
  L.circleMarker([p.lat,p.lon],{radius:11,color:'#b97916',weight:3,fillColor:'#fff3d5',fillOpacity:1}).bindTooltip(text,{permanent:true,direction:'top',offset:[0,-12]}).addTo(waypoint);
  L.circle([p.lat,p.lon],{radius:p.acc_m||0,color:'#b97916',weight:1,fillOpacity:.08}).addTo(waypoint);
  map.setView([p.lat,p.lon],17);
 };
 function renderMission(st){
  target=(st.cells||[]).find(c=>c.identity===st.target)||null;
  const chosen=selectedCandidate();
  $('mission-analysis-toggle').disabled=TrackUI.selectedCell==='all'&&!target;
  if(lastTarget!==st.target){lastTarget=st.target;if(waypoint){map?.removeLayer(waypoint);waypoint=null;}waypointIdentity=null;if(target){TrackUI.selectCell(target.identity);setExpanded(false);}revision++;}
  const active=!!target,display=target||chosen;
  mission.classList.toggle('has-target',active);document.body.classList.toggle('hunt-committed',active);
  $('mission-phase').textContent=active?'Hunting one cell':chosen?'Candidate selected':'Discover';
  $('mission-name').textContent=display?`${display.band} / ${fmt(display.bandwidth_mhz)} MHz`:'Choose your target';
  $('mission-identity').textContent=display?`${display.plmn||'Operator unknown'} • ${display.cell_id?'Cell '+display.cell_id:'PCI '+fmt(display.pci)+' · signature only'}${display.channel!=null?' · channel '+fmt(display.channel):''}`:'Select a recorded cell below, then start a hunt.';
  $('mission-end').hidden=!active;$('mission-end').disabled=pending;
  $('mission-actions').hidden=!active;$('mission-signals').hidden=!active;$('mission-evidence').hidden=!active;
  $('mission-start').hidden=active&&(!chosen||chosen.identity===target.identity);
  $('mission-start').disabled=pending||!chosen;
  $('mission-start').textContent=active?'Switch hunt to inspected cell':'Start hunt on selected cell';
  document.querySelector('.drive-best').firstChild.textContent=active?'Target observations ':'All-cell observations ';
  document.body.classList.toggle('hunt-needs-gps',!st.latest.gps||age(st.latest.gps)>3||st.latest.gps.acc_m>30);
  if(!active){$('mission-contact').textContent='No active target';$('mission-trend').textContent='Your target will stay selected through cell changes.';$('mission-guidance').textContent='Width and signal identify candidates. A parked upload test checks the receiving location.';return;}
  const r=st.latest.radio||{},prefix=target.identity.split(':')[0];
  const radioFresh=!!r.rat&&age(r)<=Math.max(3,st.config.radio_interval*3);
  const observed=radioFresh&&r[prefix+'_band']&&TrackModel.normalize(TrackModel.key(r,prefix))===TrackModel.normalize(target.identity);
  const gps=st.latest.gps,gpsOK=!!gps&&age(gps)<=3&&gps.acc_m<=30;
  $('mission-contact').textContent=!radioFresh?'Radio stale':observed?'Target observed now':`Target not observed · last seen ${elapsed(target.last_seen)} ago`;
  $('mission-contact').dataset.state=observed?'live':'lost';
  $('mission-rsrp').textContent=observed?fmt(r[prefix+'_rsrp']):'—';$('mission-sinr').textContent=observed?fmt(r[prefix+'_sinr']):'—';
  const trend=observed?TrackModel.trend(TrackUI.samples,target.identity,Date.now()/1000):null;
  $('mission-trend').textContent=trend?`${trend.label} · ${trend.change>=0?'+':''}${fmt(trend.change,1)} dB over 10 s`:'Trend unavailable · collect 10 s of continuous, GPS-matched target readings';
  $('mission-count').textContent=target.located.toLocaleString();
  $('mission-guidance').textContent=!gpsOK?'Restore phone GPS to locate new observations. Radio recording continues.':!observed?'The target stays locked. Review where it was previously observed.':trend?.label==='Strengthening'?'Signal is strengthening. This does not establish the direction of the mast.':'Collect another stretch of this cell’s signal. Inspect another approach before choosing a parked test spot.';
  const point=target.candidate_position||target.best_position;$('mission-spot').disabled=!point;
  $('mission-distance').textContent=point&&gpsOK?`${fmt(TrackModel.meters(gps,point))} m straight-line to recorded observation · GPS ±${fmt(gps.acc_m)} m. Receiver position, not mast.`:point?'Distance paused until GPS is fresh. The saved spot is a receiver observation.':'No accurate observation location saved yet.';
  $('mission-analysis-title').textContent=`Signal evidence for ${target.band} · PCI ${fmt(target.pci)}`;
  // Phone stays focused on the committed target rather than an unrelated serving cell.
  if(driveView==='phone'&&!st.running){$('drive-label').textContent=`Hunt target · ${fmt(target.bandwidth_mhz)} MHz · ${target.cell_id?'Cell '+target.cell_id:'PCI '+fmt(target.pci)+' · partial ID'}`;$('drive-best').textContent=target.located.toLocaleString();$('drive-upload').textContent=target.band;$('drive-measurement').textContent=observed?`RSRP ${fmt(r[prefix+'_rsrp'])} dBm · SINR ${fmt(r[prefix+'_sinr'])} dB`:$('mission-contact').textContent;}
 }
 const baseRender=window.renderDrive;window.renderDrive=st=>{baseRender(st);renderMission(st);};
 // Selection may change between state polls; keep the commitment button responsive.
 const selectBase=TrackUI.selectCell;TrackUI.selectCell=id=>{selectBase(id);if(state)renderMission(state);};
 document.addEventListener('click',()=>{requestAnimationFrame(()=>{if(state)renderMission(state);});});
 const hiddenObserver=new MutationObserver(()=>{if($('track-inspector').hidden)setExpanded(false);});hiddenObserver.observe($('track-inspector'),{attributes:true,attributeFilter:['hidden']});
 if(state)renderMission(state);
 window.HuntMission={get target(){return target;},render:renderMission,setExpanded};
})();


// Live driving is the default. Historical inspection is a separate, explicit view.
(() => {
 const panel=document.createElement('section');panel.id='live-hunt';
 panel.innerHTML=`<div class="mission-top"><span>Live driving hunt</span><button id="hunt-sound" aria-pressed="false">Enable chime</button></div><h3 id="live-bands">Waiting for modem</h3><p id="live-identity"></p><div id="live-detection" role="status">Listening for promising cells</div><div id="live-signals"></div><p id="live-capture"></p><div class="live-ta"><strong>Timing advance</strong><span>Unavailable · no validated live feed</span><p>Distance rings will require TA matched to this cell and a fresh GPS fix. LTE anchor TA cannot locate an NR cell by itself.</p></div><p id="live-last">Promising encounters are saved with the radio survey.</p><button id="hunt-review">Inspect recorded cells</button>`;
 $('hunt-mission').before(panel);
 const back=document.createElement('button');back.id='hunt-live-return';back.textContent='← Resume live driving';$('hunt-mission').before(back);
 let live=true,audio=null,sound=false,lastSample=null,lastAlertAt=-Infinity;
 const seen=new Map();let alertCount=0,lastNotice='',wasSA=false,lastSAAlert=-Infinity;
 function setLive(value){live=value;document.body.classList.toggle('live-hunting',value);back.hidden=value;if(value){HuntMission.setExpanded(false);TrackUI.selectCell('all');}if(state)renderLive(state);}
 $('hunt-review').onclick=()=>setLive(false);back.onclick=()=>setLive(true);
 const phoneSound=document.createElement('button');phoneSound.id='phone-hunt-sound';phoneSound.textContent='Enable detection chime';phoneSound.setAttribute('aria-pressed','false');document.querySelector('.drive-actions').prepend(phoneSound);phoneSound.onclick=()=>$('hunt-sound').click();$('hunt-sound').title=phoneSound.title='Two tones: promising cell. Three rising tones: 5G standalone. SA does not guarantee upload speed.';
 $('hunt-sound').onclick=async()=>{try{if(!audio)audio=new (window.AudioContext||window.webkitAudioContext)();if(!sound)await audio.resume();sound=!sound;$('hunt-sound').textContent=sound?'Chime on':'Enable chime';$('hunt-sound').setAttribute('aria-pressed',String(sound));phoneSound.textContent=sound?'Detection chime on':'Enable detection chime';phoneSound.setAttribute('aria-pressed',String(sound));if(sound)chime(state?.latest.radio?.rat==='NR5G-SA'&&age(state.latest.radio)<=3?'sa':'candidate');}catch{$('hunt-sound').textContent='Audio unavailable';sound=false;}};
 function chime(kind='candidate'){if(!sound||audio?.state!=='running')return;(kind==='sa'?[784,988,1319]:[660,880]).forEach((hz,i)=>{const o=audio.createOscillator(),g=audio.createGain(),t=audio.currentTime+i*.15;o.frequency.value=hz;g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(.08,t+.015);g.gain.exponentialRampToValueAtTime(.001,t+.12);o.connect(g);g.connect(audio.destination);o.start(t);o.stop(t+.13);});}
 function renderLive(st){
  if(!live)return;
  const r=st.latest.radio||{},now=Date.now()/1000,fresh=!!r.rat&&now-r.ts>=0&&now-r.ts<=Math.max(3,st.config.radio_interval*3);
  const isSA=fresh&&r.rat==='NR5G-SA';
  const prefixes=fresh?['lte','nr'].filter(p=>r[p+'_band']):[];
  const carriers=prefixes.map(p=>({prefix:p,band:r[p+'_band'],width:r[p+'_bw'],rsrp:r[p+'_rsrp'],sinr:r[p+'_sinr'],rsrq:r[p+'_rsrq'],pci:r[p+'_pci'],identity:TrackModel.key(r,p)}));
  const candidates=carriers.filter(c=>['n77','n78','n79'].includes(c.band)||(c.width>=20&&c.rsrp!=null&&c.rsrp>=-105&&c.sinr!=null&&c.sinr>=10));
  const gps=st.latest.gps,gpsOK=gps&&now-gps.ts>=0&&now-gps.ts<=3&&gps.acc_m<=30;
  $('live-bands').textContent=carriers.length?carriers.map(c=>`${c.band} (${fmt(c.width)} MHz)`).join(' + '):'Waiting for fresh radio';
  $('live-identity').textContent=carriers.map(c=>`${c.band} · ${r[c.prefix+'_cell_id']?'Cell '+r[c.prefix+'_cell_id']:'PCI '+fmt(c.pci)+' · partial ID'}`).join(' / ');
  panel.classList.toggle('promising',candidates.length>0);panel.classList.toggle('standalone',isSA);
  $('live-detection').textContent=isSA?'5G SA connected · standalone · upload speed unverified':candidates.length?`Promising ${candidates.map(c=>c.band).join(' + ')} detected · upload capacity unverified`:fresh?'Recording current connection · watching for candidates':'Radio stale · waiting for new readings';
  const signalBox=$('live-signals');signalBox.replaceChildren();
  for(const c of carriers){const row=document.createElement('div');row.className='live-signal-row';for(const text of [c.band,`${fmt(c.rsrp)} dBm RSRP`,`${fmt(c.sinr)} dB SINR`,`${fmt(c.rsrq)} dB RSRQ`]){const span=document.createElement('span');span.textContent=text;row.append(span);}signalBox.append(row);}
  $('live-capture').textContent=gpsOK?`GPS ±${fmt(gps.acc_m)} m · current radio observations mapped automatically`:'GPS missing or stale · radio saved, new positions paused';
  // Only a new radio sample can trigger an alert. Historical replay never chimes.
  if(fresh&&(lastSample===null||r.ts>lastSample)){
   lastSample=r.ts;
   const enteredSA=isSA&&!wasSA&&now-lastSAAlert>=60;
   wasSA=isSA;
   const newly=candidates.filter(c=>now-(seen.get(c.identity)??-Infinity)>=120);
   if(enteredSA){lastSAAlert=now;lastAlertAt=now;alertCount++;lastNotice=`5G SA detected · ${r.nr_band||'NR band unknown'} · PCI ${fmt(r.nr_pci)} at ${new Date(r.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})} · upload speed unverified`;chime('sa');}
   else if(newly.length&&now-lastAlertAt>=15){lastAlertAt=now;alertCount++;lastNotice=`Last detection: ${newly.map(c=>c.band+' · PCI '+fmt(c.pci)).join(' + ')} at ${new Date(r.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;chime();}
   for(const c of candidates)seen.set(c.identity,now);
  }
  $('live-last').textContent=lastNotice||'All radio observations are saved automatically. No target selection needed.';
  $('mission-analysis-title').textContent='Signal evidence along your route';
  document.body.classList.toggle('hunt-needs-gps',!gpsOK);
  if(driveView==='phone'&&!st.running){$('drive-label').textContent=isSA?'5G SA connected · speed unverified':candidates.length?'Promising connection · capacity unverified':'Live driving hunt';$('drive-upload').textContent=carriers.map(c=>c.band).join(' + ')||'—';$('drive-upload').classList.add('live-band-readout');$('drive-measurement').textContent=carriers.map(c=>`${c.band} · PCI ${fmt(c.pci)} · ${fmt(c.rsrp)} dBm · SINR ${fmt(c.sinr)} dB`).join(' / ')||'Waiting for fresh radio';document.querySelector('.drive-best').firstChild.textContent='All-cell observations ';$('drive-best').textContent=(st.cells||[]).reduce((n,c)=>n+c.located,0);}
 }
 const previous=window.renderDrive;window.renderDrive=st=>{previous(st);renderLive(st);};
 // Mission's selection refreshes must also preserve the live readout.
 const previousRender=HuntMission.render;HuntMission.render=st=>{previousRender(st);renderLive(st);};
 document.addEventListener('click',()=>requestAnimationFrame(()=>{if(state)renderLive(state);}));
 document.addEventListener('visibilitychange',()=>{if(document.hidden&&audio)audio.suspend();else if(sound&&audio)audio.resume().catch(()=>{});});
 window.LiveHunt={render:renderLive,setLive,get alerts(){return alertCount;}};
 setLive(true);
})();
