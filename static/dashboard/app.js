/* =====================================================================
   Tableline dashboard.

   One `api` object talks to the server (PRD §16b) and every screen goes
   through it. Nothing on this page invents data: if the server did not
   send it, the screen says so rather than showing a plausible number.

   Three rules the design depends on:
     - gold means exactly one thing: something needs your decision;
     - stale data is the worst bug here, so the freshness marker is driven
       by when the server answered, not by when we asked;
     - every mutating action guards against a second tap.
   ===================================================================== */

/* ---------------- token ---------------- */
/* The dashboard is opened from a long secret link and saved to a home
   screen. The token is taken out of the URL on first load so it does not
   sit in the address bar, in screenshots, or in the browser history. */
const TOKEN_KEY = "tl-token";

function readToken(){
  const fromUrl = new URLSearchParams(location.search).get("token");
  if(fromUrl){
    try{ localStorage.setItem(TOKEN_KEY, fromUrl); }catch(e){}
    history.replaceState(null, "", location.pathname);
    return fromUrl;
  }
  try{ return localStorage.getItem(TOKEN_KEY) || ""; }catch(e){ return ""; }
}
const TOKEN = readToken();

/* ---------------- api ---------------- */
/* Every method returns a Promise and may reject. Callers render the error
   state rather than assuming success — see fail() below. */

