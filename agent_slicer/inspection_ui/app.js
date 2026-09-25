"use strict";

const state = { snapshot: null, renderer: null, selected: null, selectedKey: null, refreshing: false };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const size = (bytes) => bytes == null ? "—" : bytes > 1e9 ? `${(bytes/1e9).toFixed(2)} GB` : bytes > 1e6 ? `${(bytes/1e6).toFixed(1)} MB` : bytes > 1e3 ? `${(bytes/1e3).toFixed(1)} kB` : `${bytes} B`;
const fmt = (value, digits=2) => typeof value === "number" ? value.toFixed(digits).replace(/\.00$/, "") : (value ?? "—");

async function refresh() {
  if (document.hidden && state.snapshot) return;
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.snapshot = await response.json();
    render(state.snapshot);
    $("freshness").textContent = `Evidence refreshed ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $("freshness").textContent = `Could not refresh: ${error.message}`;
  } finally {
    state.refreshing = false;
  }
}

function render(data) {
  const p = data.project;
  $("project-name").textContent = p.name;
  $("project-path").textContent = p.root;
  const pill = $("status-pill");
  pill.textContent = p.status.toUpperCase();
  pill.className = `status ${["completed","passed","prepared"].includes(p.status) ? "good" : ["rejected","blocked","error"].includes(p.status) ? "bad" : "warn"}`;
  const dims = data.machine.build_volume_mm;
  const supports = data.build.automatic_supports || {};
  const audit = data.current_audit;
  const auditedSupports = (audit && audit.supports) || {};
  const perf = data.build.performance || {};
  $("summary-cards").innerHTML = [
    card("Machine", data.machine.id, dims ? `${dims.join(" × ")} mm` : "Unresolved"),
    card("Pipeline state", p.pipeline_status, `${p.models.length} models · ${data.plan.placements.length} placements`),
    card("Plate mesh", data.build.triangle_count == null ? "—" : Number(data.build.triangle_count).toLocaleString(), "triangles"),
    card("Supports routed", supports.candidates_routed ?? "—", supports.candidates_attempted != null ? `${supports.candidates_attempted} attempted` : "Not sliced"),
    card("Slice time", perf.pipeline_total_seconds == null ? "—" : `${fmt(perf.pipeline_total_seconds)} s`, `${data.build.slice_attempts || 0} attempt(s)`),
    card("Audited delivery", audit ? audit.status : "none", audit ? `${auditedSupports.support_elements ?? auditedSupports.routed ?? "—"} support elements/contacts` : "No versioned audit"),
  ].join("");
  renderAssetPicker(data.viewer_assets);
  renderValidation(data.validation);
  renderOperations(data.plan);
  renderArtifacts(data.artifacts);
  $("limitations").innerHTML = `<h3>Evidence boundary</h3><ul>${(data.validation.limitations.length ? data.validation.limitations : ["Inspection reflects saved evidence only; it does not prove a successful physical print."]).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
}

function card(label, value, detail) { return `<div class="card"><div class="card-label">${esc(label)}</div><div class="card-value">${esc(value)}</div><div class="card-detail">${esc(detail)}</div></div>`; }

function renderAssetPicker(assets) {
  const select = $("asset-select");
  const previous = state.selected;
  select.innerHTML = assets.length ? assets.map(a => `<option value="${esc(a.path)}">${esc(a.kind)} · ${esc(a.name)}</option>`).join("") : `<option>No visual assets</option>`;
  const preferred = assets.find(a => a.path === previous) || assets.find(a => a.is_audited_slice) || assets.find(a => a.path === "artifacts/plate.stl") || assets[0];
  if (preferred) { select.value = preferred.path; showAsset(preferred); }
  else showEmpty();
  select.onchange = () => showAsset(assets.find(a => a.path === select.value));
}

function showAsset(asset) {
  if (!asset) return showEmpty();
  const key = `${asset.path}:${asset.size_bytes}:${asset.modified_unix}`;
  if (state.selectedKey === key) return;
  state.selected = asset.path;
  state.selectedKey = key;
  $("viewer-empty").style.display = "none";
  const image = $("image-preview");
  const canvas = $("mesh-canvas");
  $("slice-controls").hidden = true;
  $("viewer").querySelector(".viewer-help").style.display = "block";
  if (asset.slice_info) {
    if (state.renderer) state.renderer.stop();
    canvas.style.display = "none"; image.style.display = "block";
    $("viewer").querySelector(".viewer-help").style.display = "none";
    configureSliceViewer(asset);
  } else if (asset.path.toLowerCase().endsWith(".stl")) {
    image.style.display = "none"; canvas.style.display = "block";
    loadStl(canvas, asset.preview_url || asset.url, asset).catch(error => { showEmpty(`Could not display mesh: ${error.message}`); });
  } else {
    if (state.renderer) state.renderer.stop();
    canvas.style.display = "none"; image.style.display = "block"; image.src = asset.url;
    $("geometry-stats").innerHTML = `<span><strong>${esc(asset.kind)}</strong></span><span>${size(asset.size_bytes)}</span>`;
  }
}

function showEmpty(message="No visual artifact available") {
  if (state.renderer) state.renderer.stop();
  state.selectedKey = null;
  $("slice-controls").hidden = true;
  $("mesh-canvas").style.display = "none"; $("image-preview").style.display = "none";
  $("viewer-empty").style.display = "grid"; $("viewer-empty").textContent = message;
  $("geometry-stats").innerHTML = "";
}

function configureSliceViewer(asset) {
  const info = asset.slice_info;
  const slider = $("slice-slider");
  const stored = sessionStorage.getItem(`slice-v2:${asset.path}`);
  const remembered = stored === null ? Number.NaN : Number(stored);
  const initial = Number.isFinite(remembered) && remembered >= 0 && remembered < info.layer_count ? remembered : Math.floor(info.layer_count / 2);
  slider.max = String(info.layer_count - 1); slider.value = String(initial);
  $("slice-controls").hidden = false;
  const updateLabel = () => {
    const index = Number(slider.value);
    $("slice-label").textContent = `Layer ${index + 1} / ${info.layer_count} · ${(index * info.layer_height_mm).toFixed(2)} mm`;
  };
  const load = () => { updateLabel(); loadSliceLayer(asset, Number(slider.value)); };
  slider.oninput = updateLabel; slider.onchange = load;
  $("slice-prev").onclick = () => { slider.value = String(Math.max(0, Number(slider.value) - 1)); load(); };
  $("slice-next").onclick = () => { slider.value = String(Math.min(Number(slider.max), Number(slider.value) + 1)); load(); };
  load();
}

function loadSliceLayer(asset, index) {
  sessionStorage.setItem(`slice-v2:${asset.path}`, String(index));
  $("image-preview").src = `${asset.slice_layer_url}?index=${index}&scale=2`;
  const info = asset.slice_info;
  $("geometry-stats").innerHTML = `<span><strong>${esc(info.format.toUpperCase())} v${esc(info.version)}</strong></span><span><strong>${Number(info.resolution[0]).toLocaleString()} × ${Number(info.resolution[1]).toLocaleString()}</strong> pixels</span><span>${info.layer_count.toLocaleString()} layers · ${fmt(info.layer_height_mm, 3)} mm</span><span>Decoded from saved audited file</span>`;
}

function renderValidation(v) {
  const groups = [[`Basic file · ${v.source}`, v.basic_status, v.basic_checks], [`Production / UVtools · ${v.source}`, v.production_status, v.production_checks]];
  $("validation").innerHTML = groups.map(([name,status,checks]) => `<div class="section-label"><span>${esc(name)}</span><span>${esc(status)}</span></div>${checks.length ? checks.map(checkRow).join("") : `<div class="empty">No saved checks</div>`}`).join("") + (Object.keys(v.issue_counts || {}).length ? `<div class="section-label"><span>Detector counts</span></div>${Object.entries(v.issue_counts).map(([k,n]) => checkRow({id:k, passed:n===0, actual:n})).join("")}` : "");
}
function checkRow(c) {
  const value = c.actual ?? c.value ?? (c.expected != null ? `expected ${c.expected}` : "");
  return `<div class="check ${c.passed ? "pass" : "fail"}"><span class="check-dot"></span><span class="check-name">${esc(c.id)}</span><span class="check-value">${esc(value)}</span></div>`;
}

function renderOperations(plan) {
  const warnings = [...plan.blockers.map(x => ({...x, level:"BLOCKER"})), ...plan.warnings.map(x => ({...x, level:"WARNING"}))];
  $("operations").innerHTML = warnings.map(x => `<div class="check fail"><span class="check-dot"></span><span>${esc(x.level)} · ${esc(x.message || x.capability || "Review required")}</span></div>`).join("") + (plan.operations.length ? plan.operations.map((op,i) => `<div class="operation"><span><span class="op-index">${String(i+1).padStart(2,"0")}</span>${esc(op.id)}</span><span class="muted">${esc(op.status)}</span></div>`).join("") : `<div class="empty">No compiled plan yet</div>`);
}

function renderArtifacts(artifacts) {
  $("artifacts").innerHTML = artifacts.length ? `<div class="artifact-list">${artifacts.map(a => `<div class="artifact"><div><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.path)}</a><div class="artifact-kind">${esc(a.kind)}</div></div><div class="artifact-size">${size(a.size_bytes)}</div></div>`).join("")}</div>` : `<div class="empty">No saved artifacts</div>`;
}

