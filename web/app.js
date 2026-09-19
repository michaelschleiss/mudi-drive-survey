'use strict';
const $ = id => document.getElementById(id);
const fmt = (v, digits=0) => v == null ? '—' : Number(v).toFixed(digits);
const age = p => p ? Math.max(0, Date.now()/1000-p.ts) : Infinity;
const color = v => v == null ? '#899588' : v < 40 ? '#cf735b' : v < 80 ? '#dcbf65' : v < 150 ? '#57b59c' : '#1e7766';
let cursor=0, state=null, follow=true, watch=null, history=[], latestValid=null, lastGps=null, first=true, markers=[];
let map=null, tiles=null, track=null, tests=null, spots=null, me=null, accuracy=null;
let recordedTests=[];
let uploadControlPending=false,uploadControlTarget=false,uploadControlError='';
let selectedUploadMode='parked';
for(const id of ['upload-mode','drive-mode'])$(id).onchange=e=>{selectedUploadMode=e.target.value;for(const other of ['upload-mode','drive-mode'])$(other).value=selectedUploadMode;};
function renderUploadControls(){
  const running=!!state?.running;
  if(state?.running||state?.busy)selectedUploadMode=state.mode||'parked';
  for(const id of ['upload-mode','drive-mode']){$(id).value=selectedUploadMode;$(id).disabled=running||!!state?.busy||uploadControlPending;}
  for(const id of ['toggle','drive-pause']){
    const button=$(id);if(!button)continue;
    button.disabled=uploadControlPending||!state||(!running&&!!state.busy);
    button.textContent=uploadControlPending?(uploadControlTarget?'Starting uploads…':'Pausing uploads…'):(running?'Pause uploads Ⅱ':state?.busy?'Finishing current test…':selectedUploadMode==='parked'?'Verify parked spot ↗':'Start uploads ↗');
  }
}
async function toggleUploads(){
  if(uploadControlPending||!state)return;
  uploadControlPending=true;uploadControlTarget=!state.running;uploadControlError='';renderUploadControls();
  try{
    await post('/api/control',{running:uploadControlTarget,mode:selectedUploadMode});
    state.running=uploadControlTarget;state.mode=selectedUploadMode;
  }catch(e){uploadControlError=`Upload control failed: ${e.message}`;}
  finally{uploadControlPending=false;renderUploadControls();if(state)render(state);}
}
function setFollow(enabled){
  follow=enabled;
  for(const id of ['follow','drive-follow']){
    const button=$(id);if(!button)continue;
    button.textContent=follow?'Following position ●':'Follow position ↗';
    button.setAttribute('aria-pressed',String(follow));
    button.classList.toggle('following',follow);
  }
  if(follow&&me)map?.panTo(me.getLatLng(),{animate:false});
}
setFollow(follow);
if(window.L){
  map=L.map('map',{zoomControl:false,preferCanvas:true}).setView([52.515,13.39],13);
  L.control.zoom({position:'bottomright'}).addTo(map);
  tiles=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);
  tiles.on('tileerror',()=>{$('map-label').textContent='Basemap unavailable · GPS track still records';});
  track=L.layerGroup().addTo(map);tests=L.layerGroup().addTo(map);spots=L.layerGroup().addTo(map);
  map.on('dragstart',()=>setFollow(false));
} else $('map-label').textContent='Map library unavailable · acquisition continues';
function keep(layer){markers.push(layer);if(markers.length>6000){const old=markers.shift();track.removeLayer(old);tests.removeLayer(old);}}
function draw(events, reset){
  if(reset){track?.clearLayers();tests?.clearLayers();markers=[];lastGps=null;history=[];latestValid=null;recordedTests=[];}
  for(const e of events){
    if(e.kind==='gps' && map){
      if(lastGps && e.acc_m<=30 && lastGps.acc_m<=30 && e.ts-lastGps.ts<=3)keep(L.polyline([[lastGps.lat,lastGps.lon],[e.lat,e.lon]],{color:'#647c68',weight:3,opacity:.5}).addTo(track));
      lastGps=e;
    }
    if(e.kind==='probe'){
      recordedTests.push(e);if(recordedTests.length>500)recordedTests.shift();
      if(!e.error && e.mbps!=null){history.push(e.mbps);if(history.length>80)history.shift();latestValid=e;}
      if(map && e.path?.length>1)keep(L.polyline(e.path.map(p=>[p.lat,p.lon]),{color:color(e.mbps),weight:7,opacity:e.error?.3:.85})
        .bindTooltip(`${fmt(e.mbps)} Mbps averaged over ${fmt(e.duration,1)} s · recorded GPS path during test`).addTo(tests));
      if(map && e.lat!=null)keep(L.circleMarker([e.lat,e.lon],{radius:7,color:e.eligible?color(e.mbps):'#8c9186',weight:2,fillOpacity:e.eligible?.9:.2})
        .bindTooltip(`${fmt(e.mbps)} Mbps · ${e.lat.toFixed(6)}, ${e.lon.toFixed(6)}<br>${new Date((e.position_ts??e.ts)*1000).toLocaleTimeString()} · ${e.position_method==='interpolated'?'time-interpolated GPS':'GPS position'} · ±${fmt(e.acc_m)} m<br>${fmt(e.duration,1)} s test · ${fmt(e.footprint_m)} m footprint${e.eligible?'':' · unranked'}`).addTo(tests));
    }
  }
  const c=$('spark'), x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);
  const recent=history.slice(-45), max=Math.max(150,...recent);
  recent.forEach((v,i)=>{x.fillStyle='#b5d798';const h=Math.max(2,v/max*62);x.fillRect(i*c.width/45,c.height-h,c.width/45-4,h);});
  if(events.some(e=>e.kind==='probe')||reset)renderTestHistory();
  window.consumeRadio?.(events,reset);
}
function renderTestHistory(){
  const successful=recordedTests.filter(e=>!e.error&&e.mbps!=null),unlocated=successful.filter(e=>e.lat==null);
  $('test-count').textContent=recordedTests.length;
  $('test-summary').textContent=`${successful.length} completed · ${unlocated.length} without a GPS match · ${recordedTests.length-successful.length} failed`;
  document.querySelector('.spots-card').classList.toggle('has-tests',recordedTests.length>0);
  if(recordedTests.length&&!state?.best?.length){
    const empty=document.querySelector('#spots .empty');
    if(empty){empty.querySelector('h3').textContent='Uploads saved; no ranked locations yet.';empty.querySelector('p').textContent='See the recorded tests below. Tests need GPS during the measurement to appear on the map.';}
  }
  $('test-list').replaceChildren();
  for(const e of recordedTests.slice(-100).reverse()){
    const row=document.createElement('div');row.className='test-row';
    const when=document.createElement('time');when.textContent=new Date(e.ts*1000).toLocaleTimeString();
    const speed=document.createElement('strong');speed.textContent=e.error?'Failed':`${fmt(e.mbps,1)} Mbps`;
    const detail=document.createElement('small');
    const interval=`${new Date((e.start??e.ts)*1000).toLocaleTimeString()}–${new Date(e.ts*1000).toLocaleTimeString()}`;
    detail.textContent=e.error?`This test failed: ${e.error}`:e.eligible?`Mapped · ${fmt(e.footprint_m)} m footprint`:
      /GPS|midpoint/i.test(e.reason||'')?`Insufficient GPS coverage during this test (${interval}). Current GPS status is shown above.`:
      `This test is unranked: ${e.reason||'GPS not matched'}`;
    const method=document.createElement('small');method.textContent=e.method?`${e.mode==='parked'?'Parked':'Driving'} · ${fmt(e.transfer_seconds,1)} s · chunks ${fmt(e.min_mbps)}–${fmt(e.max_mbps)} Mbps · setup ${fmt(e.setup_seconds,2)} s${e.short?' · below duration target':''}`:'Earlier short-test method';
    row.append(when,speed,detail,method);$('test-list').append(row);
  }
}
function render(st){
  document.body.classList.toggle('verifying',!!st.running||!!st.busy);
  const gps=st.latest.gps, radio=st.latest.radio, traffic=st.latest.traffic, probe=st.latest.probe;
  const gpsFresh=age(gps)<=3, radioFresh=!!radio?.rat&&age(radio)<=Math.max(3,st.config.radio_interval*3);
  const uploadFailure=st.running&&probe?.error?`Last upload attempt failed: ${probe.error}`:'';
  $('connection').textContent=st.config.demo?'SIMULATION':st.running?'● VERIFYING':'● CELL HUNT';
  renderUploadControls();
  const active=st.active;
  $('probe-status').textContent=st.busy?`${st.mode==='parked'?'PARKED':'DRIVING'} ${fmt(Date.now()/1000-(active?.start||Date.now()/1000))} s / ~${active?.target||5} s`:st.running?'TESTING':'PAUSED';
  $('upload').textContent=st.busy&&active?.mbps!=null?fmt(active.mbps):latestValid?fmt(latestValid.mbps):'—';
  $('upload-detail').textContent=st.busy?`${active?.mbps!=null?'In-progress average':'Warming connection'} · ${active?.completed||0}/${active?.chunks||2} chunks${st.mode==='parked'?` · ${st.remaining} attempts left`:''}`:latestValid?`${fmt(age(latestValid))} s ago · ${fmt(latestValid.duration,1)} s test · ${fmt(latestValid.footprint_m)} m footprint`:'Waiting for a completed upload test';
  $('traffic').textContent=age(traffic)<=3?fmt(traffic.mbps):'—';
  $('usage').textContent=`${fmt(st.bytes/1048576,1)} MB test data sent`;
  $('network').textContent=radioFresh?(radio.rat||'No serving cell').replace('NR5G-','5G '):'Radio unavailable';
  $('carriers').textContent=radioFresh?(radio.ca||'Carrier details pending'):'Waiting for fresh modem data';
  const is5g=radioFresh && radio.nr_sinr!=null;
  $('sinr').parentElement.firstChild.textContent=is5g?'5G SINR ':'LTE SINR ';
  $('rsrp').parentElement.firstChild.textContent=is5g?'5G RSRP ':'LTE RSRP ';
  $('sinr').textContent=radioFresh?`${fmt(is5g?radio.nr_sinr:radio.lte_sinr)} dB`:'—';
  $('rsrp').textContent=radioFresh?`${fmt(is5g?radio.nr_rsrp:radio.lte_rsrp)} dBm`:'—';
  $('accuracy').textContent=gpsFresh?fmt(gps.acc_m):'—';
  $('gps-dot').style.background=gpsFresh&&gps.acc_m<=30?'#65a477':'#bd684d';
  $('gps-detail').textContent=gps?`${gps.source} · fix ${fmt(age(gps),1)} s ago${gpsFresh?'':' · STALE'}`:'Waiting for a location source';
  $('sample-count').textContent=`${st.cursor.toLocaleString()} observations`;
  $('speed').textContent=`${gpsFresh?fmt(gps.speed_kmh):'—'} km/h`;
  $('radio-age').textContent=radio?`Radio ${fmt(age(radio),1)} s ago · poll ${fmt(radio.poll_ms)} ms`:'Radio: waiting';
  $('cadence').textContent=`GPS on every fix · radio target ${st.config.radio_interval} s · ${st.running?'parked verification active':'passive cell hunt · uploads off'} · traffic 0.5 s`;
  $('clock').textContent=new Date().toLocaleTimeString();
  let notice=uploadControlError||(st.config.demo?'SIMULATION — synthetic route and speeds. No modem or internet upload tests are running.':
    !gpsFresh?'Waiting for fresh GPS. Cell readings keep recording; enable phone GPS to place new observations on the map.':
    gps.acc_m>30?'GPS uncertainty exceeds 30 m. Cell readings keep recording; new locations need a more accurate fix.':
    !radioFresh?'GPS is recording. Waiting for the Mudi modem; check the connection and SSH access.':
    uploadFailure||(!st.running?'Cell hunt active · GPS and radio record continuously. Uploads are off; verify a promising spot when parked.':'Parked upload verification running · GPS and radio continue recording.'));
  $('notice').textContent=notice;$('notice').classList.toggle('warn',!!uploadControlError||!st.config.demo&&(!gpsFresh||gps.acc_m>30||!radioFresh||!!uploadFailure));
  if(map && gps){
    const ll=[gps.lat,gps.lon];
    if(!me){me=L.circleMarker(ll,{radius:7,color:'#fff',weight:3,fillColor:'#17392f',fillOpacity:1}).addTo(map);accuracy=L.circle(ll,{radius:gps.acc_m,weight:1,color:'#68876d',fillOpacity:.08}).addTo(map);}
    me.setLatLng(ll);me.setStyle({fillOpacity:gpsFresh?1:.3});accuracy.setLatLng(ll).setRadius(Math.min(gps.acc_m,2000));
    if(first){map.setView(ll,16);first=false;}else if(follow&&gpsFresh)map.panTo(ll,{animate:false});
  }
  renderSpots(st.best);
  window.renderRadio?.(st);
  window.renderCellHunt?.(st);
  window.renderDrive?.(st);
}
let bestSignature='';
function renderSpots(best){
  const signature=JSON.stringify(best);if(signature===bestSignature)return;bestSignature=signature;
  $('spot-count').textContent=best.length;
  if(!best.length)return;
  $('spots').replaceChildren();spots?.clearLayers();
  best.forEach((s,i)=>{
    const button=document.createElement('button');button.className='spot';
    const rank=document.createElement('span');rank.className='rank';rank.textContent=String(i+1).padStart(2,'0');
    const info=document.createElement('span');info.className='spot-info';
    const title=document.createElement('strong');title.textContent=`${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}`;
    const sub=document.createElement('small');sub.textContent=`${s.confidence} · ${s.n} attempts · ${s.failed||0} failed · median ${fmt(s.median)} · ${s.verified||0} parked`;
    const details=document.createElement('small');details.textContent=`${(s.rat||'Network unknown').replace('NR5G-','5G ')} · ${s.ca||'bands unavailable'} · ${new Date(s.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
    info.append(title,sub,details);
    const score=document.createElement('span');score.className='spot-score';score.textContent=fmt(s.floor);
    const unit=document.createElement('small');unit.textContent='Mbps · lower quartile';score.append(unit);button.append(rank,info,score);
    button.onclick=()=>{setFollow(false);map?.setView([s.lat,s.lon],18);};
    $('spots').append(button);
    if(map && i<10)L.marker([s.lat,s.lon],{icon:L.divIcon({className:'',html:`<div class="marker-rank">${i+1}</div>`,iconSize:[27,27]})}).addTo(spots);
  });
}
async function post(path, data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(5000)});const result=await r.json();if(!r.ok)throw Error(result.error||'Request failed');return result;}
$('toggle').onclick=toggleUploads;
$('follow').onclick=()=>setFollow(!follow);
$('tiles').onclick=()=>{if(!map)return;const on=map.hasLayer(tiles);if(on)map.removeLayer(tiles);else tiles.addTo(map);$('tiles').textContent=on?'Basemap off':'Basemap on';};
$('gps').onclick=()=>{
  if(watch!=null){navigator.geolocation.clearWatch(watch);watch=null;$('gps').textContent='Use this device’s location ↗';return;}
  if(!window.isSecureContext||!navigator.geolocation){$('gps').textContent='Phone GPS requires trusted HTTPS';return;}
  watch=navigator.geolocation.watchPosition(async p=>{
    try{await post('/api/pos',{lat:p.coords.latitude,lon:p.coords.longitude,acc_m:p.coords.accuracy,ts:p.timestamp/1000,speed_kmh:p.coords.speed==null?null:p.coords.speed*3.6,source:'browser'});$('gps').textContent='Location active · stop';}
    catch(e){$('gps').textContent=e.message;}
  },e=>{$('gps').textContent=`Location: ${e.message}`;},{enableHighAccuracy:true,maximumAge:0,timeout:5000});
  $('gps').textContent='Acquiring location…';
};
async function tick(){
  try{const r=await fetch('/api/state?since='+cursor,{cache:'no-store',signal:AbortSignal.timeout(3000)});if(!r.ok)throw Error('Server error');
    state=await r.json();draw(state.events,state.reset);window.consumeTrack?.(state.observations||[],state.reset);cursor=state.cursor;render(state);
  }catch(e){$('connection').textContent='OFFLINE';$('notice').textContent='Connection lost. Displayed readings may be stale. Reconnecting…';$('notice').classList.add('warn');$('upload').textContent='—';$('traffic').textContent='—';$('accuracy').textContent='—';window.driveOffline?.();}
  setTimeout(tick,500);
}
tick();
