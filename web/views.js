'use strict';
let driveView='dashboard', screenLock=null;
async function keepAwake(){
  if(driveView==='dashboard'||document.visibilityState!=='visible'||screenLock)return;
  try{
    screenLock=await navigator.wakeLock.request('screen');
    $('drive-awake').textContent='Screen stays awake · keep this page in the foreground';
    screenLock.addEventListener('release',()=>{screenLock=null;$('drive-awake').textContent='Keep this page open and the phone awake';});
  }catch{$('drive-awake').textContent='Keep this page open and disable automatic screen locking';}
}
function setViewMode(view){
  driveView=['map','phone'].includes(view)?view:'dashboard';
  document.body.dataset.view=driveView;
  document.body.classList.remove('details-open');$('drive-details').setAttribute('aria-expanded','false');
  $('drive-display').hidden=driveView==='dashboard';
  const url=new URL(location.href);if(driveView==='dashboard')url.searchParams.delete('view');else url.searchParams.set('view',driveView);
  window.history.replaceState(null,'',url);
  if(driveView==='dashboard'){screenLock?.release();if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});}
  else keepAwake();
  requestAnimationFrame(()=>{map?.invalidateSize();if(follow&&me)map?.panTo(me.getLatLng(),{animate:false});});
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>setViewMode(button.dataset.view)));
$('drive-fullscreen').onclick=async()=>{
  try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}
  catch{$('drive-fullscreen').textContent='Use browser full-screen';}
};
document.addEventListener('fullscreenchange',()=>{$('drive-fullscreen').textContent=document.fullscreenElement?'Leave full screen ⛶':'Full screen ⛶';map?.invalidateSize();});
$('drive-details').onclick=()=>{const open=document.body.classList.toggle('details-open');$('drive-details').setAttribute('aria-expanded',String(open));};
$('drive-follow').onclick=()=>setFollow(!follow);
$('drive-location').onclick=()=>{$('gps').click();keepAwake();};
$('drive-pause').onclick=()=>{toggleUploads();keepAwake();};
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')keepAwake();});
window.renderDrive=st=>{
  const radio=st.latest.radio||{}, fresh=!!radio.rat&&age(radio)<=Math.max(3,st.config.radio_interval*3);
  const nr=fresh&&radio.nr_band, signal=nr?radio.nr_sinr:radio.lte_sinr;
  $('drive-label').textContent=st.running?'PARKED VERIFICATION · Mbps ↑':'CELL HUNT · ALL BANDS';
  $('drive-upload').textContent=st.running?$('upload').textContent:fresh?(nr||radio.lte_band||'—'):'—';
  $('drive-measurement').textContent=st.running?$('upload-detail').textContent:fresh?`${fmt(nr?radio.nr_bw:radio.lte_bw)} MHz reported · SINR ${fmt(signal)} dB · RSRP ${fmt(nr?radio.nr_rsrp:radio.lte_rsrp)} dBm`:'Waiting for modem · GPS recording continues';
  $('drive-best').textContent=(st.cells||[]).reduce((n,c)=>n+c.located,0);
  $('drive-session').textContent=st.config.demo?'SIMULATION':st.running?'● PARKED VERIFICATION':'● CELL HUNT · UPLOADS OFF';
  renderUploadControls();
  const gps=st.latest.gps, gpsOK=age(gps)<=3&&gps.acc_m<=30;
  const radioOK=age(st.latest.radio)<=Math.max(3,st.config.radio_interval*3)&&!!st.latest.radio.rat;
  const uploadFailure=st.running&&st.latest.probe?.error?`Last upload failed: ${st.latest.probe.error}`:'';
  $('drive-health').textContent=uploadControlError||(!gpsOK?'GPS unavailable or inaccurate · cells saved without new locations':!radioOK?'GPS recording · modem unavailable':uploadFailure||`GPS ±${fmt(gps.acc_m)} m · ${(st.latest.radio.rat||'').replace('NR5G-','5G ')} · recording`);
  $('drive-health').classList.toggle('warning',!!uploadControlError||!gpsOK||!radioOK||!!uploadFailure);
  $('drive-location').textContent=watch!=null?$('gps').textContent:'Enable phone GPS';
};
window.driveOffline=()=>{
  $('drive-upload').textContent='—';$('drive-session').textContent='OFFLINE';
  $('drive-health').textContent='Connection lost · reconnecting to recorder';$('drive-health').classList.add('warning');
};
setViewMode(new URL(location.href).searchParams.get('view'));

