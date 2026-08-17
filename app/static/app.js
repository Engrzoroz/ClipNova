const $ = s => document.querySelector(s);
const state = { video: null, clips: [{start:0,end:30}], poll:null };

function toast(msg){
  const el=$("#toast"); el.textContent=msg; el.classList.remove("hidden");
  setTimeout(()=>el.classList.add("hidden"),2600);
}
function fmt(sec){
  sec=Math.max(0,Math.floor(Number(sec)||0));
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;
  return h?`${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`:`${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
function parseTime(v){
  v=String(v||"").trim();
  if(/^\d+(\.\d+)?$/.test(v)) return Number(v);
  const p=v.split(":").map(Number);
  if(p.some(Number.isNaN)) return NaN;
  if(p.length===3) return p[0]*3600+p[1]*60+p[2];
  if(p.length===2) return p[0]*60+p[1];
  return NaN;
}
function videoIdFromUrl(url){
  try{
    const u=new URL(url);
    if(u.hostname==="youtu.be") return u.pathname.slice(1);
    if(u.pathname.startsWith("/shorts/")) return u.pathname.split("/")[2];
    return u.searchParams.get("v");
  }catch{return null}
}

function renderClips(){
  const list=$("#clipsList");
  list.innerHTML="";
  state.clips.forEach((c,i)=>{
    const row=document.createElement("div"); row.className="clipRow";
    row.innerHTML=`
      <div class="clipIndex">${String(i+1).padStart(2,"0")}</div>
      <label class="timeField"><span>Start</span><input data-i="${i}" data-k="start" value="${fmt(c.start)}" inputmode="numeric"></label>
      <div class="dash">→</div>
      <label class="timeField"><span>End</span><input data-i="${i}" data-k="end" value="${fmt(c.end)}" inputmode="numeric"></label>
      <button class="removeClip" data-remove="${i}" aria-label="remove">×</button>`;
    list.appendChild(row);
  });
  list.querySelectorAll("input").forEach(inp=>inp.addEventListener("change",e=>{
    const i=Number(e.target.dataset.i), k=e.target.dataset.k, val=parseTime(e.target.value);
    if(Number.isFinite(val)) state.clips[i][k]=val;
    e.target.value=fmt(state.clips[i][k]);
    updateStats();
  }));
  list.querySelectorAll("[data-remove]").forEach(btn=>btn.addEventListener("click",e=>{
    const i=Number(e.currentTarget.dataset.remove);
    if(state.clips.length===1){toast("Keep at least one clip.");return}
    state.clips.splice(i,1); renderClips(); updateStats();
  }));
  updateStats();
}
function updateStats(){
  $("#clipCount").textContent=state.clips.length;
  const total=state.clips.reduce((a,c)=>a+Math.max(0,c.end-c.start),0);
  $("#totalTime").textContent=fmt(total);
}

$("#analyzeBtn").addEventListener("click", async ()=>{
  const url=$("#urlInput").value.trim(), err=$("#urlError"), btn=$("#analyzeBtn");
  err.classList.add("hidden");
  if(!url){err.textContent="Paste a YouTube URL first.";err.classList.remove("hidden");return}
  btn.disabled=true; btn.querySelector("span").textContent="Analyzing…";
  try{
    const r=await fetch("/api/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
    const data=await r.json();
    if(!r.ok) throw new Error(data.detail||"Analyze failed.");
    state.video=data;
    $("#videoTitle").textContent=data.title||"Untitled video";
    $("#videoChannel").textContent=data.channel||"YouTube";
    $("#videoDuration").textContent=data.duration_text;
    $("#sourceLang").textContent=(data.language||"Auto").toUpperCase();
    $("#thumb").src=data.thumbnail||"";
    const id=data.id||videoIdFromUrl(url);
    $("#ytFrame").src=`https://www.youtube.com/embed/${encodeURIComponent(id)}?rel=0`;
    state.clips=[{start:0,end:Math.min(30,data.duration)}];
    renderClips();
    $("#workspace").classList.remove("hidden");
    setTimeout(()=>$("#workspace").scrollIntoView({behavior:"smooth",block:"start"}),150);
  }catch(e){
    err.textContent=e.message; err.classList.remove("hidden");
  }finally{
    btn.disabled=false; btn.querySelector("span").textContent="Analyze";
  }
});