async function loadStl(canvas, url, asset) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const buffer = await response.arrayBuffer();
  const mesh = parseStl(buffer);
  if (state.renderer) state.renderer.stop();
  state.renderer = createRenderer(canvas, mesh);
  const d = mesh.dimensions;
  const originalTriangles = asset.triangle_count || mesh.triangleCount;
  const sampled = Boolean(asset.preview_sampled || mesh.sampled);
  $("geometry-stats").innerHTML = `<span><strong>${Number(originalTriangles).toLocaleString()}</strong> triangles</span><span><strong>${fmt(d[0])} × ${fmt(d[1])} × ${fmt(d[2])}</strong> mm preview bounds</span><span>${sampled ? `${Number(asset.preview_triangle_count || mesh.positions.length/9).toLocaleString()} triangles streamed for display` : "Full mesh preview"}</span>`;
}

function parseStl(buffer) {
  const view = new DataView(buffer); let triangles = 0; let binary = false;
  if (buffer.byteLength >= 84) { triangles = view.getUint32(80, true); binary = 84 + triangles*50 === buffer.byteLength; }
  let source = [];
  if (binary) {
    const step = Math.max(1, Math.ceil(triangles / 200000));
    for (let i=0; i<triangles; i+=step) { const o=84+i*50; for(let v=0;v<3;v++) source.push(view.getFloat32(o+12+v*12,true),view.getFloat32(o+16+v*12,true),view.getFloat32(o+20+v*12,true)); }
  } else {
    const text = new TextDecoder().decode(buffer); const re=/vertex\s+([+\-\d.eE]+)\s+([+\-\d.eE]+)\s+([+\-\d.eE]+)/g; let m;
    while ((m=re.exec(text)) && source.length < 1800000) source.push(Number(m[1]),Number(m[2]),Number(m[3]));
    triangles = Math.floor((text.match(/endfacet/g)||[]).length);
  }
  if (!source.length) throw new Error("no triangles found");
  const min=[Infinity,Infinity,Infinity], max=[-Infinity,-Infinity,-Infinity];
  for(let i=0;i<source.length;i+=3) for(let a=0;a<3;a++){ min[a]=Math.min(min[a],source[i+a]); max[a]=Math.max(max[a],source[i+a]); }
  const center=min.map((x,i)=>(x+max[i])/2); const span=Math.max(...max.map((x,i)=>x-min[i]))||1;
  const positions=new Float32Array(source.length), normals=new Float32Array(source.length);
  for(let i=0;i<source.length;i+=9){
    for(let j=0;j<9;j+=3){positions[i+j]=(source[i+j]-center[0])/span;positions[i+j+1]=(source[i+j+1]-center[1])/span;positions[i+j+2]=(source[i+j+2]-center[2])/span;}
    const ax=positions[i+3]-positions[i], ay=positions[i+4]-positions[i+1], az=positions[i+5]-positions[i+2], bx=positions[i+6]-positions[i], by=positions[i+7]-positions[i+1], bz=positions[i+8]-positions[i+2];
    let nx=ay*bz-az*by, ny=az*bx-ax*bz, nz=ax*by-ay*bx, l=Math.hypot(nx,ny,nz)||1; nx/=l;ny/=l;nz/=l;
    for(let j=0;j<9;j+=3){normals[i+j]=nx;normals[i+j+1]=ny;normals[i+j+2]=nz;}
  }
  return {positions,normals,triangleCount:triangles,dimensions:max.map((x,i)=>x-min[i]),sampled:positions.length/9<triangles};
}

