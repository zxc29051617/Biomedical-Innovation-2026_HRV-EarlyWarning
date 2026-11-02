"use strict";
(function () {
  const core = window.CathReviewCore;
  const $ = id => document.getElementById(id);
  const findingNames = {bundle_branch_block:"束支傳導阻滯",ventricular_pacing:"心室起搏",left_ventricular_hypertrophy:"左心室肥厚",posterior_pattern:"疑似後壁型態",other_occlusion_pattern:"其他阻塞線索"};
  const state = {data:null, fingerprint:null, synthetic:false, loadVersion:0, cursor:null, anchor:null, lastExport:null};
  const canvases = new Map();
  const text = (id, value) => {$(id).textContent = value;};
  const error = (id, message) => {text(id, message || ""); $(id).hidden = !message;};
  for (const key of core.FINDINGS) {
    const label = document.createElement("label"); label.textContent = findingNames[key];
    const select = document.createElement("select"); select.id = "finding-"+key;
    for (const [value, wording] of [["unknown","未判定"],["yes","有"],["no","無"]]) {
      const option = document.createElement("option"); option.value=value; option.textContent=wording; select.append(option);
    }
    label.append(select); $("findings").append(label);
  }
  const geometry = canvas => ({left:51,right:15,top:15,bottom:30,width:canvas.clientWidth,height:canvas.clientHeight});
  function timeWindow() {
    if (!state.data) return {start:0,end:0,span:0};
    const duration = (state.data.signals_mv.length - 1) / state.data.sample_rate_hz;
    const span = Math.min(Number($("window-seconds").value), duration);
    const start = Math.min(Number($("start-time").value), Math.max(0,duration-span));
    return {start,end:start+span,span};
  }
  function drawLead(lead, canvas) {
    if (!state.data) return 0;
    const {left,right,top,bottom,width,height}=geometry(canvas), dpr=window.devicePixelRatio || 1;
    canvas.width=Math.round(width*dpr); canvas.height=Math.round(height*dpr);
    const c=canvas.getContext("2d"); c.setTransform(dpr,0,0,dpr,0,0);
    c.fillStyle="#ffffff"; c.fillRect(0,0,width,height);
    const w=width-left-right,h=height-top-bottom, range=Number($("amplitude-range").value), win=timeWindow();
    if (w<=0 || h<=0 || win.span<=0) return 0;
    const x=t=>left+(t-win.start)/win.span*w, y=v=>top+h/2-v/(2*range)*h;
    c.font="10px system-ui"; c.lineWidth=1;
    for(let i=Math.ceil(win.start/.04);i<=Math.floor(win.end/.04);i++){
      const xx=x(i*.04);c.strokeStyle=i%5===0?"#d3bdc3":"#f1e7ea"; c.beginPath();c.moveTo(xx,top);c.lineTo(xx,top+h);c.stroke();
    }
    for(let i=Math.ceil(-range/.1);i<=Math.floor(range/.1);i++){
      const yy=y(i*.1); c.strokeStyle=i%5===0?"#d3bdc3":"#f1e7ea";c.beginPath();c.moveTo(left,yy);c.lineTo(left+w,yy);c.stroke();
    }
    c.fillStyle="#526773";c.textAlign="right";c.fillText("+"+range+" mV",left-5,top+4);c.fillText("0",left-5,y(0)+4);c.fillText("−"+range,left-5,top+h);
    c.textAlign="center";
    for(let i=0;i<=5;i++) c.fillText((win.start+win.span*i/5).toFixed(2)+" s",left+w*i/5,height-9);
    const j=state.data.lead_names.indexOf(lead), fs=state.data.sample_rate_hz;
    const first=Math.max(0,Math.floor(win.start*fs)),last=Math.min(state.data.signals_mv.length-1,Math.ceil(win.end*fs));
    let clipped=0;
    c.save();c.beginPath();c.rect(left,top,w,h);c.clip();
    c.strokeStyle="#173f46";c.lineWidth=1.15;c.beginPath();
    for(let i=first;i<=last;i++){
      const value=state.data.signals_mv[i][j]; if(Math.abs(value)>range)clipped++;
      if(i===first)c.moveTo(x(i/fs),y(value));else c.lineTo(x(i/fs),y(value));
    }c.stroke();
    for(const [point,colour] of [[state.anchor,"#bc5c16"],[state.cursor,"#007c73"]]){
      if(point&&point.lead===lead&&point.index/fs>=win.start&&point.index/fs<=win.end){
        const xx=x(point.index/fs),yy=y(state.data.signals_mv[point.index][j]);
        c.strokeStyle=colour;c.lineWidth=1;c.beginPath();c.moveTo(xx,top);c.lineTo(xx,top+h);c.stroke();
        c.fillStyle=colour;c.beginPath();c.arc(xx,yy,3,0,Math.PI*2);c.fill();
      }
    }
    c.restore();
    // Reference calibration drawn outside the data plot: vertical height is exactly 1 mV.
    const calX=13, calY=y(-range+.2);c.strokeStyle="#006b64";c.lineWidth=1.5;c.beginPath();c.moveTo(calX,calY);c.lineTo(calX+5,calY);c.lineTo(calX+5,calY-h/(2*range));c.lineTo(calX+13,calY-h/(2*range));c.lineTo(calX+13,calY);c.stroke();
    return clipped;
  }
  function render() {
    if (!state.data) return;
    const duration=(state.data.signals_mv.length-1)/state.data.sample_rate_hz;
    $("start-time").max=String(Math.max(0,duration-Math.min(Number($("window-seconds").value),duration)));
    if(Number($("start-time").value)>Number($("start-time").max))$("start-time").value=$("start-time").max;
    text("start-value",Number($("start-time").value).toFixed(3)+" 秒");
    let clipped=0;for(const [lead,canvas] of canvases)clipped+=drawLead(lead,canvas);
    error("clipping-note",clipped?"目前視窗有 "+clipped+" 個導程取樣值超出垂直顯示範圍；請放大 mV 範圍。原始數值未截斷。":null);
  }
  function cursorReadout() {
    if(!state.cursor||!state.data)return;
    const {lead,index}=state.cursor,j=state.data.lead_names.indexOf(lead),value=state.data.signals_mv[index][j];
    let message=lead+" · "+(index/state.data.sample_rate_hz).toFixed(3)+" 秒 · "+value.toFixed(4)+" mV（相對訊號零點）";
    if(state.anchor&&state.anchor.lead===lead){
      const a=state.anchor.index;
      message+=" ｜ 相對手動參考點 Δt="+((index-a)/state.data.sample_rate_hz).toFixed(3)+" 秒，ΔV="+(value-state.data.signals_mv[a][j]).toFixed(4)+" mV";
    }
    text("cursor-readout",message+"；不是自動 ST 振幅判定。");
  }
  function pointAt(event,lead,canvas){
    if(!state.data)return null;
    const box=canvas.getBoundingClientRect(),g=geometry(canvas),win=timeWindow();
    const ratio=Math.max(0,Math.min(1,(event.clientX-box.left-g.left)/(g.width-g.left-g.right)));
    return {lead,index:Math.max(0,Math.min(state.data.signals_mv.length-1,Math.round((win.start+ratio*win.span)*state.data.sample_rate_hz)))};
  }
  for(const lead of core.LEADS){
    const card=document.createElement("article");card.className="lead-card";
    const title=document.createElement("h3");title.className="lead-title";title.textContent=lead;
    const hint=document.createElement("span");hint.textContent="mV / s";title.append(hint);
    const canvas=document.createElement("canvas");canvas.tabIndex=0;canvas.dataset.lead=lead;canvas.setAttribute("role","img");canvas.setAttribute("aria-label",lead+" ECG 波形；左右鍵移動游標，Enter 設參考點");
    canvas.addEventListener("pointermove",e=>{state.cursor=pointAt(e,lead,canvas);cursorReadout();render();});
    canvas.addEventListener("click",e=>{state.cursor=pointAt(e,lead,canvas);state.anchor=state.cursor;cursorReadout();render();});
    canvas.addEventListener("keydown",e=>{
      if(!state.data||!["ArrowLeft","ArrowRight","Enter"].includes(e.key))return;e.preventDefault();
      const existing=state.cursor&&state.cursor.lead===lead?state.cursor.index:Math.round(timeWindow().start*state.data.sample_rate_hz);
      state.cursor={lead,index:Math.max(0,Math.min(state.data.signals_mv.length-1,existing+(e.key==="ArrowLeft"?-1:e.key==="ArrowRight"?1:0)))};
      if(e.key==="Enter")state.anchor={...state.cursor};cursorReadout();render();
    });
    card.append(title,canvas);$("lead-grid").append(card);canvases.set(lead,canvas);
  }
  function clearCursor(){state.cursor=null;state.anchor=null;text("cursor-readout","移動游標讀取原始取樣點；點一下設參考點，再移動可看時間與電壓差。鍵盤可用左右鍵移動、Enter 設參考。");render();}
  async function load(value,synthetic,version){
    const validated=core.validateCase(value);const hash=await core.fingerprint(validated);
    if(version!==state.loadVersion)return;
    state.data=validated;state.fingerprint=hash;state.synthetic=synthetic;state.lastExport=null;
    $("review-form").reset();$("start-time").value="0";$("export-annotation").disabled=false;
    text("case-id",validated.case_id);text("source-kind",synthetic?"合成練習 · 非真實病例":"本機匯入 · 研究資料");$("source-kind").classList.toggle("synthetic",synthetic);
    const sex={male:"男性",female:"女性",unknown:"性別未知"}[validated.sex];
    text("case-meta",(validated.age_years===null?"年齡未知":validated.age_years+" 歲")+" · "+sex+" · "+validated.sample_rate_hz+" Hz · "+validated.signals_mv.length+" 個取樣點 · "+(validated.signals_mv.length/validated.sample_rate_hz).toFixed(3)+" 秒資料");
    text("representation-note",validated.representation==="median"?"Median 代表波形：不能當作連續十秒節律或用來確認逐搏變化。":"Raw 波形：依輸入 mV 值顯示，沒有額外濾波、補值或自動校正。");
    text("fingerprint","病例內容 SHA-256："+hash);text("waveform-hash","提供者記錄的來源波形 SHA-256："+validated.provenance.waveform_sha256);
    error("import-error",null);error("annotation-error",null);text("export-status","尚未判讀");clearCursor();
  }
  $("open-file").addEventListener("click",()=>$("import-file").click());
  $("import-file").addEventListener("change",async e=>{
    const file=e.target.files[0];if(!file)return;
    const version=++state.loadVersion;
    try{if(file.size>8*1024*1024)throw new Error("檔案超過本工具的 8 MiB 上限。");await load(JSON.parse(await file.text()),false,version);}
    catch(err){if(version===state.loadVersion)error("import-error",err instanceof SyntaxError?"檔案不是有效 JSON；未載入病例。":err.message);}
    finally{e.target.value="";}
  });
  $("load-demo").addEventListener("click",()=>{const version=++state.loadVersion;load(core.makeSynthetic(),true,version).catch(err=>{if(version===state.loadVersion)error("import-error",err.message);});});
  $("clear-cursor").addEventListener("click",clearCursor);
  for(const id of ["window-seconds","amplitude-range","start-time"])$(id).addEventListener("input",render);
  new ResizeObserver(render).observe($("lead-grid"));
  $("review-form").addEventListener("input",()=>text("export-status","判讀尚未匯出"));
  $("review-form").addEventListener("submit",e=>{
    e.preventDefault();error("annotation-error",null);
    try{
      if(!state.data)throw new Error("請先載入病例。");
      const review=core.validateReview({reviewer_id:$("reviewer-id").value.trim(),index_ecg_ste_amplitude_status:$("amplitude-status").value,newness:$("newness").value,findings:Object.fromEntries(core.FINDINGS.map(key=>{const v=$("finding-"+key).value;return [key,v==="unknown"?null:v==="yes"];})),notes:$("notes").value.trim()});
      const result={schema_version:"cath-ecg-annotation-v1",tool_version:"cath-review-1.0.0",case_id:state.data.case_id,case_sha256:state.fingerprint,case_hash_algorithm:"sha256_sorted_keys_json_stringify_v1",waveform_sha256:state.data.provenance.waveform_sha256,representation:state.data.representation,synthetic:state.synthetic,created_at_utc:new Date().toISOString(),...review};
      const blob=new Blob([JSON.stringify(result,null,2)+"\n"],{type:"application/json"});const url=URL.createObjectURL(blob),a=document.createElement("a");
      a.href=url;a.download=state.data.case_id+"-"+review.reviewer_id+"-annotation.json";document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      state.lastExport=result;text("export-status","已匯出判讀；關閉頁面即清空。");
    }catch(err){error("annotation-error",err.message);}
  });
})();