class ApiError extends Error {
  constructor(status, detail){
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options = {}){
  const headers = { "Accept": "application/json", ...(options.headers || {}) };
  if(TOKEN) headers["X-Tableline-Token"] = TOKEN;
  if(options.body !== undefined && !(options.body instanceof FormData)){
    headers["Content-Type"] = "application/json";
    options = { ...options, body: JSON.stringify(options.body) };
  }

  const response = await fetch(path, { ...options, headers });
  if(!response.ok){
    let detail = "";
    try{ detail = (await response.json()).detail || ""; }catch(e){}
    throw new ApiError(response.status, detail);
  }
  lastSync = Date.now();
  if(response.status === 204) return null;
  return response.json();
}

const api = {
  bootstrap    : ()                  => request("/api/bootstrap"),
  bookings     : date                => request(`/api/bookings?date=${encodeURIComponent(date)}`),
  updateBooking: (id, patch)         => request(`/api/bookings/${id}`, {method:"PATCH", body:patch}),
  createBooking: body                => request("/api/bookings", {method:"POST", body}),
  moveBooking  : (id, startTime)     => request(`/api/bookings/${id}`,
                                          {method:"PATCH", body:{start_time:startTime}}),
  resend       : id                  => request(`/api/bookings/${id}/resend`, {method:"POST"}),
  pending      : ()                  => request("/api/bookings/pending"),
  decide       : (id, accept, chan)  => request(`/api/bookings/${id}/decide`,
                                          {method:"POST", body:{accept, channel:chan}}),
  month        : (y, m)              => request(`/api/calendar?y=${y}&m=${m}`),
  day          : date                => request(`/api/calendar/${date}`),
  blocked      : ()                  => request("/api/blocked"),
  block        : (date, body)        => request(`/api/blocked/${date}`, {method:"PUT", body}),
  unblock      : date                => request(`/api/blocked/${date}`, {method:"DELETE"}),
  calls        : range               => request(`/api/calls?range=${encodeURIComponent(range)}`),
  transcript   : id                  => request(`/api/calls/${id}/transcript`),
  guests       : filter              => request(`/api/guests?filter=${encodeURIComponent(filter)}`),
  updateGuest  : (id, patch)         => request(`/api/guests/${id}`, {method:"PATCH", body:patch}),
  menu         : ()                  => request("/api/menu"),
  addDish      : body                => request("/api/menu", {method:"POST", body}),
  readMenu     : id                  => request(`/api/menu/upload/${id}/read`,
                                          {method:"POST"}),
  removeUpload : id                  => request(`/api/menu/upload/${id}`, {method:"DELETE"}),
  addDishes    : dishes              => request("/api/menu/bulk",
                                          {method:"POST", body:{dishes}}),
  uploadMenu   : file                => {
                                          const form=new FormData();
                                          form.append("file", file);
                                          return request("/api/menu/upload",
                                            {method:"POST", body:form});
                                        },
  updateDish   : (id, patch)         => request(`/api/menu/${id}`, {method:"PATCH", body:patch}),
  settings     : ()                  => request("/api/settings"),
  saveSettings : (section, values)   => request(`/api/settings/${section}`, {method:"PUT", body:values}),
  locales      : ()                  => request("/api/locales"),
  holidays     : ()                  => request("/api/holidays"),
  stats        : ()                  => request("/api/stats"),
  health       : ()                  => request("/api/health"),
};

/* Shown whenever a call rejects. Never a silent failure. */
function fail(el, retry, error){
  const why = error instanceof ApiError && error.status === 401
    ? "This dashboard link is no longer valid. Ask for a new one."
    : "Something went wrong between here and the server. Nothing has been changed.";
  el.innerHTML = `<div class="empty">
      <b style="display:block;color:var(--ink);margin-bottom:4px">That didn't load.</b>
      ${why}
      <div class="acts" style="margin-top:14px"><button class="btn">Try again</button></div>
    </div>`;
  el.querySelector(".btn").addEventListener("click", retry);
}

/* Shown while a call is in flight. */
function skeleton(rows){
  return Array.from({length: rows}, () =>
    `<tr><td colspan="5"><span class="sk"></span></td></tr>`).join("");
}

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* Errors stay until dismissed. Routine feedback fades only while nobody is
   reading or focusing it. No HTML from a server message reaches the page. */
function showToast(message, kind = "error"){
  const host=document.getElementById("toasts");
  if(!host) return;
  const text=String(message);
  if([...host.children].some(t=>t.querySelector(".note").textContent===text)) return;
  const toast=document.createElement("div");
  toast.className=`toast ${kind}`;
  const note=document.createElement("span");
  note.className="note";
  note.setAttribute("role", kind==="error" ? "alert" : "status");
  note.textContent=text;
  const close=document.createElement("button");
  close.className="toast-close";
  close.type="button";
  close.setAttribute("aria-label", "Dismiss notification");
  close.textContent="×";
  let timer;
  const remove=()=>{ clearTimeout(timer); toast.remove(); };
  const pause=()=>clearTimeout(timer);
  const resume=()=>{
    pause();
    if(kind!=="error" && !toast.matches(":hover") && !toast.contains(document.activeElement)){
      timer=setTimeout(remove,6000);
    }
  };
  close.addEventListener("click",remove);
  toast.addEventListener("pointerenter",pause);
  toast.addEventListener("pointerleave",resume);
  toast.addEventListener("focusin",pause);
  toast.addEventListener("focusout",()=>setTimeout(resume,0));
  toast.append(note,close);
  host.append(toast);
  resume();
}

/* The head script already decided whether this plays, and the CSS animation
   times itself from the first paint. What is left here is letting somebody
   skip it, and clearing up if the logo never loads.

   Must match the CSS: --reveal-hold + --reveal-fade, plus a little slack. */
const REVEAL_TOTAL_MS = 4200;

function revealBrand(){
  const root=document.documentElement;
  if(root.getAttribute("data-reveal")!=="on") return;   // the head script said no

  const reveal=document.getElementById("brandReveal");
  const logo=document.getElementById("revealLogo");
  // Preloaded in <head> when this was decided, so this is a cache hit. Set
  // here rather than in the markup so a load that skips the reveal never
  // asks for the image at all.
  logo.src="/static/dashboard/alpinecall-logo.webp";
  reveal.hidden=false;               // `hidden` stays the honest signal

  let timer;
  const finish=()=>{
    reveal.hidden=true;
    root.removeAttribute("data-reveal");
    clearTimeout(timer);
    document.removeEventListener("pointerdown",skip,true);
    document.removeEventListener("keydown",skip,true);
  };
  const skip=e=>{
    // Consume the activation so a skip cannot also seat or accept a booking.
    if(e.type==="pointerdown" || e.key==="Enter" || e.key===" ") e.preventDefault();
    finish();
  };
  reveal.addEventListener("click",finish,{once:true});
  logo.addEventListener("error",finish,{once:true});     // a missing logo is not a gate
  document.addEventListener("pointerdown",skip,true);
  document.addEventListener("keydown",skip,true);
  timer=setTimeout(finish,REVEAL_TOTAL_MS);
}

/* ---------------- state ---------------- */
/* Everything the server told us. No screen keeps its own copy of the truth. */
const state = {
  business: null,
  locale: null,
  config: null,
  rules: [],
  today: null,
  windowLastDay: null,
  countries: [],
  languages: [],
  holidaysSupported: false,
};

/* ---------------- theme ---------------- */
const root=document.documentElement;
function setTheme(t){
  root.setAttribute("data-theme",t);
  document.querySelectorAll("#theme button").forEach(b=>b.classList.toggle("on",b.dataset.t===t));
  try{localStorage.setItem("tl-theme",t)}catch(e){}
}
document.getElementById("theme").addEventListener("click",e=>{
  const b=e.target.closest("button[data-t]"); if(b) setTheme(b.dataset.t);
});
try{
  const saved=localStorage.getItem("tl-theme");
  setTheme(saved || (matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"));
}catch(e){ setTheme("light"); }

/* ---------------- formatting ---------------- */
/* The server sends times already written the venue's way. These two are for
   the few places the client composes a string itself. */
const money = n => `${state.locale?.currency_symbol ?? ""} ${n}`;

function longDate(iso){
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  const order = state.locale?.date_format || "DMY";
  const opts = order === "MDY"
    ? {weekday:"long", month:"long", day:"numeric"}
    : {weekday:"long", day:"numeric", month:"long"};
  return d.toLocaleDateString(order === "MDY" ? "en-US" : "en-GB", opts);
}

function shortDate(iso){
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  return d.toLocaleDateString("en-GB", {day:"numeric", month:"long"});
}

function clockTime(iso){
  const d = new Date(iso);
  const tz = state.business?.timezone;
  return d.toLocaleTimeString(state.locale?.clock === "24" ? "en-GB" : "en-US",
    {hour:"numeric", minute:"2-digit", hour12: state.locale?.clock !== "24", timeZone: tz});
}

/* ---------------- bookings ---------------- */
let dayKey="tonight";
let data=[];
let openRows=new Set();
let bookingsLoadedFor=null, bookingsRequest=0;
document.getElementById("rows").innerHTML=skeleton(6);

const LABEL={
  arrived:["Seated","ok"],
  confirmed:["Confirmed","ok"],
  no_show:["Did not arrive","no"],
  cancelled:["Cancelled","no"],
  pending:["Waiting on you","act"],
  declined:["Declined","no"],
};

function drawBookings(changes = new Map()){
  const q=(document.getElementById("q").value||"").toLowerCase();
  const rows=document.getElementById("rows");
  const list=data.filter(b=>!q||[b.name,b.phone,b.reference].join(" ").toLowerCase().includes(q));
  if(!list.length){
    rows.innerHTML=`<tr><td colspan="5" class="empty">${
      data.length
        ? "Nothing matches that. Try a surname or the last digits of a number."
        : "Nothing booked yet. The agent is still taking calls for this day."
    }</td></tr>`;
    return;
  }

  let html="",block=null;
  list.forEach(b=>{
    if(b.block!==block){
      block=b.block;
      const free=freeInBlock(block);
      html+=`<tr class="block"><td colspan="5">${esc(block)}${
        free===null ? "" :
        `<span class="free ${free<=4?"tight":""}">${
          free<=0?"nothing left":free+" "+esc(state.business.unit_plural)+" still free"}</span>`
      }</td></tr>`;
    }
    const i=data.indexOf(b);
    const [label,tone]=LABEL[b.status]||[b.status,""];
    const motion=changes.get?.(b.id)||"";
    html+=`<tr class="b ${b.status==="arrived"?"past":""} ${b.status==="no_show"?"missed":""} ${seenIds.has(b.id)?"":"unseen"} ${motion}" data-i="${i}" style="--row-delay:${Math.min(i,5)*20}ms">
      <td>${esc(b.time)}</td>
      <td>${esc(b.name)}${b.meta?`<div class="meta">${esc(b.meta)}</div>`:""}</td>
      <td class="r">${b.party_size}</td>
      <td><span class="pill ${tone}">${esc(label)}</span></td>
      <td class="mono col-hide">${esc(b.reference)}</td>
    </tr>
    <tr class="detail ${openRows.has(i)?"open":""}" id="d${i}"><td colspan="5">
      <div class="dl">
        <div><dt>Phone</dt><dd>${esc(b.phone||"Withheld")}</dd></div>
        <div><dt>Reference</dt><dd class="mono">${esc(b.reference)}</dd></div>
        <div><dt>History</dt><dd>${b.visits===0?"New guest":b.visits+" previous visits"}${
          b.no_shows?` · ${b.no_shows} no-show${b.no_shows>1?"s":""}`:""}</dd></div>
      </div>
      ${b.notes?`<ul class="tl"><li><time>Note</time>${esc(b.notes)}</li></ul>`:""}
      <div class="acts">${actionsFor(i,b)}</div>
    </td></tr>`;
  });
  rows.innerHTML=html;
}

function actionsFor(i,b){
  /* Call is a plain tel: link, not a button: on the tablet by the door it
     should hand off to whatever dials, and on a desktop it should do
     nothing surprising. Resend needs a number to send to. */
  const reach = (b.phone ? `<a class="btn" href="tel:${esc(b.phone)}">Call</a>` : "")
    + (b.phone ? `<button class="btn" data-act="resend" data-i="${i}">Resend text</button>` : "");
  if(b.status==="confirmed"){
    return `<button class="btn key" data-act="arrived" data-i="${i}">Mark arrived</button>
      <button class="btn no" data-act="no_show" data-i="${i}">Did not arrive</button>
      <button class="btn" data-act="move" data-i="${i}">Move</button>
      ${reach}
      <button class="btn no" data-act="cancelled" data-i="${i}">Cancel</button>`;
  }
  if(b.status==="arrived"||b.status==="no_show"){
    return `<button class="btn" data-act="confirmed" data-i="${i}">Undo</button>`;
  }
  return "";
}

/* How much of this time block is still sellable. null when the venue does
   not track capacity, so the header simply says nothing. */
function freeInBlock(block){
  if(!state.business?.tracks_capacity) return null;
  const found=(window.__blocks||[]).find(x=>x.label===block);
  if(!found||found.capacity==null) return null;
  return Math.max(0, found.capacity-found.committed);
}

document.getElementById("rows").addEventListener("click", async e=>{
  const button=e.target.closest("button[data-act]");
  if(!button) return;
  e.stopPropagation();
  if(button.dataset.busy) return;                   // guards a double tap
  button.dataset.busy="1"; button.classList.add("busy");

  const booking=data[+button.dataset.i];
  const act=button.dataset.act;
  /* A status change redraws the whole list, so its button is gone and never
     needs unbusying. Move and resend leave the row where it is, so they do
     -- hence the finally rather than a clear in the catch. */
  let redrawn=false;
  try{
    if(act==="move"){
      askToMove(booking);
    }else if(act==="resend"){
      await api.resend(booking.id);
      button.textContent="Text sent";
    }else{
      await api.updateBooking(booking.id,{status:act});
      await loadBookings();
      redrawn=true;
    }
  }catch(err){
    showToast(err.detail || "That didn't go through. Nothing has been changed.");
  }finally{
    if(!redrawn){ button.classList.remove("busy"); delete button.dataset.busy; }
  }
});

/* Moving a booking re-contends for capacity server-side, so a move into a
   full slot is refused rather than silently overbooking. */
function askToMove(booking){
  const at=new Date(booking.start_time);
  const pad=n=>String(n).padStart(2,"0");
  openSheet({
    title:`Move ${booking.name}`,
    note:"The new time is checked against capacity, the same as a new booking.",
    ok:"Move it",
    fields:[
      { name:"date", label:"New day", type:"date", required:true,
        value:`${at.getFullYear()}-${pad(at.getMonth()+1)}-${pad(at.getDate())}` },
      { name:"time", label:"New time", type:"time", required:true,
        value:`${pad(at.getHours())}:${pad(at.getMinutes())}` },
    ],
    submit: async v => {
      await api.moveBooking(booking.id, localInstant(v.date, v.time));
      await loadBookings();
    },
  });
}

const DAY_LABEL={tonight:"Tonight",tomorrow:"Tomorrow",week:"This week"};

async function loadBookings(){
  const rows=document.getElementById("rows");
  const requestedDay=dayKey, requestId=++bookingsRequest;
  const sameDay=bookingsLoadedFor===requestedDay;
  const previous=new Map(sameDay ? data.map(b=>[b.id,b.status]) : []);
  const opened=new Set(sameDay ? [...openRows].map(i=>data[i]?.id) : []);
  // A first load or a different filter gets skeletons. A background refresh
  // keeps the current list readable and animates only actual changes.
  if(!sameDay) rows.innerHTML=skeleton(6);
  rows.setAttribute("aria-busy","true");
  document.getElementById("dayTitle").textContent=DAY_LABEL[dayKey]||dayKey;
  try{
    const payload=await api.bookings(requestedDay);
    if(requestId!==bookingsRequest) return;
    data=payload.bookings;
    bookingsLoadedFor=requestedDay;
    noticeBookings(data);
    window.__blocks=payload.blocks;
    openRows=new Set(data.flatMap((b,i)=>opened.has(b.id)?[i]:[]));
    const changes=new Map(data.flatMap(b=>
      !previous.has(b.id) ? [[b.id,"arriving"]]
      : previous.get(b.id)!==b.status ? [[b.id,"status-changed"]] : []));
    freshness();
    drawBookings(changes);
    // The server names the day: "Tonight" is the venue's tonight, and a
    // specific date is written the way this venue writes dates.
    document.getElementById("dayTitle").textContent=payload.title;
    fillBookingsFoot(payload.last_seating);
    const s=payload.summary;
    document.getElementById("daySummary").textContent=
      `${s.covers} ${s.unit_plural} across ${s.count} booking${s.count===1?"":"s"}.`+
      (dayKey==="tonight"&&s.seated?` ${s.seated} ${s.seated===1?"is":"are"} seated now.`:"");
  }catch(e){
    if(requestId!==bookingsRequest) return;
    if(sameDay){
      showToast("Couldn't refresh bookings. You're still seeing the previous list.");
    }else{
      // Preserve the table and its listeners so retry can really recover.
      rows.innerHTML=`<tr><td colspan="5" class="empty">Couldn't load bookings for this day.
        <button class="btn" type="button">Try again</button></td></tr>`;
      rows.querySelector("button").addEventListener("click",loadBookings);
    }
  }finally{
    if(requestId===bookingsRequest) rows.setAttribute("aria-busy","false");
  }
}

document.getElementById("daytabs").addEventListener("click",e=>{
  const b=e.target.closest("button"); if(!b) return;
  document.querySelectorAll("#daytabs button").forEach(x=>x.classList.toggle("on",x===b));
  dayKey=["tonight","tomorrow","week"][[...b.parentElement.children].indexOf(b)];
  loadBookings();
});
document.getElementById("q").addEventListener("input",drawBookings);

/* ---------------- waiting on you ---------------- */
let pendingId=null, pendingSeenOnce=false, pendingRequest=0, pendingBusy=false;
let chan="text";

function emptyPending(message){
  document.getElementById("pending").classList.remove("needs-decision","pending-arrival");
  document.getElementById("pendingTable").classList.add("hide");
  const empty=document.getElementById("pendingEmpty");
  empty.classList.remove("hide","pending-loading");
  empty.textContent=message;
  document.getElementById("pendingNote").textContent="Nothing right now.";
}

async function loadPending(){
  if(pendingBusy) return;
  const requestId=++pendingRequest;
  const card=document.getElementById("pending");
  try{
    const payload=await api.pending();
    if(requestId!==pendingRequest) return;
    if(!payload.pending){
      pendingSeenOnce=true; pendingId=null;
      emptyPending("Everything the agent took is already confirmed.");
      return;
    }
    const p=payload.pending;
    const incoming=pendingId!==p.id;
    if(pendingSeenOnce && incoming) tone("decision");
    pendingSeenOnce=true;
    pendingId=p.id;
    if(incoming) chan=payload.defaults?.on_accept==="call"?"call":"text";
    document.getElementById("pendingTable").classList.remove("hide");
    document.getElementById("pendingEmpty").classList.add("hide");
    document.getElementById("pendingNote").textContent="Everything else is already confirmed.";
    card.classList.add("needs-decision");
    if(incoming){
      card.classList.add("pending-arrival");
      card.addEventListener("animationend",()=>card.classList.remove("pending-arrival"),{once:true});
    }
    document.getElementById("pT").textContent=p.time;
    document.getElementById("pN").textContent=p.name;
    document.getElementById("pW").textContent=
      `${p.why} If you do nothing it decides itself at ${p.deadline}`+
      (p.alternative?` and offers them ${p.alternative}.`:".");
    document.querySelectorAll("#chan button").forEach(b=>
      b.classList.toggle("on", b.dataset.c===chan));
  }catch(e){
    if(requestId!==pendingRequest) return;
    if(pendingId){
      document.getElementById("pW").textContent="Couldn't refresh this request. Try again shortly.";
    }else{
      emptyPending("Couldn't load requests. Try again shortly.");
      document.getElementById("pendingNote").textContent="Waiting for the server.";
    }
  }
}

document.getElementById("chan")?.addEventListener("click",e=>{
  const b=e.target.closest("button[data-c]"); if(!b) return;
  chan=b.dataset.c;
  document.querySelectorAll("#chan button").forEach(x=>x.classList.toggle("on",x===b));
});

["acceptBtn","declineBtn"].forEach(id=>{
  document.getElementById(id)?.addEventListener("click",e=>{
    const btn=e.currentTarget;
    if(btn.dataset.busy || !pendingId) return;      // guards a double tap
    pendingBusy=true;
    ++pendingRequest;                             // invalidate a poll in flight
    const other=document.getElementById(id==="acceptBtn"?"declineBtn":"acceptBtn");
    btn.dataset.busy="1"; btn.classList.add("busy"); other.disabled=true;

    api.decide(pendingId, id==="acceptBtn", chan)
      .then(result=>settle(result))
      .catch(err=>{
        document.getElementById("pW").textContent=
          err.detail || "That didn't go through. Nothing was sent — try again.";
      }).finally(()=>{
        pendingBusy=false;
        btn.classList.remove("busy"); delete btn.dataset.busy; other.disabled=false;
      });
  });
});

function settle(result){
  const who=result.booking.name;
  const when=result.booking.time;
  pendingId=null;
  emptyPending(`${who} ${result.accepted
    ? `is confirmed for ${when}.`
    : "has been declined."} ${result.told}`);
  loadBookings();
  setTimeout(loadPending, 400);
}


/* ---------------- the sheet ---------------- */
/* One overlay, four callers. A native <dialog> rather than a div, because
   focus trapping, Esc, and making the page behind inert come with the
   element -- hand-rolling those is how a form overlay ends up unusable with
   a keyboard and invisible to a screen reader.

   Fields are described, not written as markup, so every form on this page
   gets the same label association, the same 44px touch targets and the same
   error surface without four chances to forget one. */

const sheet      = document.getElementById("sheet");
const sheetForm  = document.getElementById("sheetForm");
const sheetBody  = document.getElementById("sheetBody");
const sheetErr   = document.getElementById("sheetErr");
const sheetOk    = document.getElementById("sheetOk");
let onSheetSubmit = null;

function field(f){
  const id = `sf-${f.name}`;
  const span = f.wide === false ? "" : ` style="grid-column:1/-1"`;
  const help = f.help ? `<div class="help">${esc(f.help)}</div>` : "";
  const required = f.required ? " required" : "";
  if(f.type === "select"){
    const options = (f.options || []).map(o =>
      `<option value="${esc(o.value)}"${o.value === f.value ? " selected" : ""}>${esc(o.label)}</option>`
    ).join("");
    return `<div class="f"${span}><label for="${id}">${esc(f.label)}</label>
      <select id="${id}" name="${esc(f.name)}"${required}>${options}</select>${help}</div>`;
  }
  if(f.type === "textarea"){
    return `<div class="f"${span}><label for="${id}">${esc(f.label)}</label>
      <textarea id="${id}" name="${esc(f.name)}"${required}>${esc(f.value ?? "")}</textarea>${help}</div>`;
  }
  const extra = ["min","max","step","placeholder","inputmode"]
    .filter(k => f[k] !== undefined).map(k => ` ${k}="${esc(f[k])}"`).join("");
  return `<div class="f"${span}><label for="${id}">${esc(f.label)}</label>
    <input id="${id}" name="${esc(f.name)}" type="${esc(f.type || "text")}"
      value="${esc(f.value ?? "")}"${extra}${required}>${help}</div>`;
}

/* `submit` receives the values and may throw: the message is shown in the
   sheet and the sheet stays open, because closing it would lose what they
   typed and tell them nothing. */
function openSheet({ title, note = "", fields = [], ok = "Save", submit }){
  document.getElementById("sheetTitle").textContent = title;
  document.getElementById("sheetNote").textContent = note;
  sheetBody.innerHTML = fields.map(field).join("");
  sheetOk.textContent = ok;
  sheetErr.classList.remove("on");
  sheetErr.textContent = "";
  onSheetSubmit = submit;
  sheet.showModal();
  sheetBody.querySelector("input,select,textarea")?.focus();
}

document.getElementById("sheetCancel").addEventListener("click", () => sheet.close());

sheetForm.addEventListener("submit", async e => {
  e.preventDefault();                       // we close on success, not on submit
  if(sheetOk.dataset.busy) return;          // guards a second tap
  sheetOk.dataset.busy = "1";
  sheetOk.classList.add("busy");
  sheetErr.classList.remove("on");

  const values = Object.fromEntries(new FormData(sheetForm).entries());
  try{
    await onSheetSubmit(values);
    sheet.close();
  }catch(err){
    sheetErr.textContent = err instanceof ApiError && err.detail
      ? err.detail
      : "That did not go through. Nothing has been changed.";
    sheetErr.classList.add("on");
  }finally{
    delete sheetOk.dataset.busy;
    sheetOk.classList.remove("busy");
  }
});

/* The venue's own today, not the browser's: a tablet left on UTC would
   otherwise offer to book yesterday. */
const venueToday = () => state.today || new Date().toISOString().slice(0,10);

/* A local date and time as the instant the server should store. The server
   reads it with fromisoformat and applies the venue's timezone, so no
   offset is invented here. */
const localInstant = (day, time) => `${day}T${time.length === 5 ? time + ":00" : time}`;

/* ---------------- add a booking ---------------- */

document.getElementById("addBooking").addEventListener("click", () => {
  openSheet({
    title: "Add a booking",
    note: "This takes capacity exactly as the agent does, so it cannot overbook.",
    ok: "Add booking",
    fields: [
      { name:"name",  label:"Name", required:true, wide:false },
      { name:"phone", label:"Phone", type:"tel", wide:false,
        help:"Optional. Without one they get no confirmation text." },
      { name:"date",  label:"Day", type:"date", value:venueToday(), required:true, wide:false },
      { name:"time",  label:"Time", type:"time", value:"19:00", required:true, wide:false },
      { name:"party_size", label:`How many ${state.business?.unit_plural || "covers"}`,
        type:"number", min:1, value:2, required:true, wide:false },
      { name:"notes", label:"Notes", type:"textarea",
        help:"Allergies, occasion, access needs, a quiet table." },
    ],
    submit: async v => {
      await api.createBooking({
        start_time: localInstant(v.date, v.time),
        party_size: Number(v.party_size),
        name: v.name,
        phone: v.phone,
        notes: v.notes,
      });
      loadBookings();
    },
  });
});

/* ---------------- calendar ---------------- */
let month=null, year=null, selected=null, monthData=null, dayView="time";

async function loadMonth(){
  const grid=document.getElementById("cal");
  document.getElementById("moLabel").textContent=
    new Date(year, month-1, 1).toLocaleDateString("en-GB",{month:"long",year:"numeric"});
  try{
    monthData=await api.month(year, month);
    drawCal();
  }catch(e){ fail(grid.closest(".card"), loadMonth, e); }
}

function drawCal(){
  if(!monthData) return;
  const first=(new Date(year, month-1, 1).getDay()+6)%7;
  const short=innerWidth<560;
  let html=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    .map(d=>`<div class="dow">${short?d[0]:d}</div>`).join("");
  for(let i=0;i<first;i++) html+=`<div class="d void"></div>`;

  monthData.days.forEach(day=>{
    const n=+day.date.slice(-2);
    const isSel=day.date===selected;
    const isToday=day.date===monthData.today;
    if(!day.in_window){
      html+=`<div class="d locked ${isSel?"sel":""}" data-d="${day.date}" tabindex="0" role="button">
        <div class="n">${n}</div><div class="c">Not open yet</div></div>`;
      return;
    }
    if(day.closed){
      html+=`<div class="d shut ${isSel?"sel":""}" data-d="${day.date}" tabindex="0" role="button">
        <div class="n">${n}</div><div class="c">${esc(day.reason)}</div></div>`;
      return;
    }
    if(!monthData.tracks_capacity){
      html+=`<div class="d ${isSel?"sel":""} ${isToday?"today":""}" data-d="${day.date}" tabindex="0" role="button">
        <div class="n">${n}</div>
        <div class="c">${day.covers} ${esc(state.business.unit_plural)}<br>${day.bookings} booking${day.bookings===1?"":"s"}</div>
      </div>`;
      return;
    }
    const pct=day.capacity?Math.round(day.peak/day.capacity*100):0;
    html+=`<div class="d ${isSel?"sel":""} ${isToday?"today":""} ${pct>=100?"hot":""}" data-d="${day.date}" tabindex="0" role="button">
      <div class="n">${n}</div>
      <div class="c">${day.covers} ${esc(state.business.unit_plural)}</div>
      <div class="bar"><i style="width:${Math.min(100,pct)}%" title="${day.peak} of ${day.capacity} at the busiest moment"></i></div>
    </div>`;
  });
  document.getElementById("cal").innerHTML=html;
  if(selected) loadDay(selected);
}

async function loadDay(date){
  selected=date;
  const body=document.getElementById("dayBody");
  try{
    const day=await api.day(date);
    drawDay(day);
  }catch(e){ fail(body, ()=>loadDay(date), e); }
}

function drawDay(day){
  const head=document.getElementById("dayHead");
  const sub=document.getElementById("daySub");
  const body=document.getElementById("dayBody");
  head.textContent=longDate(day.date);

  const inWindow=monthData?.days.find(d=>d.date===day.date)?.in_window ?? true;
  if(!inWindow){
    sub.textContent=`Outside your booking window. The agent takes nothing after ${shortDate(monthData.window_last_day)}.`;
    body.innerHTML=`<div class="empty">The agent won't book this day yet. If someone asks, it offers to take a number and ring them back.</div>`;
    return;
  }
  if(day.closed){
    sub.textContent=`${day.reason}. The agent tells callers you're shut and offers another day.`;
    body.innerHTML=`<div class="empty">Nothing booked.</div>`;
    return;
  }

  sub.textContent = day.tracks_capacity
    ? `${day.covers} ${state.business.unit_plural} across ${day.booking_count} bookings.`+
      (day.peak_at
        ? ` Busiest at ${clockTime(day.peak_at)}, when ${day.peak} of your ${day.capacity}`+
          ` bookable ${state.business.unit_plural} are taken`+
          (day.reduced ? " — you've reduced capacity for this day." : ".")
        : (day.reduced ? " You've reduced capacity for this day." : ""))
    : `${day.covers} ${state.business.unit_plural} across ${day.booking_count} bookings. You're not tracking capacity, so the agent takes everything.`;

  if(dayView==="list"){
    body.innerHTML = day.bookings.length
      ? `<table><tbody>`+day.bookings.map(b=>
          `<tr class="b"><td style="width:118px">${clockTime(b.start_time)}</td><td>${esc(b.name)}</td>
           <td class="r" style="width:78px">${b.party_size}</td>
           <td style="width:128px"><span class="pill ${b.status==="pending"?"act":"ok"}">${esc((LABEL[b.status]||[b.status])[0])}</span></td></tr>`).join("")+`</tbody></table>`
      : `<div class="empty">Nothing booked yet. The agent is still taking calls for this day.</div>`;
    return;
  }

  let html="";
  day.services.forEach(service=>{
    html+=`<div class="svc">${esc(service.label||"Service")} &mdash; ${clockTime(service.starts_at)} to ${clockTime(service.ends_at)}</div>`;
    service.slots.forEach(slot=>{
      const arrivals=day.bookings.filter(b=>b.start_time===slot.start && b.status!=="pending");
      const pct=slot.capacity?Math.round(slot.committed/slot.capacity*100):0;
      const full=day.tracks_capacity && slot.committed>=slot.capacity;
      html+=`<div class="slot ${full?"full":""} ${arrivals.length?"":"open-slot"}">
        <div class="st">${clockTime(slot.start)}</div>
        ${day.tracks_capacity?`<div class="cap">
          <span class="sbar"><i style="width:${Math.min(100,pct)}%"></i></span>
          <span class="num">${slot.committed} of ${slot.capacity}</span>
        </div>`:""}
        <div class="guests">${
          arrivals.length
            ? arrivals.map(b=>`<span class="chip">${esc(b.name)}<b>${b.party_size}</b></span>`).join("")
            : `<span class="none">${day.tracks_capacity
                ?(full?"No room left":`${slot.capacity-slot.committed} free, nobody arriving`)
                :"Nobody arriving"}</span>`
        }</div>
      </div>`;
    });
  });
  body.innerHTML=html;
  body.classList.toggle("notrack", !day.tracks_capacity);
}

function moveMonth(step){
  month+=step;
  if(month<1){ month=12; year--; }
  if(month>12){ month=1; year++; }
  selected=null;
  loadMonth();
}
function goToday(){
  const [y,m,d]=state.today.split("-").map(Number);
  year=y; month=m; selected=state.today;
  loadMonth();
}
window.moveMonth=moveMonth;
window.goToday=goToday;

document.getElementById("cal").addEventListener("click",e=>{
  const cell=e.target.closest(".d[data-d]"); if(!cell) return;
  selected=cell.dataset.d;
  drawCal();
  document.getElementById("dayCard").scrollIntoView({behavior:"smooth",block:"start"});
});
document.getElementById("cal").addEventListener("keydown",e=>{
  const cell=e.target.closest(".d[data-d]"); if(!cell) return;
  if(e.key==="Enter"||e.key===" "){ e.preventDefault(); cell.click(); }
});
document.getElementById("vTime").addEventListener("click",()=>{
  dayView="time";
  document.getElementById("vTime").classList.add("key");
  document.getElementById("vList").classList.remove("key");
  if(selected) loadDay(selected);
});
document.getElementById("vList").addEventListener("click",()=>{
  dayView="list";
  document.getElementById("vList").classList.add("key");
  document.getElementById("vTime").classList.remove("key");
  if(selected) loadDay(selected);
});

/* ---------------- blocked dates ---------------- */
async function loadBlocked(){
  const body=document.getElementById("blockedRows");
  if(!body) return;
  try{
    const payload=await api.blocked();
    const weekdayNames=["Mondays","Tuesdays","Wednesdays","Thursdays","Fridays","Saturdays","Sundays"];
    let rows=payload.closed_weekdays.map(wd=>
      `<tr><td>Every ${weekdayNames[wd]}</td><td>You're closed all day</td>
        <td><span class="pill">Closed</span></td><td class="r"></td></tr>`);

    rows=rows.concat(payload.rows.map(r=>
      `<tr><td>${esc(r.label)}</td><td>${esc(r.why)}</td>
        <td><span class="pill">${esc(r.effect)}</span></td>
        <td class="r"><button class="btn sm" data-block="${r.date}" data-kind="${r.kind}">${
          r.kind==="holiday"?"Open anyway":"Remove"}</button></td></tr>`));

    body.innerHTML = rows.length ? rows.join("")
      : `<tr><td colspan="4" class="empty">Nothing blocked. ${
          payload.holidays_supported
            ? "Public holidays for your country appear here automatically."
            : "No public holidays on file for your country — block those days yourself."}</td></tr>`;
  }catch(e){
    body.innerHTML=`<tr><td colspan="4" class="empty">Couldn't load this.</td></tr>`;
  }
}

document.getElementById("blockedRows")?.addEventListener("click", async e=>{
  const button=e.target.closest("button[data-block]"); if(!button) return;
  if(button.dataset.busy) return;
  button.dataset.busy="1"; button.classList.add("busy");
  try{
    if(button.dataset.kind==="holiday"){
      await api.block(button.dataset.block, {type:"open", reason:"Open as usual"});
    } else {
      await api.unblock(button.dataset.block);
    }
    await loadBlocked();
    await loadMonth();
  }catch(err){
    button.classList.remove("busy"); delete button.dataset.busy;
    showToast(err.detail || "That didn't go through.");
  }
});

/* ---------------- calls ---------------- */
let callRange="today";
const RANGE_LABEL={today:"Today","30":"Last 30 days","90":"Last 90 days","365":"Last 12 months"};

async function loadCalls(){
  const body=document.getElementById("calls");
  body.innerHTML=skeleton(4);
  document.getElementById("callsTitle").textContent=RANGE_LABEL[callRange];
  try{
    const payload=await api.calls(callRange);
    if(!payload.calls.length){
      body.innerHTML=`<tr><td colspan="5" class="empty">No calls in this period.</td></tr>`;
      return;
    }
    body.innerHTML=payload.calls.map(c=>`
      <tr class="b" data-call="${c.id}"><td>${esc(c.time)}</td><td class="mono">${esc(c.caller)}</td>
        <td class="col-hide">${esc(c.duration)}</td>
        <td><span class="pill ${c.needs_attention?"act":""}">${esc(c.outcome)}</span></td>
        <td class="mono col-hide">${esc(c.reference)}</td></tr>
      <tr class="detail" id="call-${c.id}"><td colspan="5">
        <div class="empty">Opening…</div>
      </td></tr>`).join("");
  }catch(e){ fail(body.closest(".card"), loadCalls, e); }
}

async function openTranscript(id){
  const cell=document.querySelector(`#call-${CSS.escape(id)} td`);
  if(!cell) return;
  try{
    const payload=await api.transcript(id);
    const turns=(payload.turns||[]).map(turn=>{
      const who=turn.role==="tool"?"tool":(turn.role==="user"?"Caller":"Agent");
      return `<div class="turn ${who==="tool"?"tool":""}">
        <span class="w">${who==="tool"?"Agent":who}</span><span>${esc(turn.text||"")}</span></div>`;
    }).join("");
    cell.innerHTML=(payload.recording_url
      ? `<div class="acts" style="margin-bottom:16px"><audio controls src="${esc(payload.recording_url)}"></audio></div>`
      : "")+(turns||`<div class="empty">No transcript was kept for this call.</div>`);
  }catch(e){
    cell.innerHTML=`<div class="empty">Couldn't load the transcript.</div>`;
  }
}

document.getElementById("calltabs").addEventListener("click",e=>{
  const b=e.target.closest("button"); if(!b) return;
  document.querySelectorAll("#calltabs button").forEach(x=>x.classList.toggle("on",x===b));
  callRange=["today","30","90","365"][[...b.parentElement.children].indexOf(b)];
  loadCalls();
});

/* ---------------- guests ---------------- */
let guestFilter="all";
const G_EMPTY={all:"No guests yet. They appear here after the first call.",
  regulars:"Nobody has been in four times yet.",
  noshows:"Nobody has failed to turn up. Good sign.",
  dnc:"Nobody has asked not to be called."};

async function loadGuests(){
  const body=document.getElementById("guestRows");
  body.innerHTML=skeleton(5);
  try{
    const payload=await api.guests(guestFilter);
    window.__guests=payload.guests;              // exactly what Export CSV writes
    document.getElementById("guestCount").textContent=
      `${payload.guests.length} guest${payload.guests.length===1?"":"s"}`;
    body.innerHTML = payload.guests.length
      ? payload.guests.map(g=>`<tr class="b">
          <td>${esc(g.name)}${g.note?`<div class="meta">${esc(g.note)}</div>`:""}${
            g.do_not_call?`<div class="meta">Asked not to be called</div>`:""}</td>
          <td class="mono col-hide">${esc(g.phone)}</td>
          <td class="r">${g.visits}</td>
          <td class="r">${g.no_shows?`<span class="pill no">${g.no_shows}</span>`:"0"}</td>
          <td>${esc(g.last)}</td></tr>`).join("")
      : `<tr><td colspan="5" class="empty">${G_EMPTY[guestFilter]}</td></tr>`;
  }catch(e){ fail(body.closest(".card"), loadGuests, e); }
}

document.getElementById("guesttabs").addEventListener("click",e=>{
  const b=e.target.closest("button"); if(!b) return;
  document.querySelectorAll("#guesttabs button").forEach(x=>x.classList.toggle("on",x===b));
  guestFilter=["all","regulars","noshows","dnc"][[...b.parentElement.children].indexOf(b)];
  loadGuests();
});

/* ---------------- menu ---------------- */
async function loadMenu(){
  const body=document.getElementById("menuRows");
  if(!body) return;
  try{
    const payload=await api.menu();
    const count=payload.items.length;
    document.getElementById("menuListNote").textContent=
      `${count} dish${count===1?"":"es"}. Turn one off when the kitchen runs out.`;
    body.innerHTML = payload.items.length
      ? payload.items.map(item=>`<tr class="${item.available?"":"past"}" data-dish="${item.id}">
          <td>${esc(item.name)}${item.tags.length?`<div class="meta">${
            item.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</div>`:""}</td>
          <td class="col-hide">${esc(item.description)}</td>
          <td class="r">${esc(item.price_display)}</td>
          <td class="r"><span class="sw ${item.available?"on":""}" data-avail="${item.id}"></span></td>
        </tr>`).join("")
      : `<tr><td colspan="4" class="empty">Nothing on the menu yet. Add a dish and the agent can answer about it.</td></tr>`;
    window.__upload = payload.upload;
    document.getElementById("menuRead").hidden =
      !(payload.upload && payload.reader_configured);
    drawMenuShot(payload.upload);
  }catch(e){
    body.innerHTML=`<tr><td colspan="4" class="empty">Couldn't load the menu.</td></tr>`;
  }
}

/* The printed menu, shown so the owner can read it while typing. The image
   is fetched through request() rather than put straight in a src, because
   the endpoint needs the dashboard token and an <img> cannot send one. */
async function drawMenuShot(upload){
  const host=document.getElementById("menuShot");
  if(!host) return;
  host.innerHTML="";
  if(!upload) return;

  if(upload.content_type==="application/pdf"){
    host.innerHTML=`<p style="font-size:13px;color:var(--ink-3);margin-top:10px">A PDF menu is saved. Open it on your computer to type from it.</p>`;
    return;
  }
  try{
    const headers=TOKEN?{ "X-Tableline-Token":TOKEN }:{};
    const response=await fetch(`/api/menu/upload/${upload.id}`,{headers});
    if(!response.ok) return;
    const url=URL.createObjectURL(await response.blob());
    host.innerHTML=`<img alt="The menu you uploaded" src="${url}"
      style="margin-top:14px;max-width:100%;border:1px solid var(--line);border-radius:10px">
      <div class="acts" style="margin-top:10px">
        <button class="btn" id="menuForget">Remove this photo</button>
        <span style="font-size:13px;color:var(--ink-3)">Dishes you already added stay. Upload another to replace it.</span>
      </div>`;
    host.querySelector("img").addEventListener("load",()=>URL.revokeObjectURL(url));
    host.querySelector("#menuForget").addEventListener("click", async e=>{
      const b=e.target; if(b.dataset.busy) return;
      b.dataset.busy="1"; b.classList.add("busy");
      try{ await api.removeUpload(upload.id); proposed=[]; drawReview(); await loadMenu(); }
      catch(err){ showToast(err.detail || "Couldn't remove it. Nothing has been changed."); }
      finally{ delete b.dataset.busy; b.classList.remove("busy"); }
    });
  }catch(e){ /* the dishes are the point; a missing photo is not an error */ }
}

(function wireMenuUpload(){
  const button=document.getElementById("menuPhoto");
  const input=document.getElementById("menuFile");
  if(!button||!input) return;

  button.addEventListener("click",()=>input.click());
  input.addEventListener("change",async ()=>{
    const file=input.files?.[0];
    if(!file) return;
    button.classList.add("busy");
    button.disabled=true;
    try{
      await api.uploadMenu(await shrink(file));
      await loadMenu();
    }catch(err){
      showToast(err.detail || "That upload didn't go through. Nothing has been changed.");
    }finally{
      button.classList.remove("busy");
      button.disabled=false;
      input.value="";                  // so the same file can be picked again
    }
  });
})();

(function wireMenuEntry(){
  const input=document.getElementById("dishIn");
  const button=document.getElementById("dishAdd");
  if(!input||!button) return;

  async function add(){
    const raw=input.value.trim();
    if(!raw) return;
    const match=raw.match(/^(.+?)\s+([\d.,]+)$/);      // "Burger 40"
    if(!match){
      input.setAttribute("aria-invalid","true");
      input.placeholder="Add a price too, like: Burger 40";
      input.value="";
      return;
    }
    input.removeAttribute("aria-invalid");
    if(button.dataset.busy) return;
    button.dataset.busy="1";
    try{
      await api.addDish({name:match[1], price:parseFloat(match[2].replace(/,/g,""))});
      input.value="";
      await loadMenu();
      input.focus();
    }catch(err){
      showToast(err.detail || "That didn't save. Try again when you're ready.");
    }finally{
      delete button.dataset.busy;
    }
  }
  button.addEventListener("click",add);
  input.addEventListener("keydown",e=>{ if(e.key==="Enter") add(); });
})();

document.getElementById("menuRows")?.addEventListener("click", async e=>{
  const toggle=e.target.closest("[data-avail]"); if(!toggle) return;
  const nowOn=!toggle.classList.contains("on");
  toggle.classList.toggle("on", nowOn);
  try{ await api.updateDish(toggle.dataset.avail, {available:nowOn}); }
  catch(err){ toggle.classList.toggle("on", !nowOn); }
});

/* ---------------- settings binding ---------------- */
/* Every editable field carries data-cfg="section.path". Reading and writing
   are both driven from that attribute, so adding a setting is one input in
   the HTML and one field in the server's schema — never a change here. */

function readPath(object, path){
  return path.split(".").reduce((o,k)=>(o==null?undefined:o[k]), object);
}

function setPath(object, path, value){
  const keys=path.split(".");
  const last=keys.pop();
  let cursor=object;
  keys.forEach(k=>{ cursor[k]=cursor[k]||{}; cursor=cursor[k]; });
  cursor[last]=value;
}

function fieldValue(el){
  if(el.classList.contains("sw")) return el.classList.contains("on");
  if(el.multiple) return [...el.selectedOptions].map(o=>o.value);
  if(el.dataset.list==="lines"){
    return el.value.split("\n").map(s=>s.trim()).filter(Boolean);
  }
  if(el.type==="number") return el.value===""?null:Number(el.value);
  if(el.tagName==="SELECT" && (el.value==="true"||el.value==="false")) return el.value==="true";
  return el.value;
}

function showField(el, value){
  if(el.classList.contains("sw")){ el.classList.toggle("on", !!value); return; }
  if(el.multiple){
    const wanted=new Set(value||[]);
    [...el.options].forEach(o=>{ o.selected=wanted.has(o.value); });
    return;
  }
  if(el.dataset.list==="lines"){ el.value=(value||[]).join("\n"); return; }
  if(typeof value==="boolean"){ el.value=String(value); return; }
  el.value = value==null ? "" : value;
}

function fillSettings(){
  document.querySelectorAll("[data-cfg]").forEach(el=>{
    const path=el.dataset.cfg;
    if(path.startsWith("hours.")||path.startsWith("capacity.buffer_pct")) return;  // derived below
    showField(el, readPath(state.config, path));
  });

  // Capacity numbers live in capacity_rules, not config.
  const firstRule=state.rules[0];
  const totalUnits=document.querySelector('[data-cfg="hours.total_units"]');
  if(totalUnits) totalUnits.value=firstRule?firstRule.total_units:"";
  const buffer=document.querySelector('[data-cfg="capacity.buffer_pct"]');
  if(buffer) buffer.value=Math.round((1-state.config.capacity.sellable_pct)*100);

  document.querySelectorAll("[data-unit-plural]").forEach(el=>{
    el.textContent=state.business.unit_plural;
  });
  updateBufferHelp();
  updateWindowPreview();
  applyCapacityToggle();
}

function updateBufferHelp(){
  const help=document.getElementById("bufferHelp");
  const buffer=document.querySelector('[data-cfg="capacity.buffer_pct"]');
  const total=document.querySelector('[data-cfg="hours.total_units"]');
  if(!help||!buffer||!total) return;
  const sellable=Math.floor(Number(total.value||0)*(1-Number(buffer.value||0)/100));
  help.textContent=`A percentage. The agent sells ${sellable}. The rest is yours at the door.`;
}

/* Collect a card's fields, grouped by the section each one belongs to.
   Hours are the one card that does not map to config -- they are
   capacity_rules -- so they are taken first and separately. */
function collectCard(card){
  if(card.id==="hoursCard") return {hours:{rules: collectHours()}};

  const sections={};
  card.querySelectorAll("[data-cfg]").forEach(el=>{
    const [section, ...rest]=el.dataset.cfg.split(".");
    sections[section]=sections[section]||{};
    setPath(sections[section], rest.join("."), fieldValue(el));
  });

  // Two fields are stored elsewhere than they are typed.
  if(sections.capacity && "buffer_pct" in sections.capacity){
    sections.capacity.sellable_pct=
      Math.max(0.01, Math.min(1, 1-Number(sections.capacity.buffer_pct)/100));
    delete sections.capacity.buffer_pct;
  }
  if(sections.hours && "total_units" in sections.hours){
    sections.hours={rules: rulesWithTotal(Number(sections.hours.total_units))};
  }
  if(sections.booking_window && sections.booking_window.mode==="rolling"){
    sections.booking_window.until=null;
  }
  return sections;
}

function rulesWithTotal(total){
  return state.rules.map(rule=>({...rule, total_units: total}));
}

async function saveCard(card, button, note){
  const sections=collectCard(card);
  const names=Object.keys(sections);
  if(!names.length) return;

  for(const section of names){
    await api.saveSettings(section, sections[section]);
  }
  await refreshSettings();
}

/* ---------------- save bars ---------------- */
/* Every settings card that holds an editable field gets its own save bar.
   Nothing saves silently: the card marks itself changed, the button wakes up,
   and after saving it says so and stamps the time. */
/* Called AFTER the settings DOM has been filled. The hours card builds its
   fields from capacity_rules, so wiring before that leaves its save button
   looking live and doing nothing. */
function wireSaving(){
  const settings=document.getElementById("v-settings");
  const bar=document.getElementById("savebar");
  const barText=document.getElementById("savebarText");

  settings.querySelectorAll("section.card").forEach(card=>{
    const editable=card.querySelectorAll("input:not([readonly]),select,textarea:not([readonly]),.sw");
    if(!editable.length) return;

    let foot=card.querySelector(".save");
    if(!foot){
      foot=document.createElement("div");
      foot.className="save";
      foot.innerHTML=`<button class="btn key">Save changes</button><span class="note"></span>`;
      card.appendChild(foot);
    }
    const button=foot.querySelector(".btn.key")||foot.querySelector("button");
    const note=foot.querySelector(".note")||(()=>{
      const n=document.createElement("span"); n.className="note"; foot.appendChild(n); return n;
    })();

    note.dataset.hint=note.textContent.trim();
    button.disabled=true;
    note.textContent=note.dataset.hint||"No changes yet.";

    const markDirty=()=>{
      card.classList.add("dirty");
      button.disabled=false;
      note.className="note dirty";
      note.textContent="Not saved yet.";
      refreshBar();
    };
    card.addEventListener("input",markDirty);
    card.addEventListener("change",markDirty);
    card.addEventListener("click",e=>{ if(e.target.closest(".sw")) markDirty(); });

    button.addEventListener("click", async ()=>{
      if(button.dataset.busy) return;               // a second tap does nothing
      button.dataset.busy="1"; button.classList.add("busy");
      try{
        await saveCard(card, button, note);
        card.classList.remove("dirty");
        button.disabled=true;
        note.className="note done";
        note.textContent=`Saved at ${new Date().toLocaleTimeString("en-GB",
          {hour:"numeric",minute:"2-digit"})}. The agent is using it from the next call.`;
        setTimeout(()=>{
          if(!card.classList.contains("dirty")){
            note.className="note";
            note.textContent=note.dataset.hint||"No changes yet.";
          }
        },6000);
      }catch(err){
        note.className="note dirty";
        note.textContent=err.detail || "That didn't save. Nothing has been changed.";
      }finally{
        button.classList.remove("busy"); delete button.dataset.busy;
        refreshBar();
      }
    });

    card._save=button;
    card._reset=()=>{
      card.classList.remove("dirty");
      button.disabled=true;
      note.className="note";
      note.textContent=note.dataset.hint||"No changes yet.";
    };
  });

  function dirtyCards(){ return [...settings.querySelectorAll("section.card.dirty")]; }

  function refreshBar(){
    const n=dirtyCards().length;
    bar.classList.toggle("show",n>0);
    barText.textContent = n===1
      ? "One card has changes you haven't saved."
      : `${n} cards have changes you haven't saved.`;
    document.querySelectorAll("#stabs button").forEach(b=>{
      const pane=document.getElementById("s-"+b.dataset.s);
      const has=pane && pane.querySelector("section.card.dirty");
      b.querySelector(".dot")?.remove();
      if(has) b.insertAdjacentHTML("beforeend",'<span class="dot"></span>');
    });
  }
  window._refreshBar=refreshBar;
  window._dirtyCards=dirtyCards;

  document.getElementById("saveAll").addEventListener("click",()=>{
    dirtyCards().forEach(c=>c._save.click());
  });
  document.getElementById("discardAll").addEventListener("click",()=>{
    dirtyCards().forEach(c=>c._reset());
    fillSettings();
    drawHours();
    refreshBar();
  });

  addEventListener("beforeunload",e=>{
    if(dirtyCards().length){ e.preventDefault(); e.returnValue=""; }
  });
}

/* ---------------- settings: locale + hours + voice ---------------- */
function fillCountries(){
  const country=document.getElementById("fCountry");
  const currency=document.getElementById("fCurrency");
  const tz=document.getElementById("fTz");
  const lang=document.getElementById("fLang");
  if(!country) return;

  country.innerHTML=state.countries.slice().sort((a,b)=>a.name.localeCompare(b.name))
    .map(c=>`<option value="${c.code}">${esc(c.name)}</option>`).join("");
  currency.innerHTML=[...new Map(state.countries.map(c=>[c.currency,c]))
    .values()].sort((a,b)=>a.currency.localeCompare(b.currency))
    .map(c=>`<option value="${c.currency}">${esc(c.currency)} — ${esc(c.currency_name)}</option>`).join("");
  tz.innerHTML=[...new Set(state.countries.map(c=>c.timezone))].sort()
    .map(z=>`<option value="${z}">${esc(z)}</option>`).join("");
  lang.innerHTML=state.languages.map(l=>`<option value="${esc(l)}">${esc(l)}</option>`).join("");

  country.addEventListener("change",()=>{
    const chosen=state.countries.find(c=>c.code===country.value);
    if(!chosen) return;
    // Selecting a country fills the rest but never locks them.
    currency.value=chosen.currency;
    tz.value=chosen.timezone;
    document.getElementById("fClock").value=chosen.clock;
    updateHolidayNote(chosen);
  });
}

async function updateHolidayNote(chosen){
  const note=document.getElementById("holNote");
  if(!note) return;
  try{
    const payload=await api.holidays();
    note.textContent=payload.supported
      ? `${chosen.name} has ${payload.holidays.length} public holidays on file. They show on the calendar and the agent won't book them unless you open the day.`
      : `No public holidays on file for ${chosen.name} yet. Block those days yourself on the calendar.`;
  }catch(e){
    note.textContent="Picking one fills in the rest below.";
  }
}

const WEEKDAYS=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];

function drawHours(){
  const wrap=document.getElementById("hoursRows");
  if(!wrap) return;
  wrap.innerHTML=WEEKDAYS.map((name,wd)=>{
    const rule=state.rules.find(r=>r.weekday===wd);
    return `<div class="tog" data-weekday="${wd}">
      <div style="min-width:120px"><b>${name}</b></div>
      <div class="grid three" style="flex:1;gap:10px;padding:0">
        <div class="f"><label>Opens</label><input type="time" class="hrOpen" value="${rule?rule.start_time:"12:00"}" ${rule?"":"disabled"}></div>
        <div class="f"><label>Closes</label><input type="time" class="hrClose" value="${rule?rule.end_time:"22:00"}" ${rule?"":"disabled"}></div>
      </div>
      <span class="sw ${rule?"on":""} hrOpenDay"></span>
    </div>`;
  }).join("");
}

/* Bound once. drawHours() replaces the rows on every settings refresh, so a
   listener attached inside it would stack up one copy per refresh. */
document.getElementById("hoursRows")?.addEventListener("click",e=>{
  const toggle=e.target.closest(".hrOpenDay"); if(!toggle) return;
  const row=toggle.closest("[data-weekday]");
  // The class is toggled by the shared .sw handler, so read it after that.
  setTimeout(()=>{
    const isOpen=toggle.classList.contains("on");
    row.querySelectorAll("input").forEach(input=>{ input.disabled=!isOpen; });
  },0);
});

/* The hours card writes capacity_rules rather than config, so it collects
   its own shape. Days switched off simply do not produce a rule. */
function collectHours(){
  const rows=[...document.querySelectorAll("#hoursRows [data-weekday]")];
  const total=Number(document.querySelector('[data-cfg="hours.total_units"]')?.value || 0);
  const slot=Number(document.querySelector('[data-cfg="capacity.slot_minutes"]')?.value || 30);
  const turn=Number(document.querySelector('[data-cfg="capacity.turn_minutes"]')?.value || 90);
  return rows.filter(row=>row.querySelector(".hrOpenDay").classList.contains("on"))
    .map(row=>({
      weekday: Number(row.dataset.weekday),
      start_time: row.querySelector(".hrOpen").value,
      end_time: row.querySelector(".hrClose").value,
      total_units: total,
      slot_minutes: slot,
      turn_minutes: turn,
      label: "",
    }));
}

function fillVoices(){
  const select=document.getElementById("fVoice");
  if(!select) return;
  fetch("/voices").then(r=>r.json()).then(payload=>{
    const known=payload.known||{};
    select.innerHTML=Object.entries(known)
      .map(([id,desc])=>`<option value="${id}">${esc(id)} — ${esc(desc)}</option>`).join("");
    select.value=state.config.voice.voice_id || payload.current || "";
  }).catch(()=>{
    select.innerHTML=`<option value="">Default</option>`;
  });
}

function applyCapacityToggle(){
  const toggle=document.getElementById("capSwitch");
  const fields=document.getElementById("capFields");
  if(!toggle||!fields) return;
  const on=toggle.classList.contains("on");
  fields.style.opacity=on?"1":".45";
  fields.querySelectorAll("input,select").forEach(x=>{ x.disabled=!on; });
}
document.getElementById("capSwitch")?.addEventListener("click",()=>setTimeout(applyCapacityToggle,0));
document.getElementById("capFields")?.addEventListener("input",updateBufferHelp);

function updateWindowPreview(){
  const mode=document.getElementById("winMode");
  const preview=document.getElementById("winPreview");
  if(!mode||!preview) return;
  document.getElementById("winDaysF").classList.toggle("hide", mode.value!=="rolling");
  document.getElementById("winDateF").classList.toggle("hide", mode.value==="rolling");

  const today=state.today;
  if(mode.value==="rolling"){
    const days=Number(document.getElementById("winDays").value||1);
    const end=new Date(today+"T00:00:00");
    end.setDate(end.getDate()+days-1);
    const next=new Date(end); next.setDate(next.getDate()+1);
    preview.innerHTML=`Today is <b>${shortDate(today)}</b>, so the agent will book up to
      <b>${shortDate(end.toISOString().slice(0,10))}</b>. Tomorrow that moves to
      <b>${shortDate(next.toISOString().slice(0,10))}</b>. Anything further out, it offers to ring back.`;
    return;
  }
  const until=document.getElementById("winDate").value;
  preview.innerHTML=until
    ? `The agent books up to <b>${shortDate(until)}</b> and then stops. It tells callers you're not taking anything after that.`
    : `Pick the last day you'll take.`;
}
["winMode","winDays","winDate"].forEach(id=>{
  document.getElementById(id)?.addEventListener("input",updateWindowPreview);
  document.getElementById(id)?.addEventListener("change",updateWindowPreview);
});

function fillTemplateHelp(){
  const help=document.getElementById("fieldHelp");
  if(!help) return;
  help.innerHTML="Fields you can use: <code>{display_name} {name} {party_size} "+
    "{date_long} {time} {reference} {manage_url} {alternative} {unit_label} "+
    "{currency_symbol}</code><br>Wrap a phrase in <code>[[double brackets]]</code> "+
    "and it is left out entirely when the value inside is empty — so a booking "+
    "with no alternative to offer doesn't send a sentence that trails off.";
}

/* The header and the footer under the bookings list are the two places the
   venue's own words appear outside a card. Both come from the server. */
function fillCalendarNote(){
  const note=document.getElementById("calNote");
  if(!note) return;
  const seats=state.rules[0]?.total_units;
  note.textContent = state.business.tracks_capacity && seats
    ? `Seats turn over through the night, so a day holds far more than ${seats} people. `+
      `The bar measures the busiest slot — the moment closest to being full.`
    : "You're not tracking capacity, so these are booking counts only.";
}

function fillChrome(){
  const name=document.getElementById("acctName");
  if(name) name.textContent=state.business.name;
  const sub=document.getElementById("acctSub");
  if(sub) sub.textContent=state.business.timezone.split("/").pop().replace(/_/g," ");
}

function fillBookingsFoot(lastSeating){
  const foot=document.getElementById("bookingsFoot");
  if(!foot) return;
  foot.textContent = lastSeating
    ? `Last seating is ${lastSeating}. The agent takes nothing past it.`
    : "";
}

function fillAccount(){
  const number=document.getElementById("fAgentNumber");
  if(number) number.value=state.business.phone_number || "Not connected yet";
  const tz=document.getElementById("fTzShown");
  if(tz) tz.value=state.business.timezone;
  const forwarding=document.getElementById("fForwarding");
  if(forwarding){
    const n=(state.business.phone_number||"").replace(/[^\d+]/g,"");
    forwarding.value = n
      ? `Dial *67*${n}# to forward when your line is busy.\n`+
        `Dial *61*${n}# to forward when nobody answers.\nDial ##002# to undo both.`
      : "Connect a number first and the forwarding codes appear here.";
  }
  const call=document.getElementById("callAgent");
  if(call) call.href=`/?business=${encodeURIComponent(state.business.slug)}`;
}

async function loadStats(){
  const body=document.getElementById("statRows");
  if(!body) return;
  try{
    const payload=await api.stats();
    body.innerHTML=payload.rows.map(r=>
      `<tr><td>${esc(r.label)}</td><td class="r">${esc(String(r.value))}</td></tr>`).join("");
  }catch(e){
    body.innerHTML=`<tr><td colspan="2" class="empty">Couldn't load this month's numbers.</td></tr>`;
  }
}

async function refreshSettings(){
  const payload=await api.settings();
  state.config=payload.config;
  state.rules=payload.capacity_rules;
  fillSettings();
  drawHours();
  const changes=document.getElementById("changeRows");
  if(changes){
    changes.innerHTML=payload.changes.length
      ? payload.changes.map(c=>
          `<tr><td>${esc(c.when)}</td><td>${esc(c.what)}</td><td class="col-hide">${esc(c.who)}</td></tr>`).join("")
      : `<tr><td colspan="3" class="empty">Nothing has been changed yet.</td></tr>`;
  }
}

/* ---------------- connection and freshness ---------------- */
/* Stale data is the highest-severity bug in this product (PRD §15f): a host
   seating guests from a frozen list is worse than a blank screen. */
let lastSync=Date.now();
let offlineFor=0;
const conn=document.getElementById("conn"), live=document.getElementById("live");

function freshness(){
  const mins=Math.floor((Date.now()-lastSync)/60000);
  const down=!navigator.onLine && offlineFor && Date.now()-offlineFor>2000;
  const stale=down||mins>=2;
  live.classList.toggle("stale",stale);
  live.textContent = down ? "Not connected"
    : mins<1 ? "Updated just now"
    : `Updated ${mins} minute${mins===1?"":"s"} ago`;
  conn.classList.toggle("show", !!down);
}
addEventListener("online",()=>{ offlineFor=0; lastSync=Date.now(); freshness(); refresh(); });
addEventListener("offline",()=>{ offlineFor=Date.now(); setTimeout(freshness,2500); });
document.getElementById("connRetry").addEventListener("click",e=>{
  const b=e.currentTarget; b.classList.add("busy");
  refresh().finally(()=>{ b.classList.remove("busy"); freshness(); });
});
setInterval(freshness,15000);

/* Bookings go stale fastest, so they are the thing that is re-read. */
setInterval(()=>{
  if(document.hidden || !navigator.onLine) return;
  if(document.getElementById("v-bookings").classList.contains("hide")) return;
  loadBookings(); loadPending();
}, 45000);

/* ---------------- shared ---------------- */
/* Every switch on the page flips here, except the menu's availability toggles,
   which own their own optimistic update and rollback. */
document.body.addEventListener("click",e=>{
  const toggle=e.target.closest(".sw");
  if(toggle && !toggle.hasAttribute("data-avail")) toggle.classList.toggle("on");

  const row=e.target.closest("tr.b");
  if(row && !e.target.closest(".btn") && !e.target.closest(".sw")){
    if(row.dataset.i!==undefined){
      const i=+row.dataset.i;
      openRows.has(i)?openRows.delete(i):openRows.add(i);
      markSeen(data[i]?.id);
      drawBookings();
    } else if(row.dataset.call!==undefined){
      const detail=document.getElementById("call-"+row.dataset.call);
      detail.classList.toggle("open");
      if(detail.classList.contains("open")) openTranscript(row.dataset.call);
    }
  }
});

const VIEWS=["bookings","calendar","calls","guests","menu","settings"];
const VIEW_LOADERS={
  bookings: ()=>{ loadBookings(); loadPending(); },
  calendar: ()=>{ loadMonth(); loadBlocked(); },
  calls: loadCalls,
  guests: loadGuests,
  menu: loadMenu,
  settings: ()=>{ refreshSettings(); loadStats(); },
};

document.getElementById("nav").addEventListener("click",e=>{
  const b=e.target.closest("button[data-v]"); if(!b) return;
  if(b.dataset.v!=="settings" && window._dirtyCards?.().length
     && !confirm("You have settings you haven't saved. Leave anyway?")) return;
  showView(b.dataset.v);
});

function showView(name){
  document.querySelectorAll("#nav button").forEach(x=>
    x.classList.toggle("on", x.dataset.v===name));
  VIEWS.forEach(v=>document.getElementById("v-"+v).classList.toggle("hide", v!==name));
  scrollTo(0,0);
  if(location.hash.slice(1)!==name) history.replaceState(null,"","#"+name);
  VIEW_LOADERS[name]?.();
}

document.getElementById("stabs").addEventListener("click",e=>{
  const b=e.target.closest("button[data-s]"); if(!b) return;
  document.querySelectorAll("#stabs button").forEach(x=>x.classList.toggle("on",x===b));
  ["venue","agent","messages","account"].forEach(s=>
    document.getElementById("s-"+s).classList.toggle("hide",s!==b.dataset.s));
  scrollTo(0,0);
});

addEventListener("hashchange",()=>{
  const name=location.hash.slice(1);
  if(VIEWS.includes(name)) showView(name);
});

/* ---------------- start ---------------- */
async function refresh(){
  const name=VIEWS.find(v=>!document.getElementById("v-"+v).classList.contains("hide"));
  if(name) await VIEW_LOADERS[name]?.();
}

async function start(){
  try{
    const [boot, locales] = await Promise.all([api.bootstrap(), api.locales()]);
    state.business=boot.business;
    state.locale=boot.locale;
    state.config=boot.config;
    state.rules=boot.capacity_rules;
    state.today=boot.today;
    state.windowLastDay=boot.window_last_day;
    state.holidaysSupported=boot.holidays_supported;
    state.countries=locales.countries;
    state.languages=locales.languages;
  }catch(err){
    document.body.innerHTML=`<div class="wrap" style="padding:60px 20px;max-width:560px">
      <h1 style="margin-bottom:10px">Can't open this dashboard</h1>
      <p style="color:var(--ink-2)">${esc(
        err instanceof ApiError && err.status===401
          ? "This link is not valid any more. Ask for a new one."
          : "The server didn't answer. Nothing has been changed."
      )}</p></div>`;
    return;
  }

  document.title=`${state.business.name} — Tableline`;
  const [y,m]=state.today.split("-").map(Number);
  year=y; month=m; selected=state.today;

  fillChrome();
  fillCalendarNote();
  fillCountries();
  fillVoices();
  fillTemplateHelp();
  fillAccount();
  fillSettings();
  drawHours();
  wireSaving();

  const initial=VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "bookings";
  showView(initial);
  freshness();
}

/* ---------------- closing and reducing a day ---------------- */
/* All three write the same override row (PRD §6). They are separate buttons
   because "we are shut" and "we can only do 20 tonight" are different
   decisions, and one form asking which would make the common one slower. */

async function afterOverride(){
  await loadMonth();
  await loadBlocked();
  if(selected) loadDay(selected);
}

const unitWord = () => state.business?.unit_plural || "covers";

document.getElementById("blockDate").addEventListener("click", () => {
  openSheet({
    title: "Block a date",
    note: "The agent stops taking bookings for it. Bookings already taken are left alone.",
    ok: "Block it",
    fields: [
      { name:"date", label:"Which day", type:"date", value:selected || venueToday(), required:true },
      { name:"reason", label:"Why", placeholder:"Private party, staff holiday, refurb",
        help:"Only you see this. It shows on the calendar so you remember." },
    ],
    submit: async v => {
      await api.block(v.date, { type:"closed", reason:v.reason });
      await afterOverride();
    },
  });
});

document.getElementById("closeDay").addEventListener("click", () => {
  if(!selected) return;
  openSheet({
    title: `Close ${longDate(selected)}`,
    note: "Nothing already booked is cancelled. You would still need to ring those guests.",
    ok: "Close this day",
    fields: [
      { name:"reason", label:"Why", placeholder:"Private party, staff holiday, refurb" },
    ],
    submit: async v => {
      await api.block(selected, { type:"closed", reason:v.reason });
      await afterOverride();
    },
  });
});

document.getElementById("changeCap").addEventListener("click", () => {
  if(!selected) return;
  openSheet({
    title: `Capacity for ${longDate(selected)}`,
    note: `A one-off change for this day only. Your usual hours are untouched.`,
    ok: "Change it",
    fields: [
      { name:"total_units", label:`How many ${unitWord()} per slot`, type:"number", min:0,
        required:true, help:"Set it to 0 to take nothing at all." },
      { name:"reason", label:"Why", placeholder:"Short staffed, half the room closed" },
    ],
    submit: async v => {
      await api.block(selected, {
        type:"reduced",
        total_units: Number(v.total_units),
        reason: v.reason,
      });
      await afterOverride();
    },
  });
});

/* ---------------- guests: export ---------------- */
/* Built from the rows already on screen, so what you export is what you
   filtered -- and it works with no connection, which a server round trip
   would not. */

function toCsv(rows){
  const head = ["Name","Phone","Visits","No-shows","Last seen","Do not call"];
  const cell = v => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const body = rows.map(g => [
    g.name, g.phone, g.visits, g.no_shows, g.last || "", g.do_not_call ? "yes" : "",
  ].map(cell).join(","));
  return [head.join(","), ...body].join("\r\n");
}

document.getElementById("exportGuests").addEventListener("click", () => {
  const rows = window.__guests || [];
  if(!rows.length) return;
  const blob = new Blob(["﻿" + toCsv(rows)], { type:"text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `guests-${guestFilter}-${venueToday()}.csv`;
  link.click();
  URL.revokeObjectURL(url);
});

/* ---------------- reading a photographed menu ---------------- */
/* The reader PROPOSES. Every dish lands in an editable row and nothing
   reaches the menu until "Add these" is pressed, because the agent reads
   these out to callers and a mis-read allergen is the one mistake here that
   hurts somebody. */

let proposed = [], reviewSaving=false;

function drawReview(animate = false){
  const host = document.getElementById("menuReview");
  if(!proposed.length){ host.innerHTML = ""; return; }

  host.innerHTML = `
    <div class="review-heading"><h3>Check the dishes</h3><span class="review-count">${proposed.length} to review</span></div>
    <p class="review-note">Check each price against the photo. Edit anything the reader missed before adding it.</p>
    <form id="menuReviewForm">
    <div class="review-list ${animate?"arriving":""}">
    ${proposed.map((d,i)=>`<div class="prop" data-i="${i}" style="--row-delay:${Math.min(i,5)*20}ms">
      <label class="review-field review-name"><span>Dish name</span>
        <input data-k="name" value="${esc(d.name)}" required aria-label="Dish name"></label>
      <label class="review-field"><span>Price${state.locale?.currency_symbol?` (${esc(state.locale.currency_symbol)})`:""}</span>
        <input data-k="price" type="number" min="0" step="0.01" inputmode="decimal"
          value="${esc(d.price ?? "")}" placeholder="Check price" aria-label="Price"></label>
      <label class="review-field"><span>Section</span>
        <input data-k="section" value="${esc(d.section||"")}" placeholder="e.g. Mains" aria-label="Section"></label>
      <button class="btn no" type="button" data-drop="${i}" aria-label="Remove ${esc(d.name)}">Remove</button>
    </div>`).join("")}
    </div>
    <div class="review-actions">
      <button class="btn key" id="menuKeep" type="submit">Add ${proposed.length===1?"this dish":`these ${proposed.length} dishes`}</button>
      <button class="btn" id="menuDrop" type="button">Discard</button>
    </div></form>
    <p class="review-disclaimer">
      Read them before you add them. Anything here is what the agent will say out loud.
      Allergy tags are only copied when the menu prints them — check them against the kitchen.
    </p>`;
}

document.getElementById("menuReview").addEventListener("input", e=>{
  const input = e.target.closest("input[data-k]");
  if(!input) return;
  const index = +input.closest(".prop").dataset.i;
  const key = input.dataset.k;
  proposed[index][key] = key === "price"
    ? (input.value === "" ? null : Number(input.value))
    : input.value;
});

document.getElementById("menuReview").addEventListener("click", e=>{
  if(reviewSaving) return;
  const drop = e.target.closest("button[data-drop]");
  if(drop){
    proposed.splice(+drop.dataset.drop, 1);
    drawReview();
    return;
  }
  if(e.target.id === "menuDrop"){ proposed = []; drawReview(); }
});

document.getElementById("menuReview").addEventListener("submit", async e=>{
  e.preventDefault();
  if(reviewSaving) return;
  const form=e.target;
  if(!form.reportValidity()) return;
  const keep=form.querySelector("#menuKeep");
  reviewSaving=true;
  const controls=[...form.querySelectorAll("input,button"),document.getElementById("menuRead"),document.getElementById("menuPhoto")];
  const disabled=controls.map(el=>el.disabled);
  controls.forEach(el=>{ el.disabled=true; });
  keep.dataset.busy = "1";
  keep.classList.add("busy");
  try{
    await api.addDishes(proposed.filter(d => (d.name||"").trim()));
    showToast(`${proposed.length===1?"The dish is":`All ${proposed.length} dishes are`} saved. The agent can use ${proposed.length===1?"it":"them"} now.`,"success");
    proposed = [];
    drawReview();
    await loadMenu();
  }catch(err){
    showToast(err.detail || "Those didn't save. Your edits are still here — try again.");
  }finally{
    reviewSaving=false;
    controls.forEach((el,i)=>{ el.disabled=disabled[i]; });
    delete keep.dataset.busy;
    keep.classList.remove("busy");
  }
});

document.getElementById("menuRead").addEventListener("click", async e=>{
  const button = e.target;
  if(!window.__upload || button.dataset.busy) return;
  button.dataset.busy = "1";
  button.classList.add("busy");
  try{
    const payload = await api.readMenu(window.__upload.id);
    proposed = payload.dishes;
    drawReview(true);
    if(!proposed.length) showToast("Nothing readable came off that one. Try a clearer photo or add the dishes by hand.","info");
  }catch(err){
    showToast(err.detail || "The reader couldn't be reached. Nothing has been changed.");
  }finally{
    delete button.dataset.busy;
    button.classList.remove("busy");
  }
});

/* A phone photo is several megabytes and more pixels than any reader uses:
   past about 1568px on the long edge it is downsampled at the other end
   anyway. Shrinking here cuts the upload, the wait and the bill at once.
   A format the browser cannot decode -- HEIC on a desktop -- falls through
   and is uploaded as it came. */
const MAX_EDGE = 1568;

async function shrink(file){
  if(!file.type.startsWith("image/")) return file;
  try{
    const bitmap = await createImageBitmap(file);
    const scale = MAX_EDGE / Math.max(bitmap.width, bitmap.height);
    if(scale >= 1 && file.size < 4_000_000) return file;

    const width = Math.round(bitmap.width * Math.min(scale, 1));
    const height = Math.round(bitmap.height * Math.min(scale, 1));
    const canvas = document.createElement("canvas");
    canvas.width = width; canvas.height = height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0, width, height);
    bitmap.close?.();

    const blob = await new Promise(done => canvas.toBlob(done, "image/jpeg", 0.85));
    if(!blob || blob.size >= file.size) return file;
    return new File([blob], "menu.jpg", { type:"image/jpeg" });
  }catch(e){
    return file;                      // a photo we cannot shrink is still a photo
  }
}

/* ---------------- this device (PRD §14) ---------------- */
/* Sound, the unseen marker and the wake lock are about THIS tablet, not the
   venue, so they live in localStorage beside the theme rather than in the
   config that every device shares. */

const device = {
  sound: (()=>{ try{ return localStorage.getItem("tl-sound")!=="off"; }catch(e){ return true; } })(),
  wake:  (()=>{ try{ return localStorage.getItem("tl-wake")==="on"; }catch(e){ return false; } })(),
};
function saveDevice(){
  try{
    localStorage.setItem("tl-sound", device.sound?"on":"off");
    localStorage.setItem("tl-wake",  device.wake ?"on":"off");
  }catch(e){}
}

/* Web Audio needs a user gesture before it may make a sound. The first tap
   anywhere on the page is that gesture -- so there is never a permission
   prompt, and never a dialog asking to "enable notifications". */
let audio=null;
function unlockAudio(){
  if(audio) return;
  try{ audio=new (window.AudioContext||window.webkitAudioContext)(); }catch(e){ audio=null; }
}
document.addEventListener("pointerdown", unlockAudio, { once:true, passive:true });

/* Two tones, generated rather than shipped as files: nothing to fetch, and
   they cannot 404 on a bad connection. "arrival" is two rising notes; the
   "decision" tone is three, so the difference is heard across a loud room. */
function tone(kind){
  if(!device.sound || !audio) return;
  if(audio.state==="suspended") audio.resume().catch(()=>{});
  const notes = kind==="decision" ? [660, 880, 1100] : [660, 880];
  const start = audio.currentTime + 0.02;
  notes.forEach((hz, i)=>{
    const osc=audio.createOscillator(), gain=audio.createGain();
    osc.type="sine"; osc.frequency.value=hz;
    const at=start + i*0.14;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.25, at+0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, at+0.12);
    osc.connect(gain).connect(audio.destination);
    osc.start(at); osc.stop(at+0.13);
  });
}

/* Which bookings this device has opened. Per business, so one tablet used
   for two venues does not mark one venue's bookings seen because the other
   was busy. Seeded with everything the first time it is ever opened: a
   marker on every row on day one says nothing. */
let seenIds=new Set(), knownIds=new Set(), seenKey=null, firstBookingsLoad=true;
function loadSeen(){
  seenKey=`tl-seen:${state.business?.id||"x"}`;
  try{
    const raw=localStorage.getItem(seenKey);
    seenIds=new Set(raw ? JSON.parse(raw) : []);
    return raw!==null;
  }catch(e){ return false; }
}
function saveSeen(){
  try{ localStorage.setItem(seenKey, JSON.stringify([...seenIds].slice(-2000))); }catch(e){}
}
function markSeen(id){
  if(!id || seenIds.has(id)) return;
  seenIds.add(id); saveSeen();
}
/* Called with each fresh list. Plays the arrival tone for ids not seen this
   session -- but not on the first load, which is the page catching up. */
function noticeBookings(list){
  const fresh=list.filter(b=>!knownIds.has(b.id));
  list.forEach(b=>knownIds.add(b.id));
  if(firstBookingsLoad){
    firstBookingsLoad=false;
    if(!loadSeen()){ list.forEach(b=>seenIds.add(b.id)); saveSeen(); }
    return;
  }
  if(fresh.length) tone("arrival");
}

/* The screen on a tablet by the door goes dark after a minute and the host
   loses the list. Optional, per device, re-acquired when the tab comes back
   because the lock is released whenever it is hidden. */
let wakeLock=null;
async function applyWake(){
  if(!("wakeLock" in navigator)) return;
  if(!device.wake){
    try{ await wakeLock?.release(); }catch(e){}
    wakeLock=null; return;
  }
  if(wakeLock && !wakeLock.released) return;
  try{ wakeLock=await navigator.wakeLock.request("screen"); }catch(e){ wakeLock=null; }
}
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) applyWake(); });

(function wireDevice(){
  const sound=document.getElementById("soundBtn"), wake=document.getElementById("wakeBtn");
  if(!sound||!wake) return;
  const paint=()=>{
    sound.setAttribute("aria-pressed", String(device.sound));
    sound.querySelector(".device-text").textContent = device.sound ? "Sound on" : "Sound off";
    wake.setAttribute("aria-pressed", String(device.wake));
    wake.querySelector(".device-text").textContent = device.wake ? "Screen stays on" : "Keep screen on";
  };
  if("wakeLock" in navigator) wake.hidden=false;
  sound.addEventListener("click", ()=>{ device.sound=!device.sound; saveDevice(); paint(); if(device.sound) tone("arrival"); });
  wake.addEventListener("click", ()=>{ device.wake=!device.wake; saveDevice(); paint(); applyWake(); });
  paint(); applyWake();
})();

revealBrand();
start();