document.getElementById('radio-layer').dispatchEvent(new Event('change'));

// A map workspace, rather than a dashboard of floating metric cards.
(() => {
 const sidebar=document.createElement('aside');sidebar.id='hunt-sidebar';
 sidebar.innerHTML='<div class="hunt-heading"><span class="eyebrow">CELL HUNT</span><h2>Choose a cell to follow</h2><p>Compare its signal along your route.</p></div><div id="hunt-list-head"><strong>Recorded cells</strong><span id="hunt-list-count">0</span></div><div id="hunt-inventory-slot"></div><details id="hunt-live-details"><summary>Live modem details</summary></details><details id="hunt-tools"><summary>Parked upload verification</summary><p>Optional: test a promising location after stopping.</p></details>';
 const toolbar=document.createElement('div');toolbar.id='hunt-map-tools';toolbar.innerHTML='<div class="hunt-map-title"><strong>Signal along your route</strong><span id="hunt-selection-caption">All observed cells</span></div><div id="hunt-map-selectors"></div>';
 const empty=document.createElement('div');empty.id='hunt-analysis-empty';empty.innerHTML='<strong>Follow a cell, then inspect its signal.</strong><span>Choose a recorded cell on the left or click a route point. Its measurements and charts will appear here.</span><small>Route points are receiver locations. Mast localization and timing advance are not yet available.</small>';
 document.body.append(sidebar,toolbar,empty);
 const home=new Map();
 function move(node,parent){if(!home.has(node)){const marker=document.createComment('layout home');node.before(marker);home.set(node,marker);}parent.append(node);}
 function restore(){for(const [node,marker] of home)marker.after(node);}
 const selectors=$('hunt-map-selectors');
 const modeBase=setViewMode;
 function updateLayout(){
  const switchButton=document.querySelector('.drive-toolbar [data-view="dashboard"]');if(switchButton)switchButton.textContent=driveView==='phone'?'Map view':'Phone GPS';
  if(driveView==='map'){
   move($('radio-dock'),$('hunt-live-details'));
   move($('radio-layer').closest('label'),selectors);
   move($('map-band').closest('label'),selectors);
   move($('map-cell').closest('label'),selectors);
   move($('radio-layer-note'),toolbar);
   move(document.querySelector('.hunt-filter'),$('hunt-inventory-slot'));
   move($('cell-inventory'),$('hunt-inventory-slot'));
   move($('drive-pause'),$('hunt-tools'));
   move($('drive-follow'),document.querySelector('.drive-toolbar'));
  }else restore();
  document.body.classList.toggle('cell-analysis',!$('track-inspector').hidden&&driveView==='map');
  requestAnimationFrame(()=>{map?.invalidateSize();window.TrackUI?.refresh?.();});
 }
 setViewMode=view=>{modeBase(view);updateLayout();};
 const observer=new MutationObserver(()=>{
  document.body.classList.toggle('cell-analysis',!$('track-inspector').hidden&&driveView==='map');
  const selection=window.TrackUI?.selectedCell;
  $('hunt-selection-caption').textContent=selection&&selection!=='all'?'Selected cell · other observations faded':'All observed cells · click a point to inspect';
  requestAnimationFrame(()=>map?.invalidateSize());
 });
 observer.observe($('track-inspector'),{attributes:true,attributeFilter:['hidden']});
 const renderBase=window.renderDrive;
 window.renderDrive=st=>{renderBase(st);$('hunt-list-count').textContent=(st.cells||[]).length;document.body.classList.toggle('verification-running',!!st.running);if(st.running)$('hunt-tools').open=true;};
 $('drive-details').textContent='Review session';$('drive-details').onclick=()=>setViewMode('dashboard');
 const reviewButton=document.querySelector('.drive-toolbar [data-view="dashboard"]');reviewButton.textContent='Phone GPS';reviewButton.onclick=e=>{e.stopImmediatePropagation();setViewMode('phone');};
 // Existing listener on the review button runs first; use a capture handler for the new action.
 reviewButton.addEventListener('click',e=>{e.stopImmediatePropagation();setViewMode(driveView==='phone'?'map':'phone');},true);
 window.addEventListener('resize',()=>{requestAnimationFrame(()=>{map?.invalidateSize();window.TrackUI?.refresh?.();});});
 updateLayout();
 // Opening the local app now lands directly in the hunting workspace on desktop.
 if(!new URL(location.href).searchParams.has('view')&&window.innerWidth>=800)setViewMode('map');
})();