$("#addClipBtn").addEventListener("click",()=>{
  if(state.clips.length>=6){toast("Maximum 6 clips in V1.");return}
  const last=state.clips[state.clips.length-1];
  const max=state.video?.duration||999999;
  let start=Math.min(last.end+5,max);
  let end=Math.min(start+30,max);
  if(end<=start){start=Math.max(0,max-30);end=max}
  state.clips.push({start,end}); renderClips();
});

function validateClips(){
  if(!state.video) return "Analyze a video first.";
  let total=0;
  for(let i=0;i<state.clips.length;i++){
    const c=state.clips[i];
    if(c.start<0||c.end<=c.start) return `Clip ${i+1}: invalid start/end time.`;
    if(c.end>state.video.duration+1) return `Clip ${i+1}: end is after the video duration.`;
    if(c.end-c.start>600) return `Clip ${i+1}: maximum length is 10 minutes.`;
    total+=c.end-c.start;
  }
  if(total>1200) return "Total selected output must be 20 minutes or less on V1.";
  return null;
}

$("#renderBtn").addEventListener("click",async()=>{
  const issue=validateClips();
  if(issue){toast(issue);return}
  if(!$("#rights").checked){toast("Please confirm you have rights/permission.");return}
  const btn=$("#renderBtn"); btn.disabled=true;
  $("#progressCard").classList.remove("hidden");
  $("#downloadBtn").classList.add("hidden");
  $("#warnings").classList.add("hidden");
  $("#progressCard").scrollIntoView({behavior:"smooth",block:"center"});
  try{
    const payload={
      url:$("#urlInput").value.trim(),
      clips:state.clips,
      quality:$("#quality").value,
      captions:$("#captions").checked,
      caption_language:$("#captionLanguage").value,
      caption_style:$("#captionStyle").value,
      confirm_rights:true
    };
    const r=await fetch("/api/render",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const data=await r.json();
    if(!r.ok) throw new Error(data.detail||"Could not start render.");
    pollJob(data.job_id);
  }catch(e){
    btn.disabled=false; toast(e.message);
  }
});

async function pollJob(id){
  clearTimeout(state.poll);
  try{
    const r=await fetch(`/api/jobs/${id}`);
    const j=await r.json();
    if(!r.ok) throw new Error(j.detail||"Job status failed.");
    $("#progressMessage").textContent=j.message||"Processing…";
    $("#progressPct").textContent=`${j.progress||0}%`;
    $("#progressBar").style.width=`${j.progress||0}%`;
    if(j.warnings?.length){
      $("#warnings").innerHTML=j.warnings.map(x=>`• ${x}`).join("<br>");
      $("#warnings").classList.remove("hidden");
    }
    if(j.state==="done"){
      $("#renderBtn").disabled=false;
      $("#downloadBtn").href=j.download_url;
      $("#downloadBtn").classList.remove("hidden");
      toast("Your clips are ready ✨");
      return;
    }
    if(j.state==="error"){
      $("#renderBtn").disabled=false;
      throw new Error(j.error||"Render failed.");
    }
    state.poll=setTimeout(()=>pollJob(id),1600);
  }catch(e){
    $("#renderBtn").disabled=false;
    $("#progressMessage").textContent="Something went wrong";
    toast(e.message);
  }
}

$("#creatorBtn").addEventListener("click",()=>$("#creatorModal").classList.remove("hidden"));
$("#closeCreator").addEventListener("click",()=>$("#creatorModal").classList.add("hidden"));
$(".modalBackdrop").addEventListener("click",()=>$("#creatorModal").classList.add("hidden"));

renderClips();
