'use strict';
let driveView='map', screenLock=null;
async function keepAwake(){
  if(document.visibilityState!=='visible'||screenLock)return;
  try{
    screenLock=await navigator.wakeLock.request('screen');
    $('drive-awake').textContent='Screen stays awake · keep this page in the foreground';
    screenLock.addEventListener('release',()=>{screenLock=null;$('drive-awake').textContent='Keep this page open and the phone awake';});
  }catch{$('drive-awake').textContent='Keep this page open and disable automatic screen locking';}
}
function setViewMode(view){
  driveView=view==='phone'?'phone':'map';
  document.body.dataset.view=driveView;
  $('drive-display').hidden=false;
  const url=new URL(location.href);url.searchParams.set('view',driveView);
  window.history.replaceState(null,'',url);
  keepAwake();
  requestAnimationFrame(()=>{map?.invalidateSize();if(follow&&me)map?.panTo(me.getLatLng(),{animate:false});});
}
document.querySelectorAll('button[data-view]').forEach(button=>button.addEventListener('click',()=>setViewMode(button.dataset.view)));
$('drive-view-switch').onclick=()=>setViewMode(driveView==='phone'?'map':'phone');
$('drive-fullscreen').onclick=async()=>{
  try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}
  catch{$('drive-fullscreen').textContent='Use browser full-screen';}
};
document.addEventListener('fullscreenchange',()=>{$('drive-fullscreen').textContent=document.fullscreenElement?'Leave full screen ⛶':'Full screen ⛶';map?.invalidateSize();});
$('drive-follow').onclick=()=>setFollow(!follow);
$('drive-location').onclick=()=>{$('gps').click();keepAwake();};
$('drive-pause').onclick=()=>{toggleUploads();keepAwake();};
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')keepAwake();});
window.renderDrive=st=>{
  const radio=st.latest.radio||{}, fresh=!!radio.rat&&age(radio)<=Math.max(3,st.config.radio_interval*3);
  const nr=fresh&&radio.nr_band, signal=nr?radio.nr_sinr:radio.lte_sinr;
  $('drive-label').textContent=st.running?'PARKED VERIFICATION · Mbps ↑':'CELL HUNT · ALL BANDS';
  $('drive-upload').textContent=st.running?$('upload').textContent:fresh?(nr||radio.lte_band||'—'):'—';
  $('drive-measurement').textContent=st.running?$('upload-detail').textContent+' · '+uploadCarrierText(st)+' · per-band traffic unconfirmed':fresh?`${fmt(nr?radio.nr_bw:radio.lte_bw)} MHz reported · SINR ${fmt(signal)} dB · RSRP ${fmt(nr?radio.nr_rsrp:radio.lte_rsrp)} dBm`:'Waiting for modem · GPS recording continues';
  $('drive-best').textContent=(st.cells||[]).reduce((n,c)=>n+c.located,0);
  $('drive-session').textContent=st.config.demo?'SIMULATION':st.running?'● PARKED VERIFICATION':'● LIVE HUNT';
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
 sidebar.innerHTML='<div class="hunt-heading"><span class="eyebrow">CELL HUNT</span><h2>Choose a cell to follow</h2><p>Compare its signal along your route.</p></div><div id="hunt-list-head"><strong>Recorded cells</strong><span id="hunt-list-count">0</span></div><div id="hunt-inventory-slot"></div><details id="hunt-live-details"><summary>Modem diagnostics</summary></details><section id="hunt-tools" aria-label="Upload test"><p class="upload-test-help">Parked test · 20 seconds</p><div id="upload-test-action"></div></section>';
 const verification=document.createElement('div');verification.id='verification-result';sidebar.querySelector('#hunt-tools').append(verification);
 const toolbar=document.createElement('div');toolbar.id='hunt-map-tools';toolbar.innerHTML='<div class="hunt-map-title"><strong>Signal along your route</strong><span id="hunt-selection-caption">All observed cells</span><button id="emf-toggle" type="button" aria-pressed="true">EMF sites · on</button></div><div id="hunt-map-selectors"></div>';
 const empty=document.createElement('div');empty.id='hunt-analysis-empty';empty.innerHTML='<strong>Follow a cell, then inspect its signal.</strong><span>Choose a recorded cell on the left or click a route point. Its measurements and charts will appear here.</span><small>Route points are receiver locations. Experimental timing-advance rings are available in the live hunt.</small>';
 document.body.append(sidebar,toolbar,empty);
 const home=new Map();
 function move(node,parent){if(!home.has(node)){const marker=document.createComment('layout home');node.before(marker);home.set(node,marker);}parent.append(node);}
 function restore(){for(const [node,marker] of home)marker.after(node);}
 const selectors=$('hunt-map-selectors');
 const modeBase=setViewMode;
 function updateLayout(){
  const switchButton=$('drive-view-switch');if(switchButton)switchButton.textContent=driveView==='phone'?'Map view':'Phone GPS';
  if(driveView==='map'){
   move($('radio-dock'),$('live-radio-slot')||$('hunt-live-details'));
   move($('radio-layer').closest('label'),selectors);
   move($('map-band').closest('label'),selectors);
   move($('map-cell').closest('label'),selectors);
   move($('radio-layer-note'),toolbar);
   move(document.querySelector('.hunt-filter'),$('hunt-inventory-slot'));
   move($('cell-inventory'),$('hunt-inventory-slot'));
   move($('drive-pause'),$('upload-test-action'));
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
 window.renderDrive=st=>{renderBase(st);$('hunt-list-count').textContent=(st.cells||[]).length;document.body.classList.toggle('verification-running',!!st.running);renderVerification(st);};
 let verificationSignature='';
 function renderVerification(st){
  const a=st.active,p=st.latest.probe;
  const signature=JSON.stringify([st.running,st.busy,st.remaining,a,p?.id,uploadControlError]);if(signature===verificationSignature)return;verificationSignature=signature;
  verification.replaceChildren();
  if(!st.running&&!st.busy&&!p&&!uploadControlError)return;
  const status=document.createElement('strong');
  status.textContent=uploadControlError?'Could not start test':st.busy?'Testing upload · 20 seconds':st.running?'Preparing next test…':p?.error?'Last test could not finish':p?'Last completed test':'Ready when you are';
  status.setAttribute('role','status');verification.append(status);
  if(uploadControlError||(!st.busy&&p?.error)){const error=document.createElement('p');error.textContent=uploadControlError||p.error;verification.append(error);}
  const average=st.busy?a?.mbps:p?.error?null:p?.mbps;
  if(st.busy||average!=null){const metric=document.createElement('div');metric.className='verification-speed';const value=document.createElement('b'),unit=document.createElement('span');value.textContent=fmt(average,1);unit.textContent='Mbps ↑';metric.append(value,unit);verification.append(metric);
   const label=document.createElement('p');label.textContent=st.busy?'Average so far':'20-second average';verification.append(label);}

 }
 window.addEventListener('resize',()=>{requestAnimationFrame(()=>{map?.invalidateSize();window.TrackUI?.refresh?.();});});
 updateLayout();
})();