function createRenderer(canvas, mesh) {
  const gl=canvas.getContext("webgl",{antialias:true}); if(!gl) throw new Error("WebGL unavailable");
  const vs=shader(gl,gl.VERTEX_SHADER,"attribute vec3 p;attribute vec3 n;uniform mat4 m;varying float l;void main(){vec3 nn=normalize((m*vec4(n,0.0)).xyz);l=.28+.72*max(0.0,dot(nn,normalize(vec3(.4,.7,.6))));gl_Position=m*vec4(p,1.0);}");
  const fs=shader(gl,gl.FRAGMENT_SHADER,"precision mediump float;varying float l;void main(){gl_FragColor=vec4(vec3(.25,.88,.79)*l,1.0);}");
  const program=gl.createProgram();gl.attachShader(program,vs);gl.attachShader(program,fs);gl.linkProgram(program);gl.useProgram(program);
  const bind=(name,data)=>{const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);const loc=gl.getAttribLocation(program,name);gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,3,gl.FLOAT,false,0,0);};bind("p",mesh.positions);bind("n",mesh.normals);
  let rx=-.55,ry=.65,zoom=1.35,drag=false,lx=0,ly=0,frame=0,stopped=false;
  const draw=()=>{if(stopped)return;const ratio=window.devicePixelRatio||1,w=Math.floor(canvas.clientWidth*ratio),h=Math.floor(canvas.clientHeight*ratio);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}gl.viewport(0,0,w,h);gl.enable(gl.DEPTH_TEST);gl.enable(gl.CULL_FACE);gl.clearColor(.043,.063,.078,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);const aspect=w/h;let m=mul(perspective(45*Math.PI/180,aspect,.1,20),translate(0,0,-2.7));m=mul(m,rotateX(rx));m=mul(m,rotateY(ry));m=mul(m,scale(zoom));gl.uniformMatrix4fv(gl.getUniformLocation(program,"m"),false,new Float32Array(m));gl.drawArrays(gl.TRIANGLES,0,mesh.positions.length/3);frame=requestAnimationFrame(draw);};
  canvas.onpointerdown=e=>{drag=true;lx=e.clientX;ly=e.clientY;canvas.setPointerCapture(e.pointerId);};canvas.onpointerup=()=>drag=false;canvas.onpointermove=e=>{if(drag){ry+=(e.clientX-lx)*.009;rx+=(e.clientY-ly)*.009;lx=e.clientX;ly=e.clientY;}};canvas.onwheel=e=>{e.preventDefault();zoom=Math.max(.45,Math.min(2.5,zoom*Math.exp(-e.deltaY*.001)));};canvas.ondblclick=()=>{rx=-.55;ry=.65;zoom=1.35;};draw();return{stop(){stopped=true;cancelAnimationFrame(frame);}};
}
function shader(gl,type,src){const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(s));return s;}
function mul(a,b){const o=Array(16).fill(0);for(let c=0;c<4;c++)for(let r=0;r<4;r++)for(let k=0;k<4;k++)o[c*4+r]+=a[k*4+r]*b[c*4+k];return o;}
function perspective(f,a,n,z){const q=1/Math.tan(f/2);return[q/a,0,0,0,0,q,0,0,0,0,(z+n)/(n-z),-1,0,0,2*z*n/(n-z),0];}
function translate(x,y,z){return[1,0,0,0,0,1,0,0,0,0,1,0,x,y,z,1];}function scale(s){return[s,0,0,0,0,s,0,0,0,0,s,0,0,0,0,1];}
function rotateX(a){const c=Math.cos(a),s=Math.sin(a);return[1,0,0,0,0,c,s,0,0,-s,c,0,0,0,0,1];}function rotateY(a){const c=Math.cos(a),s=Math.sin(a);return[c,0,-s,0,0,1,0,0,s,0,c,0,0,0,0,1];}

refresh(); setInterval(refresh, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
