// Presentation-only, synthetic OFFLINE states. No browser/server/backend calls.
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const htmlPath=path.resolve(__dirname,"..","..","src","fa-shared-context-poc","web_test_console","index.html");
const html=fs.readFileSync(htmlPath,"utf8");
const copy=value=>JSON.parse(JSON.stringify(value));
// What a bubble shows as message text: <br> is a line break; its time/copy row is not part of the message.
const shownText=node=>typeof node==="string"?node:node.tagName==="BR"?"\n":node.className==="message-meta"?"":
  (node.textContent||"")+(node.children||[]).map(shownText).join("");
// A turn's ⋯ menu (last in its action row), and its action buttons: the row plus the ones kept in that menu.
const isTurnMenu=node=>String(node.className).split(" ").includes("turn-more");
function turnMenu(turn){return turn?.children.at(-1)?.children.find(isTurnMenu)}
function turnActions(turn){
  return (turn?.children.at(-1)?.children||[]).flatMap(node=>isTurnMenu(node)?node.children[1].children:[node]);
}
const action=(enabled,reason="")=>({enabled,reason});
const when="2026-01-01T00:00:00Z";
const unknowns=["a".repeat(64),"b".repeat(64),"c".repeat(64)];
const models={default_model:"offline-chat-a",models:[
  {id:"offline-chat-a",display_name:"OFFLINE 聊天部署 A",context_window_tokens:null,max_output_tokens:null,reasoning_summary_available:false},
  {id:"offline-chat-b",display_name:"OFFLINE 聊天部署 B",context_window_tokens:64000,max_output_tokens:8000,reasoning_summary_available:false}]};

test("the page asks every confirmation in the page, never with a native dialog",()=>{
  // Embedded browsers (e.g. a desktop app's built-in browser) answer window.confirm with false and show nothing.
  const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join("\n");
  assert.doesNotMatch(html,/window\.(confirm|alert|prompt)\b/);
  assert.doesNotMatch(script,/\b(confirm|alert|prompt)\s*\(/,"no confirm(…), window.confirm(…), globalThis.confirm(…) or the like");
  assert.match(html,/<div id="page-confirm" class="page-confirm" hidden><div class="page-confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="page-confirm-message">/);
});

test("every in-page confirmation is awaited before its action is sent",()=>{
  // confirmInPage returns a Promise, which is always truthy: `if(!confirmInPage(…))return` would send at once.
  const offenders=[],sites=[];
  for(const match of html.matchAll(/\bconfirmInPage\b/g)){
    const before=html.slice(0,match.index),line=before.split("\n").length;
    if(/\bfunction\s+$/.test(before))continue;
    sites.push(line);
    if(!/\bawait\s+$/.test(before)||html[match.index+"confirmInPage".length]!=="(")offenders.push(`index.html:${line}`);
  }
  assert.deepEqual(offenders,[],"every confirmInPage call site reads `await confirmInPage(`");
  assert.ok(sites.length>0,"the scan found the call sites");
});

test("legacy fixed work and budget controls are not present in the page",()=>{
  for(const removed of ["預算與期限","既有固定測試操作","交辦這筆工作","核對原取件回條","續接原取件"])
    assert.doesNotMatch(html,new RegExp(removed));
});

function fixture(){
  return {
    mode:"FIXED_TESTS",environment:"OFFLINE_TEST",authentication:"AVAILABLE",
    agents:[{id:"configured-agent",name:"FA 助理",description:"目前配置的工作助理"}],
    authorization_mode:"backend-per-work",reason:null,console_id:"offline-console",csrf:"offline",
    tasks:[{
      task_id:"offline",label:"OFFLINE 已保存回覆",description:"合成介面狀態，不是真工作。",
      mode:"normal_pull",work_id:"offline-work",binding_id:"offline-binding",job_id:"offline-job",
      submit_request_id:"offline-submit",fetch_request_id:"offline-fetch",ack_request_id:"offline-ack",
      instruction:"OFFLINE free instruction",thread_id:"thread-offline",requested_model:"offline-chat-a",
      attachments:[],user_files:[],admission:{conversation_id:"conversation-offline",turn_number:1,model:"offline-chat-a"},
      progress:{data:{state:"cancelled",reply_available:true,delivery_id:"delivery-offline"},observed_at:when},
      reply:null,
      submission:{state:"CONFIRMED",observed_at:when,historical:true},
      acknowledgment:{state:"NOT_SENT"},reconciliation:{state:"NOT_SENT"},fetch:{state:"UNKNOWN"},
      actions:{status:action(true),fetch:action(false,"原請求已記錄，不會重送。"),
        acknowledge:action(true),reconcile:action(false),"read-reply":action(true),"confirm-reply":action(true),
        "submit-reconcile":action(false),"submit-retry":action(false),"read-context":action(false),
        "read-delegation":action(false),"cancel":action(false),"read-cancel-receipt":action(false),
        "read-files":action(false)},
      execution:null,
      status:null,error:null,
      result:{verified_bytes:true,work_state:"cancelled",message_kind:"failure",sha256:"d".repeat(64),byte_length:1607,
        content_unavailable:false,known_effects:[],unresolved_operations:unknowns,
        content:JSON.stringify({state:"cancelled",work_id:"offline-work",artifact_stage:"saved",
          finish_verified:false,model_outcome:"unknown",budget_refunded:false,
          reason:"OFFLINE cancellation fixture",model_intents:unknowns.map(request_sha256=>({request_sha256,consumed:true}))})}
    }]
  };
}

function memoryStorage(){
  const values=new Map();
  return {values,getItem:key=>values.has(key)?values.get(key):null,setItem:(key,value)=>values.set(key,String(value)),
    removeItem:key=>values.delete(key)};
}
async function open(initial=fixture(),responses={}){
  const element=(tag="")=>{
    const node={
    textContent:"",hidden:false,disabled:false,value:"",events:{},children:[],
    attributes:{},scrollHeight:100,scrollTop:0,clientHeight:100,
    style:{setProperty(name,value){this[name]=String(value)},removeProperty(name){delete this[name]}},
    setAttribute(name,value){this.attributes[name]=String(value)},
    getAttribute(name){return this.attributes[name]??null},
    focus(){this.focused=true},
    click(){this.clickCount=(this.clickCount||0)+1;this.events.click?.()},
    contains(other){return other===this||this.children.some(child=>child===other||child?.contains?.(other))},
    replaceChildren(){this.children=[]},append(...children){this.children.push(...children)},
    getAttributeNames(){return Object.keys(this.attributes)},
    // As in a browser, inserting a child already in this list moves it.
    insertBefore(child,before){
      if(this.children.includes(child))this.removeChild(child);
      const at=before?this.children.indexOf(before):-1;this.children.splice(at<0?this.children.length:at,0,child);return child;
    },
    removeChild(child){const at=this.children.indexOf(child);if(at>=0)this.children.splice(at,1);return child},
    addEventListener(name,handler){this.events[name]=handler}
    };
    Object.defineProperty(node,"tagName",{value:String(tag).toUpperCase()});
    return node;
  };
  const tags=new Map([...html.matchAll(/<([a-z0-9]+)\b[^<>]*?\sid="([^"]+)"/g)].map(m=>[m[2],m[1]]));
  const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],element(tags.get(m[1]))]));
  // The dialog starts closed through its markup (`hidden`), not through script.
  nodes.get("page-confirm").hidden=/<div id="page-confirm"[^>]*\shidden>/.test(html);
  // Form fields carry their markup type and default value, as in a browser.
  for(const [markup,tag] of html.matchAll(/<(input|textarea)\b[^<>]*>/g)){
    const node=nodes.get(/\sid="([^"]+)"/.exec(markup)?.[1]);
    if(!node)continue;
    node.type=tag==="textarea"?"textarea":/\stype="([^"]+)"/.exec(markup)?.[1]||"text";
    node.defaultValue=tag==="input"?/\svalue="([^"]*)"/.exec(markup)?.[1]??"":"";
  }
  const calls=[],holder={current:copy(initial),pending:null,pendingPosts:[],response:null,sockets:[],observers:[],...responses},timers=new Map(),timerDelays=new Map(),documentEvents={};
  let timerId=0;
  const context={console,URLSearchParams,TextEncoder,TextDecoder,Blob,Response,crypto:require("node:crypto").webcrypto,
    Date:class extends Date{static now(){return holder.now??super.now()}},
    URL:{createObjectURL(blob){holder.downloaded=blob;return "blob:offline-template"},
      revokeObjectURL(url){holder.revoked=url}},
    setTimeout(fn,delay){timers.set(++timerId,fn);timerDelays.set(timerId,delay);return timerId},
    clearTimeout(id){timers.delete(id);timerDelays.delete(id)},
    // Native dialogs fail the test: embedded browsers answer them with false and show nothing, so the page asks in
    // the page instead (#page-confirm; answer it with ui.answer).
    window:{location:{search:"",...(responses.location||{})},matchMedia:()=>({matches:false}),
      confirm:nativeDialog("confirm"),alert:nativeDialog("alert"),prompt:nativeDialog("prompt"),
      innerWidth:responses.innerWidth??1440},
    // Browser storage; pass the same responses.storage to a second open() to model a page reload.
    localStorage:responses.storage??memoryStorage(),
    document:{documentElement:{setAttribute(){}},getElementById:id=>nodes.get(id),createElement:element,
      querySelectorAll:selector=>[...nodes.values()].filter(node=>selector.split(",").includes(node.tagName.toLowerCase())),
      hidden:false,addEventListener(name,fn){documentEvents[name]=fn}},
    // Older turns load their reply as they scroll into view; tests decide which nodes are visible (seeTurn).
    IntersectionObserver:class{constructor(callback){this.callback=callback;this.nodes=new Set();holder.observers.push(this)}
      observe(node){this.nodes.add(node)}unobserve(node){this.nodes.delete(node)}disconnect(){this.nodes.clear()}},
    // Web PubSub simple WebSocket client: tests call onopen/onmessage/onclose themselves.
    WebSocket:class{constructor(url){this.url=url;this.closed=false;holder.sockets.push(this)}close(){this.closed=true}},
    fetch:async(url,options={})=>{
      calls.push({url,...options});
      if(url==="/api/realtime/negotiate"){
        const value=typeof holder.negotiateResponse==="function"?holder.negotiateResponse():holder.negotiateResponse;
        return value||{ok:true,json:async()=>({schema_version:"fa-realtime-negotiate-v1",
          url:"wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=offline",
          expires_at:"2026-01-01T01:00:00Z",team_id:"team-one"})};
      }
      if(url.startsWith("/api/native-hitl/")&&holder.hitl)return holder.hitl(url,options);
      // RTT B live text: tests hand out a streaming Response; without one the stream simply has no body.
      if(/^\/api\/instructions\/[^/]+\/stream(\?|$)/.test(url)&&holder.streamResponse)return holder.streamResponse(url,options);
      if(holder.onboarding&&url.startsWith("/api/agents/onboarding"))return holder.onboarding(url,options);
      if(url==="/api/agents"&&holder.agentDirectoryResponse)return holder.agentDirectoryResponse;
      if(holder.agentStates&&(url.startsWith("/api/state")||url.startsWith("/api/instructions/models"))){
        const identity=options.headers?.["X-Console-Agent"];
        if(identity&&holder.agentStates[identity])return {ok:true,json:async()=>copy(holder.agentStates[identity])};
      }
      if(url.startsWith("/api/instructions/progress")&&holder.agentProgressResponses?.[options.headers?.["X-Console-Agent"]])
        return holder.agentProgressResponses[options.headers["X-Console-Agent"]];
      // A running turn's light read (/status) returns the same state shape as the whole-conversation progress.
      if((url.startsWith("/api/instructions/progress")||/^\/api\/instructions\/[^/]+\/status$/.test(url))&&holder.progressResponse)
        return typeof holder.progressResponse==="function"?holder.progressResponse(url):holder.progressResponse;
      if(url.endsWith("/delegation")&&holder.delegationResponse)return holder.delegationResponse;
      if(url.endsWith("/agent-messages")&&holder.agentMessagesResponse)
        return typeof holder.agentMessagesResponse==="function"?holder.agentMessagesResponse():holder.agentMessagesResponse;
      if(url.endsWith("/context")&&holder.contextResponse)return holder.contextResponse;
      if(url.endsWith("/operation")&&holder.operationResponse)return holder.operationResponse;
      if(url.endsWith("/environment")&&holder.environmentResponse)return holder.environmentResponse;
      if(url.endsWith("/cancel-receipt")&&holder.cancelReceiptResponse)return holder.cancelReceiptResponse;
      if(url.endsWith("/appends")&&holder.appendsResponse)return holder.appendsResponse;
      if(url.startsWith("/api/instructions/")&&url.endsWith("/progress")&&holder.nativeProgressResponse)
        return typeof holder.nativeProgressResponse==="function"?holder.nativeProgressResponse():holder.nativeProgressResponse;
      if(url.endsWith("/images")&&holder.imagesResponse)return typeof holder.imagesResponse==="function"?holder.imagesResponse():holder.imagesResponse;
      if(url.includes("/images/")&&holder.imageResponse)return holder.imageResponse;
      if(url.endsWith("/reply.txt")&&holder.answerResponse)return holder.answerResponse;
      if(url.endsWith("/reply")&&holder.replyResponse)
        return typeof holder.replyResponse==="function"?holder.replyResponse(url,options):holder.replyResponse;
      if(url.endsWith("/skill-draft")&&holder.skillDraftResponse)return holder.skillDraftResponse;
      if(url.endsWith("/skill-draft.md")&&holder.skillDraftDownloadResponse)return holder.skillDraftDownloadResponse;
      if(url.endsWith("/summary")&&holder.summaryResponse)return holder.summaryResponse;
      if(url.endsWith("/attachment-sources")&&holder.attachmentResponse)return holder.attachmentResponse;
      if(url.endsWith("/user-text-file-capability")&&holder.fileCapabilityResponse)return holder.fileCapabilityResponse;
      if(url.includes("/files/")&&holder.userFileResponse)return holder.userFileResponse;
      if(url==="/api/agent-template"&&holder.agentTemplates)return holder.agentTemplates(options.headers?.["X-Console-Agent"]);
      if(holder.templateResponses?.[url])return holder.templateResponses[url];
      if(url==="/api/state"&&holder.stateResponse)return holder.stateResponse;
      if(url.startsWith("/api/state?")&&holder.threadStateResponse)return holder.threadStateResponse;
      if(url.startsWith("/api/agent-template")&&holder.templateResponse)return holder.templateResponse;
      if(options.method==="POST"){
        if(holder.response)return holder.response;
        return await new Promise(resolve=>{
          holder.pending=(value={})=>resolve({ok:true,json:async()=>value});
          holder.pendingPosts.push({url,resolve:holder.pending});
        });
      }
      return {ok:true,json:async()=>copy(holder.current)};
    }};
  vm.createContext(context);
  // The bridge flips this one line when FA_REALTIME_ENDPOINT is configured (realtime.page).
  let source=responses.realtime?html.replace("const REALTIME_CONFIGURED=false;","const REALTIME_CONFIGURED=true;"):html;
  // And this one with FA_CONSOLE_PREWARM=1 (prewarm.page).
  if(responses.prewarm)source=source.replace("const PREWARM_CONFIGURED=false;","const PREWARM_CONFIGURED=true;");
  for(const match of source.matchAll(/<script>([\s\S]*?)<\/script>/g))vm.runInContext(match[1],context);
  await new Promise(resolve=>setImmediate(resolve));
  return {nodes,calls,holder,context,timers,timerDelays,documentEvents,text:id=>nodes.get(id).textContent,
    render(value){holder.current=copy(value);context.next=copy(value);vm.runInContext("state=next;render()",context)},
    click(id){nodes.get(id).events.click()},
    tick:()=>new Promise(resolve=>setImmediate(resolve)),
    // Waits for the in-page confirmation, answers 確定 (true) or 取消 (false) and returns the question it asked.
    async answer(yes){
      for(let i=0;i<20&&nodes.get("page-confirm").hidden;i++)await new Promise(resolve=>setImmediate(resolve));
      assert.equal(nodes.get("page-confirm").hidden,false,"an in-page confirmation is open");
      const question=nodes.get("page-confirm-message").textContent;
      nodes.get(yes?"page-confirm-ok":"page-confirm-cancel").events.click();
      await new Promise(resolve=>setImmediate(resolve));
      return question;
    }};
}
function nativeDialog(name){return ()=>{throw new Error(`native ${name} dialog used; ask in the page instead`)}}

test("remote sign-in connects once on page load, shows the user and sends no work",async()=>{
  const data={...fixture(),authentication:"LOGIN_REQUIRED",authentication_refresh_at:null,remote_user:"neal@example.org"};
  const ui=await open(data,{location:{protocol:"https:"}});
  await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),["/api/authenticate"]);
  const refresh=ui.calls.findIndex(call=>call.url==="/.auth/refresh");
  assert.ok(refresh>=0&&refresh<ui.calls.findIndex(call=>call.url==="/api/authenticate"));
  assert.equal(ui.text("auth-state"),"正在連線…");
  ui.holder.current={...data,authentication:"AVAILABLE"};
  ui.holder.pendingPosts[0].resolve({...data,authentication:"AVAILABLE",authentication_refresh_at:null});
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.text("auth-state"),"已登入：neal@example.org");
  assert.equal(ui.nodes.get("login").hidden,true);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("remote re-sign-in goes to the platform login instead of a local connect",async()=>{
  const data={...fixture(),authentication:"LOGIN_EXPIRED",authentication_refresh_at:null,remote_user:"neal@example.org",
    reason:"already stopped"};
  const ui=await open(data,{location:{protocol:"https:"}});
  await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(ui.text("auth-state"),"登入已失效");
  assert.equal(ui.text("login"),"重新登入");
  ui.click("login");
  assert.equal(ui.context.window.location.href,"/.auth/login/aad?post_login_redirect_uri=%2F");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("remote empty platform rejection shows a clear message, offers re-sign-in and resends nothing",async()=>{
  const data={...fixture(),authentication:"LOGIN_REQUIRED",authentication_refresh_at:null,remote_user:"neal@example.org"};
  const rejected=new Response("",{status:403});
  const ui=await open(data,{location:{protocol:"https:"},response:rejected});
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  assert.match(ui.text("error-message"),/雲端登入層拒絕/);
  assert.equal(ui.text("auth-state"),"登入已失效");
  assert.equal(ui.nodes.get("login").hidden,false);
});

test("remote: a missing token-store entry (cookie still valid) answers LOGIN_REQUIRED and offers re-sign-in",async()=>{
  const data={...fixture(),authentication:"LOGIN_REQUIRED",authentication_refresh_at:null,remote_user:"neal@example.org"};
  const missing=new Response(JSON.stringify({error:{code:"LOGIN_REQUIRED",message:"no token"}}),
    {status:401,headers:{"Content-Type":"application/json"}});
  const ui=await open(data,{location:{protocol:"https:"},response:missing});
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  assert.equal(ui.text("auth-state"),"登入已失效");
  assert.equal(ui.text("login"),"重新登入");
  assert.equal(ui.nodes.get("login").hidden,false);
  ui.click("login");
  assert.equal(ui.context.window.location.href,"/.auth/login/aad?post_login_redirect_uri=%2F");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("remote: a sign-in refusal after connecting offers re-sign-in; an ingress 5xx stays not-confirmed",async()=>{
  const data={...fixture(),authentication:"AVAILABLE",authentication_refresh_at:null,remote_user:"neal@example.org"};
  const ui=await open(data,{location:{protocol:"https:"},response:new Response("",{status:403})});
  await ui.tick();
  assert.equal(ui.text("auth-state"),"已登入：neal@example.org");
  assert.equal(ui.nodes.get("login").hidden,true);
  ui.click("status");await ui.tick();await ui.tick();await ui.tick();
  assert.match(ui.text("error-message"),/雲端登入層拒絕/);
  assert.equal(ui.text("auth-state"),"登入已失效");
  assert.equal(ui.nodes.get("login").hidden,false);

  const gateway=await open(data,{location:{protocol:"https:"},
    response:new Response("<html>upstream</html>",{status:503,headers:{"Content-Type":"text/html"}})});
  await gateway.tick();
  gateway.click("status");await gateway.tick();await gateway.tick();await gateway.tick();
  assert.doesNotMatch(gateway.text("error-message"),/雲端登入層拒絕/);
  assert.match(gateway.text("error-full"),/尚未確認/);
  assert.equal(gateway.calls.filter(call=>call.url==="/api/tasks/offline/status").length,1);
});

test("local 8765 never auto-connects or calls the platform refresh",async()=>{
  const data={...fixture(),authentication:"LOGIN_REQUIRED",authentication_refresh_at:null};
  const ui=await open(data);
  await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST"||call.url==="/.auth/refresh").length,0);
  assert.equal(ui.text("auth-state"),"尚未登入");
  assert.equal(ui.text("login"),"");
});

test("connection refresh runs once at its deadline without replaying an in-flight POST",async()=>{
  const data=fixture(),now=Date.parse("2026-09-30T09:00:00Z");
  data.authentication_refresh_at=now/1000+300;
  const ui=await open(data,{now});
  assert.equal(ui.timers.size,1);
  assert.equal([...ui.timerDelays.values()][0],300000);
  ui.click("status");await ui.tick();
  assert.equal(ui.holder.pendingPosts.length,1);
  let finishRefresh;
  ui.holder.response=new Promise(resolve=>{finishRefresh=resolve});
  ui.holder.now=now+300000;
  const [timerId,refresh]=[...ui.timers.entries()][0];ui.timers.delete(timerId);refresh();await ui.tick();
  ui.render(data);await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/authenticate/refresh").length,1);
  finishRefresh({ok:true,json:async()=>({authentication:"AVAILABLE",authentication_refresh_at:now/1000+3300})});
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/tasks/offline/status").length,1);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,2);
  assert.equal(ui.text("auth-state"),"既有登入可用");
  ui.holder.current.authentication_refresh_at=now/1000+3300;
  ui.holder.pendingPosts[0].resolve();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/tasks/offline/status").length,1);
});

test("failed connection refresh stops until manual connect and never sends work",async()=>{
  const data=fixture(),now=Date.parse("2026-09-30T09:00:00Z");
  data.authentication_refresh_at=now/1000-1;
  const ui=await open(data,{now,response:{ok:false,json:async()=>({error:{code:"LOGIN_EXPIRED"}})}});
  const [id,refresh]=[...ui.timers.entries()][0];ui.timers.delete(id);refresh();
  await ui.tick();await ui.tick();
  assert.equal(ui.text("auth-state"),"登入已過期");
  assert.equal(ui.nodes.get("login").disabled,false);
  assert.equal(ui.timers.size,0);
  ui.render(data);
  assert.equal(ui.text("auth-state"),"登入已過期");
  ui.documentEvents.visibilitychange();await ui.tick();
  assert.equal(ui.timers.size,0);
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),["/api/authenticate/refresh"]);
  ui.holder.response={ok:true,json:async()=>({})};
  ui.holder.current.authentication_refresh_at=now/1000+3300;
  ui.click("login");await ui.tick();await ui.tick();
  assert.equal(ui.text("auth-state"),"既有登入可用");
  assert.equal(ui.timers.size,1);
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),
    ["/api/authenticate/refresh","/api/authenticate"]);
});

test("returning to a visible page refreshes an elapsed connection deadline",async()=>{
  const data=fixture(),now=Date.parse("2026-09-30T09:00:00Z");
  data.authentication_refresh_at=now/1000+300;
  const ui=await open(data,{now});
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  assert.equal(ui.timers.size,0);
  ui.holder.now=now+660000;ui.context.document.hidden=false;ui.documentEvents.visibilitychange();
  assert.equal(ui.timers.size,1);
  assert.equal([...ui.timerDelays.values()][0],1000);
});

function contextRecord(task){
  return {
    schema_version:"fa-turn-context-v1",request_id:task.task_id,job_id:task.job_id,
    conversation_id:"OFFLINE-conversation",turn_number:1,model:"offline-chat-a",read_at:when,
    source:"saved-turn-metadata",turn_material_sha256:"e".repeat(64),
    prompt_assembly:{profile:"fa-instruction-v1",prefix_utf8_bytes:321,prefix_tokens:null},
    context_read:false,skill_catalog:null,user_turn_sources:null,work_memory_source:null,
    archive:{codec:"artifact-session-fs-v1",sdk_version:"1.0.13",runtime_version:"1.0.83",sha256:"a".repeat(64),encoded_state_bytes:576,
      inline_zlib_bytes:null,part_count:1,unavailable:null,content_reverified:false},
    reply:{json_utf8_bytes:400,text_utf8_bytes:33,stored_zlib_bytes:100,sha256:"b".repeat(64)},
    compaction:null,token_usage:{full_request_tokens:null,provider_input_tokens:null,
      provider_output_tokens:null,reason:"complete-turn-usage-not-recorded"},
    wall_clock_seconds:null,ui_rendered_bytes:null,authorization:false,read_is_acknowledgment:false
  };
}

function attachmentChat(){
  const data=fixture();
  data.composer={thread_id:"attachment-thread",request_id:"attachment-draft",conversation_id:null,
    content:null,attachments:[],editable:true,can_submit:true,can_follow_up:false,input_capacity:{maximum:8192},models};
  return data;
}

function agentChat(identity){
  const data=attachmentChat();
  data.agent_mode=true;data.default_agent_id="agent-one";data.selected_agent_id=identity;
  data.agent_directory_error=null;
  data.agents=[
    {id:"agent-one",name:"第一位助理",api_prefix:"/agents/agent-one",enabled:true,reason:null},
    {id:"agent-two",name:"第二位助理",api_prefix:"/agents/agent-two",enabled:true,reason:null},
  ];
  return data;
}

function sidebarChat(identity,saved=false){
  const data=saved?downloadableChat():cancellableChat();
  Object.assign(data,Object.fromEntries(Object.entries(agentChat(identity)).filter(([key])=>
    ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"].includes(key))));
  data.tasks[0].instruction=identity==="agent-one"?"A 的最新交辦":"B 的最新交辦";
  data.tasks[0].submission.intent={created_at:"2026-10-01T00:20:00Z"};
  if(saved){
    data.tasks[0].reply.data.text=identity==="agent-one"?"A 的原回答\n保留完整內容":"B 的原回答";
    data.tasks[0].reply.observed_at="2026-10-01T00:25:00Z";
  }
  return data;
}
// Each Agent row is a div holding the row button (switches the chat) and its own ⚙ (opens that Agent's settings).
function agentButton(ui,index){return ui.nodes.get("agent-list").children[index].children[0]}
function agentGear(ui,identity){
  return ui.nodes.get("agent-list").children.map(row=>row.children[1]).find(node=>node?.attributes["data-agent-settings"]===identity);
}
function sidebar(ui,identity){
  const button=ui.nodes.get("agent-list").children.map(row=>row.children[0]).find(node=>node?.attributes["data-agent-id"]===identity);
  const copy=button?.children.find(node=>node.className==="thread-copy");
  const meta=button?.children.find(node=>node.className==="agent-activity");
  return {button,preview:copy?.children.find(node=>node.className==="thread-preview"),
    time:meta?.children.find(node=>node.className==="agent-time"),
    badge:meta?.children.find(node=>node.className==="agent-unread")};
}

test("Agent previews use each current conversation's latest known message and stable Taiwan time",async()=>{
  const a=sidebarChat("agent-one",true),b=sidebarChat("agent-two",true);
  b.tasks[0].reply.data.text="<script>不執行內容</script> "+"x".repeat(220);
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  assert.equal(sidebar(ui,"agent-one").preview.textContent,"助理：A 的原回答 保留完整內容");
  assert.match(sidebar(ui,"agent-one").time.textContent,/08:25/);
  assert.equal(sidebar(ui,"agent-two").time,undefined);
  assert.match(sidebar(ui,"agent-two").preview.textContent,/已連線/);
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.match(sidebar(ui,"agent-two").preview.textContent,/^助理：<script>不執行內容<\/script>/);
  assert.ok(sidebar(ui,"agent-two").preview.textContent.length<=120);
  assert.match(sidebar(ui,"agent-one").preview.textContent,/A 的原回答/);
  const originalTime=sidebar(ui,"agent-two").time.textContent;
  b.tasks[0].reply.observed_at="2026-10-01T00:29:00Z";
  ui.render(b);
  assert.equal(sidebar(ui,"agent-two").time.textContent,originalTime,"polling must not change the message's time");
  b.tasks[0].reply.data.text="x"+"🙂".repeat(120);ui.render(b);
  const preview=sidebar(ui,"agent-two").preview.textContent;
  assert.doesNotMatch(preview,/\p{Surrogate}/u,"shortening the preview must not split an emoji");
  assert.ok([...preview].length<=120);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("Agent previews prefer the latest user turn over a prior answer and keep empty-conversation fallback",async()=>{
  const data=sidebarChat("agent-one",true),old=data.tasks[0];
  data.tasks.push({...copy(old),task_id:"new-turn",submit_request_id:"new-turn",instruction:"新問題\r\n不是舊答案",
    reply:null,submission:{state:"CONFIRMED",intent:{created_at:"2026-10-01T00:30:00Z"}}});
  const ui=await open(data);
  assert.equal(sidebar(ui,"agent-one").preview.textContent,"你：新問題 不是舊答案");
  assert.match(sidebar(ui,"agent-one").time.textContent,/08:30/);
  const empty=agentChat("agent-one");empty.tasks=[];
  ui.render(empty);
  assert.match(sidebar(ui,"agent-one").preview.textContent,/已連線/);
  assert.equal(sidebar(ui,"agent-one").time,undefined);
  assert.equal(ui.calls.filter(call=>call.url.startsWith("/api/instructions")).length,0);
});

test("Agent previews use the visible cancellation result instead of an older saved answer",async()=>{
  const data=sidebarChat("agent-one",true);
  data.tasks[0].result=fixture().tasks[0].result;
  data.tasks[0].fetch={state:"CONFIRMED",observed_at:"2026-10-01T00:30:00Z"};
  data.composer.conversation.turns[0].state="cancelled";
  const ui=await open(data);
  assert.match(sidebar(ui,"agent-one").preview.textContent,/^助理：工作已取消，並非正常完成。/);
  assert.doesNotMatch(sidebar(ui,"agent-one").preview.textContent,/A 的原回答/);
  assert.match(sidebar(ui,"agent-one").time.textContent,/08:30/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an unselected running Agent is not read in the background; opening it shows its latest state",async()=>{
  // 2026-10-05 slimming: the old five-second sidebar read of a non-selected Agent's full progress is removed.
  const b=sidebarChat("agent-two"),a=agentChat("agent-one");a.tasks=[];
  const ui=await open(b,{agentStates:{"agent-one":a,"agent-two":b}});
  sidebar(ui,"agent-one").button.events.click();await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="A 未送出的草稿";ui.nodes.get("instruction").events.input();
  assert.equal([...ui.timerDelays.values()].includes(5000),false,"no background timer for the unselected Agent");
  ui.render(a);
  assert.equal(ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")).length,0);
  assert.equal(sidebar(ui,"agent-two").badge,undefined);
  assert.equal(ui.nodes.get("instruction").value,"A 未送出的草稿");
  ui.holder.agentStates["agent-two"]=sidebarChat("agent-two",true);
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"助理：B 的原回答");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("a late B admission updates only B's preview without a background watch, resend or draft move",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");a.tasks=[];b.tasks=[];
  const ui=await open(b,{agentStates:{"agent-one":a,"agent-two":b}});
  ui.nodes.get("instruction").value="只交辦一次給 B";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  sidebar(ui,"agent-one").button.events.click();await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="A 還沒送的內容";ui.nodes.get("instruction").events.input();
  const complete=sidebarChat("agent-two",true),task=complete.tasks[0];
  task.task_id=task.submit_request_id=b.composer.request_id;task.thread_id=b.composer.thread_id;
  const accepted=copy(task);accepted.reply=null;accepted.status={data:{state:"starting"}};
  ui.holder.pendingPosts[0].resolve({task:accepted});await ui.tick();await ui.tick();
  assert.match(sidebar(ui,"agent-two").preview.textContent,/^你：/);
  assert.equal([...ui.timerDelays.values()].includes(5000),false);
  assert.equal(ui.nodes.get("instruction").value,"A 還沒送的內容");
  assert.equal(ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")).length,0);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("public handoff changes and B returns mark only an unselected Agent without copying A's conversation",async()=>{
  const a=sidebarChat("agent-one"),b=agentChat("agent-two");b.tasks=[];
  const projection=secondHopProjection(a);
  projection.messages=[projection.messages[0]];
  projection.messages[0].status="queued";
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},
    agentMessagesResponse:()=>({ok:true,json:async()=>copy(projection)})});
  handoffButton(ui).events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").badge,undefined);
  projection.messages[0].status="completed";
  handoffButton(ui).events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").badge.textContent,"新動態");
  assert.doesNotMatch(sidebar(ui,"agent-two").preview.textContent,/A 的最新交辦/);
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").badge,undefined);
  sidebar(ui,"agent-one").button.events.click();await ui.tick();await ui.tick();
  assert.equal(handoffButton(ui).disabled,false,"switching back must restore the original handoff control");
  handoffButton(ui).events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").badge,undefined,"an unchanged card must not re-arm the badge");
  projection.messages.push(secondHopProjection(a).messages[1]);
  handoffButton(ui).events.click();await ui.tick();await ui.tick();
  assert.match(handoffPanel(ui).textContent,/第二位助理 → 第一位助理：結果／結果已保存/);
  assert.equal(sidebar(ui,"agent-two").badge.textContent,"新動態");
  assert.equal(sidebar(ui,"agent-one").badge,undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a late original A handoff response marks A but never replaces the selected B conversation",async()=>{
  const a=sidebarChat("agent-one"),b=agentChat("agent-two");b.tasks=[];
  let finish;
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},
    agentMessagesResponse:new Promise(resolve=>{finish=resolve})});
  handoffButton(ui).events.click();await ui.tick();
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="B 的原草稿";ui.nodes.get("instruction").events.input();
  finish({ok:true,json:async()=>secondHopProjection(a,"blocked","unknown")});await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-one").badge.textContent,"新動態");
  assert.equal(sidebar(ui,"agent-two").badge,undefined);
  assert.equal(handoffPanel(ui),undefined);
  assert.equal(ui.nodes.get("instruction").value,"B 的原草稿");
  assert.match(sidebar(ui,"agent-one").preview.textContent,/A 的最新交辦/);
  sidebar(ui,"agent-one").button.events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-one").badge,undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("Agent switching restores handoff controls after the selected conversation finishes loading",async()=>{
  const a=secondPieceChat(),b=agentChat("agent-two");b.tasks=[];
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  agentButton(ui,0).events.click();await ui.tick();await ui.tick();
  assert.equal(handoffButton(ui).disabled,false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("agent switching pins every request and keeps identical draft IDs separate",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  ui.nodes.get("instruction").value="只給第一位的草稿";ui.nodes.get("instruction").events.input();
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第二位助理");
  assert.equal(ui.nodes.get("instruction").value,"");
  ui.nodes.get("instruction").value="只給第二位的草稿";ui.nodes.get("instruction").events.input();
  agentButton(ui,0).events.click();await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第一位助理");
  assert.equal(ui.nodes.get("instruction").value,"只給第一位的草稿");
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"只給第二位的草稿");
  assert.ok(ui.calls.some(call=>call.headers?.["X-Console-Agent"]==="agent-two"));
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("late Agent A submission cannot clear Agent B's same-ID draft or pending request",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two"),id=a.composer.request_id;
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  ui.nodes.get("instruction").value="第一位的交辦";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.holder.pendingPosts.length,1);
  assert.equal(ui.calls.find(call=>call.method==="POST").headers["X-Console-Agent"],"agent-one");
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="第二位尚在送出的交辦";ui.nodes.get("instruction").events.input();
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.holder.pendingPosts.length,2);
  ui.holder.pendingPosts[0].resolve({task:{task_id:id,submission:{state:"CONFIRMED"}}});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第二位助理");
  assert.equal(ui.nodes.get("instruction").value,"第二位尚在送出的交辦");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  ui.holder.pendingPosts[1].resolve({task:{task_id:id,submission:{state:"CONFIRMED"}}});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"");
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.deepEqual(posts.map(call=>call.headers["X-Console-Agent"]),["agent-one","agent-two"]);
  assert.equal(posts.length,2);
});

for(const operation of ["send","status"]){
  test(`returning to Agent A during ${operation} rereads current state after the original response`,async()=>{
    const a=operation==="send"?agentChat("agent-one"):{
      ...cancellableChat(),...Object.fromEntries(Object.entries(agentChat("agent-one"))
        .filter(([key])=>["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"].includes(key)))};
    const b=agentChat("agent-two"),id=a.composer.request_id;
    const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
    if(operation==="send"){
      ui.nodes.get("instruction").value="原交辦只送一次";ui.nodes.get("instruction").events.input();
      ui.click("send-instruction");
    }else ui.click("status");
    await ui.tick();
    agentButton(ui,1).events.click();await ui.tick();await ui.tick();
    agentButton(ui,0).events.click();await ui.tick();await ui.tick();
    const reads=ui.calls.filter(call=>call.url.startsWith("/api/state")&&call.headers?.["X-Console-Agent"]==="agent-one").length;
    ui.holder.pendingPosts[0].resolve({task:{task_id:id,submission:{state:"CONFIRMED"}},
      composer:{thread_id:"stale-thread-must-not-win"}});
    await ui.tick();await ui.tick();
    assert.equal(ui.calls.filter(call=>call.url.startsWith("/api/state")&&
      call.headers?.["X-Console-Agent"]==="agent-one").length,reads+1);
    assert.equal(ui.text("chat-title"),"第一位助理");
    assert.equal(ui.text("send-instruction"),"送出");
    assert.equal(ui.text("chat-presence").includes("傳送中"),false);
    assert.equal(ui.calls.some(call=>call.url.includes("stale-thread-must-not-win")),false);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
    if(operation==="status")assert.ok(ui.timers.size>0);
  });
}

test("Agent bootstrap uses the explicit default even when the directory order is reversed",async()=>{
  const a=agentChat("agent-one"),bootstrap=agentChat(null);
  bootstrap.agents.reverse();bootstrap.tasks=[];bootstrap.composer=null;
  const ui=await open(bootstrap,{agentStates:{"agent-one":a}});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第一位助理");
  assert.ok(ui.calls.some(call=>call.headers?.["X-Console-Agent"]==="agent-one"));
  assert.equal(ui.calls.some(call=>call.headers?.["X-Console-Agent"]==="agent-two"),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an unavailable selected Agent keeps its draft and never switches to the default",async()=>{
  const b=agentChat("agent-two");
  const ui=await open(b,{agentStates:{"agent-two":b}});
  ui.nodes.get("instruction").value="保留第二位的草稿";ui.nodes.get("instruction").events.input();
  const agents=copy(b.agents);agents[1].enabled=false;agents[1].reason="AGENT_CONFIGURATION_INVALID";
  ui.holder.agentDirectoryResponse={ok:true,json:async()=>({default_agent_id:"agent-one",agents})};
  ui.click("refresh-agents");await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第二位助理");
  assert.equal(ui.nodes.get("instruction").value,"保留第二位的草稿");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(agentButton(ui,1).disabled,true);
  assert.match(ui.text("agent-directory-note"),/無法核對/);
  assert.equal(ui.calls.some(call=>call.headers?.["X-Console-Agent"]==="agent-one"),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a late Agent progress failure does not release another Agent's in-flight poll",async()=>{
  const a={...cancellableChat(),...Object.fromEntries(Object.entries(agentChat("agent-one"))
    .filter(([key])=>["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"].includes(key)))};
  const b=copy(a);b.selected_agent_id="agent-two";
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  let finishA,finishB;
  ui.holder.progressResponse=new Promise(resolve=>{finishA=resolve});
  const waitingA=vm.runInContext("updateProgress()",ui.context);await ui.tick();
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  ui.holder.progressResponse=new Promise(resolve=>{finishB=resolve});
  const waitingB=vm.runInContext("updateProgress()",ui.context);await ui.tick();
  finishA({ok:false,status:503,json:async()=>({error:{code:"OLD_AGENT_ERROR"}})});
  await waitingA;await ui.tick();
  const extra=vm.runInContext("updateProgress()",ui.context);await ui.tick();
  const count=ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")).length;
  finishB({ok:true,json:async()=>copy(b)});await waitingB;await extra;
  assert.equal(count,2);
  assert.equal(ui.text("chat-title"),"第二位助理");
  assert.equal(ui.nodes.get("error").hidden,true);
});

test("Agent receipt downloads use the selected scope instead of an unscoped anchor",async()=>{
  const data={...cancellableChat(),...Object.fromEntries(Object.entries(agentChat("agent-two"))
    .filter(([key])=>["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"].includes(key)))};
  const bytes=new TextEncoder().encode('{"original_agent":"agent-two","confirmed":false}');
  const ui=await open(data,{summaryResponse:{ok:true,headers:{get:name=>({
    "content-type":"application/json","content-disposition":'attachment; filename="receipt-summary.json"',
  })[name]??null},arrayBuffer:async()=>bytes.buffer}});
  ui.click("summary-download");await ui.tick();await ui.tick();
  const call=ui.calls.find(call=>call.url.endsWith("/summary"));
  assert.equal(call.headers["X-Console-Agent"],"agent-two");
  assert.equal(await ui.holder.downloaded.text(),'{"original_agent":"agent-two","confirmed":false}');
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a shared login completed after an Agent switch refreshes B without restoring A",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  a.authentication=b.authentication="LOGIN_REQUIRED";
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  ui.click("login");await ui.tick();
  assert.equal(ui.holder.pendingPosts[0].url,"/api/authenticate");
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第二位助理");
  ui.holder.agentStates["agent-two"].authentication="AVAILABLE";
  ui.holder.pendingPosts[0].resolve({authentication:"AVAILABLE",authentication_refresh_at:null,
    default_agent_id:"agent-one",agents:b.agents});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第二位助理");
  assert.equal(ui.text("auth-state"),"既有登入可用");
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),["/api/authenticate"]);
});

for(const automatic of [false,true]){
  test(`${automatic?"automatic":"manual"} login recovery restores the directory without changing Agent B`,async()=>{
    const ready=agentChat("agent-two"),blocked=copy(ready);
    blocked.authentication=automatic?"AVAILABLE":"LOGIN_EXPIRED";
    blocked.agent_directory_error="AGENT_DIRECTORY_UNAVAILABLE";
    blocked.agents=blocked.agents.map(agent=>({...agent,enabled:false,reason:"AGENT_DIRECTORY_UNAVAILABLE"}));
    const ui=await open(blocked,{agentStates:{"agent-two":ready},response:{ok:true,json:async()=>({
      authentication:"AVAILABLE",authentication_refresh_at:null,default_agent_id:"agent-one",agents:ready.agents,
    })}});
    if(automatic)await vm.runInContext("refreshAuthentication()",ui.context);
    else ui.click("login");
    await ui.tick();await ui.tick();
    assert.equal(ui.text("chat-title"),"第二位助理");
    assert.equal(ui.text("auth-state"),"既有登入可用");
    assert.equal(agentButton(ui,1).disabled,false);
    assert.equal(ui.nodes.get("instruction").disabled,false);
    assert.equal(ui.nodes.get("agent-directory-note").hidden,true);
    assert.equal(ui.calls.some(call=>call.headers?.["X-Console-Agent"]==="agent-one"),false);
    assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),
      [automatic?"/api/authenticate/refresh":"/api/authenticate"]);
  });
}
test("a silent sign-in refresh that fails with a 503 keeps the login and retries; only a sign-in failure expires it",async()=>{
  const now=Date.parse(when),data={...fixture(),authentication_refresh_at:now/1000+5};
  const ui=await open(data,{now,response:{ok:false,status:503,
    json:async()=>({error:{code:"AGENT_DIRECTORY_UNAVAILABLE"}})}});
  await vm.runInContext("refreshAuthentication()",ui.context);
  assert.equal(vm.runInContext("state.authentication",ui.context),"AVAILABLE");
  assert.equal(vm.runInContext("authenticationStopped",ui.context),false);
  assert.equal(vm.runInContext("state.authentication_refresh_at",ui.context),now/1000+60,"retried in a minute, not every second");
  assert.notEqual(vm.runInContext("lastError?.code",ui.context),"LOGIN_EXPIRED");
  ui.holder.response={ok:false,status:502,json:async()=>{throw new SyntaxError("ingress HTML")}};
  await vm.runInContext("refreshAuthentication()",ui.context);
  assert.equal(vm.runInContext("state.authentication",ui.context),"AVAILABLE","a non-JSON 5xx is not an expired sign-in");
  ui.holder.response={ok:false,status:401,json:async()=>({error:{code:"PLATFORM_REQUEST_REJECTED"}})};
  await vm.runInContext("refreshAuthentication()",ui.context);
  assert.equal(vm.runInContext("state.authentication",ui.context),"LOGIN_EXPIRED");
  assert.equal(vm.runInContext("authenticationStopped",ui.context),true);
  const wrong=await open(data,{now,response:{ok:false,status:403,json:async()=>({error:{code:"IDENTITY_MISMATCH"}})}});
  await vm.runInContext("refreshAuthentication()",wrong.context);
  assert.equal(vm.runInContext("authenticationStopped",wrong.context),true,"a wrong account still stops, as before");
  const denied=await open(data,{now,response:{ok:false,status:403,
    json:async()=>({error:{code:"REMOTE_PRINCIPAL_DENIED"}})}});
  await vm.runInContext("refreshAuthentication()",denied.context);
  assert.equal(vm.runInContext("authenticationStopped",denied.context),true,"a remote wrong account stops too");
  assert.equal(vm.runInContext("state.authentication",denied.context),"LOGIN_EXPIRED");
});
function attachmentCatalog(data,entries=[{
  source_id:"source-one",state:"recorded",media_type:"text/plain",
  reference:{source_id:"source-one",version:1,sha256:"a".repeat(64),byte_length:42},
}]){
  const composer=data.composer,cid=composer.conversation_id??null;
  return {thread_id:composer.thread_id,request_id:composer.request_id,conversation_id:cid,
    catalog:{schema_version:"fa-attachment-source-catalog-v1",state:"metadata",read_at:when,
      ...(cid===null?{}:{conversation_id:cid}),entries,authorization:false,
      content_reverified:false,dependencies_reverified:false}};
}
function attachmentCheckbox(ui,index=0){return ui.nodes.get("attachment-options").children[index].children[0]}

function fileCapability(data,profile="fa-user-text-files-v1"){
  return {thread_id:data.composer.thread_id,request_id:data.composer.request_id,
    conversation_id:data.composer.conversation_id??null,read_at:when,
    user_text_file_profile:profile,authorization:false};
}
function selectedFile(name,text){
  const bytes=new TextEncoder().encode(text);
  return {name,size:bytes.byteLength,arrayBuffer:async()=>bytes.buffer,text:()=>{throw Error("Do not replace malformed UTF8")}};
}
async function readyFileInput(ui,data){
  ui.holder.fileCapabilityResponse={ok:true,json:async()=>fileCapability(data)};
  ui.click("read-user-file-capability");await ui.tick();
}

test("literal selected file preserves BOM CRLF and U+FFFD and is never posted before Send",async()=>{
  const data=attachmentChat(),ui=await open(data),text="\ufeff  Name,Value\r\nsample,\ufffd🙂\r\n";
  assert.equal(ui.calls.some(call=>call.url.endsWith("/user-text-file-capability")),false);
  await readyFileInput(ui,data);
  const input=ui.nodes.get("user-text-files");
  assert.equal(input.disabled,false);
  input.files=[selectedFile("selected.csv",text)];input.events.change();await ui.tick();
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  assert.match(ui.nodes.get("user-file-selected").children[0].children[1].textContent,/bytes/);
  assert.doesNotMatch(ui.text("user-file-note"),/sample,/);
  ui.nodes.get("instruction").value="Keep this literal file.";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].body),{content:"Keep this literal file.",model:"offline-chat-a",
    files:[{name:"selected.csv",text}]});
});

function imageChat(){
  const data=attachmentChat();
  data.composer.state_profile="foundry-native-v1";
  data.composer.models=copy(models);data.composer.models.models[0].supports_image_input=true;
  return data;
}
function imageSupport(ui){
  const png=Uint8Array.from([137,80,78,71,13,10,26,10,1,2,3]),sizes=[];
  ui.context.createImageBitmap=async()=>({width:4000,height:1000,close(){}});
  ui.context.btoa=value=>Buffer.from(value,"binary").toString("base64");
  const create=ui.context.document.createElement;
  ui.context.document.createElement=tag=>{
    const node=create(tag);
    if(tag==="canvas"){
      node.getContext=()=>({drawImage(){}});
      node.toBlob=done=>{sizes.push([node.width,node.height]);done(new Blob([png],{type:"image/png"}))};
    }
    return node;
  };
  return {png,sizes};
}

test("attached images are converted, uploaded once each and sent as references only",async()=>{
  const data=imageChat(),ui=await open(data),{png,sizes}=imageSupport(ui),sha="f".repeat(64);
  const input=ui.nodes.get("user-images");
  assert.equal(ui.nodes.get("user-image-picker").hidden,false);
  assert.equal(input.disabled,false);
  input.files=[{name:"My Photo.jpg",type:"image/jpeg"}];
  const changed=input.events.change();
  for(let i=0;i<5&&!ui.holder.pendingPosts.length;i++)await ui.tick();
  const upload=ui.holder.pendingPosts.at(-1);
  assert.equal(upload.url,"/api/instructions/attachment-draft/image-upload");
  assert.deepEqual(JSON.parse(ui.calls.at(-1).body),{name:"My-Photo.png",png_base64:Buffer.from(png).toString("base64")});
  assert.deepEqual(sizes,[[2000,500]]);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  upload.resolve({name:"My-Photo.png",sha256:sha,byte_length:png.length,media_type:"image/png"});
  await changed;await ui.tick();
  assert.match(ui.text("user-image-note"),/1／4/);
  ui.holder.environmentResponse={ok:true,json:async()=>({thread_id:data.composer.thread_id,environment:{
    schema_version:"fa-native-environment-metadata-v1",agent_id:"configured-agent",hosted_session_id:"current-sid",
    hosted_agent_version:"42",state:"unverified",observed_sessions:[],
    state_scope:"platform-metadata-only",preparation_verified:false,scope:"entire-agent",
    model_started:false,original_work_replayed:false}})};
  ui.nodes.get("instruction").value="What is this?";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  const checked=copy(data);
  checked.composer.environment_preparation={state:"CONFIRMED",automatic_retry:false,receipt:{
    schema_version:"fa-native-environment-v1",agent_id:"configured-agent",request_id:"idle-current",
    previous_hosted_session_id:"current-sid",previous_agent_version:"42",hosted_session_id:"current-sid",
    hosted_agent_version:"42",state:"current",reason:null,scope:"entire-agent",
    model_started:false,original_work_replayed:false}};
  ui.holder.current=checked;ui.holder.pending(checked);await ui.tick();await ui.tick();
  const submits=ui.calls.filter(call=>call.method==="POST"&&call.url.endsWith("/submit"));
  assert.equal(submits.length,1);
  assert.deepEqual(JSON.parse(submits[0].body),{content:"What is this?",model:"offline-chat-a",
    images:[{name:"My-Photo.png",sha256:sha,byte_length:png.length,media_type:"image/png"}]});
});

test("images follow the model's declared support on both native lanes and stay off elsewhere",async()=>{
  const data=imageChat();data.composer.models.models[0].supports_image_input=false;
  const ui=await open(data);
  assert.equal(ui.nodes.get("user-images").disabled,true);
  assert.match(ui.text("user-image-note"),/不讀圖片/);
  const responses=imageChat();responses.composer.state_profile="foundry-responses-v1";
  ui.render(responses);
  assert.equal(ui.nodes.get("user-images").disabled,false);
  assert.equal(ui.nodes.get("user-image-picker").hidden,false);
  const legacy=imageChat();delete legacy.composer.state_profile;
  ui.render(legacy);
  assert.equal(ui.nodes.get("user-images").disabled,true);
});

function questionItem(thread,{freeform=true}={}){
  const qid="e".repeat(64);
  return {thread_id:thread,label:"報告寄送",item:{schema_version:"fa-native-attention-v1",type:"question_required",
    item_id:qid,question:{question_id:qid,question_digest:"f".repeat(64),call:{agent_id:"configured-agent",
      conversation_id:"original-cid",request_id:"original-rid",sdk_session_id:"original-sdk",tool_call_id:"ask-user-local-1"}},
    text:"要寄給 <b>誰</b>？",choices:["Ann","Bo"],allow_freeform:freeform,created_at:when,
    expires_at:"2026-01-01T00:10:00Z",purpose:"attention-only-not-approval"}};
}
function answerReply(item,body,{state="CONFIRMED",request_id="a".repeat(64)}={}){
  return {request_id,original_request_resent:false,purpose:"readback-only-not-execution-permission",
    commands:[{request_id,kind:"answer",state,thread_id:"attachment-thread",code:state==="CONFIRMED"?null:"LOCAL_OFFLINE",
      command:{kind:"answer",request_id,question_id:item.question.question_id,question_digest:item.question.question_digest,
        answer:body.answer,was_freeform:body.was_freeform},selection:item.question,observation:null}]};
}
test("an SDK question shares the attention list and another thread's card only opens that conversation",async()=>{
  const data=hitlChat();
  data.composer.attention=[questionItem("other-thread"),{thread_id:"x",item:{type:"answer_awaited",
    purpose:"attention-only-not-approval",question:{turn_number:2}}}];
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>({})})});
  assert.equal(ui.nodes.get("attention-panel").hidden,false);
  const rows=ui.nodes.get("attention-items").children;
  assert.equal(rows.length,1);
  assert.equal(rows[0].children[0].textContent,"助理在問你：報告寄送");
  assert.equal(rows[0].children[1].textContent,"要寄給 <b>誰</b>？");
  assert.equal(rows[0].children.length,3);
  const before=ui.calls.length;
  rows[0].children[2].events.click();await ui.tick();
  const reads=ui.calls.slice(before);
  assert.ok(reads.length>0&&reads.every(call=>call.method!=="POST"));
  assert.ok(reads.some(call=>call.url.includes("other-thread")));
  const off=hitlChat();off.composer.native_hitl.enabled=false;off.composer.attention=[questionItem("other-thread")];
  ui.render(off);
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
});
test("a question choice sends one answer, never a decision, and an unconfirmed original is only reconciled",async()=>{
  const data=hitlChat(),entry=questionItem(data.composer.thread_id),qid=entry.item.item_id,posts=[];
  data.composer.attention=[entry];
  let state="UNKNOWN";
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    const body=JSON.parse(options.body);posts.push({url,body});
    const reply=answerReply(entry.item,url.endsWith("/answers")?body:{answer:"Bo",was_freeform:false},{state});
    return {ok:true,json:async()=>reply};
  }});
  const buttons=hitlNodes(ui,"data-question-choice",qid);
  assert.deepEqual(buttons.map(button=>button.textContent),["Ann","Bo"]);
  buttons[1].events.click();
  // The same redraw turns the card into its record (Neal 2026-10-07); a second click on the old node sends nothing.
  assert.equal(hitlNodes(ui,"data-question-card",qid).length,0);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",qid)[0].children.map(node=>node.textContent),
    ["助理問：要寄給 <b>誰</b>？","你的回答：Bo","送出中…"]);
  buttons[1].events.click();buttons[0].events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"one click answers; no second confirmation");
  assert.equal(posts.length,1);
  assert.ok(posts[0].url.endsWith("/"+data.composer.thread_id+"/answers"));
  assert.deepEqual(posts[0].body,{question:entry.item.question,answer:"Bo",was_freeform:false});
  const record=hitlNodes(ui,"data-hitl-record",qid)[0];
  assert.equal(record.children[1].textContent,"你的回答：Bo");
  assert.match(record.children[2].textContent,/還不確定回答有沒有送到；只核對原回答，不會重送或改答/);
  assert.equal(hitlNodes(ui,"data-question-choice",qid).length,0);
  const check=hitlNodes(ui,"data-hitl-record-check","a".repeat(64))[0];
  assert.equal(check.textContent,"只核對原回答");
  state="CONFIRMED";check.events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(posts.length,2);
  assert.ok(posts[1].url.endsWith("/answer-reconcile"));
  assert.deepEqual(posts[1].body,{request_id:"a".repeat(64)});
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",qid)[0].children.map(node=>node.textContent),
    ["助理問：要寄給 <b>誰</b>？","你的回答：Bo"],"confirmed: the sending note goes away and nothing claims delivery");
  assert.ok(posts.every(post=>!post.url.endsWith("/decisions")));
});
test("an own answer keeps its draft across refreshes and is sent as freeform once",async()=>{
  const data=hitlChat(),entry=questionItem(data.composer.thread_id),qid=entry.item.item_id,posts=[];
  data.composer.attention=[entry];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    const body=JSON.parse(options.body);posts.push(body);
    return {ok:true,json:async()=>answerReply(entry.item,body)};
  }});
  const strict=hitlChat();strict.composer.attention=[questionItem(strict.composer.thread_id,{freeform:false})];
  ui.render(strict);
  assert.equal(hitlNodes(ui,"data-question-send",qid).length,0,"no own answer when the question does not allow one");
  ui.render(data);
  const send=hitlNodes(ui,"data-question-send",qid)[0];
  assert.equal(send.disabled,true);
  const input=hitlNodes(ui,"data-hitl-rule","question:"+qid)[0];
  input.value="寄給 Cy";input.events.input();
  assert.equal(hitlNodes(ui,"data-question-send",qid)[0].disabled,false);
  ui.render(data);
  assert.equal(hitlNodes(ui,"data-hitl-rule","question:"+qid)[0].value,"寄給 Cy");
  hitlNodes(ui,"data-question-send",qid)[0].events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"one click sends; no second confirmation");
  assert.deepEqual(posts,[{question:entry.item.question,answer:"寄給 Cy",was_freeform:true}]);
  assert.equal(hitlNodes(ui,"data-hitl-record",qid)[0].children[1].textContent,"你的回答：寄給 Cy");
});
test("an own answer goes out with Enter, never with Shift+Enter or while an IME candidate is picked",async()=>{
  const data=hitlChat(),entry=questionItem(data.composer.thread_id),qid=entry.item.item_id,posts=[];
  data.composer.attention=[entry];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    const body=JSON.parse(options.body);posts.push(body);
    return {ok:true,json:async()=>answerReply(entry.item,body)};
  }});
  const input=hitlNodes(ui,"data-hitl-rule","question:"+qid)[0];
  input.value="寄給 Cy";input.events.input();
  let prevented=0;const key=fields=>({key:"Enter",shiftKey:false,ctrlKey:false,altKey:false,metaKey:false,
    isComposing:false,keyCode:13,preventDefault(){prevented++},...fields});
  input.events.keydown(key({shiftKey:true}));
  input.events.keydown(key({isComposing:true}));
  input.events.keydown(key({keyCode:229}));
  input.events.compositionstart();input.events.keydown(key());input.events.compositionend();
  await ui.tick();
  assert.equal(posts.length,0);assert.equal(prevented,0);
  input.events.keydown(key());
  await ui.tick();await ui.tick();
  assert.equal(prevented,1);
  assert.deepEqual(posts,[{question:entry.item.question,answer:"寄給 Cy",was_freeform:true}]);
});

function hitlChat(){
  const data=attachmentChat(),pid="a".repeat(64);
  const approval={pending_id:pid,pending_digest:"b".repeat(64),call:{
    agent_id:"configured-agent",conversation_id:"original-cid",request_id:"original-rid",
    sdk_session_id:"original-sdk",tool_call_id:"original-call"}};
  data.composer.state_profile="foundry-native-v1";
  data.composer.native_hitl={enabled:true,can_approve:true,references:[approval],commands:[],error:null};
  data.composer.attention=[{thread_id:data.composer.thread_id,label:"原工作",item:{
    schema_version:"fa-native-attention-v1",type:"approval_required",item_id:pid,approval,
    created_at:when,expires_at:"2026-01-01T00:10:00Z",purpose:"attention-only-not-approval"}}];
  return data;
}
function hitlRead(data=hitlChat()){
  const item=data.composer.attention[0].item;
  return {rules:{schema_version:"fa-native-hitl-rules-v1",team_id:"team-one",revision:0,etag:null,rules:[]},
    read_at:when,can_approve:true,commands:[],readback:{
      schema_version:"fa-native-hitl-readback-v1",approval:item.approval,status:"pending",decision:null,grant:null,
      expired:false,turn_closed:false,authorization:null,observed_at:when,current_rules_revision:0,
      purpose:"readback-only-not-execution-permission",attention:item,
      pending:{pending_id:item.item_id,created_at:when,expires_at:item.expires_at,
        command:{action_digest:"c".repeat(64),review:{rules_revision:0,source:"default",outcome:"ask",matched_rule_ids:[]},
          action:{call:item.approval.call,category:"shell",tool:"run_command",target:"<b>original workspace</b>",
            arguments:{command:"echo <script>do not execute this HTML</script>",payload:"x".repeat(70*1024)}}}}}};
}
function hitlNodes(ui,attribute,value){
  const found=[],walk=node=>{
    if(node.attributes?.[attribute]===value)found.push(node);
    for(const child of node.children||[])walk(child);
  };
  for(const id of ["attention-items","attention-dock","conversation-history","hitl-rule-list","hitl-command-list","hitl-history-items"])if(ui.nodes.has(id))walk(ui.nodes.get(id));
  return found;
}
function teamAttentionCase(target="agent-two",kind="approval"){
  const data=nativeChat("completed"),scope=agentChat("agent-one"),parent=data.tasks[0];
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
    data[key]=scope[key];
  parent.instruction="請 Agent B 查 VM 的 CPU、memory 和 disk space。";
  parent.admission.conversation_id=data.composer.conversation_id="a-cid";
  data.composer.native_hitl={enabled:true,can_approve:true,team_attention_available:true,references:[],commands:[],error:null};
  data.composer.attention=[];
  const child=hitlAgent(target),entry=kind==="approval"?child.composer.attention[0]:questionItem("unknown-b-thread");
  const reference=entry.item.approval||entry.item.question;
  Object.assign(reference.call,{agent_id:target,conversation_id:target==="agent-one"?"a-cid":"b-cid",
    request_id:target==="agent-one"?parent.task_id:"b-rid",sdk_session_id:"b-sdk",tool_call_id:"b-call"});
  child.composer.attention=[entry];
  const view=kind==="approval"?hitlRead(child):null;
  if(view){
    view.rules.revision=1;view.rules.etag="rules-v1";view.rules.rules=[
      {rule_id:"shell-ask",text:"shell 先問我",behavior:"ask_first"}];
    view.readback.pending.command.review={rules_revision:1,source:"model",outcome:"ask",matched_rule_ids:["shell-ask"]};
    view.readback.pending.command.action.arguments={command:"free -h"};
    view.readback.pending.command.action.target="/home/agent/.fa-assistant-v12/desktop/files";
  }
  const root={agent_id:"agent-one",conversation_id:"a-cid",request_id:parent.task_id};
  const page={schema_version:"fa-native-hitl-team-attention-v1",team_id:"team-one",
    items:[{item:entry.item,origin:{state:"verified",root,sender:target==="agent-one"?null:root}}],
    next_cursor:null,observed_at:when};
  return {data,parent,entry,reference,view,page};
}
for(const target of ["agent-one","agent-two"]){
  test(`Team approval ${target} appears under A's original turn and keeps the original decision route`,async()=>{
    const c=teamAttentionCase(target),posts=[],reads=[];
    const ui=await open(c.data,{now:Date.parse(when),hitl:async(url,options)=>{
      if(options.method==="POST"){
        const body=JSON.parse(options.body);posts.push({url,body,headers:options.headers});
        return {ok:true,json:async()=>hitlCommandReply(body)};
      }
      reads.push({url,headers:options.headers});
      return {ok:true,json:async()=>copy(url.includes("/attention")?c.page:c.view)};
    }});
    await ui.tick();await ui.tick();await ui.tick();
    const row=ui.nodes.get("conversation-history").children.find(node=>
      node.children.some(child=>child.textContent===c.parent.instruction));
    assert.ok(JSON.stringify(row).includes(c.reference.pending_id),"The card belongs below the root turn, not just the overview");
    const card=hitlNodes(ui,"data-hitl-card",c.reference.pending_id)[0];
    assert.match(JSON.stringify(card),/free -h/);
    assert.match(JSON.stringify(card),/shell 先問我/);
    assert.ok(reads.some(read=>read.url.includes("/team/pending/")&&read.headers["X-Console-Agent"]===target));
    assert.equal(posts.length,0);
    hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();await ui.tick();await ui.tick();
    assert.equal(ui.nodes.get("page-confirm").hidden,true,"one click decides; no second confirmation");
    assert.ok(JSON.stringify(row).includes(c.reference.pending_id));
    assert.deepEqual(hitlNodes(ui,"data-hitl-record",c.reference.pending_id)[0].children.map(node=>node.textContent),
      [`${target==="agent-one"?"助理":"第二位助理"}要執行：free -h`,"你允許了這次"],"another Agent's record names it");
    assert.equal(posts.length,1);
    assert.match(posts[0].url,/\/team\/decisions$/);
    assert.equal(posts[0].headers["X-Console-Agent"],target);
    assert.deepEqual(posts[0].body.approval,c.reference);
    assert.equal(vm.runInContext("selectedAgent",ui.context),"agent-one");
    assert.equal(vm.runInContext("state.composer.request_id",ui.context),c.parent.task_id);
  });
}
test("B's Team question is answered from A with B's original question reference, never an approval",async()=>{
  const c=teamAttentionCase("agent-two","question"),posts=[];
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url,options)=>{
    if(options.method==="POST"){
      const body=JSON.parse(options.body);posts.push({url,body,headers:options.headers});
      return {ok:true,json:async()=>answerReply(c.entry.item,body)};
    }
    return {ok:true,json:async()=>copy(c.page)};
  }});
  await ui.tick();await ui.tick();
  const choices=hitlNodes(ui,"data-question-choice",c.reference.question_id);
  assert.equal(choices.length,2);
  choices[0].events.click();await ui.tick();await ui.tick();
  assert.equal(posts.length,1);
  assert.match(posts[0].url,/\/team\/answers$/);
  assert.equal(posts[0].headers["X-Console-Agent"],"agent-two");
  assert.deepEqual(posts[0].body.question,c.reference);
  assert.equal(posts.some(post=>post.url.endsWith("/decisions")),false);
  assert.equal(vm.runInContext("selectedAgent",ui.context),"agent-one");
});

test("Team discovery continues through empty pages and refreshes while A has no active turn",async()=>{
  const c=teamAttentionCase(),requests=[];
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    requests.push(url);
    return {ok:true,json:async()=>copy(!url.includes("/attention")?c.view:
      url.includes("?cursor=")?c.page:{...c.page,items:[],next_cursor:"opaque-cursor"})};
  }});
  await ui.tick();await ui.tick();await ui.tick();
  assert.ok(requests.includes("/api/native-hitl/team/attention?cursor=opaque-cursor"));
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once").length,1);
  const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===60000);
  assert.ok(timer,"An idle selected Agent still observes Team attention, on the slow background timer");
  assert.equal([...ui.timerDelays.values()].includes(5000),false);
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  assert.equal(ui.timers.has(timer[0]),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Web PubSub push hints (Neal 2026-10-06 08:28: 「如果可以降低FA portal and api 的呼叫次數，就加Azure Web PubSub」).
async function connectPush(ui){
  for(let i=0;i<6&&ui.holder.sockets.filter(socket=>!socket.closed).length===0;i++)await ui.tick();
  const socket=ui.holder.sockets.at(-1);
  assert.ok(socket&&!socket.closed,"one Web PubSub connection");
  socket.onopen();await ui.tick();await ui.tick();
  return socket;
}
function pushEvent(socket,event){
  socket.onmessage({data:JSON.stringify({schema_version:"fa-realtime-event-v1",team_id:"team-one",at:when,...event})});
}
test("without a configured endpoint the page negotiates nothing and polls as before",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)})});
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.some(call=>call.url==="/api/realtime/negotiate"),false);
  assert.equal(ui.holder.sockets.length,0);
  assert.equal(ui.timerDelays.get(vm.runInContext("teamHitlTimer",ui.context)),60000);
});

test("with push the Team list is read once, then only when a card changes; a dropped connection polls again",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)})});
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length;
  const team=()=>vm.runInContext("teamHitlTimer",ui.context);
  await ui.tick();
  assert.equal(reads(),0,"the first read waits for the connection instead of reading twice");
  const socket=await connectPush(ui);
  assert.equal(ui.calls.filter(call=>call.url==="/api/realtime/negotiate").length,1);
  assert.equal(ui.calls.find(call=>call.url==="/api/realtime/negotiate").headers?.["X-Console-Agent"],"agent-one");
  assert.match(socket.url,/^wss:\/\/wps-fa-test\.webpubsub\.azure\.com\/client\/hubs\/fa\?access_token=/);
  assert.equal(reads(),1);
  assert.equal(ui.timerDelays.get(team()),300000,"no 60 second Team poll while push is open; a 5 minute safety read");
  const card={type:"hitl.approval",agent_id:"agent-two",conversation_id:"b-cid",request_id:"b-rid",pending_id:"p-1",
    status:"pending"};
  pushEvent(socket,card);await ui.tick();await ui.tick();
  assert.equal(reads(),2,"one card event, one Team read");
  pushEvent(socket,card);await ui.tick();await ui.tick();
  assert.equal(reads(),2,"the same event twice reads once");
  pushEvent(socket,{...card,at:"2026-01-01T00:00:09.123Z"});await ui.tick();await ui.tick();
  assert.equal(reads(),2,"the same event published again with its own time reads once");
  pushEvent(socket,{...card,team_id:"other-team"});pushEvent(socket,{...card,agent_id:"not-in-this-console"});
  pushEvent(socket,{type:"turn.finished",request_id:"no-agent"});
  await ui.tick();await ui.tick();
  assert.equal(reads(),2,"events for another Team, an unknown Agent, or a non-card hint without its Agent are ignored");
  pushEvent(socket,{type:"hitl.approval",pending_id:"p-9",status:"allow"});await ui.tick();await ui.tick();
  assert.equal(reads(),2,"a decision is taken off the list by its card ID, even without its other IDs; no read");
  assert.equal(ui.nodes.get("attention-panel").hidden,true,"no loading block for an event read");
  socket.onclose();await ui.tick();
  assert.equal(ui.timerDelays.get(team()),60000,"a dropped connection goes back to the 60 second poll");
  const retry=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===5000);
  assert.ok(retry,"and connects again later");
  ui.timers.delete(retry[0]);retry[1]();
  await connectPush(ui);
  assert.equal(ui.calls.filter(call=>call.url==="/api/realtime/negotiate").length,2,"every reconnect negotiates a new URL");
  assert.equal(reads(),3,"after a fallback the list is read once to catch up");
  assert.equal(ui.timerDelays.get(team()),300000);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a failed negotiate or a connection that never opens falls back to polling",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};
  const hitl=async(url)=>({ok:true,json:async()=>copy(url.includes("/attention")?empty:c.view)});
  const refused=await open(c.data,{now:Date.parse(when),realtime:true,hitl,
    negotiateResponse:{ok:false,json:async()=>({error:{code:"BACKEND_UNAVAILABLE"}})}});
  await refused.tick();await refused.tick();await refused.tick();
  assert.equal(refused.holder.sockets.length,0);
  assert.equal(refused.timerDelays.get(vm.runInContext("teamHitlTimer",refused.context)),60000);
  assert.ok([...refused.timerDelays.values()].includes(5000),"negotiate is retried later");
  const stuck=await open(c.data,{now:Date.parse(when),realtime:true,hitl});
  for(let i=0;i<4;i++)await stuck.tick();
  assert.equal(stuck.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length,0);
  const cap=[...stuck.timers.entries()].find(([id])=>stuck.timerDelays.get(id)===10000);
  assert.ok(cap,"a connection gets at most 10 seconds to open");
  stuck.timers.delete(cap[0]);cap[1]();await stuck.tick();await stuck.tick();
  assert.equal(stuck.holder.sockets[0].closed,true);
  assert.equal(stuck.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length,1,
    "then the list is read by polling");
  assert.equal(stuck.timerDelays.get(vm.runInContext("teamHitlTimer",stuck.context)),60000);
});

test("with push a running turn is read every 3 seconds, and at once when it ends",async()=>{
  const data=nativeChat("started"),rid=data.tasks[0].task_id;
  const ui=await open(data,{realtime:true});
  const socket=await connectPush(ui);
  assert.ok([...ui.timerDelays.values()].includes(3000),"the running turn's status is read every 3 seconds");
  assert.equal([...ui.timerDelays.values()].includes(1000),false,"not every second");
  const done=nativeChat("completed");
  done.tasks[0].reply={data:{text:"推送後的回覆"},sha256:"a".repeat(64),byte_length:18,observed_at:when};
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  const before=ui.calls.length;
  pushEvent(socket,{type:"turn.finished",agent_id:"configured-agent",request_id:rid});
  pushEvent(socket,{type:"turn.finished",agent_id:"configured-agent",request_id:rid,at:"2026-01-01T00:00:01Z"});
  await ui.tick();await ui.tick();await ui.tick();
  assert.deepEqual(ui.calls.slice(before).map(call=>call.url).filter(url=>url.startsWith("/api/instructions")),
    [`/api/instructions/${rid}/status`],"the end event reads that turn once");
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value),
    /推送後的回覆/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("with push dropped a running turn goes back to the one-second status read",async()=>{
  const data=nativeChat("started");
  const ui=await open(data,{realtime:true});
  const socket=await connectPush(ui);
  assert.ok([...ui.timerDelays.values()].includes(3000));
  socket.onclose();await ui.tick();
  assert.ok([...ui.timerDelays.values()].includes(1000),"polling every second again");
  assert.equal([...ui.timerDelays.values()].includes(3000),false);
});

test("a card in the open conversation is read with that conversation; a hint while hidden waits for the page",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)})});
  const socket=await connectPush(ui);
  const team=()=>ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length;
  const progress=()=>ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")).length;
  const [teamBefore,progressBefore]=[team(),progress()];
  pushEvent(socket,{type:"hitl.question",agent_id:"agent-one",conversation_id:"a-cid",request_id:c.parent.task_id,
    question_id:"q-1",status:"pending"});
  await ui.tick();await ui.tick();
  assert.equal(progress(),progressBefore+1,"this conversation's own cards come with its read");
  assert.equal(team(),teamBefore);
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  pushEvent(socket,{type:"hitl.approval",agent_id:"agent-two",conversation_id:"b-cid",request_id:"b-rid",
    pending_id:"p-2",status:"pending"});
  await ui.tick();await ui.tick();
  assert.equal(team(),teamBefore,"nothing is read while the page is hidden");
  ui.context.document.hidden=false;ui.documentEvents.visibilitychange();await ui.tick();await ui.tick();
  assert.equal(team(),teamBefore+1,"the hint is read once when the page is shown again");
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  ui.context.document.hidden=false;ui.documentEvents.visibilitychange();await ui.tick();await ui.tick();
  assert.equal(team(),teamBefore+1,"showing the page without a hint reads nothing");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("with push open a lost hint is caught by the 5 minute safety read, and a decided card leaves without a read",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};let page=empty;
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?page:c.view)})});
  const socket=await connectPush(ui);
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length;
  const cards=()=>vm.runInContext("teamHitlView.items.length",ui.context);
  assert.equal(cards(),0);
  page=c.page;
  const safety=vm.runInContext("teamHitlTimer",ui.context);
  assert.equal(ui.timerDelays.get(safety),300000);
  const run=ui.timers.get(safety);ui.timers.delete(safety);run();await ui.tick();await ui.tick();
  assert.equal(reads(),2,"the card whose hint was lost appears with the safety read");
  assert.equal(cards(),1);
  page=empty;
  pushEvent(socket,{type:"hitl.approval",agent_id:"agent-two",conversation_id:"b-cid",request_id:"b-rid",
    pending_id:c.reference.pending_id,status:"allow"});
  await ui.tick();await ui.tick();
  assert.equal(cards(),0,"the decided card is taken off by its ID");
  assert.equal(reads(),2,"without another Team scan");
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a card decided while a Team scan runs is read again after it, and a card here leaves the list too",async()=>{
  const c=teamAttentionCase("agent-one"),empty={...copy(c.page),items:[]};let page=c.page,held=null;
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>{
    if(!url.includes("/attention"))return {ok:true,json:async()=>copy(c.view)};
    if(held!==null)return new Promise(resolve=>{held=resolve});
    return {ok:true,json:async()=>copy(page)};
  }});
  const socket=await connectPush(ui);
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length;
  const cards=()=>vm.runInContext("teamHitlView.items.length",ui.context);
  assert.equal(cards(),1);
  // A card in the open conversation: the Team entry would outlast the conversation's own read, so it is dropped too.
  pushEvent(socket,{type:"hitl.approval",agent_id:"agent-one",conversation_id:"a-cid",request_id:c.parent.task_id,
    pending_id:c.reference.pending_id,status:"allow"});
  await ui.tick();await ui.tick();
  assert.equal(cards(),0,"the decided card here leaves the Team list");
  // A scan started before a decision returns the card from before it: one more read follows at once.
  held=false;
  pushEvent(socket,{type:"hitl.approval",agent_id:"agent-two",conversation_id:"b-cid",request_id:"b-rid",
    pending_id:"other-new-card",status:"pending"});
  await ui.tick();await ui.tick();
  assert.equal(typeof held,"function","a Team scan is running");
  const during=reads();
  pushEvent(socket,{type:"hitl.approval",agent_id:"agent-one",conversation_id:"a-cid",request_id:c.parent.task_id,
    pending_id:c.reference.pending_id,status:"deny"});
  await ui.tick();
  const finish=held;held=null;page=empty;
  finish({ok:true,json:async()=>copy(c.page)});
  await ui.tick();await ui.tick();await ui.tick();await ui.tick();
  assert.equal(reads(),during+1,"read once more right after the scan that started before the decision");
  assert.equal(cards(),0,"the decided card does not come back");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("hiding and showing the page does not push the 5 minute safety read further away",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]},start=Date.parse(when);
  const ui=await open(c.data,{now:start,realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)})});
  await connectPush(ui);
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/team/attention")).length;
  const team=()=>{const id=vm.runInContext("teamHitlTimer",ui.context);return id===null?null:ui.timerDelays.get(id)};
  assert.equal(team(),300000);
  ui.holder.now=start+240000;
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  ui.context.document.hidden=false;ui.documentEvents.visibilitychange();await ui.tick();
  assert.equal(team(),60000,"due 5 minutes after the last read, not 5 minutes after showing the page");
  ui.holder.now=start+360000;
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  const before=reads();
  ui.context.document.hidden=false;ui.documentEvents.visibilitychange();await ui.tick();await ui.tick();
  assert.equal(reads(),before+1,"overdue: read at once on showing the page");
});

test("a slow earlier negotiate never opens a second socket",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]},answers=[];
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)}),
    negotiateResponse:()=>new Promise(resolve=>answers.push(resolve))});
  for(let i=0;i<3;i++)await ui.tick();
  const cap=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===10000);
  ui.timers.delete(cap[0]);cap[1]();await ui.tick();
  const retry=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===5000);
  ui.timers.delete(retry[0]);retry[1]();await ui.tick();
  assert.equal(answers.length,2,"two negotiates in flight");
  const url={ok:true,json:async()=>({schema_version:"fa-realtime-negotiate-v1",
    url:"wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=offline",
    expires_at:"2026-01-01T01:00:00Z",team_id:"team-one"})};
  answers[1](url);await ui.tick();await ui.tick();
  answers[0](url);await ui.tick();await ui.tick();
  assert.equal(ui.holder.sockets.length,1,"only the current attempt connects");
});

test("the API saying push is off keeps polling and does not ask again",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)}),
    negotiateResponse:{ok:false,json:async()=>({error:{code:"REALTIME_DISABLED"}})}});
  for(let i=0;i<4;i++)await ui.tick();
  assert.equal(ui.holder.sockets.length,0);
  assert.equal([...ui.timerDelays.values()].some(delay=>[5000,15000,60000,300000].includes(delay)&&delay!==60000),false,
    "no reconnect timer");
  assert.equal(ui.timerDelays.get(vm.runInContext("teamHitlTimer",ui.context)),60000);
  ui.render(c.data);await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/realtime/negotiate").length,1);
});

test("a push hint reads once even while progress is paused, and a good answer resumes polling",async()=>{
  // Coordinator, stage v15 12:40: a hint is another way back from a page stuck on a read error.
  const data=nativeChat("started"),rid=data.tasks[0].task_id;
  const ui=await open(data,{realtime:true});
  const socket=await connectPush(ui);
  vm.runInContext("progressPaused=true",ui.context);
  const done=nativeChat("completed");done.tasks[0].reply={data:{text:"推送後讀到的回覆"},sha256:"a".repeat(64),byte_length:24,observed_at:when};
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  const before=ui.calls.length;
  pushEvent(socket,{type:"turn.finished",agent_id:"configured-agent",request_id:rid});
  for(let i=0;i<4;i++)await ui.tick();
  assert.deepEqual(ui.calls.slice(before).map(call=>call.url),[`/api/instructions/${rid}/status`]);
  assert.equal(vm.runInContext("progressPaused",ui.context),false);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/推送後讀到的回覆/);
});

// Coordinator, stage v15 12:40: the platform's get_session timed out, the API answered 503 NATIVE_BOOTSTRAP_UNKNOWN on a
// progress read, and the page stopped polling on the error though the turn had finished; later reads all worked.
for(const [label,failure] of [
  ["a 503 the API marks retryable",{ok:false,status:503,json:async()=>({error:{code:"NATIVE_BOOTSTRAP_UNKNOWN",automatic_retry:true}})}],
  ["an upstream 5xx the bridge reports as BACKEND_UNAVAILABLE",{ok:false,status:409,json:async()=>({error:{code:"BACKEND_UNAVAILABLE",upstream_status:500,automatic_retry:false}})}],
]){
  test(`a running turn's read that fails with ${label} is read again with backoff and the reply then shows`,async()=>{
    const data=nativeChat("started"),rid=data.tasks[0].task_id;
    const ui=await open(data);await ui.tick();
    ui.holder.progressResponse=failure;
    assert.deepEqual(await fireProgress(ui),[`/api/instructions/${rid}/status`]);
    assert.equal(vm.runInContext("progressPaused",ui.context),false,"polling goes on");
    assert.match(ui.text("error-message"),/暫時讀不到進度，正在重試/);
    const retry=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===2000);
    assert.ok(retry,"the next read waits 2 s (1→2→4…30)");
    const done=nativeChat("completed");done.tasks[0].reply={data:{text:"重試後的回覆"},sha256:"a".repeat(64),byte_length:18,observed_at:when};
    ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
    ui.timers.delete(retry[0]);await retry[1]();await ui.tick();await ui.tick();
    assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/重試後的回覆/);
    assert.equal(ui.nodes.get("error").hidden,true,"back to normal once a read succeeds");
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("a read conflict on the opening /status is not re-read in a tight loop",async()=>{
  // #686 review: a 409 RESULT_UNKNOWN/412 reset the backoff to 1 s and the opening read re-armed at 0 ms.
  const {data}=restartedConversation(2);
  const ui=await open(data,{progressResponse:()=>({ok:false,status:409,
    json:async()=>({error:{code:"RESULT_UNKNOWN",upstream_status:412}})})});
  for(let round=0;round<4;round++){
    const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
    if(!timer)break;
    ui.timers.delete(timer[0]);await timer[1]();await ui.tick();await ui.tick();
  }
  assert.equal(ui.calls.filter(call=>call.url==="/api/instructions/rid-1/status").length,1);
  assert.equal([...ui.timerDelays.entries()].some(([id,delay])=>ui.timers.has(id)&&delay===0),false,"no 0 ms re-read left");
});

test("a reply read that fails transiently is read again after the backoff",async()=>{
  const data=nativeChat("completed");data.tasks[0].reply=null;data.composer.observed_at=when;
  let answer={ok:false,status:503,json:async()=>({error:{code:"NATIVE_BOOTSTRAP_UNKNOWN",automatic_retry:true}})};
  const ui=await open(data,{replyResponse:()=>answer});
  for(let i=0;i<3;i++)await ui.tick();
  const reads=()=>ui.calls.filter(call=>call.url.endsWith("/reply")).length;
  assert.equal(reads(),1);
  assert.match(ui.text("error-message"),/暫時讀不到進度，正在重試/);
  const done=copy(data);done.tasks[0].reply={data:{text:"重讀後的回覆"},sha256:"a".repeat(64),byte_length:18,observed_at:when};
  answer={ok:true,json:async()=>done};
  const retry=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===2000);
  assert.ok(retry,"read again in 2 s");
  ui.timers.delete(retry[0]);retry[1]();for(let i=0;i<3;i++)await ui.tick();
  assert.equal(reads(),2);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/重讀後的回覆/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const [label,failure] of [
  ["a 403",{ok:false,status:403,json:async()=>({error:{code:"BACKEND_DENIED",automatic_retry:false}})}],
  ["a 503 the API marks not retryable (Responses disabled)",{ok:false,status:503,json:async()=>({error:{code:"NATIVE_TURNS_DISABLED",automatic_retry:false}})}],
  ["a 502 the bridge raises itself",{ok:false,status:502,json:async()=>({error:{code:"NATIVE_STATE_INVALID",automatic_retry:false}})}],
]){
  test(`a read error that is not retryable still pauses polling: ${label}`,async()=>{
    const data=nativeChat("started");
    const ui=await open(data);await ui.tick();
    ui.holder.progressResponse=failure;
    await fireProgress(ui);
    assert.equal(vm.runInContext("progressPaused",ui.context),true);
  });
}

test("opening a conversation whose one /status read fails transiently reads it again after the backoff",async()=>{
  const {data}=restartedConversation(2);
  let answer={ok:false,status:503,json:async()=>({error:{code:"NATIVE_BOOTSTRAP_UNKNOWN",automatic_retry:true}})};
  const ui=await open(data,{progressResponse:()=>answer});
  const fire=delay=>{const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===delay);
    assert.ok(timer,"timer "+delay);ui.timers.delete(timer[0]);return timer[1]()};
  await fire(0);await ui.tick();await ui.tick();
  const ready=copy(data);ready.composer.observed_at=when;
  ready.tasks[1].reply={data:{text:"開啟時重試讀到的回覆"},sha256:"a".repeat(64),byte_length:30,observed_at:when};
  answer={ok:true,json:async()=>ready};
  await fire(2000);await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/instructions/rid-1/status").length,2);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/開啟時重試讀到的回覆/);
});

test("a one-Agent console ignores the Team's other Agents' hints",async()=>{
  const data=nativeChat("completed");
  const ui=await open(data,{realtime:true});
  const socket=await connectPush(ui);
  const before=ui.calls.length;
  pushEvent(socket,{type:"agent.received",agent_id:"some-other-agent",conversation_id:"x-cid",request_id:"other-rid",
    sender_agent_id:"configured-agent",status:"accepted"});
  pushEvent(socket,{type:"turn.finished",agent_id:"some-other-agent",request_id:"other-turn"});
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.length,before);
});

// The PR's request-count table: the same session with and without push, in virtual time, GET requests only.
async function countedSession(push,turnMs,endMs){
  const data=nativeChat("started"),scope=agentChat("agent-one");
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])data[key]=scope[key];
  data.composer.native_hitl={enabled:true,can_approve:true,team_attention_available:true,references:[],commands:[],error:null};
  data.composer.attention=[];
  const rid=data.tasks[0].task_id,done=copy(data);
  done.tasks[0].native_receipt.status="completed";done.tasks[0].status.data.state="completed";
  done.tasks[0].reply={data:{text:"完成"},sha256:"a".repeat(64),byte_length:6,observed_at:when};
  done.composer.conversation.turns=[{request_id:rid,state:"completed"}];
  done.composer.can_follow_up=done.composer.conversation.can_follow_up=true;
  const empty={schema_version:"fa-native-hitl-team-attention-v1",team_id:"team-one",items:[],next_cursor:null,observed_at:when};
  let finished=false;
  const ui=await open(data,{realtime:push,progressResponse:{ok:true,json:async()=>copy(finished?done:data)},
    hitl:async()=>({ok:true,json:async()=>copy(empty)})});
  let now=0,socket=null;const due=new Map();
  for(let i=0;i<6;i++)await ui.tick();
  if(push){socket=ui.holder.sockets.at(-1);socket.onopen();await ui.tick();await ui.tick()}
  const events=[[turnMs,()=>{finished=true;if(socket)pushEvent(socket,{type:"turn.finished",agent_id:"agent-one",request_id:rid})}],
    [turnMs+10000,()=>{if(socket)pushEvent(socket,{type:"hitl.approval",agent_id:"agent-two",conversation_id:"b-cid",
      request_id:"b-rid",pending_id:"p",status:"pending"})}]];
  while(true){
    for(const [id,delay] of ui.timerDelays)if(!due.has(id))due.set(id,now+delay);
    for(const id of [...due.keys()])if(!ui.timers.has(id))due.delete(id);
    const timer=[...due.entries()].sort((a,b)=>a[1]-b[1])[0],event=events[0];
    const at=Math.min(timer?timer[1]:Infinity,event?event[0]:Infinity);
    if(at>endMs)break;
    now=at;
    if(event&&event[0]<=(timer?timer[1]:Infinity))events.shift()[1]();
    else{const fn=ui.timers.get(timer[0]);ui.timers.delete(timer[0]);due.delete(timer[0]);fn()}
    for(let k=0;k<4;k++)await ui.tick();
  }
  const urls=ui.calls.filter(call=>!call.method||call.method==="GET").map(call=>call.url);
  return {total:urls.length,team:urls.filter(url=>url.startsWith("/api/native-hitl/team/attention")).length,
    status:urls.filter(url=>url.endsWith("/status")).length};
}
test("push lowers the request count of the same session (the PR's table)",async()=>{
  const short=[await countedSession(false,20000,120000),await countedSession(true,20000,120000)];
  const long=[await countedSession(false,60000,600000),await countedSession(true,60000,600000)];
  assert.deepEqual(short,[{total:25,team:3,status:20},{total:12,team:2,status:7}]);
  assert.deepEqual(long,[{total:73,team:11,status:60},{total:26,team:3,status:20}]);
});

test("push about another Agent only marks its sidebar; a conversation opened here is imported once",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};
  const ui=await open(c.data,{now:Date.parse(when),realtime:true,hitl:async(url)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?empty:c.view)})});
  const socket=await connectPush(ui);
  const before=ui.calls.length;
  pushEvent(socket,{type:"agent.replied",agent_id:"agent-two",conversation_id:"b-cid",request_id:"b-new",
    sender_agent_id:"agent-one",source_request_id:"a-rid"});
  pushEvent(socket,{type:"turn.finished",agent_id:"agent-two",request_id:"b-rid"});
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.length,before,"no read for another Agent");
  assert.equal(vm.runInContext("agentUnread.has('agent-two')",ui.context),true);
  pushEvent(socket,{type:"agent.received",agent_id:"agent-one",conversation_id:"a-new-cid",request_id:"recv-1",
    sender_agent_id:"agent-two",status:"accepted"});
  await ui.tick();await ui.tick();
  const imported=ui.calls.slice(before).map(call=>call.url);
  assert.deepEqual(imported,[`/api/instructions/received?request_id=recv-1&thread_id=${encodeURIComponent(c.data.composer.thread_id)}`]);
  assert.equal(vm.runInContext("selectedThread",ui.context),c.data.composer.thread_id,"the open conversation stays");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an already verified Team card is usable while later discovery pages are still loading",async()=>{
  const c=teamAttentionCase();let finish;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    if(url.includes("?cursor="))return new Promise(resolve=>{finish=resolve});
    return {ok:true,json:async()=>copy(url.includes("/attention")?{...c.page,next_cursor:"next-page"}:c.view)};
  }});
  await ui.tick();await ui.tick();
  assert.match(ui.text("team-attention-status"),/正在讀取全 Team/);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,false);
  finish({ok:true,json:async()=>({...copy(c.page),items:[]})});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("team-attention-status").hidden,true);
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Stage 14, 2026-10-05 (Neal's recording): an empty background Team read showed a loading "需要處理" block
// for a moment every five seconds and pushed the conversation down.
test("a background Team read never shows a loading block; an error shows once and stays until a read succeeds",async()=>{
  const c=teamAttentionCase(),empty={...copy(c.page),items:[]};let finish=null,fail=false;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    if(!url.includes("/attention"))return {ok:true,json:async()=>copy(c.view)};
    if(fail)return {ok:false,status:503,json:async()=>({error:{code:"BACKEND_UNAVAILABLE"}})};
    if(finish===null)return {ok:true,json:async()=>copy(empty)};
    return new Promise(resolve=>{finish=resolve});
  }});
  const tick=async()=>{
    const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===60000);
    ui.timers.delete(timer[0]);const run=timer[1]();await ui.tick();return run;
  };
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
  finish=undefined;await tick();
  assert.equal(typeof finish,"function","the background read is in flight");
  ui.render(c.data);
  assert.equal(ui.nodes.get("attention-panel").hidden,true,"no loading block while a background read runs");
  assert.equal(ui.nodes.get("team-attention-status").hidden,true);
  finish({ok:true,json:async()=>copy(empty)});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
  fail=true;await (await tick());await ui.tick();
  assert.equal(ui.nodes.get("attention-panel").hidden,false,"a failed read is shown, not silently dropped");
  assert.match(ui.text("team-attention-status"),/尚未讀完/);
  fail=false;finish=undefined;await tick();ui.render(c.data);
  assert.equal(ui.nodes.get("attention-panel").hidden,false,"the retry does not hide and re-show the error");
  finish({ok:true,json:async()=>copy(empty)});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
  assert.equal(ui.nodes.get("hitl-status").hidden,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("switching conversations keeps the Team-wide list and does not rescan it",async()=>{
  const c=teamAttentionCase(),reads=[];
  const other=copy(c.data);
  Object.assign(other.composer,{thread_id:"second-thread",request_id:"second-thread"});
  other.tasks=other.tasks.map(task=>({...task,thread_id:"second-thread"}));
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    if(url.includes("/attention"))reads.push(url);
    return {ok:true,json:async()=>copy(url.includes("/attention")?c.page:c.view)};
  }});
  await ui.tick();await ui.tick();
  assert.equal(reads.length,1);
  const scope=vm.runInContext("hitlObservationScope",ui.context);
  ui.holder.current=copy(other);
  await ui.context.switchThread("second-thread");await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("selectedThread",ui.context),"second-thread");
  assert.notEqual(vm.runInContext("hitlObservationScope",ui.context),scope,"the conversation's own HITL view was reset");
  assert.equal(reads.length,1,"a conversation switch reuses the Team-wide list");
  assert.equal(vm.runInContext("teamHitlView.complete",ui.context),true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("while a later Team scan is still reading, other conversations' cached cards do not flash in",async()=>{
  const c=teamAttentionCase("agent-one"),empty={...copy(c.page),items:[]};let finish=null;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    if(!url.includes("/attention"))return {ok:true,json:async()=>copy(c.view)};
    if(finish===null)return {ok:true,json:async()=>copy(empty)};
    if(url.includes("?cursor="))return new Promise(resolve=>{finish=resolve});
    return {ok:true,json:async()=>({...copy(empty),next_cursor:"next-page"})};
  }});
  await ui.tick();await ui.tick();
  const cached=copy(c.data);cached.composer.attention=[{...copy(c.entry),thread_id:"another-thread"}];
  ui.render(cached);
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,0);
  finish=undefined;
  const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===60000);
  ui.timers.delete(timer[0]);timer[1]();await ui.tick();await ui.tick();
  assert.equal(typeof finish,"function","the second page is still being read");
  ui.render(cached);
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,0);
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
  finish({ok:true,json:async()=>copy(empty)});await ui.tick();await ui.tick();
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an owner-requested Team refresh shows its own loading line until it finishes",async()=>{
  const c=teamAttentionCase();let finish=null;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    if(!url.includes("/attention"))return {ok:true,json:async()=>copy(c.view)};
    if(finish===null)return {ok:true,json:async()=>copy(c.page)};
    return new Promise(resolve=>{finish=resolve});
  }});
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("attention-panel").hidden,false);
  assert.equal(ui.nodes.get("team-attention-status").hidden,true);
  finish=undefined;
  ui.click("read-team-attention");await ui.tick();
  assert.equal(ui.nodes.get("team-attention-status").hidden,false);
  assert.match(ui.text("team-attention-status"),/正在讀取全 Team/);
  finish({ok:true,json:async()=>copy(c.page)});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("team-attention-status").hidden,true);
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("the open conversation's new card shows from its own progress without waiting for the slow Team read",async()=>{
  const c=teamAttentionCase("agent-one"),empty={...copy(c.page),items:[]};
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>
    ({ok:true,json:async()=>copy(url.includes("/attention")?empty:c.view)})});
  await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("teamHitlView.complete",ui.context),true);
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,0);
  const next=copy(c.data);next.composer.attention=[{...copy(c.entry),thread_id:c.data.composer.thread_id}];
  ui.render(next);await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,1);
  const other=copy(c.data);other.composer.attention=[{...copy(c.entry),thread_id:"another-thread"}];
  ui.render(other);
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,0,
    "another conversation's cached card still waits for the complete Team list");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a late Team discovery response cannot replace the next Agent's view or draft",async()=>{
  const c=teamAttentionCase(),other=agentChat("agent-two");let finish;
  other.tasks=[];
  const ui=await open(c.data,{now:Date.parse(when),agentStates:{"agent-two":other},
    hitl:()=>new Promise(resolve=>{finish=resolve})});
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  typeAttachmentText(ui,"B 的原草稿");
  finish({ok:true,json:async()=>copy(c.page)});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"B 的原草稿");
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,0);
  assert.equal(vm.runInContext("selectedAgent",ui.context),"agent-two");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a late B decision result never rewrites a different Agent's current draft",async()=>{
  const c=teamAttentionCase(),other=agentChat("agent-two"),posts=[];let finish;
  other.tasks=[];
  const ui=await open(c.data,{now:Date.parse(when),agentStates:{"agent-two":other},hitl:async(url,options)=>{
    if(options.method==="POST"){
      posts.push({url,headers:options.headers,body:JSON.parse(options.body)});
      return new Promise(resolve=>{finish=resolve});
    }
    return {ok:true,json:async()=>copy(url.includes("/attention")?c.page:c.view)};
  }});
  await ui.tick();await ui.tick();
  hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();await ui.tick();
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  typeAttachmentText(ui,"B 此刻尚未送出的草稿");
  finish({ok:true,json:async()=>hitlCommandReply(posts[0].body)});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"B 此刻尚未送出的草稿");
  assert.equal(posts.length,1);
  assert.equal(posts[0].headers["X-Console-Agent"],"agent-two");
  assert.equal(posts[0].body.approval.pending_id,c.reference.pending_id);
  assert.equal(vm.runInContext("selectedAgent",ui.context),"agent-two");
});

for(const following of ["C-read","A-rules","C-same-id"]){
  test(`B's UNKNOWN reconciliation survives a later ${following} and the pending card disappearing`,async()=>{
    const c=teamAttentionCase(),posts=[],rid="d".repeat(64);
    const command={approval:c.reference,choice:"allow_once",rule:null,expected_rules_revision:1,expected_rules_etag:"rules-v1"};
    const retained=hitlCommandReply(command,{state:"UNKNOWN",request_id:rid});
    retained.commands[0].thread_id=null;
    c.view.commands=copy(retained.commands);
    c.view.readback.status="allow";
    let finishOther;
    const other=copy(c.view),ref=copy(c.reference),item=copy(c.entry.item);
    ref.pending_id="c".repeat(64);ref.call.agent_id="agent-three";ref.call.request_id="c-rid";
    item.item_id=ref.pending_id;item.approval=ref;
    other.readback.approval=ref;other.readback.pending.pending_id=ref.pending_id;
    other.readback.pending.command.action.call=ref.call;
    other.commands=following==="C-same-id"?hitlCommandReply({...command,approval:ref},{state:"UNKNOWN",request_id:rid}).commands:[];
    if(following!=="A-rules"){
      c.data.agents.push({id:"agent-three",name:"第三位助理",enabled:true,reason:null,api_prefix:"/agents/agent-three"});
      c.page.items.push({item,origin:copy(c.page.items[0].origin)});
    }
    const ui=await open(c.data,{now:Date.parse(when),hitl:async(url,options)=>{
      if(options.method==="POST"){
        posts.push({url,body:JSON.parse(options.body),headers:options.headers});
        return {ok:true,json:async()=>copy(retained)};
      }
      if(url.includes("/attention"))return {ok:true,json:async()=>copy(c.page)};
      if(url.endsWith("/rules"))return {ok:true,json:async()=>({rules:copy(c.view.rules),commands:[],can_approve:true,read_at:when})};
      if(url.endsWith("/pending/"+ref.pending_id))return new Promise(resolve=>{finishOther=resolve});
      return {ok:true,json:async()=>copy(c.view)};
    }});
    await ui.tick();await ui.tick();
    assert.equal(hitlNodes(ui,"data-hitl-reconcile",rid).length,1);
    if(following==="A-rules")ui.click("read-hitl-rules");
    else finishOther({ok:true,json:async()=>copy(other)});
    await ui.tick();await ui.tick();
    c.page.items=[];
    await vm.runInContext("readTeamAttention()",ui.context);await ui.tick();
    assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,0);
    const rows=ui.nodes.get("hitl-command-list").children;
    const row=rows.find(entry=>entry.textContent.includes("第二位助理"));
    assert.ok(row,"B's original UNKNOWN observation must remain available after another console returns");
    const button=row.children.find(child=>child.attributes["data-hitl-reconcile"]===rid);
    assert.ok(button);
    button.events.click();await ui.tick();await ui.tick();
    assert.equal(posts.length,1);
    assert.match(posts[0].url,/\/team\/reconcile$/);
    assert.equal(posts[0].headers["X-Console-Agent"],"agent-two");
    assert.deepEqual(posts[0].body,{request_id:rid});
  });
}

for(const kind of ["unavailable","wrong-root"]){
  test(`Team ${kind} origin stays in the overview and cannot be attached to A's unrelated turn`,async()=>{
    const c=teamAttentionCase();
    if(kind==="unavailable")c.page.items[0].origin={state:"unavailable",root:null,sender:null};
    else c.page.items[0].origin.root.conversation_id="not-a-cid";
    const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>
      ({ok:true,json:async()=>copy(url.includes("/attention")?c.page:c.view)})});
    await ui.tick();await ui.tick();
    assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,1);
    assert.doesNotMatch(JSON.stringify(ui.nodes.get("conversation-history").children),new RegExp(c.reference.pending_id));
    assert.match(JSON.stringify(ui.nodes.get("attention-items").children),/第二位助理/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

for(const bad of ["agent-unavailable","wrong-call","expired","read-only"]){
  test(`B's Team approval ${bad} never gains a decision through A`,async()=>{
    const c=teamAttentionCase();
    c.view=copy(c.view);
    if(bad==="agent-unavailable")c.data.agents[1].enabled=false;
    if(bad==="wrong-call")c.view.readback.approval.call.agent_id="agent-one";
    if(bad==="expired")c.view.readback.expired=true;
    if(bad==="read-only")c.view.can_approve=false;
    const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>
      ({ok:true,json:async()=>copy(url.includes("/attention")?c.page:c.view)})});
    await ui.tick();await ui.tick();
    const buttons=hitlNodes(ui,"data-hitl-choice","allow_once");
    assert.ok(buttons.length===0||buttons.every(button=>button.disabled));
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
    if(bad==="agent-unavailable")assert.equal(ui.calls.some(call=>call.url.includes("/team/pending/")),false);
  });
}

for(const choice of ["deny","allow_rule"]){
  test(`B's Team ${choice} goes out with one click, once, and does not change A's draft`,async()=>{
    const c=teamAttentionCase(),posts=[];
    const ui=await open(c.data,{now:Date.parse(when),hitl:async(url,options)=>{
      if(options.method==="POST"){
        const body=JSON.parse(options.body);posts.push({body,headers:options.headers});
        return {ok:true,json:async()=>hitlCommandReply(body)};
      }
      return {ok:true,json:async()=>copy(url.includes("/attention")?c.page:c.view)};
    }});
    await ui.tick();await ui.tick();
    typeAttachmentText(ui,"A尚未送出的草稿");
    if(choice==="allow_rule"){
      hitlNodes(ui,"data-hitl-choice","allow_rule")[0].events.click();await ui.tick();
      const input=hitlNodes(ui,"data-hitl-rule",c.reference.pending_id)[0];
      input.value="只允許B在原工作目錄讀CPU資訊";input.events.input();
    }
    const button=hitlNodes(ui,"data-hitl-choice",choice)[0];
    button.events.click();button.events.click();
    await ui.tick();await ui.tick();
    assert.equal(ui.nodes.get("page-confirm").hidden,true);
    assert.equal(posts.length,1);
    assert.equal(posts[0].headers["X-Console-Agent"],"agent-two");
    assert.equal(posts[0].body.approval.pending_id,c.reference.pending_id);
    assert.equal(posts[0].body.expected_rules_revision,1);
    assert.equal(posts[0].body.choice,choice);
    assert.equal(ui.nodes.get("instruction").value,"A尚未送出的草稿");
    assertNothingSent(ui);
  });
}

test("a pending approval automatically reads the original operation and presents a plain shell summary",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
  view.readback.pending.command.action.target="/home/agent/.fa-assistant-v12/desktop/files";
  view.readback.pending.command.action.arguments.command="echo hitl-ok-offline";
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/pending/"+pid)).length,1);
  const summary=hitlNodes(ui,"data-hitl-summary",pid)[0];
  assert.match(summary?.textContent||"",/echo hitl-ok-offline/);
  assert.match(summary.textContent,/shell|終端|指令/);
  assert.match(summary.textContent,/desktop\/files/);
  const detail=hitlNodes(ui,"data-hitl-details",pid)[0];
  assert.ok(detail);
  assert.notEqual(detail.open,true);
  assert.ok(hitlNodes(ui,"data-hitl-material",pid)[0].textContent.includes("x".repeat(70*1024)));
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const reason of ["ask_rule","review_uncertain","human_request_unavailable","review_unavailable","review_invalid"]){
  test(`approval reason ${reason} is plain language and technical state stays in details`,async()=>{
    const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
    const rule="任何助理要用 shell 執行命令之前，先問我";
    view.rules.revision=1;view.rules.etag="rules-1";
    view.rules.rules=[{rule_id:"ask-shell",text:rule,behavior:"ask_first"}];
    view.readback.pending.command.review={rules_revision:1,reason,source:"model",outcome:"ask",
      matched_rule_ids:reason==="ask_rule"?["ask-shell"]:[]};
    const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
    await ui.tick();
    const card=hitlNodes(ui,"data-hitl-card",pid)[0],detail=hitlNodes(ui,"data-hitl-details",pid)[0];
    const why=hitlNodes(ui,"data-hitl-reason",pid)[0].textContent;
    if(reason==="ask_rule")assert.equal(why,`Team 規則「${rule}」要求先問你。`);
    else if(reason==="review_uncertain")assert.equal(why,"系統無法確定這個動作是否在你原本的要求範圍內，所以先問你。");
    else assert.doesNotMatch(why,/核清|原審查|真人要求的範圍/);
    assert.equal(card.children[1].textContent,"等你決定（08:10 前有效）");
    assert.doesNotMatch(card.children.filter(child=>child!==detail).map(child=>child.textContent).join(" "),/不是效果已完成|重新讀取原操作/);
    assert.ok(detail.children.includes(hitlNodes(ui,"data-hitl-read",pid)[0]));
    assert.match(JSON.stringify(detail),/不是效果已完成/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("allow this kind first opens an editable scoped suggestion and only 確認規則並允許 writes it",async()=>{
  const data=hitlAgent("agent-one"),view=hitlRead(data),pid=view.readback.approval.pending_id,posts=[];
  view.readback.pending.command.action.arguments={command:"free -h"};
  view.readback.pending.command.action.target="/home/agent/desktop/files";
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    if(options.method==="POST"){
      posts.push({url,body:JSON.parse(options.body)});
      return {ok:true,json:async()=>hitlCommandReply(posts.at(-1).body)};
    }
    return {ok:true,json:async()=>copy(view)};
  }});
  await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-rule-editor",pid)[0]?.hidden,true);
  const select=hitlNodes(ui,"data-hitl-choice","allow_rule")[0];
  assert.equal(select.disabled,false);
  select.events.click();await ui.tick();
  assert.equal(posts.length,0);
  assert.equal(hitlNodes(ui,"data-hitl-rule-editor",pid)[0].hidden,false);
  const input=hitlNodes(ui,"data-hitl-rule",pid)[0];
  assert.match(input.value,/第一位助理/);assert.match(input.value,/free -h/);assert.match(input.value,/desktop\/files/);
  input.value="只允許第一位助理查詢這個 VM 的記憶體資訊";input.events.input();
  ui.render(data);await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-rule",pid)[0].value,input.value);
  const confirm=hitlNodes(ui,"data-hitl-choice","allow_rule")[0];
  assert.equal(confirm.textContent,"確認規則並允許");
  confirm.events.click();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"the rule editor's own button is the confirmation; no dialog on top");
  assert.equal(posts.length,1);
  assert.equal(posts[0].body.choice,"allow_rule");
  assert.equal(posts[0].body.rule.text,"只允許第一位助理查詢這個 VM 的記憶體資訊");
  assert.deepEqual(posts[0].body.approval,view.readback.approval);
});

test("a toolbox tool set to always shows its name and exact arguments and is approved once, never as a rule",async()=>{
  const data=hitlAgent("agent-one"),view=hitlRead(data),pid=view.readback.approval.pending_id,posts=[];
  Object.assign(view.readback.pending.command.action,{category:"mcp",tool:"team-Docs-search",
    target:"team-Docs/team-Docs-search",arguments:{query:"quarterly numbers",limit:3}});
  view.readback.pending.command.review={rules_revision:0,source:"toolbox",outcome:"ask",reason:"toolbox_always",
    matched_rule_ids:[]};
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    if(options.method==="POST"){
      posts.push(JSON.parse(options.body));
      return {ok:true,json:async()=>hitlCommandReply(posts.at(-1))};
    }
    return {ok:true,json:async()=>copy(view)};
  }});
  await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-summary",pid)[0].textContent,"要使用 Toolbox 工具：team-Docs-search");
  assert.deepEqual(JSON.parse(hitlNodes(ui,"data-hitl-arguments",pid)[0].textContent),{query:"quarterly numbers",limit:3});
  assert.match(hitlNodes(ui,"data-hitl-reason",pid)[0].textContent,/每次呼叫都要你批准/);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_rule").length,0,"an always tool never becomes a Team rule");
  hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"the card already shows the tool and its exact arguments");
  assert.equal(posts.length,1);assert.equal(posts[0].choice,"allow_once");assert.equal(posts[0].rule,null);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",pid)[0].children.map(node=>node.textContent),
    ["助理要使用 Toolbox 工具：team-Docs-search","你允許了這次"]);
});

test("a Teams send card shows its target path as an ID and the whole message before approval",async()=>{
  const data=hitlAgent("agent-one"),view=hitlRead(data),pid=view.readback.approval.pending_id;
  const message="Line ".repeat(3000)+"end.",parentUrl="https://graph.example.test/v1.0/chats/19:fake-chat/messages";
  Object.assign(view.readback.pending.command.action,{category:"mcp",tool:"connector-teams_write-create_entity",
    target:"connector-teams_write/connector-teams_write-create_entity",
    arguments:{agentId:"agent-example",parentUrl,jsonBody:JSON.stringify({body:{contentType:"text",content:message}})}});
  view.readback.pending.command.review={rules_revision:0,source:"toolbox",outcome:"ask",reason:"toolbox_always",
    matched_rule_ids:[]};
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-target",pid)[0].textContent,`目標（路徑／ID，未換成名稱）：${parentUrl}`);
  const body=hitlNodes(ui,"data-hitl-body",pid)[0].textContent;
  assert.equal(body,"完整內容：\n"+JSON.stringify({body:{contentType:"text",content:message}},null,2));
  assert.equal(body.includes(message),true,"the message is not cut");
  view.readback.status="effect_unknown";
  hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-card",pid)[0].children[1].textContent,
    "結果不確定：這個動作可能已經生效，也可能沒有；系統沒有重送。請先核對實際結果，再決定要不要重做。");
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an older review never quotes a newer Team rule as its original reason",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
  view.rules.revision=2;view.rules.etag="rules-2";
  view.rules.rules=[{rule_id:"changed-rule",text:"這是新規則，不是原先比對的內容",behavior:"ask_first"}];
  view.readback.pending.command.review={rules_revision:1,reason:"ask_rule",source:"model",outcome:"ask",
    matched_rule_ids:["changed-rule"]};
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  const why=hitlNodes(ui,"data-hitl-reason",pid)[0].textContent;
  assert.match(why,/規則剛有變動/);assert.doesNotMatch(why,/這是新規則/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const source of ["too-long","user-cleared"]){
  test(`a ${source} rule suggestion never truncates a command or overwrites an empty human draft`,async()=>{
    const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
    view.readback.pending.command.action.arguments={command:source==="too-long"?"echo "+"x".repeat(600):"free -h"};
    const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
    hitlNodes(ui,"data-hitl-choice","allow_rule")[0].events.click();await ui.tick();
    const input=hitlNodes(ui,"data-hitl-rule",pid)[0];
    if(source==="user-cleared"){assert.ok(input.value);input.value="";input.events.input()}
    assert.equal(input.value,"");
    ui.render(data);await ui.tick();
    assert.equal(hitlNodes(ui,"data-hitl-rule",pid)[0].value,"");
    assert.equal(hitlNodes(ui,"data-hitl-choice","allow_rule")[0].disabled,true);
    assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,false);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

for(const required of [false,true]){
  test(`human takeover notice is ${required?"prominent for the original takeover reason":"kept in details for an ordinary shell action"}`,async()=>{
    const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
    if(required){
      Object.assign(view.readback.pending.command.review,{reason:"human_takeover",takeover_required:true,outcome:"deny"});
      view.readback.status="deny";
    }
    const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
    const card=hitlNodes(ui,"data-hitl-card",pid)[0],detail=hitlNodes(ui,"data-hitl-details",pid)[0];
    const notice=hitlNodes(ui,"data-hitl-takeover",pid)[0];
    assert.ok(notice);
    assert.equal(card.children.includes(notice),required);
    assert.equal(detail.children.includes(notice),!required);
  });
}

test("approval operation details stay open across progress repaints without rereading or deciding",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  await ui.tick();await ui.tick();
  const details=hitlNodes(ui,"data-hitl-details",pid)[0];
  details.open=true;details.events.toggle?.();
  ui.render(data);await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-details",pid)[0].open,true);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/pending/"+pid)).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("new approval rule waits for the rules read before accepting the first edit",async()=>{
  const data=hitlChat(),rules=hitlRead(data).rules;
  let finish,slow=false;
  const ui=await open(data,{now:Date.parse(when),hitl:async(url)=>{
    if(!url.endsWith("/rules"))return {ok:true,json:async()=>copy(hitlRead(data))};
    if(slow)return new Promise(resolve=>{finish=resolve});
    return {ok:true,json:async()=>({rules:copy(rules),commands:[],can_approve:true,read_at:when})};
  }});
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  slow=true;ui.click("read-hitl-rules");await ui.tick();
  assert.equal(ui.nodes.get("new-hitl-rule").disabled,true);
  assert.equal(ui.nodes.get("hitl-rule-text").disabled,true);
  finish({ok:true,json:async()=>({rules:copy(rules),commands:[],can_approve:true,read_at:when})});
  await ui.tick();await ui.tick();
  ui.click("new-hitl-rule");
  const input=ui.nodes.get("hitl-rule-text");input.value="Ask before changing this workspace.";input.events.input();
  assert.equal(input.value,"Ask before changing this workspace.");
  assert.equal(ui.nodes.get("save-hitl-rule").disabled,false);
  ui.click("save-hitl-rule");
  assert.match(await ui.answer(false),/^確認新增這條 Team 規則？\nAsk before changing this workspace.\n處理：先問我\n/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false,"取消 sends nothing");
  // 確定 sends the confirmed rule exactly once, even when pressed twice.
  ui.click("save-hitl-rule");await ui.tick();
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await ui.tick();await ui.tick();
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.equal(posts.length,1);
  assert.match(posts[0].url,/^\/api\/native-hitl\/[^/]+\/rules$/);
  assert.deepEqual(JSON.parse(posts[0].body),{operation:"add",rule_id:null,
    rule:{text:"Ask before changing this workspace.",behavior:"ask_first"},
    expected_rules_revision:rules.revision,expected_rules_etag:rules.etag});
});

test("native HITL joins the same attention panel and displays complete original data before three choices",async()=>{
  const data=hitlChat(),view=hitlRead(data);
  data.composer.attention.push(questionItem("question-thread"));
  let finish;
  const ui=await open(data,{now:Date.parse(when),hitl:()=>new Promise(resolve=>{finish=resolve})});
  assert.equal(ui.nodes.get("attention-panel").hidden,false);
  assert.equal(ui.nodes.get("attention-items").children.length,2);
  const pid=view.readback.approval.pending_id;
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once").length,0);
  finish({ok:true,json:async()=>copy(view)});await ui.tick();await ui.tick();
  const material=hitlNodes(ui,"data-hitl-material",pid)[0];
  assert.match(material.textContent,/<script>do not execute this HTML<\/script>/);
  assert.ok(material.textContent.includes("x".repeat(70*1024)));
  assert.equal(material.children.length,0);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,false);
  assert.equal(hitlNodes(ui,"data-hitl-choice","deny")[0].disabled,false);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_rule")[0].disabled,false);
  assert.equal(hitlNodes(ui,"data-hitl-rule-editor",pid)[0].hidden,true);
  assert.ok(ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/")).every(call=>call.method!=="POST"));
});

test("native HITL rule typing keeps the editor in place and never approves on input",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
  hitlNodes(ui,"data-hitl-choice","allow_rule")[0].events.click();await ui.tick();
  const input=hitlNodes(ui,"data-hitl-rule",pid)[0];
  input.value="Only allow echo in this exact workspace.";input.events.input();
  assert.equal(hitlNodes(ui,"data-hitl-rule",pid)[0],input);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_rule")[0].disabled,false);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

function hitlCommandReply(body,{state="CONFIRMED",request_id="d".repeat(64)}={}){
  const {approval,...fields}=body,kind=approval?"decision":"rules_change";
  const command={kind,request_id,...fields,...(approval?{pending_id:approval.pending_id,pending_digest:approval.pending_digest}:{})};
  return {request_id,original_request_resent:false,purpose:"readback-only-not-execution-permission",
    commands:[{request_id,kind,state,thread_id:"attachment-thread",command,selection:approval??null,receipt:null,observation:null}]};
}
for(const choice of ["allow_once","allow_rule","deny"]){
  test(`native HITL explicit ${choice} sends once and the card becomes a record that never claims the effect`,async()=>{
    const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id,posts=[];
    const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
      if(options.method==="POST"){
        const body=JSON.parse(options.body);posts.push({url,body});
        const reply=hitlCommandReply(body);
        view.readback.status=choice==="deny"?"deny":"allow";view.readback.decision=reply.commands[0].command;
        return {ok:true,json:async()=>reply};
      }
      return {ok:true,json:async()=>copy(view)};
    }});
    hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
    if(choice==="allow_rule"){
      hitlNodes(ui,"data-hitl-choice","allow_rule")[0].events.click();await ui.tick();
      const input=hitlNodes(ui,"data-hitl-rule",pid)[0];input.value="Only echo in the reviewed directory.";input.events.input();
    }
    const button=hitlNodes(ui,"data-hitl-choice",choice)[0];button.events.click();button.events.click();
    const asked="助理要執行：echo <script>do not execute this HTML</script>";
    const decided={allow_once:"你允許了這次",allow_rule:"你允許了，並新增規則「Only echo in the reviewed directory.」",
      deny:"你拒絕了"}[choice];
    assert.deepEqual(hitlNodes(ui,"data-hitl-record",pid)[0].children.map(node=>node.textContent),[asked,decided,"送出中…"]);
    await ui.tick();await ui.tick();await ui.tick();
    assert.equal(posts.length,1);assert.ok(posts[0].url.endsWith("/decisions"));
    assert.equal(posts[0].body.choice,choice);
    assert.deepEqual(posts[0].body.approval,view.readback.approval);
    assert.equal(posts[0].body.expected_rules_revision,0);
    assert.equal(posts[0].body.rule?.text,choice==="allow_rule"?"Only echo in the reviewed directory.":undefined);
    const record=hitlNodes(ui,"data-hitl-record",pid)[0];
    assert.deepEqual(record.children.map(node=>node.textContent),[asked,decided]);
    assert.doesNotMatch(JSON.stringify(record),/已完成|已送達|已執行/);
    assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once").length,0);
  });
}
test("native HITL full policy or expired call cannot silently become another choice",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
  view.rules.revision=20;view.rules.etag="version-20";
  view.rules.rules=Array.from({length:20},(_,n)=>({rule_id:"rule-"+n,text:"Scope "+n,behavior:"ask_first"}));
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-rule-editor",pid)[0].hidden,true);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_rule")[0].disabled,true);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,false);
  hitlNodes(ui,"data-hitl-choice","allow_rule")[0].events.click();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  ui.holder.now=Date.parse("2026-01-01T00:10:01Z");ui.render(data);
  for(const choice of ["allow_once","allow_rule","deny"])assert.equal(hitlNodes(ui,"data-hitl-choice",choice)[0].disabled,true);
});
test("native HITL rule CRUD shows exact text and uses the observed revision",async()=>{
  const data=hitlChat(),rules=hitlRead(data).rules,commands=[],posts=[];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    if(options.method==="POST"){
      const body=JSON.parse(options.body);posts.push(body);
      assert.equal(body.expected_rules_revision,rules.revision);
      assert.equal(body.expected_rules_etag,rules.etag);
      const reply=hitlCommandReply(body,{request_id:String(posts.length).repeat(64)});
      commands.push(reply.commands[0]);
      if(body.operation==="add")rules.rules.push({rule_id:"original-rule",...body.rule});
      else if(body.operation==="update")rules.rules[0]={rule_id:body.rule_id,...body.rule};
      else rules.rules=[];
      rules.revision++;rules.etag="etag-"+rules.revision;
      return {ok:true,json:async()=>({...reply,commands:copy(commands)})};
    }
    return {ok:true,json:async()=>({rules:copy(rules),commands:copy(commands),can_approve:true,read_at:when})};
  }});
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  const input=ui.nodes.get("hitl-rule-text");input.value="Ask before <script>literal</script>";input.events.input();
  ui.click("save-hitl-rule");
  assert.match(await ui.answer(true),/\nAsk before <script>literal<\/script>\n/);await ui.tick();await ui.tick();
  assert.equal(posts.length,1);assert.equal(posts[0].operation,"add");
  assert.equal(ui.nodes.get("hitl-rule-list").children[0].children[0].textContent,"Ask before <script>literal</script>");
  ui.nodes.get("hitl-rule-list").children[0].children.find(node=>node.textContent==="編輯").events.click();
  input.value="Updated scope";input.events.input();
  ui.click("save-hitl-rule");
  assert.match(await ui.answer(true),/^確認更新這條 Team 規則？\nUpdated scope\n/);await ui.tick();await ui.tick();
  assert.equal(posts[1].operation,"update");assert.equal(posts[1].rule_id,"original-rule");
  ui.nodes.get("hitl-rule-list").children[0].children.find(node=>node.textContent==="刪除").events.click();
  assert.equal(await ui.answer(true),"確認從目前清單刪除這條規則？\nUpdated scope\n原批准、效果與歷史會保留。");
  await ui.tick();await ui.tick();
  assert.equal(posts[2].operation,"delete");assert.equal(posts[2].rule,null);
  assert.deepEqual(posts.map(body=>body.expected_rules_revision),[0,1,2]);
});

for(const editing of [false,true]){
  test(`native HITL deleting another rule preserves the unsent ${editing?"edit":"new"} draft and its revision`,async()=>{
    const data=hitlChat(),rules=hitlRead(data).rules,posts=[];
    rules.revision=2;rules.etag="etag-2";rules.rules=[
      {rule_id:"delete-this",text:"Rule to delete",behavior:"ask_first"},
      {rule_id:"keep-this",text:"Existing editable rule",behavior:"allow_automatically"}];
    const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
      if(options.method==="POST"){
        const body=JSON.parse(options.body);posts.push(body);
        rules.rules=rules.rules.filter(rule=>rule.rule_id!==body.rule_id);
        rules.revision=3;rules.etag="etag-3";
        return {ok:true,json:async()=>hitlCommandReply(body)};
      }
      return {ok:true,json:async()=>({rules:copy(rules),commands:[],can_approve:true,read_at:when})};
    }});
    ui.click("read-hitl-rules");await ui.tick();await ui.tick();
    if(editing)ui.nodes.get("hitl-rule-list").children[1].children.find(node=>node.textContent==="編輯").events.click();
    const input=ui.nodes.get("hitl-rule-text");input.value="My still-unsent exact rule text";input.events.input();
    const before=JSON.parse(JSON.stringify(ui.context.hitlDraft()));
    ui.nodes.get("hitl-rule-list").children[0].children.find(node=>node.textContent==="刪除").events.click();
    await ui.answer(true);await ui.tick();await ui.tick();
    assert.deepEqual(posts.map(body=>body.operation),["delete"]);
    assert.equal(input.value,"My still-unsent exact rule text");
    assert.deepEqual(JSON.parse(JSON.stringify(ui.context.hitlDraft())),before);
    assert.equal(ui.nodes.get("save-hitl-rule").disabled,true,"the new server revision must not silently rebase the draft");
    assert.equal(ui.nodes.get("hitl-rule-list").children.length,1);
  });
}

for(const boundary of ["thread","config","logout","reconnect","edit"]){
  for(const failure of [false,true]){
    test(`native HITL late ${failure?"error":"result"} and finally cannot enter the next ${boundary}`,async()=>{
      const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
      const pending=[];
      const ui=await open(data,{now:Date.parse(when),hitl:url=>url.endsWith("/rules")?
        Promise.resolve({ok:true,json:async()=>({rules:copy(view.rules),commands:[],can_approve:true,read_at:when})}):
        new Promise((resolve,reject)=>pending.push({resolve,reject}))});
      if(boundary==="edit"){ui.click("read-hitl-rules");await ui.tick();await ui.tick()}
      hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();
      if(boundary==="thread"){
        const next=copy(data);next.composer.thread_id="next-thread";next.composer.attention[0].thread_id="next-thread";
        ui.holder.current=next;await ui.context.switchThread("next-thread");
      }else if(boundary==="config"){
        const next=copy(data);next.console_id="next-console";ui.render(next);
      }else if(boundary==="logout"){
        ui.render({...copy(data),authentication:"LOGIN_REQUIRED"});ui.render(data);
      }else if(boundary==="reconnect"){
        ui.holder.response={ok:true,json:async()=>({})};ui.click("login");await ui.tick();await ui.tick();
      }else{
        ui.nodes.get("hitl-rule-text").value="New unsent rule";ui.nodes.get("hitl-rule-text").events.input();
      }
      hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();
      assert.equal(pending.length,2);
      if(failure)pending[0].reject(new Error("OLD ERROR"));
      else pending[0].resolve({ok:true,json:async()=>copy(view)});
      await ui.tick();await ui.tick();
      assert.equal(hitlNodes(ui,"data-hitl-material",pid).length,0);
      assert.equal(hitlNodes(ui,"data-hitl-read",pid)[0].disabled,true);
      assert.equal(hitlNodes(ui,"data-hitl-card",pid)[0].children.some(node=>node.textContent.includes("OLD ERROR")),false);
      pending[1].resolve({ok:true,json:async()=>copy(view)});await ui.tick();await ui.tick();
      assert.equal(hitlNodes(ui,"data-hitl-read",pid)[0].disabled,false);
      if(boundary==="edit")assert.equal(ui.nodes.get("hitl-rule-text").value,"New unsent rule");
      assert.equal(ui.calls.filter(call=>call.url.startsWith("/api/native-hitl/")&&call.method==="POST").length,0);
    });
  }
}
test("native HITL absent or legacy does not request or render native approval controls",async()=>{
  for(const off of ["flag","legacy"]){
    const data=hitlChat();
    if(off==="flag")delete data.composer.native_hitl;else delete data.composer.state_profile;
    const ui=await open(data,{now:Date.parse(when)});
    assert.equal(ui.nodes.get("hitl-rules-panel").hidden,true);
    assert.equal(ui.nodes.get("attention-panel").hidden,true);
    assert.equal(ui.calls.some(call=>call.url.startsWith("/api/native-hitl/")),false);
  }
});

test("native HITL unavailable API is explicit and never becomes an empty policy",async()=>{
  const ui=await open(hitlChat(),{now:Date.parse(when),
    hitl:async()=>({ok:false,status:409,json:async()=>({error:{code:"NATIVE_HITL_UNAVAILABLE"}})})});
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  assert.match(ui.text("hitl-rules-note"),/未啟用|不可用|尚未提供/);
  assert.equal(ui.nodes.get("save-hitl-rule").disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});
function hitlAgent(identity){
  const data=hitlChat(),agent=agentChat(identity);
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])data[key]=agent[key];
  data.composer.attention[0].item.approval.call.agent_id=identity;
  return data;
}
for(const failure of [false,true]){
  test(`native HITL old writer ${failure?"error":"result"} cannot clear a different Agent's rule draft`,async()=>{
    const a=hitlAgent("agent-one"),b=hitlAgent("agent-two"),viewA=hitlRead(a),viewB=hitlRead(b);
    let finish,reject,posted;
    const ui=await open(a,{now:Date.parse(when),agentStates:{"agent-two":b},hitl:(url,options)=>{
      if(options.method==="POST"){
        posted=JSON.parse(options.body);return new Promise((resolve,fail)=>{finish=resolve;reject=fail});
      }
      return Promise.resolve({ok:true,json:async()=>copy(options.headers?.["X-Console-Agent"]==="agent-two"?viewB:viewA)});
    }});
    const pid=viewA.readback.approval.pending_id;
    hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
    hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();await ui.tick();
    await ui.context.switchAgent("agent-two");await ui.tick();
    hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
    hitlNodes(ui,"data-hitl-choice","allow_rule")[0].events.click();await ui.tick();
    const draft=hitlNodes(ui,"data-hitl-rule",pid)[0];draft.value="Only Agent B's reviewed scope.";draft.events.input();
    const before=ui.calls.length;
    if(failure)reject(new Error("OLD OWNER RESPONSE"));
    else finish({ok:true,json:async()=>hitlCommandReply(posted)});
    await ui.tick();await ui.tick();
    assert.equal(hitlNodes(ui,"data-hitl-rule",pid)[0].value,"Only Agent B's reviewed scope.");
    assert.equal(ui.calls.length,before);
    assert.equal(ui.calls.filter(call=>call.method==="POST"&&call.url.endsWith("/decisions")).length,1);
    assert.equal(ui.calls.find(call=>call.url.endsWith("/decisions")).headers["X-Console-Agent"],"agent-one");
  });
}
test("native HITL automatic sign-in refresh invalidates an older read error",async()=>{
  const data=hitlChat(),pid=data.composer.attention[0].item.item_id;
  const pending=[];
  const ui=await open(data,{now:Date.parse(when),hitl:()=>new Promise(resolve=>{pending.push(resolve)})});
  ui.holder.response={ok:true,json:async()=>({authentication:"AVAILABLE",authentication_refresh_at:null})};
  await ui.context.refreshAuthentication();
  assert.equal(pending.length,2);
  pending[0]({ok:false,status:401,json:async()=>({error:{code:"LOGIN_REQUIRED"}})});await ui.tick();await ui.tick();
  assert.equal(ui.text("auth-state"),"既有登入可用");
  assert.equal(hitlNodes(ui,"data-hitl-read",pid)[0].disabled,true);
  pending[1]({ok:true,json:async()=>copy(hitlRead(data))});await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-read",pid)[0].disabled,false);
});
test("native HITL rule drafts prevent a remote-session reload from discarding unsent text",async()=>{
  const ui=await open(hitlChat(),{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>({
    rules:hitlRead().rules,commands:[],can_approve:true,read_at:when})})});
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("hitl-rule-text").disabled,false);
  ui.context.window.location.protocol="https:";
  let reloads=0;ui.context.window.location.reload=()=>{reloads++};
  ui.holder.now=Date.parse(when)+20000;
  ui.nodes.get("hitl-rule-text").value="An unsent owner policy";ui.nodes.get("hitl-rule-text").events.input();
  const changed={status:403,ok:false,headers:{get:()=>"application/json"},
    clone:()=>({json:async()=>({error:{code:"REMOTE_SESSION_CHANGED"}})})};
  await ui.context.remoteResponse(changed);
  await ui.tick();
  assert.equal(reloads,0);
  assert.equal(ui.nodes.get("hitl-rule-text").value,"An unsent owner policy");
  ui.nodes.get("hitl-rule-text").value="";ui.nodes.get("hitl-rule-text").events.input();
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,1);
});

test("any unsent field or chosen file keeps a remote-session reload from discarding it, not just the composer",async()=>{
  const ui=await open(fixture(),{now:Date.parse(when)});
  ui.context.window.location.protocol="https:";
  let reloads=0;ui.context.window.location.reload=()=>{reloads++};
  ui.holder.now=Date.parse(when)+20000;
  const changed={status:403,ok:false,headers:{get:()=>"application/json"},
    clone:()=>({json:async()=>({error:{code:"REMOTE_SESSION_CHANGED"}})})};
  const body=ui.nodes.get("skill-new-body");
  body.value="An unsent SKILL.md draft";
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,0);
  assert.equal(body.value,"An unsent SKILL.md draft");
  body.value="";
  ui.nodes.get("template-file").files=[{name:"agent.json"}];
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,0,"a chosen file is a draft too");
  ui.nodes.get("template-file").files=[];
  const header=ui.nodes.get("team-mcp-header");
  header.value=header.defaultValue;  // a markup default the user did not change is not a draft
  ui.nodes.get("settings-search").value="filter text";  // nor is a search filter
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,1);
  vm.runInContext('editTeamMcp({server_label:"crm",server_url:"https://mcp.example/crm",allowed_tools:["read"],'+
    'require_approval:"always"})',ui.context);
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,2,"an opened but unchanged Team MCP edit is not a draft");
  ui.nodes.get("team-mcp-url").value="https://mcp.example/changed";
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,2,"a changed one is");
});
test("an attachment-only draft (nothing typed) keeps a remote-session reload from discarding it",async()=>{
  const ui=await open(fixture(),{now:Date.parse(when)});
  ui.context.window.location.protocol="https:";
  let reloads=0;ui.context.window.location.reload=()=>{reloads++};
  ui.holder.now=Date.parse(when)+20000;
  const changed={status:403,ok:false,headers:{get:()=>"application/json"},
    clone:()=>({json:async()=>({error:{code:"REMOTE_SESSION_CHANGED"}})})};
  for(const drafts of ["attachmentDrafts","userFileDrafts","userImageDrafts"]){
    vm.runInContext(`${drafts}.set(attachmentDraftKey(),[{name:"notes.txt"}])`,ui.context);
    await ui.context.remoteResponse(changed);await ui.tick();
    assert.equal(reloads,0,`${drafts} is a draft`);
    vm.runInContext(`${drafts}.set(attachmentDraftKey(),[])`,ui.context);  // removing the last one leaves []
  }
  await ui.context.remoteResponse(changed);await ui.tick();
  assert.equal(reloads,1);
});

test("native HITL remote sign-in layer refusal exposes sign-in without replaying a control",async()=>{
  const data=hitlChat();data.remote_user="OFFLINE owner";
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>new Response("<html>OFFLINE sign in</html>",
    {status:403,headers:{"content-type":"text/html"}})});
  ui.context.window.location.protocol="https:";
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("login").hidden,false);
  assert.equal(ui.text("login"),"重新登入");
  assert.match(ui.text("auth-state"),/失效|連線/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

for(const status of ["allow","deny","expired","interrupted","effect_unknown","closed","authorized"]){
  test(`native HITL ${status} readback stays read-only`,async()=>{
    const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
    if(status==="closed")view.readback.turn_closed=true;
    else if(status==="authorized")view.readback.authorization={effect_token:"original-token"};
    else view.readback.status=status;
    const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
    hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
    for(const choice of ["allow_once","allow_rule","deny"])assert.equal(hitlNodes(ui,"data-hitl-choice",choice)[0].disabled,true);
    assert.equal(hitlNodes(ui,"data-hitl-read",pid)[0].disabled,false);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}
test("native HITL human takeover only opens the existing viewer panel",async()=>{
  const data=hitlAgent("agent-one"),view=hitlRead(data),pid=view.readback.approval.pending_id;
  Object.assign(view.readback.pending.command.review,{reason:"human_takeover",takeover_required:true,outcome:"deny"});
  view.readback.status="deny";
  data.composer.hosted_session_id="original-hosted-session";
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
  const card=hitlNodes(ui,"data-hitl-card",pid)[0];
  assert.ok(card.children.some(node=>node.textContent.includes("2FA")&&node.textContent.includes("CAPTCHA")));
  const button=card.children.find(node=>node.textContent==="開啟既有助理電腦面板");
  const before=ui.calls.length;button.events.click();await ui.tick();
  assert.equal(ui.nodes.get("desktop-panel").open,true);
  assert.equal(ui.calls.length,before);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native HITL retained references are readable history, not a pending attention count",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id;
  data.composer.attention=[];view.readback.status="interrupted";view.readback.turn_closed=true;
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>copy(view)})});
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
  assert.equal(ui.nodes.get("hitl-history-panel").hidden,false);
  hitlNodes(ui,"data-hitl-read",pid)[0].events.click();await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

function literalDownloadState(text=""){
  const data=attachmentChat(),task=data.tasks[0],bytes=Buffer.from(text);
  task.instruction="Original literal request";task.thread_id=data.composer.thread_id;
  task.user_files=[{name:"original.txt",utf8_bytes:bytes.length,sha256:require("node:crypto").createHash("sha256").update(bytes).digest("hex")}];
  task.actions["read-files"]=action(true);
  return data;
}
function originalFileResponse(file,raw){
  return {ok:true,headers:{get:name=>({
    "content-type":"text/plain; charset=utf-8","cache-control":"no-store","x-content-type-options":"nosniff",
    "content-disposition":`attachment; filename="${file.name}"`,"x-content-sha256":file.sha256,
  }[name]||null)},arrayBuffer:async()=>new Uint8Array(raw).buffer};
}
function originalFileButton(ui){
  return turnActions(ui.nodes.get("conversation-history").children[0])
    .find(button=>button.textContent.startsWith("下載原檔 "));
}

test("literal empty original file downloads verified raw bytes instead of becoming a missing result",async()=>{
  const data=literalDownloadState(),ui=await open(data),file=data.tasks[0].user_files[0];
  ui.holder.userFileResponse=originalFileResponse(file,[]);
  await originalFileButton(ui).events.click();
  assert.equal(ui.calls.at(-1).url,"/api/instructions/offline/files/original.txt");
  assert.equal(ui.holder.downloaded.size,0);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

async function chooseLiteral(ui,files){
  ui.nodes.get("user-text-files").files=files;
  await ui.nodes.get("user-text-files").events.change();
}
function binaryFile(name,bytes){
  return {name,size:bytes.length,arrayBuffer:async()=>Uint8Array.from(bytes).buffer};
}

for(const [label,file] of [
  ["invalid-utf8",binaryFile("input.txt",[0xc3,0x28])],
  ["nul",binaryFile("input.txt",[65,0,66])],
  ["invalid-tail",binaryFile("input.txt",[0xef,0xbb,0xbf,0xf0,0x9f])],
]){
  test(`literal ${label} is rejected as a batch without replacement text or a POST`,async()=>{
    const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
    await chooseLiteral(ui,[file]);
    assert.equal(ui.nodes.get("user-file-selected").children.length,0);
    assert.match(ui.text("user-file-limit"),/不是有效UTF-8或含NUL/);
    typeAttachmentText(ui);
    assert.equal(ui.nodes.get("send-instruction").disabled,true);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
    ui.click("clear-user-files");assert.equal(ui.nodes.get("send-instruction").disabled,false);
  });
}

for(const [label,name,size] of [
  ["File.size","input.txt",8193],["path","../input.txt",3],["URL","https://x/input.txt",3],
  ["extension","input.exe",3],["nonlogical-name","原件.txt",3],["long-name","x".repeat(129)+".txt",3],
]){
  test(`literal ${label} is rejected before reading any user File`,async()=>{
    const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
    let reads=0;
    await chooseLiteral(ui,[{name,size,arrayBuffer:async()=>{reads++;return new Uint8Array(3).buffer}}]);
    assert.equal(reads,0);assert.equal(ui.nodes.get("user-file-selected").children.length,0);
    assert.match(ui.text("user-file-limit"),/名稱不是路徑或URL/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("literal actual bytes are checked independently of declared File.size",async()=>{
  const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
  await chooseLiteral(ui,[{name:"input.txt",size:1,arrayBuffer:async()=>new Uint8Array(8193).buffer}]);
  assert.equal(ui.nodes.get("user-file-selected").children.length,0);
  assert.match(ui.text("user-file-limit"),/最多8192 bytes/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("literal exact JSON escaped UTF8 limit includes content and file names and updates on typing",async()=>{
  const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
  typeAttachmentText(ui,"Read.");
  const overhead=Buffer.byteLength('{"content":"Read.","files":[{"name":"one.txt","text":""}]}');
  const text="x".repeat(8192-overhead);
  await chooseLiteral(ui,[selectedFile("one.txt",text)]);
  assert.match(ui.text("user-file-limit"),/8192／8192/);
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  typeAttachmentText(ui,"Read.\n");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  typeAttachmentText(ui,"Read.");
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  await chooseLiteral(ui,[selectedFile("new.txt","\n".repeat(4096))]);
  assert.equal(ui.nodes.get("user-file-selected").children[0].children[0].value,"one.txt");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("literal empty local File is retained and safe json content is never rendered",async()=>{
  const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
  await chooseLiteral(ui,[selectedFile("empty.txt",""),selectedFile("data.json","<script>unsafe()</script>\r\n=FORMULA()")]);
  assert.equal(ui.nodes.get("user-file-selected").children.length,2);
  assert.match(ui.nodes.get("user-file-selected").children[0].children[1].textContent,/0 bytes/);
  assert.equal(ui.nodes.get("user-file-selected").children.some(row=>row.children.some(node=>
    /unsafe|FORMULA/.test(node.textContent))),false);
  typeAttachmentText(ui);ui.click("send-instruction");await ui.tick();
  assert.equal(JSON.parse(ui.calls.at(-1).body).files[0].text,"");
});

test("literal and reference attachments share the four-name bound without silently renaming a file",async()=>{
  const data=attachmentChat(),ui=await open(data);await readAttachments(ui,data);selectAttachment(ui);
  await readyFileInput(ui,data);
  await chooseLiteral(ui,[selectedFile("a.txt","A"),selectedFile("b.md","B"),selectedFile("c.csv","C")]);
  typeAttachmentText(ui);
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  assert.match(ui.text("user-file-limit"),/4／4/);
  const name=ui.nodes.get("user-file-selected").children[0].children[0];
  name.value="source-1.txt";name.events.change();
  assert.equal(ui.nodes.get("user-file-selected").children[0].children[0].value,"source-1.txt");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  let read=0;
  await chooseLiteral(ui,Array.from({length:4},(_,n)=>({name:`f${n}.txt`,size:0,arrayBuffer:async()=>{read++;return new ArrayBuffer(0)}})));
  assert.equal(read,0);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("literal duplicate File names fail before reading and do not use file positions as identity",async()=>{
  const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
  let reads=0;
  const file={name:"same.txt",size:0,arrayBuffer:async()=>{reads++;return new ArrayBuffer(0)}};
  await chooseLiteral(ui,[file,file]);
  assert.equal(reads,0);assert.match(ui.text("user-file-limit"),/不重名/);
});

for(const failure of ["missing-profile","missing-auth","unknown-profile","wrong-cid","null-profile","404"]){
  test(`literal capability ${failure} never enables File reads or falls back to references`,async()=>{
    const data=attachmentChat(),ui=await open(data),value=fileCapability(data);
    if(failure==="missing-profile")delete value.user_text_file_profile;
    if(failure==="missing-auth")delete value.authorization;
    if(failure==="unknown-profile")value.user_text_file_profile="unrecognized";
    if(failure==="wrong-cid")value.conversation_id="other-cid";
    if(failure==="null-profile")value.user_text_file_profile=null;
    ui.holder.fileCapabilityResponse=failure==="404"?
      {ok:false,json:async()=>({error:{code:"USER_FILE_CAPABILITY_UNAVAILABLE"}})}:{ok:true,json:async()=>value};
    ui.click("read-user-file-capability");await ui.tick();
    assert.equal(ui.nodes.get("user-text-files").disabled,true);
    let reads=0;
    await chooseLiteral(ui,[{name:"a.txt",size:0,arrayBuffer:async()=>{reads++;return new ArrayBuffer(0)}}]);
    assert.equal(reads,0);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/user-text-file-capability")).length,1);
    assert.equal(ui.calls.some(call=>call.url.endsWith("/attachment-sources")||call.method==="POST"),false);
  });
}

for(const outcome of ["success","error"]){
  test(`literal late File ${outcome} cannot replace a newer chosen file or clear its busy state`,async()=>{
    const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
    let oldResolve,oldReject,newResolve;
    const input=ui.nodes.get("user-text-files");
    input.files=[{name:"old.txt",size:3,arrayBuffer:()=>new Promise((resolve,reject)=>{oldResolve=resolve;oldReject=reject})}];
    const oldRead=input.events.change();await ui.tick();
    input.files=[{name:"new.txt",size:3,arrayBuffer:()=>new Promise(resolve=>{newResolve=resolve})}];
    const newRead=input.events.change();await ui.tick();
    if(outcome==="success")oldResolve(new TextEncoder().encode("old").buffer);else oldReject(new Error("old failure"));
    await oldRead;
    typeAttachmentText(ui);
    assert.equal(ui.nodes.get("send-instruction").disabled,true);
    assert.match(ui.text("user-file-note"),/正在本機讀取/);
    newResolve(new TextEncoder().encode("new").buffer);await newRead;
    assert.equal(ui.nodes.get("user-file-selected").children[0].children[0].value,"new.txt");
    assert.equal(ui.nodes.get("send-instruction").disabled,false);
  });
}

for(const change of ["thread","draft","login","lost-login"]){
  test(`literal late decode after ${change} cannot publish or POST original file contents`,async()=>{
    const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
    let finish;
    ui.nodes.get("user-text-files").files=[{name:"old.txt",size:3,arrayBuffer:()=>new Promise(resolve=>{finish=resolve})}];
    const pending=ui.nodes.get("user-text-files").events.change();await ui.tick();
    if(change==="login"){
      ui.holder.response={ok:true,json:async()=>({})};ui.click("login");await ui.tick();await ui.tick();
    }else{
      const newer=copy(data);
      if(change==="thread"){newer.composer.thread_id="different";newer.composer.request_id="different-rid"}
      if(change==="draft")newer.composer.request_id="new-rid";
      if(change==="lost-login")newer.authentication="LOGIN_EXPIRED";
      ui.render(newer);
    }
    finish(new TextEncoder().encode("old").buffer);await pending;
    assert.equal(ui.nodes.get("user-file-selected").children.length,0);
    assert.equal(ui.calls.some(call=>/\/submit$|\/preflight$/.test(call.url)),false);
  });
}

test("literal late capability result cannot enable a new draft and its finally cannot end a newer read",async()=>{
  const data=attachmentChat(),ui=await open(data);let oldRead,newRead;
  ui.holder.fileCapabilityResponse=new Promise(resolve=>{oldRead=resolve});
  ui.click("read-user-file-capability");await ui.tick();
  const next=copy(data);next.composer.request_id="next-rid";ui.render(next);
  ui.holder.fileCapabilityResponse=new Promise(resolve=>{newRead=resolve});
  ui.click("read-user-file-capability");await ui.tick();
  oldRead({ok:true,json:async()=>fileCapability(data)});await ui.tick();
  assert.equal(ui.nodes.get("read-user-file-capability").disabled,true);
  assert.equal(ui.nodes.get("user-text-files").disabled,true);
  newRead({ok:true,json:async()=>fileCapability(next)});await ui.tick();
  assert.equal(ui.nodes.get("user-text-files").disabled,false);
});

test("literal clearing a draft during a capability refresh cannot leave that observation stuck busy",async()=>{
  const data=attachmentChat(),ui=await open(data);await readyFileInput(ui,data);
  await chooseLiteral(ui,[selectedFile("draft.txt","local")]);
  let oldRead;
  ui.holder.fileCapabilityResponse=new Promise(resolve=>{oldRead=resolve});
  ui.click("read-user-file-capability");await ui.tick();
  ui.click("clear-user-files");
  assert.equal(ui.nodes.get("user-file-selected").children.length,0);
  assert.equal(ui.nodes.get("read-user-file-capability").disabled,false);
  oldRead({ok:true,json:async()=>fileCapability(data)});await ui.tick();
  assert.equal(ui.nodes.get("user-text-files").disabled,true);
  await readyFileInput(ui,data);
  assert.equal(ui.nodes.get("user-text-files").disabled,false);
});

test("literal and refs follow-up snapshots move together to the returned new RID only",async()=>{
  const data=attachmentChat();
  data.composer.editable=false;data.composer.can_submit=false;data.composer.can_follow_up=true;
  data.composer.conversation_id="captured-cid";data.composer.user_files=[{name:"previous.txt",sha256:"b".repeat(64),utf8_bytes:9}];
  const ui=await open(data);await readAttachments(ui,data);selectAttachment(ui);await readyFileInput(ui,data);
  await chooseLiteral(ui,[selectedFile("next.csv","\ufeffnext\r\n")]);typeAttachmentText(ui,"Next exact turn");
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.calls.at(-1).url,"/api/instructions/attachment-thread/next");
  const next=copy(data);next.composer.request_id="next-rid";next.composer.editable=true;next.composer.can_follow_up=false;next.composer.user_files=[];
  ui.holder.pending(next);await ui.tick();
  const sent=ui.calls.at(-1);
  assert.equal(sent.url,"/api/instructions/next-rid/submit");
  assert.deepEqual(JSON.parse(sent.body),{content:"Next exact turn",model:"offline-chat-a",
    attachments:[{name:"source-1.txt",source:attachmentCatalog(data).catalog.entries[0].reference}],
    files:[{name:"next.csv",text:"\ufeffnext\r\n"}]});
  assert.equal(data.composer.user_files[0].name,"previous.txt");
});

test("literal prepared metadata is read-only and never becomes the next turn files",async()=>{
  const data=attachmentChat();
  data.composer.editable=false;data.composer.can_submit=false;
  data.composer.user_files=[{name:"frozen.md",sha256:"b".repeat(64),utf8_bytes:12}];
  const ui=await open(data);
  assert.equal(ui.nodes.get("user-text-files").disabled,true);
  assert.equal(ui.nodes.get("user-file-selected").children[0].children[0].disabled,true);
  data.composer.can_follow_up=true;ui.render(data);
  assert.equal(ui.nodes.get("user-file-selected").children.length,0);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("literal follow-up stops before submit when authentication changes while next is pending",async()=>{
  const data=attachmentChat();data.composer.editable=false;data.composer.can_follow_up=true;
  const ui=await open(data);await readyFileInput(ui,data);await chooseLiteral(ui,[selectedFile("safe.txt","original")]);
  typeAttachmentText(ui);ui.click("send-instruction");await ui.tick();const finish=ui.holder.pending;
  const expired=copy(data);expired.authentication="LOGIN_EXPIRED";ui.render(expired);
  const next=copy(data);next.composer.request_id="new-rid";next.composer.editable=true;next.composer.can_follow_up=false;
  finish(next);await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/submit")).length,0);
});

test("literal original download preserves BOM CRLF and never displays parsed file content",async()=>{
  const text="\ufeff{\r\n\"value\":\"<script>data</script>\"}\r\n",data=literalDownloadState(text),ui=await open(data);
  ui.holder.userFileResponse=originalFileResponse(data.tasks[0].user_files[0],Buffer.from(text));
  await originalFileButton(ui).events.click();
  assert.deepEqual(Buffer.from(await ui.holder.downloaded.arrayBuffer()),Buffer.from(text));
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const damage of ["denied","404","hash","body","filename"]){
  test(`literal original download ${damage} never creates a success-shaped file`,async()=>{
    const data=literalDownloadState("original"),ui=await open(data),file=data.tasks[0].user_files[0];
    const response=originalFileResponse(file,Buffer.from(damage==="body"?"different":"original"));
    if(damage==="hash"||damage==="filename"){
      const original=response.headers.get;
      response.headers.get=name=>name===(damage==="hash"?"x-content-sha256":"content-disposition")?"wrong":original(name);
    }
    ui.holder.userFileResponse=["denied","404"].includes(damage)?
      {ok:false,json:async()=>({error:{code:damage==="denied"?"BACKEND_DENIED":"USER_FILE_UNAVAILABLE"}})}:response;
    await originalFileButton(ui).events.click();
    assert.equal(ui.holder.downloaded,undefined);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("literal late original download after thread switch cannot save or contaminate the new page",async()=>{
  const data=literalDownloadState("original"),ui=await open(data);let finish;
  ui.holder.userFileResponse=new Promise(resolve=>{finish=resolve});
  const pending=originalFileButton(ui).events.click();await ui.tick();
  const newer=attachmentChat();newer.composer.thread_id="other";newer.composer.request_id="other-rid";ui.render(newer);
  finish(originalFileResponse(data.tasks[0].user_files[0],Buffer.from("original")));await pending;
  assert.equal(ui.holder.downloaded,undefined);
});

test("attachment picker reads only on click and sends the exact selected reference with original text",async()=>{
  const data=attachmentChat(),ui=await open(data),value=attachmentCatalog(data);
  assert.equal(ui.calls.some(call=>call.url.endsWith("/attachment-sources")),false);
  ui.holder.attachmentResponse={ok:true,json:async()=>copy(value)};
  ui.click("read-attachment-sources");await ui.tick();
  assert.equal(ui.calls.at(-1).url,"/api/instructions/attachment-draft/attachment-sources");
  const checkbox=attachmentCheckbox(ui);checkbox.checked=true;checkbox.events.change();
  ui.nodes.get("instruction").value="  Keep exact text.\n";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");ui.click("send-instruction");await ui.tick();
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].body),{content:"  Keep exact text.\n",model:"offline-chat-a",
    attachments:[{name:"source-1.txt",source:value.catalog.entries[0].reference}]});
  assert.equal(posts[0].url,"/api/instructions/attachment-draft/submit");
  // P01: no permission disclaimer or digest in the picker text; the version is in words, the digest only on hover.
  assert.doesNotMatch(ui.text("attachment-note"),/候選不是讀取權限|metadata/);
  const option=ui.nodes.get("attachment-options").children[0].children[1];
  assert.match(option.textContent,/（第 1 版）$/);
  assert.doesNotMatch(option.textContent,/SHA256/);
});

async function readAttachments(ui,data,entries){
  ui.holder.attachmentResponse={ok:true,json:async()=>attachmentCatalog(data,entries)};
  ui.click("read-attachment-sources");await ui.tick();
}
function selectAttachment(ui,index=0){
  const checkbox=attachmentCheckbox(ui,index);checkbox.checked=true;checkbox.events.change();
}
function typeAttachmentText(ui,value="Send only this draft."){
  ui.nodes.get("instruction").value=value;ui.nodes.get("instruction").events.input();
}

test("attachment states are explicit and only recorded entries can be selected up to four",async()=>{
  const data=attachmentChat(),ui=await open(data);
  const entries=Array.from({length:5},(_,n)=>({source_id:`source-${n}`,state:"recorded",media_type:"text/plain",
    reference:{source_id:`source-${n}`,version:1,sha256:String(n).repeat(64),byte_length:42}}));
  entries.push(...["missing","expired","withdrawn","unsupported"].map(state=>({source_id:state,state,reference:null,media_type:null})));
  await readAttachments(ui,data,entries);
  for(let n=5;n<9;n++)assert.equal(attachmentCheckbox(ui,n).disabled,true);
  for(let n=0;n<4;n++)selectAttachment(ui,n);
  assert.equal(ui.nodes.get("attachment-selected").children.length,4);
  assert.equal(attachmentCheckbox(ui,4).disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  const name=ui.nodes.get("attachment-selected").children[0].children[0];
  name.value="../not-a-file.txt";name.events.change();typeAttachmentText(ui);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.match(ui.text("attachment-selection-note"),/不是路徑或URL/);
});

for(const state of ["disabled","unsupported-scope"]){
  test(`attachment ${state} is explained without a permission or automatic request`,async()=>{
    const data=attachmentChat(),ui=await open(data),value=attachmentCatalog(data,[]);
    value.catalog.state=state;
    ui.holder.attachmentResponse={ok:true,json:async()=>value};ui.click("read-attachment-sources");await ui.tick();
    assert.equal(ui.nodes.get("attachment-options").children.length,0);
    assert.match(ui.text("attachment-note"),state==="disabled"?/沒有開啟文字附件/:/不支援文字附件/);
    typeAttachmentText(ui);
    assert.equal(ui.nodes.get("send-instruction").disabled,false);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  });
}

test("attachment ordering and version changes never replace an unsent name and ExactSourceRef",async()=>{
  const data=attachmentChat(),ui=await open(data);
  const initial=attachmentCatalog(data).catalog.entries;
  await readAttachments(ui,data,initial);selectAttachment(ui);
  const name=ui.nodes.get("attachment-selected").children[0].children[0];
  name.value="human-label.md";name.events.change();
  const changed=copy(initial);changed[0].reference.version=2;changed[0].reference.sha256="b".repeat(64);
  changed.unshift({source_id:"new-first",state:"recorded",media_type:"application/json",
    reference:{source_id:"new-first",version:1,sha256:"c".repeat(64),byte_length:99}});
  await readAttachments(ui,data,changed);typeAttachmentText(ui);
  const selected=ui.nodes.get("attachment-selected").children[0];
  assert.equal(selected.children[0].value,"human-label.md");
  assert.match(selected.children[1].textContent,/source-one（第 1 版）/);
  assert.match(ui.text("attachment-selection-note"),/尚未替換/);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  selected.children[2].events.click();selectAttachment(ui,1);
  assert.match(ui.nodes.get("attachment-selected").children[0].children[1].textContent,/source-one（第 2 版）/);
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
});

for(const change of ["thread","request","cid","nested-cid","authorization","content","dependencies"]){
  test(`attachment wrong ${change} response is rejected without filling the picker`,async()=>{
    const data=attachmentChat(),ui=await open(data),value=attachmentCatalog(data);
    if(change==="thread")value.thread_id="other-thread";
    if(change==="request")value.request_id="other-draft";
    if(change==="cid")value.conversation_id="other-conversation";
    if(change==="nested-cid")value.catalog.conversation_id="other-conversation";
    if(change==="authorization")value.catalog.authorization=true;
    if(change==="content")value.catalog.content_reverified=true;
    if(change==="dependencies")value.catalog.dependencies_reverified=true;
    ui.holder.attachmentResponse={ok:true,json:async()=>value};ui.click("read-attachment-sources");await ui.tick();
    assert.equal(ui.nodes.get("attachment-options").children.length,0);
    assert.match(ui.text("attachment-note"),/不符.*不替換/);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/attachment-sources")).length,1);
  });
}

test("attachment scoped 404 preserves selected refs and never falls back or auto-submits",async()=>{
  const data=attachmentChat();data.composer.conversation_id="old-captured-cid";
  const ui=await open(data);await readAttachments(ui,data);selectAttachment(ui);
  ui.holder.attachmentResponse={ok:false,json:async()=>({error:{code:"ATTACHMENT_CATALOG_UNAVAILABLE",upstream_status:404}})};
  const start=ui.calls.length;ui.click("read-attachment-sources");await ui.tick();typeAttachmentText(ui);
  assert.match(ui.text("attachment-note"),/不改查新對話目錄/);
  assert.equal(ui.nodes.get("attachment-selected").children.length,1);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.deepEqual(ui.calls.slice(start).map(call=>call.url),["/api/instructions/attachment-draft/attachment-sources"]);
});

for(const code of ["CONVERSATION_ARCHIVED","CONVERSATION_SESSION_RESET_REQUIRED"]){
  test(`attachment ${code} explains the original conversation failure without switching source`,async()=>{
    const data=attachmentChat(),ui=await open(data);
    ui.holder.attachmentResponse={ok:false,json:async()=>({error:{code}})};
    ui.click("read-attachment-sources");await ui.tick();
    assert.match(ui.text("attachment-note"),code==="CONVERSATION_ARCHIVED"?/原對話已封存/:/需由你明確重設/);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/attachment-sources")).length,1);
    assert.equal(ui.nodes.get("attachment-options").children.length,0);
  });
}

for(const outcome of ["success","error","rejection"]){
  test(`late attachment ${outcome} cannot affect another thread or finish its newer read`,async()=>{
    const data=attachmentChat(),ui=await open(data);
    let finish,fail,newRead;
    ui.holder.attachmentResponse=new Promise((resolve,reject)=>{finish=resolve;fail=reject});
    ui.click("read-attachment-sources");await ui.tick();
    const newer=copy(data);newer.composer.thread_id="other-thread";newer.composer.request_id="other-draft";
    newer.composer.conversation_id="other-cid";ui.render(newer);
    ui.holder.attachmentResponse=new Promise(resolve=>{newRead=resolve});
    ui.click("read-attachment-sources");await ui.tick();
    if(outcome==="success")finish({ok:true,json:async()=>attachmentCatalog(data)});
    else if(outcome==="error")finish({ok:false,json:async()=>({error:{code:"ATTACHMENT_CATALOG_UNAVAILABLE"}})});
    else fail(new Error("OFFLINE late reader failure"));
    await ui.tick();await ui.tick();
    assert.equal(ui.nodes.get("read-attachment-sources").disabled,true);
    assert.equal(ui.nodes.get("attachment-options").children.length,0);
    assert.doesNotMatch(ui.text("attachment-note"),/不可用|不符|沒有回應/);
    newRead({ok:true,json:async()=>attachmentCatalog(newer)});await ui.tick();
    assert.equal(ui.nodes.get("read-attachment-sources").disabled,false);
    assert.equal(ui.nodes.get("attachment-options").children.length,1);
  });
}

test("attachment read from an earlier draft cannot revive when the thread stays the same",async()=>{
  const data=attachmentChat(),ui=await open(data);let old;
  ui.holder.attachmentResponse=new Promise(resolve=>{old=resolve});
  ui.click("read-attachment-sources");await ui.tick();
  const next=copy(data);next.composer.request_id="new-draft";ui.render(next);
  old({ok:true,json:async()=>attachmentCatalog(data)});await ui.tick();
  assert.equal(ui.nodes.get("attachment-options").children.length,0);
  assert.equal(ui.nodes.get("read-attachment-sources").disabled,false);
});

test("attachment reconnect invalidates old reads without losing selected original references",async()=>{
  const data=attachmentChat(),ui=await open(data);await readAttachments(ui,data);selectAttachment(ui);
  let old;ui.holder.attachmentResponse=new Promise(resolve=>{old=resolve});
  ui.click("read-attachment-sources");await ui.tick();
  ui.holder.response={ok:true,json:async()=>({})};ui.click("login");await ui.tick();await ui.tick();
  old({ok:true,json:async()=>attachmentCatalog(data)});await ui.tick();
  assert.equal(ui.nodes.get("attachment-options").children.length,0);
  assert.equal(ui.nodes.get("attachment-selected").children.length,1);
  typeAttachmentText(ui);assert.equal(ui.nodes.get("send-instruction").disabled,true);
});

test("attachment frozen command survives reload and a follow-up starts with no inherited attachments",async()=>{
  const data=attachmentChat();data.composer.editable=false;data.composer.can_submit=false;
  data.composer.content="Frozen original";data.composer.attachments=[{
    name:"original.txt",source:attachmentCatalog(data).catalog.entries[0].reference}];
  const ui=await open(data);
  assert.equal(ui.nodes.get("attachment-selected").children[0].children[0].disabled,true);
  assert.equal(ui.nodes.get("read-attachment-sources").disabled,true);
  assert.match(ui.text("attachment-note"),/附件已經送出，不能更換/);
  data.composer.can_follow_up=true;ui.render(data);
  assert.equal(ui.nodes.get("attachment-selected").children.length,0);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("attachment missing original command is explicit and cannot look like a new empty draft",async()=>{
  const data=attachmentChat();data.composer.command_incomplete=true;
  data.composer.editable=false;data.composer.can_submit=false;
  const ui=await open(data);
  assert.match(ui.text("attachment-note"),/缺少完整 command/);
  assert.match(ui.text("instruction-reason"),/不能猜測.*重送/);
  assert.equal(ui.nodes.get("read-attachment-sources").disabled,true);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("attachment follow-up transfers its own snapshot to the next RID without changing the previous turn",async()=>{
  const data=attachmentChat();data.composer.editable=false;data.composer.can_submit=false;data.composer.can_follow_up=true;
  data.composer.conversation_id="captured-old-cid";data.composer.attachments=[{name:"previous.txt",
    source:{source_id:"previous-source",version:1,sha256:"b".repeat(64),byte_length:10}}];
  const previous=copy(data.composer.attachments),ui=await open(data);
  await readAttachments(ui,data);selectAttachment(ui);typeAttachmentText(ui,"New follow-up");
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.calls.at(-1).url,"/api/instructions/attachment-thread/next");
  const next=copy(data);next.composer.request_id="next-rid";next.composer.editable=true;next.composer.can_submit=true;
  next.composer.can_follow_up=false;next.composer.content=null;next.composer.attachments=[];
  ui.holder.pending(next);await ui.tick();
  const post=ui.calls.at(-1);
  assert.equal(post.url,"/api/instructions/next-rid/submit");
  assert.deepEqual(JSON.parse(post.body).attachments,[{name:"source-1.txt",
    source:attachmentCatalog(data).catalog.entries[0].reference}]);
  assert.deepEqual(data.composer.attachments,previous);
  assert.equal(ui.nodes.get("attachment-selected").children[0].children[0].value,"source-1.txt");
});

test("attachment follow-up finishing on another thread never changes its draft",async()=>{
  const data=attachmentChat();data.composer.editable=false;data.composer.can_follow_up=true;data.composer.conversation_id="original-cid";
  const ui=await open(data);await readAttachments(ui,data);selectAttachment(ui);typeAttachmentText(ui,"Original follow-up");
  ui.click("send-instruction");await ui.tick();
  const finishNext=ui.holder.pending;
  const other=attachmentChat();other.composer.thread_id="other";other.composer.request_id="other-rid";ui.render(other);
  typeAttachmentText(ui,"Unsent other draft");
  const next=copy(data);next.composer.request_id="original-next";next.composer.editable=true;next.composer.can_follow_up=false;next.composer.attachments=[];
  finishNext(next);await ui.tick();
  assert.equal(ui.calls.at(-1).url,"/api/instructions/original-next/submit");
  ui.holder.pending({task:{task_id:"original-next",submission:{state:"CONFIRMED"}}});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"Unsent other draft");
  assert.equal(ui.nodes.get("attachment-selected").children.length,0);
});

test("attachment follow-up cannot submit after login is lost while next is pending",async()=>{
  const data=attachmentChat();data.composer.editable=false;data.composer.can_follow_up=true;data.composer.conversation_id="original-cid";
  const ui=await open(data);await readAttachments(ui,data);selectAttachment(ui);typeAttachmentText(ui);
  ui.click("send-instruction");await ui.tick();
  const pending=ui.holder.pending;
  const loggedOut=copy(data);loggedOut.authentication="LOGIN_EXPIRED";ui.render(loggedOut);
  const next=copy(data);next.composer.request_id="new-but-not-submitted";next.composer.editable=true;next.composer.can_follow_up=false;
  pending(next);await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/submit")).length,0);
  assert.equal(ui.nodes.get("read-attachment-sources").disabled,true);
});

function cancellableChat(){
  const data=fixture(),task=data.tasks[0];
  task.submit_request_id=task.task_id;
  task.instruction="Original work";task.thread_id="cancel-thread";task.result=null;
  task.progress={data:{state:"running",reply_available:false,delivery_id:null},observed_at:when};
  task.cancellation={state:"NOT_SENT"};
  task.actions.cancel=action(true);
  task.actions["read-reply"]=action(false);
  task.actions["confirm-reply"]=action(false);
  task.admission={conversation_id:"cancel-conversation",turn_number:1,model:"offline-chat-a"};
  data.composer={thread_id:task.thread_id,request_id:task.task_id,content:task.instruction,
    editable:false,can_submit:false,can_follow_up:false,can_new_conversation:true,
    input_capacity:{maximum:8192},models,progress_available:true,
    conversation:{conversation_id:"cancel-conversation",can_follow_up:false,
      turns:[{request_id:task.task_id,state:"running",model:"offline-chat-a"}]}};
  return data;
}

function blockedConversation(blocker="SDK_STATE_INCOMPLETE"){
  const data=cancellableChat(),composer=data.composer;
  composer.conversation.turns[0].state="failed";
  composer.follow_up_blocker=blocker;
  composer.conversation.follow_up_blocker=blocker;
  composer.can_reset=true;
  data.tasks[0].actions.cancel=action(false);
  return data;
}

function nativeChat(status="started",agent=false){
  const data=cancellableChat(),task=data.tasks[0],composer=data.composer;
  task.state_profile=composer.state_profile="foundry-native-v1";
  delete task.binding_id;delete task.job_id;delete task.work_id;delete task.execution;
  task.native_receipt={status,cancel_requested:false,delivered:false,failure_code:null,hosted_session_id:"native-sid"};
  task.status={data:{state:status},observed_at:when};task.progress=null;task.error=null;
  task.actions.cancel=action(["accepted","started"].includes(status));
  task.actions["read-reply"]=action(status==="completed");
  task.actions["confirm-reply"]=action(status==="completed");
  composer.state_profile="foundry-native-v1";composer.hosted_session_id="native-sid";
  composer.conversation.turns[0].state=status;
  // A turn that ended unknown does not lock the conversation (#679); the server answers the next submit.
  composer.can_follow_up=composer.conversation.can_follow_up=["completed","unknown"].includes(status);
  if(agent){
    task.instruction=null;task.agent_text="Agent original message <script>not markup</script>";
    task.source={kind:"agent_request",message_kind:"request",message_id:task.task_id};
  }
  if(status==="completed")task.reply={data:{text:"完整回覆\n".repeat(9000)},sha256:"a".repeat(64),byte_length:117000};
  return data;
}

// 2026-10-05 slimming (dev v15): while a turn ran the page read the whole conversation (11.5-14.8 s each) and the
// separate reply read came one round after the finish. Now only the running turn is read; its reply comes with it.
async function fireProgress(ui){
  const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===1000);
  assert.ok(timer,"the progress timer is scheduled");
  const before=ui.calls.length;
  ui.timers.delete(timer[0]);await timer[1]();await ui.tick();await ui.tick();
  return ui.calls.slice(before).map(call=>call.url).filter(url=>url.startsWith("/api/instructions"));
}
for(const lane of ["foundry-native-v1","foundry-responses-v1"]){
  test(`a running own turn is read through its status only, never the whole conversation: ${lane}`,async()=>{
    const data=nativeChat("started"),rid=data.tasks[0].task_id;
    data.tasks[0].state_profile=data.composer.state_profile=lane;
    const ui=await open(data);await ui.tick();
    const reads=async()=>(await fireProgress(ui)).filter(url=>!url.endsWith("/stream"));
    assert.deepEqual(await reads(),[`/api/instructions/${rid}/status`]);
    assert.deepEqual(await reads(),[`/api/instructions/${rid}/status`],"no per-turn progress read on top");
    // RTT B: a Responses turn opens its live text once (this offline fetch has no body, so it stops there).
    assert.equal(ui.calls.filter(call=>call.url===`/api/instructions/${rid}/stream`).length,
      lane==="foundry-responses-v1"?1:0);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

function liveStream(){
  let controller;
  const body=new ReadableStream({start(value){controller=value}});
  const send=value=>controller.enqueue(new TextEncoder().encode(
    (Number.isInteger(value.sequence_number)?`id: ${value.sequence_number}\n`:"")+"data: "+JSON.stringify(value)+"\n\n"));
  return {response:new Response(body,{status:200,headers:{"content-type":"text/event-stream"}}),send,
    close:()=>controller.close()};
}
async function liveTurn(){
  const data=nativeChat("started"),rid=data.tasks[0].task_id;
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  const streams=[];
  const ui=await open(data,{streamResponse:url=>{const stream=liveStream();streams.push({url,...stream});return stream.response}});
  await ui.tick();
  return {ui,rid,streams,shown:()=>JSON.stringify(ui.nodes.get("conversation-history").children,
    (key,value)=>key==="events"?undefined:value)};
}
const liveSettle=async ui=>{for(let i=0;i<6;i++)await ui.tick()};

test("live text of a running Responses turn shows commentary as it arrives, marked not saved",async()=>{
  const {ui,rid,streams,shown}=await liveTurn();
  assert.equal(streams.length,1);
  assert.equal(streams[0].url,`/api/instructions/${rid}/stream`);
  const live=streams[0];
  live.send({type:"response.created",sequence_number:0});
  live.send({type:"response.in_progress",sequence_number:1});
  live.send({type:"response.output_item.added",sequence_number:2,item_id:"msg_c",phase:"commentary"});
  live.send({type:"response.output_text.delta",sequence_number:3,item_id:"msg_c",delta:"你好"});
  await liveSettle(ui);
  assert.match(shown(),/你好/);
  assert.match(shown(),/回覆中，尚未確認保存。/);
  live.send({type:"response.output_text.delta",sequence_number:4,item_id:"msg_c",delta:"，世界"});
  await liveSettle(ui);
  assert.match(shown(),/你好，世界/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a second response.in_progress resets live text and other items never show",async()=>{
  const {ui,streams,shown}=await liveTurn();
  const live=streams[0];
  live.send({type:"response.in_progress",sequence_number:1});
  live.send({type:"response.output_item.added",sequence_number:2,item_id:"msg_c",phase:"commentary"});
  live.send({type:"response.output_text.delta",sequence_number:3,item_id:"msg_c",delta:"舊的部分輸出"});
  live.send({type:"response.output_text.delta",sequence_number:4,item_id:"msg_other",delta:"不顯示"});
  await liveSettle(ui);
  assert.match(shown(),/舊的部分輸出/);
  assert.doesNotMatch(shown(),/不顯示/);
  live.send({type:"response.in_progress",sequence_number:5});
  live.send({type:"response.output_text.delta",sequence_number:6,item_id:"msg_c",delta:"新的"});
  await liveSettle(ui);
  assert.doesNotMatch(shown(),/舊的部分輸出/);
  assert.match(shown(),/新的/);
});

test("final_answer ends live text and reads the saved reply at once, which replaces it",async()=>{
  const {ui,rid,streams,shown}=await liveTurn();
  const live=streams[0];
  live.send({type:"response.output_item.added",sequence_number:2,item_id:"msg_c",phase:"commentary"});
  live.send({type:"response.output_text.delta",sequence_number:3,item_id:"msg_c",delta:"預覽文字"});
  await liveSettle(ui);
  const done=nativeChat("completed");
  done.tasks[0].state_profile=done.composer.state_profile="foundry-responses-v1";
  done.tasks[0].reply={data:{text:"保存的回覆"},sha256:"a".repeat(64),byte_length:15,observed_at:when};
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  live.send({type:"response.output_item.added",sequence_number:9,item_id:"msg_f",phase:"final_answer"});
  await liveSettle(ui);
  const now=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
  assert.ok(now,"the saved reply is read at once");
  const before=ui.calls.length;
  ui.timers.delete(now[0]);await now[1]();await liveSettle(ui);
  assert.deepEqual(ui.calls.slice(before).map(call=>call.url).filter(url=>url.startsWith("/api/instructions")),
    [`/api/instructions/${rid}/status`]);
  assert.match(shown(),/保存的回覆/);
  assert.doesNotMatch(shown(),/預覽文字/);
  assert.equal(streams.length,1,"the stream is not reopened after its end");
});

for(const end of ["completed","failed"]){
  test(`a turn that ends ${end} without a reply in hand drops its preview text`,async()=>{
    const {ui,streams,shown}=await liveTurn();
    streams[0].send({type:"response.output_item.added",sequence_number:2,item_id:"msg_c",phase:"commentary"});
    streams[0].send({type:"response.output_text.delta",sequence_number:3,item_id:"msg_c",delta:"預覽文字"});
    await liveSettle(ui);
    assert.match(shown(),/預覽文字/);
    const ended=nativeChat(end);
    ended.tasks[0].state_profile=ended.composer.state_profile="foundry-responses-v1";
    ended.tasks[0].native_receipt.status=end;ended.tasks[0].reply=null;
    ui.render(ended);
    assert.doesNotMatch(shown(),/預覽文字|尚未確認保存/,"the same render that sees the end drops the preview");
    await liveSettle(ui);
    assert.doesNotMatch(shown(),/預覽文字|尚未確認保存/);
  });
}

test("a turn queued past the wait opens its live text once, when its receipt shows it started",async()=>{
  const data=nativeChat("accepted"),rid=data.tasks[0].task_id;
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  const streams=[];let started=false;
  const ui=await open(data,{streamResponse:url=>{
    if(!started)return new Response(JSON.stringify({error:{code:"NATIVE_STREAM_NOT_STARTED",automatic_retry:true}}),{status:409});
    const stream=liveStream();streams.push({url,...stream});return stream.response;
  }});
  await liveSettle(ui);
  const waits=()=>[...ui.timers.entries()].filter(([id])=>ui.timerDelays.get(id)===400);
  for(let i=0;i<25;i++){
    const [wait]=waits();assert.ok(wait,`wait ${i+1} is scheduled`);
    ui.timers.delete(wait[0]);wait[1]();await liveSettle(ui);
  }
  assert.equal(waits().length,0,"the wait is bounded");
  const asked=()=>ui.calls.filter(call=>call.url.startsWith(`/api/instructions/${rid}/stream`)).length;
  const before=asked();
  ui.render(data);await liveSettle(ui);
  assert.equal(asked(),before,"still queued: no new stream");
  started=true;
  const running=nativeChat("started");
  running.tasks[0].state_profile=running.composer.state_profile="foundry-responses-v1";
  ui.render(running);await liveSettle(ui);
  assert.equal(streams.length,1);
  assert.equal(streams[0].url,`/api/instructions/${rid}/stream`);
  ui.render(running);await liveSettle(ui);
  assert.equal(asked(),before+1,"opened once");
});

test("live text waits for a turn that has not started and resumes a dropped stream from its last event",async()=>{
  const data=nativeChat("started"),rid=data.tasks[0].task_id;
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  const streams=[],refusals=["NATIVE_RID_MISSING","NATIVE_STREAM_NOT_STARTED"];
  const ui=await open(data,{streamResponse:url=>{
    if(refusals.length)return new Response(JSON.stringify({error:{code:refusals.shift(),automatic_retry:true}}),{status:409});
    const stream=liveStream();streams.push({url,...stream});return stream.response;
  }});
  await liveSettle(ui);
  for(const _ of [1,2]){
    const wait=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===400);
    assert.ok(wait,"a turn the API has not started is asked again shortly");
    ui.timers.delete(wait[0]);wait[1]();await liveSettle(ui);
  }
  assert.equal(streams[0].url,`/api/instructions/${rid}/stream`);
  streams[0].send({type:"response.output_item.added",sequence_number:2,item_id:"msg_c",phase:"commentary"});
  streams[0].send({type:"response.output_text.delta",sequence_number:7,item_id:"msg_c",delta:"一半"});
  streams[0].close();
  await liveSettle(ui);
  const retry=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===500);
  assert.ok(retry,"a dropped stream is reopened after a short wait");
  ui.timers.delete(retry[0]);retry[1]();await liveSettle(ui);
  assert.equal(streams[1].url,`/api/instructions/${rid}/stream?starting_after=7`);
  streams[1].send({type:"response.output_text.delta",sequence_number:8,item_id:"msg_c",delta:"繼續"});
  await liveSettle(ui);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/一半繼續/);
  streams[1].close();await liveSettle(ui);
  const again=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===500);
  assert.ok(again,"a stream that brought events resumes with a fresh budget");
  ui.timers.delete(again[0]);again[1]();await liveSettle(ui);
  assert.equal(streams[2].url,`/api/instructions/${rid}/stream?starting_after=8`);
  streams[2].close();await liveSettle(ui);
  assert.ok([...ui.timers.entries()].some(([id])=>ui.timerDelays.get(id)===1000),"an empty drop waits longer");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("the reply arrives with the status read that sees the finish, without a separate reply read",async()=>{
  const data=nativeChat("started"),rid=data.tasks[0].task_id;
  const ui=await open(data);await ui.tick();
  const done=nativeChat("completed");
  done.tasks[0].reply={data:{text:"完成的回覆"},sha256:"a".repeat(64),byte_length:15,observed_at:when};
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  assert.deepEqual(await fireProgress(ui),[`/api/instructions/${rid}/status`]);
  await ui.tick();
  const text=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(text,/完成的回覆/);
  assert.equal(ui.calls.some(call=>call.url.endsWith("/reply")),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const end of ["unknown","failed"]){
  test(`a turn that ends ${end} gets one whole-conversation read for continuations and blockers`,async()=>{
    const data=nativeChat("started"),rid=data.tasks[0].task_id;
    const ui=await open(data);await ui.tick();
    const ended=nativeChat(end);ended.tasks[0].native_receipt.status=end;ended.tasks[0].reply=null;
    ui.holder.progressResponse={ok:true,json:async()=>copy(ended)};
    assert.deepEqual(await fireProgress(ui),[`/api/instructions/${rid}/status`]);
    const next=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
    assert.ok(next,"one whole read follows at once");
    const before=ui.calls.length;
    ui.timers.delete(next[0]);await next[1]();await ui.tick();
    assert.ok(ui.calls.slice(before).some(call=>call.url.startsWith("/api/instructions/progress")));
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

// fdbb on dev, 2026-10-05: after the workbench bridge restarts (every stage deploy starts a new container) the
// selected old conversation said 「尚未取得原對話的接續狀態」 for minutes and sent no read at all.
// fdbb, dev v15 2026-10-06 09:20 (main 46910b28): after a bridge restart the selected new conversation (finished, one
// turn) sent /reply, /progress, /environment and /status together and then showed 「尚未取得原對話的接續狀態」 for
// over three minutes with no further read; the bridge answered the same /status with can_follow_up=true at 09:24.
function restartedFinished(){
  const data=nativeChat("completed"),task=data.tasks[0],composer=data.composer;
  task.state_profile=composer.state_profile="foundry-responses-v1";task.reply=null;
  task.actions["read-progress"]=action(true);
  composer.environment_policy="fa-environment-system-policy-20261004";
  Object.assign(composer,{observed_at:null,can_follow_up:false,follow_up_blocker:null,editable:false});
  composer.conversation.can_follow_up=false;
  const answered=(observed,follow)=>{
    const value=nativeChat("completed"),t=value.tasks[0];
    t.state_profile=value.composer.state_profile="foundry-responses-v1";t.actions["read-progress"]=action(true);
    t.reply={data:{text:"重啟前的回覆"},sha256:"a".repeat(64),byte_length:18,observed_at:when};
    value.composer.environment_policy=composer.environment_policy;
    Object.assign(value.composer,{observed_at:observed,can_follow_up:follow,follow_up_blocker:null,editable:false});
    value.composer.conversation.can_follow_up=follow;
    return value;
  };
  return {data,answered};
}
async function drain(ui,rounds=8){
  for(let round=0;round<rounds;round++){
    for(const [id,fn] of [...ui.timers]){ui.timers.delete(id);await fn();await ui.tick();await ui.tick()}
    await ui.tick();
  }
}
// Neal 2026-10-06 09:58: 「1. 為什麼會同時發出4個讀取? 2. FA portal為什麼要控制user能否傳送訊息、能否續問? 有遵守我們的原則嗎」
// The workbench is a thin client: the owner may always write and the server answers each submit; opening a conversation
// reads only its latest turn, once.
function restartedConversation(turns=3){
  const {data,answered}=restartedFinished(),base=data.tasks[0];
  data.tasks=[];data.composer.conversation.turns=[];
  for(let i=0;i<turns;i++){
    const task=copy(base);task.task_id=task.submit_request_id="rid-"+i;task.instruction="q"+i;task.reply=null;
    data.tasks.push(task);data.composer.conversation.turns.push({request_id:task.task_id,state:"completed"});
  }
  data.composer.request_id=data.tasks.at(-1).task_id;
  // What the bridge now says after a restart: not read yet, and the owner may write at once.
  data.composer.can_follow_up=data.composer.conversation.can_follow_up=true;
  return {data,answered};
}
// Like a real IntersectionObserver: every turn that came on screen together is reported in one callback.
function seeTurns(ui,rids){
  for(const observer of ui.holder.observers){
    const entries=[...observer.nodes].filter(node=>rids.includes(node.faTurnId)).map(target=>({isIntersecting:true,target}));
    if(entries.length)observer.callback(entries);
  }
}
test("after a bridge restart any conversation can be written to at once, and opening it sends one read",async()=>{
  const {data,answered}=restartedConversation(3),rid="rid-2";
  const ready=answered("2026-01-01T00:00:02.000000+00:00",true);
  ready.tasks=copy(data.tasks);ready.tasks[2].reply={data:{text:"最新一則的回覆"},sha256:"a".repeat(64),byte_length:21,observed_at:when};
  ready.composer.request_id=rid;ready.composer.conversation.turns=copy(data.composer.conversation.turns);
  let finish;
  const ui=await open(data,{progressResponse:new Promise(resolve=>{finish=resolve})});
  assert.equal(ui.nodes.get("instruction").disabled,false,"writable before anything is read");
  typeAttachmentText(ui,"重啟後直接送");
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  const reads=()=>ui.calls.filter(call=>call.method!=="POST").map(call=>call.url).filter(url=>url.startsWith("/api/")&&
    !url.startsWith("/api/state")&&!url.startsWith("/api/agents"));
  const first=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
  ui.timers.delete(first[0]);first[1]();await ui.tick();
  assert.deepEqual(reads(),[`/api/instructions/${rid}/status`],"one read: the latest turn's status");
  finish({ok:true,json:async()=>copy(ready)});await ui.tick();await ui.tick();await ui.tick();
  for(const [id,fn] of [...ui.timers]){ui.timers.delete(id);await fn();await ui.tick()}
  assert.deepEqual(reads(),[`/api/instructions/${rid}/status`],"no reply, progress, environment or whole read on open");
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/最新一則的回覆/);
  assertNothingSent(ui);
});

test("older turns load their reply as they scroll into view, newest first and two at a time",async()=>{
  const {data}=restartedConversation(5);
  data.composer.observed_at=when;
  const finishes=[];
  // Each turn's /status is held until the test answers it.
  const ui=await open(data,{progressResponse:()=>new Promise(resolve=>finishes.push(resolve))});
  const statusReads=()=>ui.calls.map(call=>/^\/api\/instructions\/(rid-\d)\/status$/.exec(call.url)?.[1]).filter(Boolean);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/正在載入回覆/,"not a row of blank replies");
  assert.deepEqual(statusReads(),[],"nothing is read until a turn is on screen");
  seeTurns(ui,["rid-0","rid-1","rid-2","rid-3"]);
  await ui.tick();
  assert.deepEqual(statusReads(),["rid-3","rid-2"],"newest first, two at a time");
  const answer=rid=>{const value=copy(data);const task=value.tasks.find(t=>t.task_id===rid);
    task.reply={data:{text:"回覆 "+rid},sha256:"a".repeat(64),byte_length:9,observed_at:when};return {ok:true,json:async()=>value}};
  finishes[0](answer("rid-3"));await ui.tick();await ui.tick();
  assert.deepEqual(statusReads(),["rid-3","rid-2","rid-1"]);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/回覆 rid-3/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// stage 2026-10-10 15:13-15:24: while a turn waited for an approval the page sent one progress read and then none, also
// after a reload, so the pending card and the final state never showed. A read that does not come back no longer holds
// every later read.
test("a running turn's status read that never comes back stops holding the next reads",async()=>{
  const data=nativeChat("started"),rid=data.tasks[0].task_id;
  let answer=new Promise(()=>{});
  const ui=await open(data,{progressResponse:()=>answer});await ui.tick();
  const statusReads=()=>ui.calls.filter(call=>call.url===`/api/instructions/${rid}/status`).length;
  const fire=async delay=>{const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===delay);
    assert.ok(timer,`a ${delay} ms timer is scheduled`);ui.timers.delete(timer[0]);timer[1]();for(let i=0;i<4;i++)await ui.tick()};
  await fire(1000);
  assert.equal(statusReads(),1);
  await fire(45000);
  assert.equal(ui.nodes.get("refresh-progress").disabled,false,"更新回覆 works again");
  assert.match(ui.text("error-message"),/暫時讀不到進度，正在重試/);
  const done=nativeChat("completed");
  done.tasks[0].reply={data:{text:"逾時後讀到的回覆"},sha256:"a".repeat(64),byte_length:24,observed_at:when};
  answer={ok:true,json:async()=>copy(done)};
  await fire(2000);
  assert.equal(statusReads(),2,"the next read goes out after the usual backoff");
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value),
    /逾時後讀到的回覆/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false,"nothing is sent");
});

// stage/dev 2026-10-12: completed replies showed 「正在載入回覆…」 for good. That turn's /status read had failed (or came
// back without the reply), the bubble was not drawn again, and nothing read it later; 「更新回覆」 did not either.
for(const outcome of ["failed","no-reply"]){
  test(`an older turn whose reply read ${outcome==="failed"?"fails":"brings no reply"} stops saying it is loading, and 更新回覆 reads it again`,async()=>{
    const {data}=restartedConversation(2);
    data.composer.observed_at=when;
    const finishes=[];
    const ui=await open(data,{progressResponse:url=>/\/status$/.test(url)?new Promise(resolve=>finishes.push(resolve)):
      {ok:true,json:async()=>copy(data)}});
    // Only rid-0's turn (the first one); rid-1 is not on screen and keeps waiting for its own read.
    const shown=()=>JSON.stringify(ui.nodes.get("conversation-history").children[0],(key,value)=>key==="events"?undefined:value);
    const statusReads=()=>ui.calls.filter(call=>call.url==="/api/instructions/rid-0/status").length;
    seeTurns(ui,["rid-0"]);await ui.tick();
    assert.equal(statusReads(),1);
    assert.match(shown(),/正在載入回覆/);
    finishes.shift()(outcome==="failed"?{ok:false,status:502,json:async()=>({error:{code:"NATIVE_RESULT_INCOMPLETE"}})}:
      {ok:true,json:async()=>{const value=copy(data);value.tasks[0].error={code:"NATIVE_RESULT_INCOMPLETE"};return value}});
    for(let i=0;i<4;i++)await ui.tick();
    assert.doesNotMatch(shown(),/正在載入回覆/,"the bubble no longer says it is loading");
    assert.match(shown(),/尚未載入回覆，可從選單更新。/);
    assert.equal(statusReads(),1,"not read again by itself");
    ui.click("refresh-progress");
    for(let i=0;i<4;i++)await ui.tick();
    seeTurns(ui,["rid-0"]);await ui.tick();
    assert.equal(statusReads(),2,"更新回覆 reads that turn's saved reply again");
    const value=copy(data);value.tasks[0].reply={data:{text:"補讀到的回覆"},sha256:"a".repeat(64),byte_length:18,observed_at:when};
    finishes.shift()({ok:true,json:async()=>value});
    for(let i=0;i<4;i++)await ui.tick();
    assert.match(shown(),/補讀到的回覆/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false,"nothing is sent");
  });
}

test("a turn that ended unknown does not block a new message, and that turn is never sent again",async()=>{
  const data=nativeChat("unknown"),composer=data.composer,old=data.tasks[0].task_id;
  Object.assign(composer,{follow_up_blocker:null,can_follow_up:true,editable:false,observed_at:when});
  composer.conversation.can_follow_up=true;
  const ui=await open(data);
  assert.equal(ui.nodes.get("instruction").disabled,false);
  typeAttachmentText(ui,"接著問一個新問題");
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.holder.pendingPosts[0].url,`/api/instructions/${composer.thread_id}/next`);
  const next=copy(data);
  Object.assign(next.composer,{request_id:"new-rid",editable:true,can_submit:true,content:null});
  ui.holder.pendingPosts[0].resolve(next);await ui.tick();await ui.tick();
  assert.equal(ui.holder.pendingPosts[1].url,"/api/instructions/new-rid/submit");
  assert.equal(ui.calls.some(call=>call.url.includes(old)&&call.method==="POST"),false,"the unknown turn is not resent");
});

test("a message the server refused as busy keeps its text for sending again",async()=>{
  const data=nativeChat("completed"),composer=data.composer;
  composer.state_profile=data.tasks[0].state_profile="foundry-responses-v1";
  Object.assign(composer,{request_id:"draft-rid",editable:true,can_submit:true,content:null,observed_at:when});
  const ui=await open(data);
  typeAttachmentText(ui,"正在忙時送出的訊息");
  ui.click("send-instruction");await ui.tick();
  const busy=copy(data.tasks[0]);
  Object.assign(busy,{task_id:"draft-rid",submit_request_id:"draft-rid",instruction:"正在忙時送出的訊息",reply:null,
    submission:{state:"CONFIRMED"}});
  busy.native_receipt={...busy.native_receipt,status:"busy_refused"};busy.status={data:{state:"busy_refused"}};
  ui.holder.pendingPosts[0].resolve({task:busy});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"正在忙時送出的訊息","the server did not run it; the text stays");
});

test("a message whose send ended unknown leaves the composer, so it is never one click from a new RID",async()=>{
  // #679 review: with the composer always open, an UNKNOWN send left its text in place and one more click sent it
  // again under a new RID. The text stays in its own turn instead (AGENTS.md 7: never resend an unknown request).
  const data=nativeChat("completed"),composer=data.composer;
  Object.assign(composer,{request_id:"draft-rid",editable:true,can_submit:true,content:null,observed_at:when});
  const ui=await open(data);
  typeAttachmentText(ui,"結果未知的訊息");
  const after=copy(data),sent=copy(data.tasks[0]);
  Object.assign(sent,{task_id:"draft-rid",submit_request_id:"draft-rid",instruction:"結果未知的訊息",reply:null,
    native_receipt:null,submission:{state:"UNKNOWN",code:"BACKEND_UNAVAILABLE"},status:{data:{state:"unknown"}}});
  after.tasks.push(sent);
  Object.assign(after.composer,{request_id:"draft-rid",editable:false,can_submit:false,content:"結果未知的訊息",
    can_follow_up:true});
  ui.holder.current=after;
  ui.holder.response={ok:false,status:503,json:async()=>({error:{code:"BACKEND_UNAVAILABLE"}})};
  ui.click("send-instruction");
  for(let i=0;i<5;i++)await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),["/api/instructions/draft-rid/submit"]);
  assert.equal(ui.nodes.get("instruction").value,"","the sent text left the composer");
  assert.equal(ui.nodes.get("send-instruction").disabled,true,"nothing to send again with one click");
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/結果未知的訊息/,"the text stays in its turn");
  typeAttachmentText(ui,"另一個新問題");
  assert.equal(ui.nodes.get("send-instruction").disabled,false,"different text is a new message");
});

test("a refusal that is final for its RID offers no same-RID resend and says the text can be sent again",async()=>{
  // #679 review round 3: only a version refusal goes out again with the same RID; the bridge refuses any other.
  const data=nativeChat("completed"),final=copy(data.tasks[0]);
  Object.assign(final,{task_id:"refused-rid",submit_request_id:"refused-rid",instruction:"附了不能附的來源",reply:null,
    native_receipt:null,admission:null,not_accepted:true,status:{data:{state:"not_accepted"}},
    submission:{state:"REFUSED",code:"ATTACHMENTS_DISABLED",not_dispatched:true}});
  final.actions["submit-retry"]=action(false);
  data.tasks.push(final);
  Object.assign(data.composer,{request_id:"refused-rid",editable:false,can_submit:false,content:"附了不能附的來源",
    can_follow_up:true,observed_at:when});
  const ui=await open(data),shown=JSON.stringify(ui.nodes.get("conversation-history").children);
  assert.match(shown,/這則訊息沒有送達，助理沒有執行.*可以修改後重新送出/);
  assert.doesNotMatch(shown,/以原內容重送/,"no same-RID resend button");
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a same-RID retry whose answer is unknown also leaves the composer; a refusal still waiting for it keeps the text",async()=>{
  // #679 review round 2: the version resend (submit-retry) journals its outcome apart from the first submit, so the
  // first rule (submission UNKNOWN) missed it and the text was one click from a new RID.
  const base=nativeChat("completed"),sent=copy(base.tasks[0]);
  Object.assign(sent,{task_id:"retry-rid",submit_request_id:"retry-rid",instruction:"換版重送的訊息",reply:null,
    native_receipt:null,admission:null,status:{data:{state:"unknown"}},
    submission:{state:"REFUSED",code:"AGENT_VERSION_FOLLOWING"}});
  const shaped=notAccepted=>{
    const data=copy(base);data.tasks.push({...copy(sent),not_accepted:notAccepted});
    Object.assign(data.composer,{request_id:"retry-rid",editable:false,can_submit:false,content:"換版重送的訊息",
      can_follow_up:true,observed_at:when});
    return data;
  };
  const ui=await open(shaped(true));
  typeAttachmentText(ui,"換版重送的訊息");ui.render(shaped(true));
  assert.equal(ui.nodes.get("instruction").value,"換版重送的訊息","not accepted yet: it goes out again with the same RID");
  ui.render(shaped(false));
  assert.equal(ui.nodes.get("instruction").value,"","the retry's answer is unknown: the text leaves the composer");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("steps the page opens by itself are not the owner's click: no read, and they close when the turn ends",async()=>{
  const data=nativeChat("accepted"),task=data.tasks[0];
  task.state_profile=data.composer.state_profile="foundry-responses-v1";
  task.native_receipt.dispatch={state:"needs_response",inflight_request_id:task.task_id};
  task.native_receipt.response_id=null;task.error={code:"NATIVE_RESPONSE_UNCONFIRMED"};
  task.response_reconciliation={state:"waiting",failures:1};task.actions["read-progress"]=action(true);
  const ui=await open(data);
  assert.equal(inlineProcessing(ui).open,true,"a just-sent turn waiting for its handoff shows its steps");
  inlineProcessing(ui).events.toggle?.();await ui.tick();await ui.tick(); // a browser fires this after code sets open
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/progress")).length,0,"no read the owner did not ask for");
  const done=nativeChat("completed");done.tasks[0].state_profile=done.composer.state_profile="foundry-responses-v1";
  ui.render(done);
  assert.equal(inlineProcessing(ui).open,false,"closed again once the turn has ended");
});

test("older turns on screen are read after the opening read of the latest turn, not together with it",async()=>{
  const {data}=restartedConversation(4);
  const finishes=[];
  const ui=await open(data,{progressResponse:()=>new Promise(resolve=>finishes.push(resolve))});
  const statusReads=()=>ui.calls.map(call=>/^\/api\/instructions\/(rid-\d)\/status$/.exec(call.url)?.[1]).filter(Boolean);
  seeTurns(ui,["rid-0","rid-1","rid-2"]);await ui.tick();
  const first=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
  ui.timers.delete(first[0]);first[1]();await ui.tick();
  assert.deepEqual(statusReads(),["rid-3"],"opening reads only the latest turn");
  const ready=copy(data);ready.composer.observed_at=when;
  ready.tasks[3].reply={data:{text:"最新一則"},sha256:"a".repeat(64),byte_length:12,observed_at:when};
  finishes[0]({ok:true,json:async()=>ready});
  for(let i=0;i<4;i++)await ui.tick();
  assert.deepEqual(statusReads(),["rid-3","rid-2","rid-1"],"then the older turns on screen, newest first, two at a time");
});

test("expanding a finished turn's steps reads its progress once",async()=>{
  const data=nativeChat("completed");data.tasks[0].actions["read-progress"]=action(true);data.composer.observed_at=when;
  const snapshot=liveProcessing().tasks[0].progress.data.progress;
  const ui=await open(data,{nativeProgressResponse:{ok:true,json:async()=>({request_id:data.tasks[0].task_id,progress:{
    data:{state:"completed",progress:snapshot},recorded:true,finished:true,truncated:false,byte_length:700}})}});
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/progress")).length,0,"nothing on open");
  const panel=inlineProcessing(ui);panel.children[0].click();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/progress")).length,1);
  assert.match(processingText(inlineProcessing(ui)),/使用工具：網路搜尋/);
  assert.equal(inlineProcessing(ui).open,true,"stays open once loaded");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("waiting for a peer's reply keeps the whole-conversation read that brings it in",async()=>{
  const data=nativeChat("started");data.composer.awaiting_agent_reply=true;
  const ui=await open(data);await ui.tick();
  const reads=await fireProgress(ui);
  assert.ok(reads.length>=1&&reads[0].startsWith("/api/instructions/progress"));
  assert.equal(reads.some(url=>url.endsWith("/status")),false);
});

for(const lane of ["foundry-native-v1","foundry-responses-v1"]){
  test(`completed progress automatically loads the original reply on a new Agent thread: ${lane}`,async()=>{
    const data=nativeChat("started"),scope=agentChat("agent-one");
    for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
      data[key]=scope[key];
    data.tasks[0].state_profile=data.composer.state_profile=lane;
    data.tasks[0].thread_id=data.composer.thread_id="new-native-thread";
    const ui=await open(data);
    const done=copy(data),task=done.tasks[0];
    task.native_receipt.status=task.status.data.state="completed";
    task.actions["read-reply"]=task.actions["confirm-reply"]=action(true);
    task.reply=null;
    task.progress={recorded:true,finished:true,truncated:false,byte_length:0,data:{state:"completed"}};
    done.composer.conversation.turns[0].state="completed";
    done.composer.can_follow_up=done.composer.conversation.can_follow_up=true;
    done.composer.progress_available=false;
    const loaded=copy(done);
    loaded.tasks[0].reply={data:{text:"結論：輸出是 `ok-offline`。"},observed_at:when};
    ui.holder.current=loaded;
    ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
    await vm.runInContext("updateProgress()",ui.context);
    await ui.tick();await ui.tick();
    const replies=ui.calls.filter(call=>call.url===`/api/instructions/${task.task_id}/reply`);
    assert.equal(replies.length,1,"Completed progress must fetch the saved reply without a menu click");
    assert.equal(replies[0].headers["X-Console-Agent"],"agent-one");
    const text=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
    assert.match(text,/ok-offline/);
    assert.doesNotMatch(text,/尚未載入回覆/);
    ui.render(loaded);await ui.tick();
    assert.equal(ui.calls.filter(call=>call.url===replies[0].url).length,1);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("automatic reply failure stays visible and only a manual read retries it",async()=>{
  const data=nativeChat("completed");data.tasks[0].reply=null;
  const ui=await open(data,{replyResponse:{ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})}});
  await ui.tick();
  ui.render(data);await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply")).length,1);
  assert.equal(ui.nodes.get("error").hidden,false);
  const loaded=copy(data);loaded.tasks[0].reply={data:{text:"原回覆已讀回"},observed_at:when};
  ui.holder.replyResponse={ok:true,json:async()=>copy(loaded)};
  ui.click("fetch");await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply")).length,2);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/原回覆已讀回/);
});

test("late progress cannot erase an automatically loaded reply",async()=>{
  const data=nativeChat("started"),done=nativeChat("completed");done.tasks[0].reply=null;
  let finishReply,finishProgress;
  const ui=await open(data,{replyResponse:()=>new Promise(resolve=>{finishReply=resolve})});
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  await vm.runInContext("updateProgress()",ui.context);await ui.tick();
  ui.holder.progressResponse=new Promise(resolve=>{finishProgress=resolve});
  const reading=vm.runInContext("updateProgress()",ui.context);await ui.tick();
  const loaded=copy(done);loaded.tasks[0].reply={data:{text:"保留原答案"},observed_at:when};
  finishReply({ok:true,json:async()=>copy(loaded)});await ui.tick();await ui.tick();
  finishProgress({ok:true,json:async()=>copy(done)});await reading;await ui.tick();
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/保留原答案/);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply")).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a Responses turn that completes while hidden loads its reply when the tab becomes visible",async()=>{
  const data=nativeChat("started");
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  let finish;
  const ui=await open(data);
  ui.holder.progressResponse=new Promise(resolve=>{finish=resolve});
  const reading=vm.runInContext("updateProgress()",ui.context);await ui.tick();
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  const done=copy(data),task=done.tasks[0];
  task.native_receipt.status=task.status.data.state="completed";
  task.actions["read-reply"]=action(true);
  done.composer.conversation.turns[0].state="completed";
  done.composer.can_follow_up=done.composer.conversation.can_follow_up=true;
  const loaded=copy(done);loaded.tasks[0].reply={data:{text:"背景完成的原答案"},observed_at:when};
  ui.holder.current=loaded;
  finish({ok:true,json:async()=>copy(done)});await reading;await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply")).length,0);
  ui.context.document.hidden=false;ui.documentEvents.visibilitychange();
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply")).length,1);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/背景完成的原答案/);
  assert.equal(ui.nodes.get("fetch").disabled,false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const changed of ["agent","thread","login"]){
  test(`an automatic reply from the previous ${changed} scope never replaces the current view`,async()=>{
    const data=nativeChat("completed"),scope=agentChat("agent-one");data.tasks[0].reply=null;
    for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
      data[key]=scope[key];
    const other=copy(data);other.tasks=[];other.composer.request_id="other-draft";
    if(changed==="agent")other.selected_agent_id="agent-two";
    if(changed==="thread")other.composer.thread_id="other-thread";
    if(changed==="login")other.authentication="LOGIN_EXPIRED";
    let finish;
    const ui=await open(data,{replyResponse:()=>new Promise(resolve=>{finish=resolve}),
      agentStates:{"agent-one":data,"agent-two":other}});
    if(changed==="agent"){
      sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
    }else ui.render(other);
    ui.nodes.get("instruction").value="目前未送草稿";ui.nodes.get("instruction").events.input();
    const loaded=copy(data);loaded.tasks[0].reply={data:{text:"舊範圍回覆不可採用"},observed_at:when};
    finish({ok:true,json:async()=>loaded});await ui.tick();await ui.tick();
    assert.doesNotMatch(JSON.stringify(ui.nodes.get("conversation-history").children),/舊範圍回覆不可採用/);
    assert.equal(ui.nodes.get("instruction").value,"目前未送草稿");
    assertNothingSent(ui);
  });
}

function appendChat(){
  const data=nativeChat("started"),rid=data.composer.request_id;
  data.composer.can_append=true;
  data.composer.append_target={request_id:rid,append_id:"append-offline-a",pending:false};
  data.tasks[0].appends=[];
  data.tasks[0].actions["read-appends"]=action(true);
  return data;
}

test("a completed Responses handoff keeps observing until the peer reply appears on the original thread",async()=>{
  const data=nativeChat("completed"),scope=agentChat("agent-one"),first=data.tasks[0];
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
    data[key]=scope[key];
  first.state_profile=data.composer.state_profile="foundry-responses-v1";
  first.reply.data.text="已交給另一位助理，結果會回到本對話。";
  data.composer.awaiting_agent_reply=true;
  const ui=await open(data);
  const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===1000);
  assert.ok(timer,"A saved handoff reply must not stop observation of the expected peer return");
  const returned=copy(first);
  Object.assign(returned,{task_id:"peer-return",submit_request_id:"peer-return",instruction:null,agent_text:"收到",
    source:{kind:"agent_result",sender:{agent_id:"agent-two"},recipient_agent_id:"agent-one"},
    reply:{data:{text:"B 回覆：收到。"},observed_at:when},admission:{...first.admission,turn_number:2}});
  const done=copy(data);done.tasks.push(returned);done.composer.awaiting_agent_reply=false;
  done.composer.conversation.turns.push({request_id:returned.task_id,state:"completed"});
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  ui.timers.delete(timer[0]);await timer[1]();await ui.tick();
  const text=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(text,/第二位助理.*回覆/);
  assert.match(text,/B 回覆：收到/);
  assert.equal([...ui.timers.keys()].some(id=>ui.timerDelays.get(id)===1000),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Audit #13 round 2 (2026-10-14): a full read while a delegated reply is awaited costs 3+N API reads and the wait has
// no limit, but backing off from the first unchanged read showed the reply 4-8 s late (release 30 dev test). Unchanged
// reads keep the usual pace for 30 s, then double up to 5 s whether push is open or not.
const UNAVAILABLE_READ=()=>({ok:false,status:503,json:async()=>({error:{code:"NATIVE_BOOTSTRAP_UNKNOWN",automatic_retry:true}})});
async function awaitingReplyUi(options={}){
  const data=nativeChat("started");data.composer.awaiting_agent_reply=true;
  const ui=await open(data,{now:Date.parse(when),...options});await ui.tick();
  if(options.realtime)await connectPush(ui);
  const fire=async()=>{
    const id=vm.runInContext("progressTimer",ui.context),fn=ui.timers.get(id);
    assert.ok(fn,"the progress timer is scheduled");
    ui.timers.delete(id);ui.timerDelays.delete(id);await fn();await ui.tick();await ui.tick();
  };
  ui.holder.progressResponse=()=>({ok:true,json:async()=>copy(data)});
  const delay=()=>ui.timerDelays.get(vm.runInContext("progressTimer",ui.context));
  return {ui,data,fire,delay};
}
test("waiting for a delegated reply reads unchanged conversations every second for 30 s, then backs off to 5 s",async()=>{
  for(const realtime of [false,true]){
    const {ui,data,fire,delay}=await awaitingReplyUi({realtime});
    let clock=Date.parse(when),reply=copy(data),i=0;
    ui.holder.now=clock;
    ui.holder.progressResponse=()=>({ok:true,json:async()=>copy(reply)});
    const run=async count=>{
      const delays=[];
      for(let n=0;n<count;n++){
        // A read's own timestamps are not a change.
        reply=copy(reply);reply.composer.observed_at=reply.tasks[0].status.observed_at=new Date(clock).toISOString();
        clock+=delay();ui.holder.now=clock;i++;
        await fire();delays.push(delay());
      }
      return delays;
    };
    const usual=realtime?3000:1000,within=realtime?11:30;
    const backoff=realtime?[4000,5000,5000]:[2000,4000,5000,5000];
    assert.deepEqual(await run(within+backoff.length),[...Array(within).fill(usual),...backoff],"push open: "+realtime);
    reply=copy(reply);reply.tasks[0].progress={recorded:true,finished:false,truncated:false,byte_length:1,data:{state:"started"}};
    assert.deepEqual(await run(1),[usual],"a change reads at the usual pace again");
    assert.deepEqual(await run(2),[usual,usual],"and the 30 s start over");
    await run(within+backoff.length);
    assert.equal(delay(),5000);
    // A push hint reads at once and starts the 30 s over.
    vm.runInContext("realtimeReadProgress()",ui.context);await ui.tick();await ui.tick();
    assert.equal(delay(),usual);
    assert.deepEqual(await run(1),[usual]);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  }
});

// Audit round 1 (2026-10-14): a long (error) wait must not restart on every render, and must be short without push.
test("a render in the middle of a long wait does not push the next progress read further out",async()=>{
  const {ui,fire,delay}=await awaitingReplyUi({realtime:true});
  let clock=Date.parse(when);
  const tick=ms=>{clock+=ms;ui.holder.now=clock};
  ui.holder.now=clock;
  ui.holder.progressResponse=UNAVAILABLE_READ;
  for(let i=0;i<8;i++)await fire();
  assert.equal(delay(),30000,"the wait reached 30 s");
  const timer=vm.runInContext("progressTimer",ui.context);
  tick(29000);
  vm.runInContext("render()",ui.context);
  assert.equal(vm.runInContext("progressTimer",ui.context),timer,"the render keeps the timer that is already due in 1 s");
  assert.equal(ui.timers.has(timer),true);
  tick(1000);
  const before=ui.calls.length;
  await fire();
  assert.ok(ui.calls.length>before,"the read goes out at the original 30 s");
});
test("a tab that becomes visible reads within 1 s instead of waiting out the backoff",async()=>{
  for(const realtime of [false,true]){
    const {ui,fire,delay}=await awaitingReplyUi({realtime});
    ui.holder.progressResponse=UNAVAILABLE_READ;
    for(let i=0;i<8;i++)await fire();
    assert.ok(delay()>=(realtime?30000:5000));
    ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
    assert.equal(vm.runInContext("progressTimer",ui.context),null,"nothing is read while hidden");
    ui.context.document.hidden=false;ui.documentEvents.visibilitychange();await ui.tick();
    assert.ok(delay()<=1000,"visible again, push open: "+realtime);
    assert.equal(vm.runInContext("progressRetryMs",ui.context),1000);
  }
});
test("without push a waiting conversation is read at least every 5 s",async()=>{
  const {ui,fire,delay}=await awaitingReplyUi();
  ui.holder.progressResponse=UNAVAILABLE_READ;
  const delays=[];
  for(let i=0;i<8;i++){await fire();delays.push(delay())}
  assert.equal(Math.max(...delays),5000,JSON.stringify(delays));
});
test("the first good read after a failed one starts the backoff over from 1 s",async()=>{
  const {ui,data,fire}=await awaitingReplyUi({realtime:true});
  ui.holder.progressResponse=UNAVAILABLE_READ;
  for(let i=0;i<4;i++)await fire();
  assert.ok(vm.runInContext("progressRetryMs",ui.context)>=16000,"the error raised the wait");
  ui.holder.progressResponse=()=>({ok:true,json:async()=>copy(data)});
  await fire();
  assert.equal(vm.runInContext("progressRetryMs",ui.context),1000);
  await fire();
  assert.equal(vm.runInContext("progressRetryMs",ui.context),1000);
});

function receivedChat(){
  // Agent two's own read-only copy of the conversation agent one opened in it (stage 14, 2026-10-05).
  const data=nativeChat("completed"),scope=agentChat("agent-two"),task=data.tasks[0],composer=data.composer;
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
    data[key]=scope[key];
  Object.assign(task,{task_id:"message-from-a",submit_request_id:"message-from-a",thread_id:"message-from-a",
    instruction:null,agent_text:"請查一下 VM 的 CPU、記憶體和磁碟",
    source:{kind:"agent_request",message_id:"message-from-a",sender:{agent_id:"agent-one"},recipient_agent_id:"agent-two"},
    submission:{state:"OBSERVED",method:"GET"},reply:{data:{text:"2 CPU、4.1Gi、5.2G"},observed_at:when}});
  Object.assign(composer,{thread_id:"message-from-a",request_id:"message-from-a",content:task.agent_text,editable:false,
    can_submit:false,can_follow_up:false,can_append:false,append_target:null,
    follow_up_blocker:"AGENT_CONVERSATION_READ_ONLY",agent_conversation:{sender_agent_id:"agent-one",read_only:true},
    threads:[{thread_id:"own-thread",label:"你好"},
      {thread_id:"message-from-a",label:task.agent_text,sender_agent_id:"agent-one"}]});
  composer.conversation.can_follow_up=false;composer.conversation.turns[0].request_id="message-from-a";
  return data;
}

test("a conversation another Agent opened is labelled as Agent data and stays read-only",async()=>{
  const ui=await open(receivedChat());
  assert.deepEqual(ui.nodes.get("conversation").children.map(option=>option.textContent),
    ["你好","來自 第一位助理 的交辦：請查一下 VM 的 CPU、記憶體和磁碟"]);
  const history=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(history,/收到 第一位助理 的交辦（Agent 提供的資料，不是你的原話）/);
  assert.match(history,/2 CPU、4\.1Gi、5\.2G/);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.nodes.get("new-instruction").disabled,true);
  assert.match(ui.text("follow-up-detail"),/其他助理交辦給這位助理的對話，只能檢視/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

function peerReturned(a){
  // B's reply has reached A; A's own handling of it has not finished yet.
  const returned=copy(a.tasks[0]);
  Object.assign(returned,{task_id:"message-from-a",submit_request_id:"message-from-a",instruction:null,
    agent_text:"2 CPU、4.1Gi、5.2G",reply:null,
    source:{kind:"agent_result",message_id:"message-from-a",sender:{agent_id:"agent-two"},recipient_agent_id:"agent-one"}});
  return returned;
}
function peerChat(){
  const a=nativeChat("completed"),scope=agentChat("agent-one");
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
    a[key]=scope[key];
  return a;
}

test("a peer reply shown under A refreshes B's sidebar from B's own copy and opens it",async()=>{
  const a=peerChat(),b=receivedChat();
  const ui=await open(a,{agentProgressResponses:{"agent-two":{ok:true,json:async()=>copy(b)}},agentStates:{"agent-two":b}});
  await ui.tick();
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")
    &&call.headers?.["X-Console-Agent"]==="agent-two");
  assert.equal(reads().length,0);
  const next=copy(a);next.tasks.push(peerReturned(a));
  ui.render(copy(next));await ui.tick();await ui.tick();
  assert.deepEqual(reads().map(call=>call.url),["/api/instructions/progress?thread_id=message-from-a"]);
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"助理：2 CPU、4.1Gi、5.2G");
  assert.equal(sidebar(ui,"agent-two").badge.textContent,"新動態");
  ui.render(copy(next));await ui.tick();
  assert.equal(ui.calls.filter(call=>call.headers?.["X-Console-Agent"]==="agent-two").length,1);
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.ok(ui.calls.some(call=>call.url==="/api/instructions/models?thread_id=message-from-a"
    &&call.headers?.["X-Console-Agent"]==="agent-two"));
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a Responses-lane peer preview reads B's saved reply once instead of showing A's brief",async()=>{
  // dev v15 (main 601aca90): B's turn is on the Responses lane, whose progress read carries no saved answer,
  // so B's preview showed A's handoff text. The preview reads B's reply through B's own /reply, once.
  const a=peerChat(),b=receivedChat(),answered=receivedChat();
  for(const data of [b,answered])data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  b.tasks[0].reply=null;
  const peerReads=[];
  let ui;
  ui=await open(a,{agentProgressResponses:{"agent-two":{ok:true,json:async()=>copy(b)}},agentStates:{"agent-two":b},
    replyResponse:(url,options)=>{
      if(options.headers?.["X-Console-Agent"]!=="agent-two")return {ok:true,json:async()=>copy(ui?.holder.current??a)};
      peerReads.push(url);return {ok:true,json:async()=>copy(answered)};
    }});
  await ui.tick();
  const next=copy(a);next.tasks.push(peerReturned(a));
  ui.render(copy(next));await ui.tick();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"助理：2 CPU、4.1Gi、5.2G");
  assert.deepEqual(ui.calls.filter(call=>call.headers?.["X-Console-Agent"]==="agent-two").map(call=>call.url),[
    "/api/instructions/progress?thread_id=message-from-a","/api/instructions/message-from-a/reply"]);
  assert.deepEqual(peerReads,["/api/instructions/message-from-a/reply"]);
  assert.equal(sidebar(ui,"agent-two").badge.textContent,"新動態");
  ui.render(copy(next));await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.headers?.["X-Console-Agent"]==="agent-two").length,2);
  // No background polling of B: firing every pending timer reads A only, never B again.
  for(const [id,fn] of [...ui.timers]){ui.timers.delete(id);ui.timerDelays.delete(id);try{await fn()}catch{}}
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.headers?.["X-Console-Agent"]==="agent-two").length,2);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a Responses-lane peer preview keeps B's progress view when B's /reply read fails",async()=>{
  // A failed /reply read (here a non-JSON 502 from ingress) must not drop the progress read: B still gets
  // the new-activity badge and opens this conversation, and /reply is not retried.
  const a=peerChat(),b=receivedChat();
  b.tasks[0].state_profile=b.composer.state_profile="foundry-responses-v1";
  b.tasks[0].reply=null;
  let ui;
  ui=await open(a,{agentProgressResponses:{"agent-two":{ok:true,json:async()=>copy(b)}},agentStates:{"agent-two":b},
    replyResponse:(url,options)=>options.headers?.["X-Console-Agent"]!=="agent-two"
      ?{ok:true,json:async()=>copy(ui?.holder.current??a)}
      :{ok:false,status:502,json:async()=>{throw new SyntaxError("Unexpected token '<'")}}});
  await ui.tick();
  const next=copy(a);next.tasks.push(peerReturned(a));
  ui.render(copy(next));await ui.tick();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"Agent：請查一下 VM 的 CPU、記憶體和磁碟");
  assert.equal(sidebar(ui,"agent-two").badge.textContent,"新動態");
  ui.render(copy(next));await ui.tick();await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.headers?.["X-Console-Agent"]==="agent-two").map(call=>call.url),[
    "/api/instructions/progress?thread_id=message-from-a","/api/instructions/message-from-a/reply"]);
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.ok(ui.calls.some(call=>call.url==="/api/instructions/models?thread_id=message-from-a"
    &&call.headers?.["X-Console-Agent"]==="agent-two"));
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("after a reload an earlier peer reply updates B's preview once without marking it new",async()=>{
  const a=peerChat(),b=receivedChat();
  a.tasks.push(peerReturned(a));
  const ui=await open(a,{agentProgressResponses:{"agent-two":{ok:true,json:async()=>copy(b)}},agentStates:{"agent-two":b}});
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.headers?.["X-Console-Agent"]==="agent-two").length,1);
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"助理：2 CPU、4.1Gi、5.2G");
  assert.equal(sidebar(ui,"agent-two").badge,undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an older A conversation opened later does not replace B's preview or the B conversation it opens",async()=>{
  const a=peerChat(),bOwn=sidebarChat("agent-two",true),b=receivedChat();
  bOwn.composer.thread_id="b-own-thread";bOwn.tasks[0].thread_id="b-own-thread";
  const old=copy(a);old.composer.thread_id="old-a-thread";old.tasks.push(peerReturned(a));
  const ui=await open(a,{agentStates:{"agent-one":old,"agent-two":bOwn},
    agentProgressResponses:{"agent-two":{ok:true,json:async()=>copy(b)}}});
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"助理：B 的原回答");
  sidebar(ui,"agent-one").button.events.click();await ui.tick();await ui.tick();
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/instructions/progress?thread_id=message-from-a")),false);
  assert.equal(sidebar(ui,"agent-two").preview.textContent,"助理：B 的原回答");
  assert.equal(sidebar(ui,"agent-two").badge,undefined);
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
  assert.equal(ui.calls.some(call=>call.url.includes("thread_id=message-from-a")),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("attachment toolbar icons disclose only the selected original picker without sending",async()=>{
  const ui=await open(imageChat()),before=ui.calls.length;
  for(const [id,label] of [
    ["attachment-picker","文字來源附件"],["user-file-picker","選取文字檔"],["user-image-picker","附加圖片"],
  ]){
    const toggle=ui.nodes.get(id+"-toggle");
    assert.ok(toggle,"The attachment picker needs an accessible toolbar icon");
    assert.match(html,new RegExp(`<button[^>]*id="${id}-toggle"[^>]*aria-label="${label}"[^>]*title="${label}"`));
    assert.equal(ui.nodes.get(id).open,false);
    ui.click(id+"-toggle");await ui.tick();
    assert.equal(ui.nodes.get(id).open,true);
    assert.equal(toggle.getAttribute("aria-expanded"),"true");
    for(const other of ["attachment-picker","user-file-picker","user-image-picker"]){
      if(other!==id)assert.equal(ui.nodes.get(other).open,false);
    }
    ui.click(id+"-toggle");await ui.tick();
    assert.equal(ui.nodes.get(id).open,false);
    assert.equal(toggle.getAttribute("aria-expanded"),"false");
  }
  assert.equal(ui.calls.length,before);
});

test("attachment picker disclosure preserves text and selected files and closes on thread change",async()=>{
  const data=imageChat(),ui=await open(data);
  ui.nodes.get("instruction").value="保留未送文字";ui.nodes.get("instruction").events.input();
  ui.nodes.get("user-text-files").value="pending-original.txt";
  assert.ok(ui.nodes.get("user-file-picker-toggle"));
  ui.click("user-file-picker-toggle");await ui.tick();
  assert.equal(ui.nodes.get("user-file-picker").open,true);
  ui.click("user-file-picker-toggle");await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"保留未送文字");
  assert.equal(ui.nodes.get("user-text-files").value,"pending-original.txt");
  ui.click("user-file-picker-toggle");await ui.tick();
  const changed=copy(data);changed.composer.thread_id="another-thread";changed.composer.request_id="another-draft";
  ui.render(changed);
  assert.equal(ui.nodes.get("user-file-picker").open,false);
  assert.equal(ui.nodes.get("user-file-picker-toggle").getAttribute("aria-expanded"),"false");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("busy native append stays on the original RID and does not block cancellation",async()=>{
  const data=appendChat(),rid=data.composer.request_id,ui=await open(data);
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.nodes.get("model").disabled,true);
  ui.nodes.get("instruction").value="補充原工作";ui.nodes.get("instruction").events.input();
  assert.equal(ui.text("send-instruction"),"追加");
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.holder.pendingPosts.length,1);
  const sent=ui.calls.find(call=>call.method==="POST");
  assert.equal(sent.url,`/api/instructions/${rid}/append`);
  assert.deepEqual(JSON.parse(sent.body),{append_id:"append-offline-a",content:"補充原工作"});
  assert.equal(ui.nodes.get("cancel-instruction").disabled,false);
  const next=copy(data);
  next.composer.append_target.append_id="append-offline-b";
  next.tasks[0].appends=[{request_id:rid,append_id:"append-offline-a",content:"補充原工作",status:"accepted",delivery:null}];
  ui.holder.current=next;
  ui.holder.pendingPosts[0].resolve({append:copy(next.tasks[0].appends[0])});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.calls.some(call=>call.url.endsWith("/next")||call.url.endsWith("/submit")),false);
});

test("unknown native append keeps the text and offers readback, never another POST",async()=>{
  const data=appendChat(),rid=data.composer.request_id;
  const ui=await open(data,{response:{ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})}});
  const retained=copy(data);
  retained.composer.can_append=false;
  retained.composer.append_target.pending=true;
  retained.tasks[0].appends=[{request_id:rid,append_id:"append-offline-a",content:"未確認的補充",
    status:"unknown",submission_state:"UNKNOWN",delivery:null}];
  ui.holder.current=retained;
  ui.nodes.get("instruction").value="未確認的補充";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"未確認的補充");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  const content=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(content,/核對追加/);
  assert.match(content,/未確認的補充/);
});

test("native append does not require a fresh model list or replace another Agent's draft",async()=>{
  const a=appendChat(),b=appendChat();
  for(const [data,name] of [[a,"agent-one"],[b,"agent-two"]]){
    const source=agentChat(name);
    for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
      data[key]=source[key];
    data.composer.models={models:[],default_model:null};
  }
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b}});
  ui.nodes.get("instruction").value="第一位的追加";ui.nodes.get("instruction").events.input();
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  ui.click("send-instruction");await ui.tick();
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="第二位的草稿";ui.nodes.get("instruction").events.input();
  ui.holder.pendingPosts[0].resolve({append:{
    request_id:a.composer.request_id,append_id:"append-offline-a",content:"第一位的追加",status:"accepted",
  }});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("chat-title"),"第二位助理");
  assert.equal(ui.nodes.get("instruction").value,"第二位的草稿");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  assert.equal(ui.calls.find(call=>call.method==="POST").headers["X-Console-Agent"],"agent-one");
});

test("native append readback shows SDK queued delivery without another send",async()=>{
  const data=appendChat(),rid=data.composer.request_id;
  const row={request_id:rid,append_id:"append-old",content:"已保存的追加",status:"accepted",delivery:null};
  data.tasks[0].appends=[row];
  const done=copy(data);done.tasks[0].appends[0]={...row,status:"applied",delivery:"queued"};
  const ui=await open(data,{appendsResponse:{ok:true,json:async()=>({
    appends:{request_id:rid,appends:copy(done.tasks[0].appends)},
  })}});
  ui.holder.current=done;
  await vm.runInContext("readAppends(state.tasks[0])",ui.context);
  const content=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(content,/助理已收到，在這次回覆中接著處理。/);
  assert.doesNotMatch(content,/下一輪/);
  assert.doesNotMatch(content,/SDK/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/appends")).length,1);
});

test("native append progress reads pending receipts without reposting",async()=>{
  const data=appendChat(),rid=data.composer.request_id;
  data.tasks[0].appends=[{request_id:rid,append_id:"append-old",content:"等待中的追加",status:"sending",delivery:null}];
  const done=copy(data);done.tasks[0].appends[0]={...done.tasks[0].appends[0],status:"applied",delivery:"steering"};
  const ui=await open(data,{progressResponse:{ok:true,json:async()=>copy(data)},
    appendsResponse:{ok:true,json:async()=>({appends:{request_id:rid,appends:copy(done.tasks[0].appends)}})}});
  ui.holder.current=done;
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/appends")).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an explicitly unapplied append can return to the composer only by a human click",async()=>{
  const data=nativeChat("completed"),rid=data.composer.request_id;
  data.tasks[0].appends=[{request_id:rid,append_id:"append-not-applied",content:"我要保留的原文",
    status:"not_applied",delivery:null}];
  data.tasks[0].actions["read-appends"]=action(true);
  const ui=await open(data);
  const descendants=node=>[node,...(node.children||[]).flatMap(descendants)];
  const restore=descendants(ui.nodes.get("conversation-history")).find(node=>node.textContent==="帶回輸入框");
  assert.ok(restore);
  assert.equal(ui.nodes.get("instruction").value,"");
  restore.events.click();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"我要保留的原文");
  assertNothingSent(ui);
});

// G plan "no lost message": an append made in the turn's tail (reply shown, turn not yet saved as completed) is settled
// not_applied, a definite "never delivered", so it goes out once as a new turn under a new RID. unknown never does.
async function appendInTail(status,{draft="",reason=status==="unknown"?"sdk_unconfirmed":"turn_finished"}={}){
  const data=appendChat(),rid=data.composer.request_id,ui=await open(data),content="回覆後馬上補的一句";
  ui.nodes.get("instruction").value=content;ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  const row={request_id:rid,append_id:"append-offline-a",content,status,delivery:null,reason,receipt_observed:true,
    ...(status==="unknown"?{submission_state:"UNKNOWN"}:{})};
  const done=nativeChat("completed");done.tasks[0].appends=[row];done.tasks[0].actions["read-appends"]=action(true);
  if(!draft){
    // The append landed after the turn's appends were settled: its own answer is not_applied.
    ui.holder.current=done;
    ui.holder.pendingPosts[0].resolve({append:copy(row)});await ui.tick();await ui.tick();await ui.tick();
    return {ui,done,rid,content};
  }
  // Accepted while the turn ran; the owner then types the next message; a later read finds it was never applied.
  const running=copy(data);running.composer.append_target.append_id="append-offline-b";
  running.tasks[0].appends=[{...row,status:"accepted"}];ui.holder.current=running;
  ui.holder.pendingPosts[0].resolve({append:{...row,status:"accepted"}});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"");
  ui.nodes.get("instruction").value=draft;ui.nodes.get("instruction").events.input();
  ui.holder.current=done;
  ui.holder.appendsResponse={ok:true,json:async()=>({appends:{request_id:rid,appends:[copy(row)]}})};
  await vm.runInContext("readAppends(state.tasks[0])",ui.context);await ui.tick();
  return {ui,done,rid,content};
}
const historyText=ui=>JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
test("an append settled not_applied is sent once as a new turn under a new request ID",async()=>{
  const {ui,done,rid,content}=await appendInTail("not_applied");
  assert.match(historyText(ui),/改成新訊息送出/);
  assert.doesNotMatch(historyText(ui),/帶回輸入框|不會自動再送/);
  const nexts=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.deepEqual(nexts.map(post=>post.url),[`/api/instructions/${done.composer.thread_id}/next`]);
  assert.match(historyText(ui),new RegExp(content),"the text shows as sending meanwhile");
  const next=copy(done);
  Object.assign(next.composer,{request_id:"next-rid",content:null,editable:true,can_submit:true,can_follow_up:false});
  nexts[0].resolve(next);await ui.tick();await ui.tick();
  const submits=ui.calls.filter(call=>call.method==="POST"&&call.url.endsWith("/submit"));
  assert.deepEqual(submits.map(call=>call.url),["/api/instructions/next-rid/submit"]);
  assert.notEqual("next-rid",rid);
  const body=JSON.parse(submits[0].body);
  assert.equal(body.content,content);assert.ok(body.model);
  assert.deepEqual(Object.keys(body).sort(),["content","model"],"no draft attachments go with it");
  const sent=copy(done.tasks[0]);
  Object.assign(sent,{task_id:"next-rid",submit_request_id:"next-rid",instruction:content,reply:null,appends:[],
    submission:{state:"CONFIRMED"},native_receipt:{...sent.native_receipt,status:"accepted"},
    status:{data:{state:"accepted"},observed_at:when}});
  const after=copy(done);after.tasks.push(sent);
  Object.assign(after.composer,{request_id:"next-rid",content,editable:false,can_follow_up:true});
  ui.holder.current=after;
  ui.holder.pendingPosts.find(post=>post.url.endsWith("/submit")).resolve({task:sent});
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("lastError",ui.context),null);
  assert.equal(ui.nodes.get("error").hidden,true);
  // Later reads of the same not_applied receipt send nothing more.
  ui.holder.appendsResponse={ok:true,json:async()=>({appends:{request_id:rid,appends:copy(done.tasks[0].appends)}})};
  await vm.runInContext("readAppends(state.tasks[0])",ui.context);
  ui.render(after);await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST"&&/\/(next|submit|append)$/.test(call.url)).length,3,
    "the append, one /next and one /submit");
  assert.equal(ui.nodes.get("instruction").value,"");
});
test("an append answered not_applied while its original still stops and saves goes out at once as a new turn",async()=>{
  // The bridge offers a follow-up while a turn runs (native.py view: can_follow_up does not look at the status).
  const data=appendChat();data.composer.can_follow_up=true;
  const rid=data.composer.request_id,ui=await open(data),content="回覆後一秒補的一句";
  ui.nodes.get("instruction").value=content;ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  // Hosted answers at once (2026-10-19): the original's last SDK round is over, but it is still started here.
  const row={request_id:rid,append_id:"append-offline-a",content,status:"not_applied",delivery:null,
    reason:"turn_finished",receipt_observed:true};
  const running=copy(data);running.composer.append_target.append_id="append-offline-b";running.tasks[0].appends=[row];
  ui.holder.current=running;
  ui.holder.pendingPosts[0].resolve({append:copy(row)});await ui.tick();await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("runningOwnTurn()?.task_id",ui.context),rid,"the original still runs");
  const nexts=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.deepEqual(nexts.map(post=>post.url),[`/api/instructions/${running.composer.thread_id}/next`]);
  assert.match(historyText(ui),/改成新訊息送出/);
  const next=copy(running);
  Object.assign(next.composer,{request_id:"next-rid",content:null,editable:true,can_submit:true,can_follow_up:false,
    append_target:null});
  nexts[0].resolve(next);await ui.tick();await ui.tick();
  const submits=ui.calls.filter(call=>call.method==="POST"&&call.url.endsWith("/submit"));
  assert.deepEqual(submits.map(call=>call.url),["/api/instructions/next-rid/submit"]);
  assert.equal(JSON.parse(submits[0].body).content,content);
  // Turn 1 then completes (saved), with the resent text as its own turn queued behind it.
  const done=nativeChat("completed");done.tasks[0].appends=[row];done.tasks[0].actions["read-appends"]=action(true);
  const sent=copy(done.tasks[0]);
  Object.assign(sent,{task_id:"next-rid",submit_request_id:"next-rid",instruction:content,reply:null,appends:[],
    submission:{state:"CONFIRMED"},native_receipt:{...sent.native_receipt,status:"accepted"},
    status:{data:{state:"accepted"},observed_at:when}});
  const after=copy(done);after.tasks.push(sent);
  Object.assign(after.composer,{request_id:"next-rid",content,editable:false,can_follow_up:true});
  ui.holder.current=after;
  ui.holder.pendingPosts.find(post=>post.url.endsWith("/submit")).resolve({task:sent});
  await ui.tick();await ui.tick();await ui.tick();
  ui.render(after);await ui.tick();
  assert.equal(vm.runInContext("lastError",ui.context),null);
  assert.equal(vm.runInContext("state.tasks[0].status.data.state",ui.context),"completed");
  assert.match(historyText(ui),/改成新訊息送出/);
  assert.doesNotMatch(historyText(ui),/帶回輸入框|不會自動再送/);
  assert.equal(vm.runInContext("runningOwnTurn()?.task_id",ui.context),"next-rid","the resent text runs as turn 2");
  assert.equal(ui.calls.filter(call=>call.method==="POST"&&/\/(next|submit|append)$/.test(call.url)).length,3,
    "the append, one /next and one /submit");
});
// Review P1 on #916: Responses has no queue (a human turn sent while the slot holder runs is busy_refused), so there the
// resend waits for the original to end, as before.
test("on Responses an append answered not_applied while its original still runs goes out only after it ends",async()=>{
  const data=appendChat();data.composer.can_follow_up=true;
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  const rid=data.composer.request_id,ui=await open(data),content="回覆後一秒補的一句";
  ui.nodes.get("instruction").value=content;ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  const row={request_id:rid,append_id:"append-offline-a",content,status:"not_applied",delivery:null,
    reason:"turn_finished",receipt_observed:true};
  const running=copy(data);running.composer.append_target.append_id="append-offline-b";running.tasks[0].appends=[row];
  ui.holder.current=running;
  ui.holder.pendingPosts[0].resolve({append:copy(row)});await ui.tick();await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("runningOwnTurn()?.task_id",ui.context),rid,"the original still runs");
  assert.equal(ui.calls.some(call=>call.url.endsWith("/next")||call.url.endsWith("/submit")),false,"not while it runs");
  assert.match(historyText(ui),/改成新訊息送出/);
  const done=nativeChat("completed");done.tasks[0].appends=[row];done.tasks[0].actions["read-appends"]=action(true);
  done.tasks[0].state_profile=done.composer.state_profile="foundry-responses-v1";
  ui.holder.current=done;ui.render(done);await ui.tick();await ui.tick();
  const nexts=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.deepEqual(nexts.map(post=>post.url),[`/api/instructions/${done.composer.thread_id}/next`]);
  const next=copy(done);
  Object.assign(next.composer,{request_id:"next-rid",content:null,editable:true,can_submit:true,can_follow_up:false});
  nexts[0].resolve(next);await ui.tick();await ui.tick();
  const submits=ui.calls.filter(call=>call.method==="POST"&&call.url.endsWith("/submit"));
  assert.deepEqual(submits.map(call=>call.url),["/api/instructions/next-rid/submit"]);
  assert.equal(JSON.parse(submits[0].body).content,content);
});
// Review P2 on #916: A1 accepted just before the channel closed, A2 answered not_applied after it. A read that still
// shows A1 unsettled must not let A2 go first; once A1 settles, A1 goes out, then A2.
test("two tail appends go out as new turns in their append order",async()=>{
  const data=appendChat();data.composer.can_follow_up=true;
  const rid=data.composer.request_id,ui=await open(data);
  const entry=(id,content,status)=>({request_id:rid,append_id:id,content,status,delivery:null,
    reason:status==="not_applied"?"turn_finished":null,receipt_observed:true});
  ui.nodes.get("instruction").value="A1";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  const second=copy(data);second.composer.append_target.append_id="append-offline-b";
  second.tasks[0].appends=[entry("append-offline-a","A1","accepted")];ui.holder.current=second;
  ui.holder.pendingPosts[0].resolve({append:entry("append-offline-a","A1","accepted")});await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="A2";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();
  const stale=copy(second);stale.composer.append_target.append_id="append-offline-c";
  stale.tasks[0].appends=[entry("append-offline-a","A1","accepted"),entry("append-offline-b","A2","not_applied")];
  ui.holder.current=stale;
  ui.holder.pendingPosts.filter(post=>post.url.endsWith("/append")).at(-1).resolve({append:entry("append-offline-b","A2","not_applied")});
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.calls.some(call=>call.url.endsWith("/next")||call.url.endsWith("/submit")),false,"A2 waits for A1");
  const settled=copy(stale);
  settled.tasks[0].appends=[entry("append-offline-a","A1","not_applied"),entry("append-offline-b","A2","not_applied")];
  ui.holder.current=settled;ui.render(settled);await ui.tick();await ui.tick();
  const order=()=>ui.calls.filter(call=>call.method==="POST"&&call.url.endsWith("/submit"))
    .map(call=>JSON.parse(call.body).content);
  const first=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.equal(first.length,1);
  const next=copy(settled);
  Object.assign(next.composer,{request_id:"next-a1",content:null,editable:true,can_submit:true,can_follow_up:false,
    append_target:null});
  first[0].resolve(next);await ui.tick();await ui.tick();
  assert.deepEqual(order(),["A1"],"A1 first");
  // A1's turn, then A2's: once A1's resend has ended, A2 goes out next.
  const done=nativeChat("completed");done.tasks[0].appends=copy(settled.tasks[0].appends);
  done.tasks[0].actions["read-appends"]=action(true);
  const a1=copy(done.tasks[0]);
  Object.assign(a1,{task_id:"next-a1",submit_request_id:"next-a1",instruction:"A1",appends:[],submission:{state:"CONFIRMED"}});
  const after=copy(done);after.tasks.push(a1);
  Object.assign(after.composer,{request_id:"next-a1",content:"A1",editable:false,can_follow_up:true,append_target:null});
  ui.holder.current=after;
  ui.holder.pendingPosts.find(post=>post.url.endsWith("/submit")).resolve({task:a1});
  await ui.tick();await ui.tick();await ui.tick();
  ui.render(after);await ui.tick();await ui.tick();
  const then=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next")).at(-1);
  const nextA2=copy(after);
  Object.assign(nextA2.composer,{request_id:"next-a2",content:null,editable:true,can_submit:true,can_follow_up:false});
  then.resolve(nextA2);await ui.tick();await ui.tick();
  assert.deepEqual(order(),["A1","A2"],"then A2");
});
test("an append whose outcome is unknown is never sent again as a new turn",async()=>{
  const {ui,content}=await appendInTail("unknown");
  await ui.tick();ui.render(ui.holder.current);await ui.tick();
  assert.equal(ui.calls.some(call=>call.url.endsWith("/next")||call.url.endsWith("/submit")),false);
  assert.doesNotMatch(historyText(ui),/改成新訊息送出/);
  assert.match(historyText(ui),new RegExp(content));
});
test("an append left not_applied by the owner's 停止 is never sent again as a new turn",async()=>{
  const {ui,content}=await appendInTail("not_applied",{reason:"cancelled"});
  await ui.tick();ui.render(ui.holder.current);await ui.tick();
  assert.equal(ui.calls.some(call=>call.url.endsWith("/next")||call.url.endsWith("/submit")),false);
  assert.equal(vm.runInContext('[...sentAppendDrafts.values()][0].resend',ui.context),undefined);
  assert.doesNotMatch(historyText(ui),/改成新訊息送出/);
  assert.match(historyText(ui),/帶回輸入框/);
  assert.match(historyText(ui),new RegExp(content));
});
test("a not_applied append waits out the owner's own draft: nothing is sent and the restore button stays",async()=>{
  const {ui}=await appendInTail("not_applied",{draft:"我正在打的下一則"});
  // (A /next may be taken ahead for the typed draft, as for any text left in the box when a turn ends.)
  assert.equal(ui.calls.some(call=>call.url.endsWith("/submit")),false);
  assert.equal(vm.runInContext('[...sentAppendDrafts.values()][0].resend',ui.context),"kept");
  assert.equal(ui.nodes.get("instruction").value,"我正在打的下一則");
  assert.match(historyText(ui),/帶回輸入框/);
});
test("native append ignores the original turn's frozen attachments but preserves unsent drafts",async()=>{
  const data=appendChat();
  data.composer.attachments=[{name:"original.txt",source:{source_id:"original",version:1,sha256:"a".repeat(64),byte_length:4}}];
  const ui=await open(data);
  ui.nodes.get("instruction").value="只補充文字";ui.nodes.get("instruction").events.input();
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  vm.runInContext('userFileDrafts.set(attachmentDraftKey(),[{name:"future.txt",text:"kept",utf8_bytes:4}]);renderComposer()',ui.context);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.match(ui.text("instruction-reason"),/附件保留/);
  assert.equal(vm.runInContext("userFileDrafts.get(attachmentDraftKey())[0].text",ui.context),"kept");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an unsent native append remains visible after the original turn completes",async()=>{
  const data=appendChat(),done=nativeChat("completed"),ui=await open(data);
  ui.nodes.get("instruction").value="還沒送出的追加";ui.nodes.get("instruction").events.input();
  ui.holder.current=done;ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(ui.nodes.get("instruction").value,"還沒送出的追加");
  assert.equal(ui.nodes.get("instruction").disabled,false);
  // RTT A round 2: the turn ended with text in the box, so the conversation's next RID is taken ahead (local only).
  const nexts=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.deepEqual(nexts.map(post=>post.url),[`/api/instructions/${done.composer.thread_id}/next`]);
  const next=copy(done);
  Object.assign(next.composer,{request_id:"next-rid",content:null,editable:true,can_submit:true,can_follow_up:false});
  nexts[0].resolve(next);await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("state.composer.request_id",ui.context),"next-rid");
  assert.equal(ui.nodes.get("instruction").value,"還沒送出的追加","the text moves with the composer to the next RID");
  assert.equal(vm.runInContext(`inputDrafts.get(scopeKey("next-rid"))`,ui.context),"還沒送出的追加");
  assert.equal(vm.runInContext(`inputDrafts.get(scopeKey(${JSON.stringify(done.composer.request_id)}))`,ui.context),"");
  assertNothingSent(ui);
});

for(const nextText of ["下一筆不同文字","相同但尚未送出的文字"]){
  test(`an older native append ACK preserves the next draft: ${nextText}`,async()=>{
    const data=appendChat(),rid=data.composer.request_id,ui=await open(data);
    const sentText="相同但尚未送出的文字";
    ui.nodes.get("instruction").value=sentText;ui.nodes.get("instruction").events.input();
    ui.click("send-instruction");await ui.tick();
    const next=copy(data);next.composer.append_target.append_id="append-offline-b";
    next.tasks[0].appends=[{request_id:rid,append_id:"append-offline-a",content:sentText,
      status:"accepted",receipt_observed:true}];
    ui.render(next);
    ui.nodes.get("instruction").value=nextText;ui.nodes.get("instruction").events.input();
    ui.holder.appendsResponse={ok:true,json:async()=>({appends:{request_id:rid,appends:copy(next.tasks[0].appends)}})};
    await vm.runInContext("readAppends(state.tasks[0])",ui.context);
    assert.equal(ui.nodes.get("instruction").value,nextText);
    ui.holder.pendingPosts[0].resolve({append:copy(next.tasks[0].appends[0])});
    await ui.tick();await ui.tick();
    assert.equal(ui.nodes.get("instruction").value,nextText);
    assert.equal(ui.nodes.get("send-instruction").disabled,false);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  });
}

test("a sent unknown append cannot become an unchanged ordinary follow-up after completion",async()=>{
  const data=appendChat(),rid=data.composer.request_id,ui=await open(data,{
    response:{ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})},
  });
  const done=nativeChat("completed");
  done.tasks[0].appends=[{request_id:rid,append_id:"append-offline-a",content:"結果未知的原文",
    status:"unknown",submission_state:"UNKNOWN",delivery:null}];
  ui.holder.current=done;
  ui.nodes.get("instruction").value="結果未知的原文";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"結果未知的原文");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  assert.equal(ui.calls.some(call=>call.url.endsWith("/next")||call.url.endsWith("/submit")),false);
});

test("a sent unknown append stays protected through login renewal until its own receipt is read",async()=>{
  const data=appendChat(),rid=data.composer.request_id,ui=await open(data,{
    response:{ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})},
  });
  const done=nativeChat("completed");
  done.tasks[0].appends=[{request_id:rid,append_id:"append-offline-a",content:"重新登入前已送出的原文",
    status:"unknown",submission_state:"UNKNOWN",delivery:null,receipt_observed:false}];
  ui.holder.current=done;
  ui.nodes.get("instruction").value=done.tasks[0].appends[0].content;ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();await ui.tick();
  ui.render({...done,authentication:"LOGIN_EXPIRED"});ui.render(done);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  const uncertain=copy(done);uncertain.tasks[0].appends[0].receipt_observed=true;
  ui.holder.current=uncertain;
  ui.holder.appendsResponse={ok:true,json:async()=>({appends:{request_id:rid,appends:copy(uncertain.tasks[0].appends)}})};
  await vm.runInContext("readAppends(state.tasks[0])",ui.context);
  assert.equal(ui.nodes.get("instruction").value,done.tasks[0].appends[0].content);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  const confirmed=copy(uncertain);confirmed.tasks[0].appends[0].status="accepted";
  ui.holder.current=confirmed;
  ui.holder.appendsResponse={ok:true,json:async()=>({appends:{request_id:rid,appends:copy(confirmed.tasks[0].appends)}})};
  await vm.runInContext("readAppends(state.tasks[0])",ui.context);
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("GET confirmation clears only the unchanged sent append snapshot before the next ID",async()=>{
  const data=appendChat(),rid=data.composer.request_id,ui=await open(data,{
    response:{ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})},
  });
  const unknown=copy(data);
  unknown.composer.can_append=false;unknown.composer.append_target.pending=true;
  unknown.tasks[0].appends=[{request_id:rid,append_id:"append-offline-a",content:"同一筆已送內容",
    status:"unknown",submission_state:"UNKNOWN",delivery:null}];
  ui.holder.current=unknown;
  ui.nodes.get("instruction").value="同一筆已送內容";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();await ui.tick();
  const confirmed=copy(data);
  confirmed.composer.append_target.append_id="append-offline-b";
  confirmed.tasks[0].appends=[{...unknown.tasks[0].appends[0],status:"accepted",receipt_observed:true}];
  ui.holder.current=confirmed;
  ui.holder.appendsResponse={ok:true,json:async()=>({appends:{request_id:rid,appends:copy(confirmed.tasks[0].appends)}})};
  await vm.runInContext("readAppends(state.tasks[0])",ui.context);
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("group panel is default off and does not query or alter the composer",async()=>{
  const data=nativeChat("started"),ui=await open(data);
  assert.equal(ui.nodes.get("open-groups").hidden,true);
  assert.equal(ui.nodes.get("group-panel").hidden,true);
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/groups")),false);
  assert.equal(ui.text("chat-title"),data.agents[0].name);
});

test("group transcript keeps literal speaker data separate from approval and the original draft",async()=>{
  const data=nativeChat("started");data.groups_available=true;
  const ui=await open(data);
  ui.nodes.get("instruction").value="Keep my one-to-one draft.";
  ui.context.groupPreview={transcript:{group:{group_id:"a".repeat(64),question:"Original group question",
    state:"open",round:1,awaiting_final:true,posts:[{}],
    deliveries:[{recipient_agent_id:"peer-b",refusal_code:"NATIVE_TURNS_DISABLED"}]},posts:[
      {reference:{speaker_agent_id:"configured-agent",kind:"fyi",round:1},content:"<script>Not approval</script>"},
    ]},turns:[],unconfirmed_request_ids:[]};
  vm.runInContext('groupView=groupPreview;el("group-panel").hidden=false;renderGroup()',ui.context);
  const articles=ui.nodes.get("group-transcript").children;
  assert.equal(articles[1].children[1].textContent,"<script>Not approval</script>");
  assert.match(articles[0].textContent,/仍待主持助理收尾/);
  assert.match(ui.nodes.get("group-turns").children[0].textContent,/^系統：.*版本資格拒絕/);
  assert.equal(ui.nodes.get("instruction").value,"Keep my one-to-one draft.");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  assert.equal(ui.nodes.get("attention-items").children.length,0);
});

test("late group reads after login changes cannot publish an old group",async()=>{
  const data=nativeChat("completed");data.groups_available=true;
  const ui=await open(data);
  let finish;
  ui.holder.templateResponses={["/api/groups/"+"a".repeat(64)]:new Promise(resolve=>{finish=resolve})};
  const reading=vm.runInContext('readGroup("a".repeat(64))',ui.context);
  await ui.tick();
  ui.render({...data,authentication:"LOGIN_EXPIRED"});
  finish({ok:true,json:async()=>({transcript:{group:{group_id:"a".repeat(64)},posts:[]},turns:[]})});
  await reading;
  assert.equal(vm.runInContext("groupView",ui.context),null);
  assert.equal(ui.nodes.get("group-panel").hidden,true);
});

test("group opening closes the menu and retries only the retained original body without erasing a newer draft",async()=>{
  const data=nativeChat("completed");data.groups_available=true;
  const options={options:{enabled:true,member_agent_ids:["configured-agent","peer-b"],images:false},request_id:"group-original"};
  const id="a".repeat(64),group={group_id:id,question:"Original group question.",state:"open",round:1,
    awaiting_final:true,posts:[],orchestrator_agent_id:"configured-agent"};
  const ui=await open(data,{templateResponses:{
    "/api/groups/options":{ok:true,json:async()=>copy(options)},
    "/api/groups/list":{ok:true,json:async()=>({groups:[],next_after:null})},
    "/api/groups/open":{ok:false,json:async()=>({error:{code:"NATIVE_GROUP_UNKNOWN"}})},
    ["/api/groups/"+id]:{ok:true,json:async()=>({transcript:{group,posts:[]},turns:[],unconfirmed_request_ids:[]})},
  }});
  ui.holder.templateResponses["/api/groups/options"]={ok:true,json:async()=>{
    const sent=ui.calls.filter(call=>call.url==="/api/groups/open");
    return {...copy(options),pending_opening:sent.length===1?JSON.parse(sent[0].body):null};
  }};
  ui.nodes.get("chat-menu").open=true;
  await ui.nodes.get("open-groups").events.click();
  assert.equal(ui.nodes.get("chat-menu").open,false);
  vm.runInContext('for(const member of groupMemberInputs)member.checked=true',ui.context);
  ui.nodes.get("group-question").value="Original group question.";
  await ui.nodes.get("group-open").events.click();
  ui.nodes.get("group-question").value="A newer unsent group question.";
  ui.holder.templateResponses["/api/groups/open"]={ok:true,json:async()=>group};
  options.request_id="next-group";
  await ui.nodes.get("group-open").events.click();
  const sent=ui.calls.filter(call=>call.url==="/api/groups/open");
  assert.equal(sent.length,2);
  assert.equal(sent[0].body,sent[1].body);
  assert.equal(sent[0].headers["X-Console-CSRF"],data.csrf);
  assert.equal(ui.nodes.get("group-question").value,"A newer unsent group question.");
  assert.equal(ui.calls.some(call=>/\/start$/.test(call.url)),false);
});

test("Responses progress automatically POSTs one eligible original reconcile with CSRF",async()=>{
  const data=nativeChat("accepted"),task=data.tasks[0];
  task.state_profile=data.composer.state_profile="foundry-responses-v1";
  task.native_receipt.response_id=null;
  task.native_receipt.dispatch={state:"needs_response",inflight_request_id:task.task_id};
  task.actions["submit-reconcile"]=action(true);
  const ui=await open(data,{progressResponse:{ok:true,json:async()=>copy(data)},
    response:{ok:true,json:async()=>({task:copy(task)})}});
  await vm.runInContext("updateProgress()",ui.context);
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,`/api/tasks/${task.task_id}/submit-reconcile`);
  assert.equal(posts[0].headers["X-Console-CSRF"],data.csrf);
  assert.deepEqual(JSON.parse(posts[0].body),{});
  assert.equal(ui.calls.some(call=>call.url.endsWith("/submit")||call.url.endsWith("/submit-retry")),false);
});

for(const scenario of ["old-lane","queued","started","unknown","has-response-id","stopped","agent-source"]){
  test(`Responses automatic reconcile does not replay ${scenario}`,async()=>{
    const data=nativeChat("accepted"),task=data.tasks[0];
    task.state_profile=data.composer.state_profile="foundry-responses-v1";
    task.native_receipt.response_id=null;
    task.native_receipt.dispatch={state:"needs_response",inflight_request_id:task.task_id};
    task.actions["submit-reconcile"]=action(true);
    if(scenario==="old-lane")task.state_profile=data.composer.state_profile="foundry-native-v1";
    if(scenario==="queued")task.native_receipt.dispatch.state="already_queued";
    if(["started","unknown"].includes(scenario))task.native_receipt.status=scenario;
    if(scenario==="has-response-id")task.native_receipt.response_id="resp-original";
    if(scenario==="stopped")task.actions["submit-reconcile"]=action(false);
    if(scenario==="agent-source")task.source={kind:"agent_request"};
    const ui=await open(data,{progressResponse:{ok:true,json:async()=>copy(data)}});
    await vm.runInContext("updateProgress()",ui.context);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("temporary Responses handoff uncertainty is not rendered as terminal unknown",async()=>{
  const data=nativeChat("accepted"),task=data.tasks[0];
  task.state_profile=data.composer.state_profile="foundry-responses-v1";
  task.native_receipt.dispatch={state:"needs_response",inflight_request_id:task.task_id};
  task.native_receipt.response_id=null;
  task.error={code:"NATIVE_RESPONSE_UNCONFIRMED"};
  task.response_reconciliation={state:"waiting",failures:1};
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.match(panel.children[0].textContent,/正在等助理接手/);
  assert.match(panel.children[1].textContent,/訊息已送到/);
  assert.doesNotMatch(panel.children[1].textContent,/不會自動重送|RID/);
  const stopped=copy(data);stopped.tasks[0].response_reconciliation={state:"stopped",failures:10};
  ui.render(stopped);
  assert.match(inlineProcessing(ui).children[1].textContent,/一直沒有接手.*停止/);
  const unknown=nativeChat("unknown");
  unknown.tasks[0].state_profile=unknown.composer.state_profile="foundry-responses-v1";
  ui.render(unknown);
  assert.equal(ui.nodes.get("follow-up-blocker").hidden,true,"an unknown end does not lock the conversation");
  assert.equal(ui.nodes.get("instruction").disabled,false);
});

// API #668 (3118f7dc, 2026-10-06): submit answers once Hosted accepted, with dispatch needs_response, and the POST
// follows in the background; for about a second before the first progress read nothing is in doubt yet.
test("a just-sent Responses turn waiting for its background POST shows as accepted, not a handoff in doubt",async()=>{
  const data=nativeChat("accepted"),task=data.tasks[0];
  task.state_profile=data.composer.state_profile="foundry-responses-v1";
  task.native_receipt.dispatch={state:"needs_response",inflight_request_id:task.task_id};
  task.native_receipt.response_id=null;task.error=null;task.response_reconciliation=null;
  const ui=await open(data),panel=inlineProcessing(ui);
  // Plain wording (P08): an accepted turn reads 正在準備; a handoff in doubt reads 正在等助理接手.
  assert.doesNotMatch(panel.children[0].textContent,/正在等助理接手/);
  assert.match(panel.children[0].textContent,/正在準備/,"shown as accepted, like any other just-sent turn");
  const reconciling=copy(data);reconciling.tasks[0].response_reconciliation={state:"waiting",failures:0};
  ui.render(reconciling);
  assert.match(inlineProcessing(ui).children[0].textContent,/正在等助理接手/,"once reconciling, the doubt is shown");
  const failed=copy(data);failed.tasks[0].error={code:"NATIVE_RESPONSE_UNCONFIRMED"};
  ui.render(failed);
  assert.match(inlineProcessing(ui).children[0].textContent,/正在等助理接手/,"a send that reported an error shows it at once");
});

test("Responses platform history keeps platform roles separate from verified FA speakers",async()=>{
  const data=nativeChat("completed");
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  data.composer.platform_history={error:null,page:{
    schema_version:"fa-platform-history-v1",source_kind:"conversation",source_id:"conv-original",
    messages:[
      {item_id:"item-input",platform_role:"user",text:"<script>Platform data only</script>",state:"available"},
      {item_id:"item-final",platform_role:"assistant",text:"Original saved final answer.",state:"available"},
      {item_id:"item-missing-phase",platform_role:"assistant",text:null,state:"unconfirmed"},
    ],next_after:null,previous_response_id:null,fallback:"native-result",
  }};
  const ui=await open(data);
  assert.equal(ui.nodes.get("platform-history-panel").hidden,false);
  const messages=ui.nodes.get("platform-history-messages").children;
  assert.equal(messages.length,3);
  assert.match(messages[0].children[0].textContent,/平台.*不等於真人/);
  assert.equal(messages[0].children[1].textContent,"<script>Platform data only</script>");
  assert.equal(messages[1].children[1].textContent,"Original saved final answer.");
  assert.match(messages[2].children[1].textContent,/未確認.*原.*RID/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("Responses platform history next page is a scoped GET and late login results are discarded",async()=>{
  const data=nativeChat("completed");
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  const page={schema_version:"fa-platform-history-v1",source_kind:"conversation",source_id:"conv-original",
    messages:[],next_after:"item-original",previous_response_id:null,fallback:null};
  data.composer.platform_history={page,error:null};
  const ui=await open(data);
  let resolve;
  const path=`/api/instructions/${data.composer.thread_id}/platform-history?after=item-original`;
  ui.holder.templateResponses={[path]:new Promise(done=>{resolve=done})};
  const reading=ui.nodes.get("platform-history-next").events.click();
  await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url===path).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  ui.render({...data,authentication:"LOGIN_EXPIRED"});
  resolve({ok:true,json:async()=>({thread_id:data.composer.thread_id,platform_history:{error:null,
    page:{...page,messages:[{item_id:"late",platform_role:"assistant",state:"available",text:"Old login result"}]}}})});
  await reading;
  assert.equal(ui.nodes.get("platform-history-messages").children.length,0);
});

test("P04: platform history sits in the support details and is read once, only when opened",async()=>{
  const aside=html.indexOf('id="diagnostics"'),panel=html.indexOf('id="platform-history-panel"');
  assert.ok(aside>0&&panel>aside,"the panel is inside ⋯ → 支援明細");
  assert.ok(html.indexOf('id="chat-scroll"')<html.indexOf('id="conversation-history"'));
  assert.doesNotMatch(html.slice(html.indexOf('id="chat-scroll"'),html.indexOf('id="conversation-history"')),/id="platform-history/);
  const data=nativeChat("completed");
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  data.composer.platform_history={page:null,error:null,observed_at:null};
  const path=`/api/instructions/${data.composer.thread_id}/platform-history`;
  const page={schema_version:"fa-platform-history-v1",source_kind:"conversation",source_id:"conv-original",
    messages:[{item_id:"item-final",platform_role:"assistant",text:"Platform answer.",state:"available"}],
    next_after:null,previous_response_id:null,fallback:null};
  const ui=await open(data);
  ui.holder.templateResponses={[path]:{ok:true,json:async()=>({thread_id:data.composer.thread_id,
    platform_history:{page,error:null,observed_at:when}})}};
  assert.equal(ui.calls.some(call=>call.url.startsWith(path)),false,"nothing is read on load");
  assert.match(ui.text("platform-history-note"),/展開.*才讀/);
  const node=ui.nodes.get("platform-history-panel");
  node.open=true;node.events.toggle();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url===path).length,1);
  assert.equal(ui.nodes.get("platform-history-messages").children[0].children[1].textContent,"Platform answer.");
  node.open=false;node.events.toggle();node.open=true;node.events.toggle();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url===path).length,1,"reopening does not read again");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("completed Responses history stops active polling without requiring a VM answer download",async()=>{
  const data=nativeChat("completed");
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  data.tasks[0].reply=null;
  const ui=await open(data);
  assert.equal(vm.runInContext("progressTimer",ui.context),null);
});

// 2026-10-05 slimming (Neal: 「把非必要的邏輯拿掉」): a send is one submit. charter-agent paints the message and
// calls the agent with no version or environment check first; the Hosted platform restores idle sessions itself.
function directSendChat(profile="foundry-native-v1"){
  const data=attachmentChat();
  Object.assign(data.composer,{state_profile:profile,environment_policy:"fa-environment-system-policy-20261004"});
  data.tasks=[];
  return data;
}
const unverifiedEnvironment={ok:true,json:async()=>({thread_id:"attachment-thread",environment:{
  schema_version:"fa-native-environment-metadata-v1",agent_id:"configured-agent",hosted_session_id:"native-sid",
  hosted_agent_version:"41",state:"unverified",observed_sessions:[],state_scope:"platform-metadata-only",
  preparation_verified:false,scope:"entire-agent",model_started:false,original_work_replayed:false}})};
for(const profile of ["foundry-native-v1","foundry-responses-v1"]){
  test(`a native draft is sent with one submit and no environment check first (${profile})`,async()=>{
    const ui=await open(directSendChat(profile),{environmentResponse:unverifiedEnvironment});
    await ui.tick();
    const before=ui.calls.length;
    typeAttachmentText(ui,"直接送出");
    ui.click("send-instruction");await ui.tick();await ui.tick();
    const sent=ui.calls.slice(before);
    assert.deepEqual(sent.filter(call=>call.method==="POST").map(call=>call.url),["/api/instructions/attachment-draft/submit"]);
    assert.deepEqual(JSON.parse(sent.find(call=>call.method==="POST").body),{content:"直接送出",model:"offline-chat-a"});
    assert.equal(sent.some(call=>/\/environment(-auto|-prepare)?$/.test(call.url)),false);
  });
}

test("a follow-up takes the next RID and submits it, with no environment check first",async()=>{
  const data=nativeChat("completed");data.composer.environment_policy="fa-environment-system-policy-20261004";
  const ui=await open(data,{environmentResponse:{...unverifiedEnvironment,json:async()=>({
    ...(await unverifiedEnvironment.json()),thread_id:data.composer.thread_id})}});
  await ui.tick();
  const before=ui.calls.length;
  typeAttachmentText(ui,"接著問");
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.holder.pendingPosts.at(-1).url,`/api/instructions/${data.composer.thread_id}/next`);
  const next=copy(data);
  Object.assign(next.composer,{request_id:"follow-rid",editable:true,can_submit:true,can_follow_up:false,content:null});
  ui.holder.pendingPosts.at(-1).resolve(next);await ui.tick();await ui.tick();
  const sent=ui.calls.slice(before);
  assert.deepEqual(sent.filter(call=>call.method==="POST").map(call=>call.url),
    [`/api/instructions/${data.composer.thread_id}/next`,"/api/instructions/follow-rid/submit"]);
  assert.equal(JSON.parse(sent.at(-1).body).content,"接著問");
  assert.equal(sent.some(call=>/\/environment/.test(call.url)),false);
});

// RTT A round 2 (dev #706: 220-290 ms between the click and the submit went to /next): text typed after a finished turn
// takes the conversation's next RID ahead, so the send's first request is the submit. The RID is still fixed before
// the send, and a send while that request is on its way waits for it instead of asking a second time.
function followUpReady(data,rid="follow-rid"){
  const next=copy(data);
  Object.assign(next.composer,{request_id:rid,editable:true,can_submit:true,can_follow_up:false,content:null});
  next.composer.conversation.can_follow_up=false;
  delete next.csrf; // like the bridge: /next answers scoped_state without the page's CSRF value
  return next;
}
test("typing after a finished turn takes the next RID ahead and the send goes straight to its submit",async()=>{
  const data=nativeChat("completed");data.composer.environment_policy="fa-environment-system-policy-20261004";
  const ui=await open(data);await ui.tick();
  assert.equal(ui.calls.some(call=>call.method==="POST"),false,"opening the conversation takes nothing");
  typeAttachmentText(ui,"接著問");
  const ahead=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.deepEqual(ahead.map(post=>post.url),[`/api/instructions/${data.composer.thread_id}/next`]);
  typeAttachmentText(ui,"接著問，再多打幾個字");
  assert.equal(ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next")).length,1,"asked once per sent RID");
  ahead[0].resolve(followUpReady(data));await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("state.composer.request_id",ui.context),"follow-rid");
  assert.equal(ui.nodes.get("instruction").value,"接著問，再多打幾個字");
  const before=ui.calls.length;
  ui.click("send-instruction");
  assert.deepEqual(ui.calls.slice(before).filter(call=>call.method==="POST").map(call=>call.url),
    ["/api/instructions/follow-rid/submit"],"the send's first request is its submit");
  assert.equal(JSON.parse(ui.calls.at(-1).body).content,"接著問，再多打幾個字");
  assert.equal(ui.calls.at(-1).headers["X-Console-CSRF"],"offline","the page's CSRF value is kept across the switch");
});

test("a read that shows the next RID before /next answers keeps the typed text in the box",async()=>{
  const data=nativeChat("completed"),ui=await open(data);await ui.tick();
  typeAttachmentText(ui,"別的讀取先到");
  const ahead=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.equal(ahead.length,1);
  ui.render(followUpReady({...data,csrf:"offline"}));
  assert.equal(vm.runInContext("state.composer.request_id",ui.context),"follow-rid");
  assert.equal(ui.nodes.get("instruction").value,"別的讀取先到","the box never goes blank");
  ahead[0].resolve(followUpReady(data));await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"別的讀取先到");
  ui.click("send-instruction");
  const sent=ui.calls.filter(call=>call.method==="POST").map(call=>call.url);
  assert.deepEqual(sent,[`/api/instructions/${data.composer.thread_id}/next`,"/api/instructions/follow-rid/submit"]);
  assert.equal(JSON.parse(ui.calls.at(-1).body).content,"別的讀取先到");
});
test("a send waiting for its next RID is not offered again when another read shows that RID first",async()=>{
  const data=nativeChat("completed"),ui=await open(data);await ui.tick();
  typeAttachmentText(ui,"送出後別的讀取先到");
  const ahead=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.equal(ahead.length,1);
  ui.click("send-instruction");await ui.tick();
  ui.render(followUpReady({...data,csrf:"offline"}));
  assert.equal(ui.nodes.get("send-instruction").disabled,true,"the message is already on its way");
  ui.click("send-instruction");await ui.tick();
  ahead[0].resolve(followUpReady(data));await ui.tick();await ui.tick();
  const submits=ui.calls.filter(call=>call.url==="/api/instructions/follow-rid/submit");
  assert.equal(submits.length,1,"one submit for the RID");
  assert.equal(JSON.parse(submits[0].body).content,"送出後別的讀取先到");
});
test("text typed while the next RID was taken stays with the conversation after switching away and back",async()=>{
  const data=nativeChat("completed"),ui=await open(data);await ui.tick();
  typeAttachmentText(ui,"還沒送出就切走");
  const ahead=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.equal(ahead.length,1);
  const other=copy(data);other.tasks=[];
  Object.assign(other.composer,{thread_id:"other-thread",request_id:"other-draft",content:null,editable:true,
    can_submit:true,can_follow_up:false,conversation:null});
  ui.render(other);
  ahead[0].resolve(followUpReady(data));await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("state.composer.thread_id",ui.context),"other-thread","not applied after the switch");
  ui.render(followUpReady({...data,csrf:"offline"}));
  assert.equal(vm.runInContext("state.composer.request_id",ui.context),"follow-rid");
  assert.equal(ui.nodes.get("instruction").value,"還沒送出就切走");
  assertNothingSent(ui);
});

test("a send while the next RID is still on its way waits for that request instead of asking again",async()=>{
  const data=nativeChat("completed"),ui=await open(data);await ui.tick();
  typeAttachmentText(ui,"很快就按送出");
  const ahead=ui.holder.pendingPosts.filter(post=>post.url.endsWith("/next"));
  assert.equal(ahead.length,1);
  ui.click("send-instruction");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/next")).length,1,"no second /next");
  assert.equal(ui.nodes.get("conversation-history").children.at(-1).children[3].textContent,"助理準備中…");
  ahead[0].resolve(followUpReady(data));await ui.tick();await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),
    [`/api/instructions/${data.composer.thread_id}/next`,"/api/instructions/follow-rid/submit"]);
});

test("a finished conversation whose next RID is already taken does not keep reading",async()=>{
  const data=nativeChat("completed");
  data.composer.conversation.turns[0].state="completed";
  const ui=await open(followUpReady(data));await ui.tick();
  assert.equal(vm.runInContext("progressTimer",ui.context),null);
});

// Typing prewarm (README#typing-prewarm): with FA_CONSOLE_PREWARM=1 the bridge flips one page line, and typing in a
// native composer then asks the bridge, at most every 2 minutes per Agent, to wake that Agent's VM before the send.
// It is a hint only: the send never waits for it, and its answer or failure is never shown.
const prewarmPosts=ui=>ui.calls.filter(call=>call.url==="/api/instructions/prewarm");
test("typing in a native composer asks once to wake the Agent's VM, and again only after 2 minutes",async()=>{
  const ui=await open(directSendChat(),{prewarm:true});await ui.tick();
  assert.equal(prewarmPosts(ui).length,0,"opening the conversation asks nothing");
  typeAttachmentText(ui,"打");
  assert.equal(prewarmPosts(ui).length,1);
  assert.equal(prewarmPosts(ui)[0].method,"POST");
  assert.equal(prewarmPosts(ui)[0].body,"{}");
  assert.equal(prewarmPosts(ui)[0].headers["X-Console-CSRF"],"offline");
  typeAttachmentText(ui,"打字中");typeAttachmentText(ui,"");typeAttachmentText(ui,"再打一次");
  assert.equal(prewarmPosts(ui).length,1,"at most once per 2 minutes");
  ui.holder.now=Date.now()+120001;
  typeAttachmentText(ui,"兩分鐘後再打");
  assert.equal(prewarmPosts(ui).length,2);
});
test("a send while the prewarm is still on its way goes out at once",async()=>{
  const ui=await open(directSendChat(),{prewarm:true});await ui.tick();
  typeAttachmentText(ui,"馬上送出");
  assert.equal(prewarmPosts(ui).length,1);
  const before=ui.calls.length;
  ui.click("send-instruction");
  assert.deepEqual(ui.calls.slice(before).filter(call=>call.method==="POST").map(call=>call.url),
    ["/api/instructions/attachment-draft/submit"],"the submit does not wait for the unanswered prewarm");
  assert.equal(JSON.parse(ui.calls.at(-1).body).content,"馬上送出");
  assert.equal(prewarmPosts(ui).length,1);
});
test("a refused prewarm shows nothing and changes nothing",async()=>{
  const refused={ok:false,json:async()=>({error:{code:"PREWARM_DISABLED",message:"這個工作台沒有開啟打字預熱。"}})};
  const ui=await open(directSendChat(),{prewarm:true,response:refused});await ui.tick();
  const shown=[ui.text("error-message"),ui.text("instruction-reason"),ui.text("chat-presence")];
  typeAttachmentText(ui,"打字");await ui.tick();await ui.tick();await ui.tick();
  assert.equal(prewarmPosts(ui).length,1);
  assert.deepEqual([ui.text("error-message"),ui.text("instruction-reason"),ui.text("chat-presence")],shown);
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  assert.equal(ui.nodes.get("instruction").value,"打字");
});
test("without the bridge setting, typing never asks to wake the VM",async()=>{
  const ui=await open(directSendChat());await ui.tick();
  typeAttachmentText(ui,"打字");
  assert.equal(prewarmPosts(ui).length,0);
});
test("typing while the Agent's own turn runs, or outside the native lane, asks nothing",async()=>{
  const running=await open(nativeChat("started"),{prewarm:true});await running.tick();
  assert.ok(vm.runInContext("runningOwnTurn()",running.context),"the fixture's own turn is running");
  typeAttachmentText(running,"回合中打字");
  assert.equal(prewarmPosts(running).length,0);
  const legacy=await open(attachmentChat(),{prewarm:true});await legacy.tick();
  typeAttachmentText(legacy,"舊路線");
  assert.equal(prewarmPosts(legacy).length,0);
});
test("the prewarm goes to the selected Agent",async()=>{
  const data=agentChat("agent-one");data.composer.state_profile="foundry-native-v1";
  const ui=await open(data,{prewarm:true});await ui.tick();
  typeAttachmentText(ui,"給第一位助理");
  assert.equal(prewarmPosts(ui).length,1);
  assert.equal(prewarmPosts(ui)[0].headers["X-Console-Agent"],"agent-one");
});

test("the accepted turn shows from the submit reply before the full state is read",async()=>{
  const ui=await open(directSendChat(),{environmentResponse:unverifiedEnvironment});
  await ui.tick();
  typeAttachmentText(ui,"已接單要馬上看到");
  ui.holder.stateResponse=ui.holder.threadStateResponse=new Promise(()=>{});
  ui.click("send-instruction");await ui.tick();
  const accepted=nativeChat("accepted").tasks[0];
  Object.assign(accepted,{task_id:"attachment-draft",submit_request_id:"attachment-draft",thread_id:"attachment-thread",
    instruction:"已接單要馬上看到",submission:{state:"CONFIRMED"}});
  ui.holder.pendingPosts.at(-1).resolve({task:accepted});await ui.tick();await ui.tick();
  const text=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(text,/正在準備回覆/);
  assert.doesNotMatch(text,/助理準備中/);
  assert.ok(ui.calls.some(call=>call.url.startsWith("/api/state")),"the full state is still read afterwards");
});

// RTT A (Neal 2026-10-06, Grokbot and charter-agent): the message and a busy reply bubble are painted the moment the
// owner sends (dev v17: 5-9 ms). Every reply placeholder that is still moving pulses; a settled one does not.
test("a send paints the message and a pulsing reply at once; only moving states pulse",async()=>{
  const ui=await open(directSendChat(),{environmentResponse:unverifiedEnvironment});
  await ui.tick();
  typeAttachmentText(ui,"馬上就要看到");
  ui.holder.stateResponse=ui.holder.threadStateResponse=new Promise(()=>{});
  ui.click("send-instruction");
  const sending=turnNode(ui).children[3];
  assert.match(shownText(turnNode(ui).children[1]),/馬上就要看到/,"painted before the bridge answers anything");
  assert.equal(sending.textContent,"助理準備中…","the assistant's side is busy at once, never called accepted");
  assert.equal(sending.className,"assistant-message pending waiting");
  assert.equal(sending.getAttribute("aria-busy"),"true");
  const accepted=nativeChat("accepted").tasks[0];
  Object.assign(accepted,{task_id:"attachment-draft",submit_request_id:"attachment-draft",thread_id:"attachment-thread",
    instruction:"馬上就要看到",submission:{state:"CONFIRMED"}});
  ui.holder.pendingPosts.at(-1).resolve({task:accepted});await ui.tick();await ui.tick();
  const preparing=turnNode(ui).children[3];
  assert.equal(preparing.textContent,"正在準備回覆…");
  assert.equal(preparing.className,"assistant-message pending waiting");
  ui.render(nativeChat("unknown"));
  const settled=turnNode(ui).children[3];
  assert.doesNotMatch(settled.textContent,/^正在/);
  assert.equal(settled.className,"assistant-message pending");
  assert.equal(settled.getAttribute("aria-busy"),null);
  assert.match(html,/@media \(prefers-reduced-motion:reduce\)\{\.assistant-message\.waiting::after\{animation:none/);
});


function serverFollowingCase(code="AGENT_VERSION_FOLLOWING"){
  const data=agentChat("agent-one"),rid=data.composer.request_id,thread=data.composer.thread_id;
  Object.assign(data.composer,{state_profile:"foundry-native-v1",environment_policy:"fa-environment-system-policy-20261004"});
  data.tasks=[];
  const refused=copy(data),task=nativeChat("unknown",true).tasks[0];
  Object.assign(task,{task_id:rid,submit_request_id:rid,thread_id:thread,instruction:"換版也只送一次",
    native_receipt:null,admission:null,not_accepted:true,status:{data:{state:"not_accepted"},observed_at:when},
    submission:{state:"REFUSED",code,not_dispatched:true,server_following:true}});
  task.actions["submit-retry"]=action(true);
  refused.tasks=[task];Object.assign(refused.composer,{editable:false,can_submit:false,content:"換版也只送一次"});
  const accepted=copy(refused);accepted.tasks[0].not_accepted=false;
  accepted.tasks[0].status={data:{state:"accepted"},observed_at:when};accepted.tasks[0].submission={state:"CONFIRMED"};
  return {data,rid,thread,refused,accepted,
    rejection:{ok:false,status:code==="NATIVE_SESSION_CHANGED"?409:503,json:async()=>({error:{code,
      not_dispatched:true,server_following:true,request_id:rid,automatic_retry:false}})}};
}
const postUrls=(ui,before=0)=>ui.calls.slice(before).filter(call=>call.method==="POST").map(call=>call.url);
const fireDelay=async(ui,delay)=>{
  const entry=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===delay);
  assert.ok(entry,`expected ${delay}ms timer; timers=${JSON.stringify([...ui.timerDelays.values()])}`);
  ui.timers.delete(entry[0]);ui.timerDelays.delete(entry[0]);entry[1]();await ui.tick();await ui.tick();
};

for(const code of ["AGENT_VERSION_FOLLOWING","NATIVE_SESSION_CHANGED"]){
  test(`server_following ${code} retries the same RID without environment routes`,async()=>{
    const c=serverFollowingCase(code),ui=await open(c.data,{now:0});
    ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
      [`/api/tasks/${c.rid}/submit-retry`]:{ok:true,json:async()=>{ui.holder.current=copy(c.accepted);return {task:c.accepted.tasks[0]}}}};
    typeAttachmentText(ui,"換版也只送一次");
    const before=ui.calls.length;ui.holder.current=copy(c.refused);ui.click("send-instruction");
    await ui.tick();await ui.tick();
    assert.deepEqual(postUrls(ui,before),[`/api/instructions/${c.rid}/submit`]);
    assert.equal([...ui.timerDelays.values()].includes(3000),true);
    // ADR-0009 M4: a short change says nothing; the "換版中" text appears only 30 s after the automatic resend starts (M4.4).
    assert.doesNotMatch(ui.text("instruction-reason")+ui.text("error-message"),/助理正在換到新版本|訊息已保留/);
    await fireDelay(ui,30000);
    assert.match(ui.text("instruction-reason")+ui.text("error-message"),/助理正在換到新版本|訊息已保留/);
    await fireDelay(ui,3000);
    assert.deepEqual(postUrls(ui,before),[`/api/instructions/${c.rid}/submit`,`/api/tasks/${c.rid}/submit-retry`]);
    assert.equal(ui.calls.some(call=>/environment-(auto|prepare|reconcile)|without-files|\/environment$/.test(call.url)),false);
    assert.equal(ui.nodes.get("instruction").value,"");
  });
}

test("server_following keeps retrying every three seconds until success",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0});
  let retries=0;
  const retryResponse={_ok:false,status:503,get ok(){return this._ok},json:async()=>{
    retries++;
    if(retries===1){
      retryResponse._ok=false;
      return {error:{code:"AGENT_VERSION_FOLLOWING",not_dispatched:true,server_following:true,request_id:c.rid}};
    }
    retryResponse._ok=true;ui.holder.current=copy(c.accepted);return {task:c.accepted.tasks[0]};
  }};
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
    [`/api/tasks/${c.rid}/submit-retry`]:retryResponse};
  typeAttachmentText(ui,"換版也只送一次");const before=ui.calls.length;ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();await fireDelay(ui,3000);await fireDelay(ui,3000);
  assert.equal(postUrls(ui,before).filter(url=>url.endsWith("/submit-retry")).length,2);
  assert.equal(ui.nodes.get("instruction").value,"");
});

test("server_following stops after ten minutes and leaves the manual retry",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0});
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection};
  typeAttachmentText(ui,"換版也只送一次");const before=ui.calls.length;ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();ui.holder.now=600001;await fireDelay(ui,3000);
  assert.deepEqual(postUrls(ui,before),[`/api/instructions/${c.rid}/submit`]);
  assert.match(ui.text("error-message"),/換版還沒完成，訊息已保留。可稍後以原內容重送/);
  const shown=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(shown,/以原內容重送/);
});

test("server_following stops on a different error and shows it",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0});
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
    [`/api/tasks/${c.rid}/submit-retry`]:{ok:false,status:503,json:async()=>({error:{code:"NATIVE_FILES_UNKNOWN"}})}};
  typeAttachmentText(ui,"換版也只送一次");const before=ui.calls.length;ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();await fireDelay(ui,3000);
  assert.deepEqual(postUrls(ui,before),[`/api/instructions/${c.rid}/submit`,`/api/tasks/${c.rid}/submit-retry`]);
  assert.equal(vm.runInContext("lastError.code",ui.context),"NATIVE_FILES_UNKNOWN");
  assert.match(ui.text("error-message"),/未確認|不會自動重送/);
  assert.doesNotMatch(ui.text("error-message"),/沒有送出/);
});

for(const code of ["AGENT_VERSION_FOLLOWING","NATIVE_SESSION_CHANGED"]){
  test(`plain ${code} refusal is shown and not auto retried`,async()=>{
    const c=serverFollowingCase(code),ui=await open(c.data,{now:0});
    ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:{ok:false,status:code==="NATIVE_SESSION_CHANGED"?409:503,
      json:async()=>({error:{code,not_dispatched:true,request_id:c.rid}})}};
    typeAttachmentText(ui,"換版也只送一次");const before=ui.calls.length;ui.holder.current=copy(c.refused);ui.click("send-instruction");
    await ui.tick();await ui.tick();
    assert.deepEqual(postUrls(ui,before),[`/api/instructions/${c.rid}/submit`]);
    assert.equal([...ui.timerDelays.values()].includes(3000),false);
    assert.match(ui.text("error-message"),code==="NATIVE_SESSION_CHANGED"?/新版本還沒準備好/:/系統正在更新/);
  });
}





test("unflagged NATIVE_SESSION_CHANGED does not say the message was not sent",async()=>{
  const c=serverFollowingCase("NATIVE_SESSION_CHANGED"),ui=await open(c.data,{now:0});
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:{ok:false,status:409,
    json:async()=>({error:{code:"NATIVE_SESSION_CHANGED"}})}};
  typeAttachmentText(ui,"換版也只送一次");ui.click("send-instruction");
  await ui.tick();await ui.tick();
  assert.match(ui.text("error-message"),/未確認|不會自動重送/);
  assert.doesNotMatch(ui.text("error-message"),/沒有送出/);
});

test("plain NATIVE_FILES_UNKNOWN does not say the message was not sent",async()=>{
  const c=serverFollowingCase("AGENT_VERSION_FOLLOWING"),ui=await open(c.data,{now:0});
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:{ok:false,status:503,
    json:async()=>({error:{code:"NATIVE_FILES_UNKNOWN"}})}};
  typeAttachmentText(ui,"換版也只送一次");ui.click("send-instruction");
  await ui.tick();await ui.tick();
  assert.match(ui.text("error-message"),/未確認|不會自動重送/);
  assert.doesNotMatch(ui.text("error-message"),/沒有送出/);
});





test("leaving a server_following wait removes the automatic-send promise",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0});
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
    [`/api/tasks/${c.rid}/submit-retry`]:{ok:false,status:503,json:async()=>({error:{code:"AGENT_VERSION_FOLLOWING",not_dispatched:true,server_following:true,request_id:c.rid}})}};
  typeAttachmentText(ui,"換版也只送一次");ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();await fireDelay(ui,30000);
  assert.match(ui.text("error-message"),/完成後會自動送出/);
  vm.runInContext("clearServerFollowingState();render()",ui.context);
  assert.equal([...ui.timerDelays.values()].includes(3000),false,"leaving the wait clears the retry timer");
  for(const [id,fn] of [...ui.timers.entries()].filter(([id])=>ui.timerDelays.get(id)===3000)){
    ui.timers.delete(id);ui.timerDelays.delete(id);fn();
  }
  await ui.tick();await ui.tick();
  ui.render(c.refused);await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.url.endsWith("/submit-retry")).map(call=>call.url),[]);
  assert.match(ui.text("error-message"),/可稍後以原內容重送/);
  assert.doesNotMatch(ui.text("error-message"),/自動送出/);
  const tools=turnNode(ui).children.at(-1).children;
  assert.ok(tools.some(node=>node.textContent==="以原內容重送"&&!node.disabled));
});

test("manual same-RID retry joins server_following loop instead of only saying automatic",async()=>{
  const c=serverFollowingCase(),ui=await open(c.refused,{now:0});
  let retries=0;
  const retryResponse={_ok:false,status:503,get ok(){return this._ok},json:async()=>{
    retries++;
    if(retries===1){
      retryResponse._ok=false;
      return {error:{code:"AGENT_VERSION_FOLLOWING",not_dispatched:true,server_following:true,request_id:c.rid}};
    }
    retryResponse._ok=true;ui.holder.current=copy(c.accepted);return {task:c.accepted.tasks[0]};
  }};
  ui.holder.templateResponses={[`/api/tasks/${c.rid}/submit-retry`]:retryResponse};
  const button=turnNode(ui).children.at(-1).children.find(node=>node.textContent==="以原內容重送");
  assert.ok(button);
  const before=ui.calls.length;
  button.events.click();await ui.tick();await ui.tick();
  assert.deepEqual(postUrls(ui,before),[`/api/tasks/${c.rid}/submit-retry`]);
  assert.equal([...ui.timerDelays.values()].includes(3000),true);
  await fireDelay(ui,30000);
  assert.match(ui.text("error-message"),/完成後會自動送出/);
  await fireDelay(ui,3000);
  assert.deepEqual(postUrls(ui,before),[`/api/tasks/${c.rid}/submit-retry`,`/api/tasks/${c.rid}/submit-retry`]);
  assert.equal(ui.nodes.get("instruction").value,"");
});

test("server_following outside an active retry does not promise automatic send",async()=>{
  const ui=await open(nativeChat("completed"));
  const message=vm.runInContext('friendlyError({code:"AGENT_VERSION_FOLLOWING",server_following:true})',ui.context);
  assert.match(message,/可稍後以原內容重送/);
  assert.doesNotMatch(message,/自動送出/);
});

test("agent.environment push fires the waiting server_following retry immediately",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0,realtime:true});
  const socket=await connectPush(ui);
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
    [`/api/tasks/${c.rid}/submit-retry`]:{ok:true,json:async()=>{ui.holder.current=copy(c.accepted);return {task:c.accepted.tasks[0]}}}};
  typeAttachmentText(ui,"換版也只送一次");const before=ui.calls.length;ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();
  pushEvent(socket,{type:"agent.environment",agent_id:"agent-one"});await ui.tick();await ui.tick();
  assert.deepEqual(postUrls(ui,before),[`/api/instructions/${c.rid}/submit`,`/api/tasks/${c.rid}/submit-retry`]);
  assert.equal([...ui.timerDelays.values()].includes(3000),false);
});



test("unknown invocation errors do not say the message was not sent unless not_dispatched",async()=>{
  const ui=await open(nativeChat("completed"));
  const unknown=vm.runInContext('friendlyError({code:"FOUNDRY_INVOCATION_UNKNOWN"})',ui.context);
  assert.match(unknown,/未確認|核對|不會自動重送/);
  assert.doesNotMatch(unknown,/沒有送出/);
  const refused=vm.runInContext('friendlyError({code:"FOUNDRY_INVOCATION_UNKNOWN",not_dispatched:true})',ui.context);
  assert.match(refused,/訊息沒有送出/);
});



test("unsupported profile and environment-unavailable blockers do not claim a message was not sent",async()=>{
  const ui=await open(nativeChat("completed"));
  const profile=vm.runInContext('friendlyError({code:"NATIVE_PROFILE_UNSUPPORTED"})',ui.context);
  assert.match(profile,/設定組合不支援/);
  assert.doesNotMatch(profile,/沒有送出/);
  const blocked=blockedConversation("NATIVE_ENVIRONMENT_UNAVAILABLE");
  ui.render(blocked);
  assert.match(ui.text("follow-up-detail"),/目前不能接續|請聯絡管理員/);
  assert.doesNotMatch(ui.text("follow-up-detail"),/沒有送出/);
});

test("not-dispatched NATIVE_SESSION_CHANGED asks to resend later, not to follow environment",async()=>{
  const data=nativeChat("unknown"),task=data.tasks[0];
  task.native_receipt=null;task.admission=null;task.not_accepted=true;
  task.status={data:{state:"not_accepted"},observed_at:when};
  task.submission={state:"REFUSED",code:"NATIVE_SESSION_CHANGED",not_dispatched:true};
  task.actions["submit-retry"]=action(true);
  const ui=await open(data);
  const shown=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(shown,/助理的新版本還沒準備好.*訊息沒有送出.*以原內容重送/);
  assert.doesNotMatch(shown,/接續新環境|在新環境接續|檢查不搬檔接續/);
  assert.doesNotMatch(shown,/接單|伺服器|不會自動重送/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Stage release 28: a model picked after a completed turn was refused with NATIVE_CAPTURE_CHANGED and the page said only
// 「原因見支援明細」 under a 「發生問題」 header. Interim until a conversation can switch models.
function pinnedChat(){
  const data=nativeChat("completed");
  // The default differs from the pinned model, so showing the pinned one is not the default by chance.
  data.composer.models={...copy(models),default_model:"offline-chat-b"};
  return data;
}
test("after a completed turn the model dropdown shows the conversation's model and a new pick opens a new conversation",async()=>{
  const ui=await open(pinnedChat()),model=ui.nodes.get("model");
  assert.equal(model.value,"offline-chat-a");
  assert.equal(ui.nodes.get("model-lock-note").hidden,true);
  model.value="offline-chat-b";model.events.change();
  assert.equal(model.value,"offline-chat-a","the pick goes back to the conversation's model");
  assert.equal(ui.nodes.get("model-lock-note").hidden,false);
  assert.equal(ui.text("model-lock-text"),"這段對話固定用 OFFLINE 聊天部署 A；要換模型請開新對話");
  assert.equal(ui.text("model-lock-new-conversation"),"用 OFFLINE 聊天部署 B 開新對話");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false,"changing the dropdown writes nothing");
  ui.render(pinnedChat());
  assert.equal(model.value,"offline-chat-a","a later read keeps the conversation's model");
  ui.click("model-lock-new-conversation");await ui.tick();
  assert.deepEqual(postUrls(ui),["/api/instructions/conversations/new"]);
  assert.equal(vm.runInContext("modelDrafts.get(scopeKey(OPENING_DRAFT))",ui.context),"offline-chat-b");
  assert.equal(model.value,"offline-chat-b","the opening conversation starts on the picked model");
  assert.equal(ui.nodes.get("model-lock-note").hidden,true);
});
test("before any completed turn the model stays free to choose",async()=>{
  for(const data of [nativeChat("unknown"),(()=>{const value=pinnedChat();value.tasks[0].admission=null;return value})()]){
    data.composer.models={...copy(models),default_model:"offline-chat-a"};
    const ui=await open(data),model=ui.nodes.get("model");
    model.value="offline-chat-b";model.events.change();
    assert.equal(model.value,"offline-chat-b");
    assert.equal(ui.nodes.get("model-lock-note").hidden,true);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  }
});
test("a NATIVE_CAPTURE_CHANGED refusal says the conversation keeps its model and nothing ran",async()=>{
  const data=pinnedChat(),refused=copy(nativeChat("unknown").tasks[0]);
  Object.assign(refused,{task_id:"capture-refused",submit_request_id:"capture-refused",instruction:"換模型再問",
    native_receipt:null,admission:null,not_accepted:true,status:{data:{state:"not_accepted"},observed_at:when},
    submission:{state:"REFUSED",code:"NATIVE_CAPTURE_CHANGED",not_dispatched:true}});
  data.tasks.push(refused);
  const ui=await open(data);
  const said="這段對話固定用 OFFLINE 聊天部署 A，換模型要開新對話；這則訊息沒有執行。";
  const shown=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.ok(shown.includes(said),"the refused turn says why");
  assert.doesNotMatch(shown,/支援明細|修改後重新送出/);
  vm.runInContext('lastError={code:"NATIVE_CAPTURE_CHANGED",not_dispatched:true,request_id:"capture-refused"};render()',ui.context);
  assert.equal(ui.text("error-message"),said);
  assert.notEqual(ui.text("chat-presence"),"發生問題");
  assert.equal(ui.text("chat-presence"),"需要處理");
  assert.equal(vm.runInContext('friendlyError({code:"NATIVE_CAPTURE_CHANGED",not_dispatched:true})',ui.context),said);
  // Without not_dispatched it is a changed resend or receipt, not a model pick: the general text and header stay.
  assert.doesNotMatch(vm.runInContext('friendlyError({code:"NATIVE_CAPTURE_CHANGED"})',ui.context),/沒有執行|換模型/);
  vm.runInContext('lastError={code:"NATIVE_CAPTURE_CHANGED"};render()',ui.context);
  assert.equal(ui.text("chat-presence"),"發生問題");
  // With no known model the sentence leaves the name out.
  ui.render(nativeChat("unknown"));
  assert.equal(vm.runInContext('friendlyError({code:"NATIVE_CAPTURE_CHANGED",not_dispatched:true})',ui.context),
    "這段對話固定用原本的模型，換模型要開新對話；這則訊息沒有執行。");
});

// FA_NATIVE_MODEL_SWITCH: the API reports the selected Hosted version's flag with the Responses options; the page only
// follows it. With it on the interim pin above is off, and a switching send says once that its turn is slower.
function switchingChat(){
  const data=pinnedChat();
  data.composer.responses={enabled:true,history_source:"conversation",model_switch:true};
  Object.assign(data.composer,{request_id:"draft-rid",editable:true,can_submit:true,content:null,observed_at:when});
  return data;
}
test("with model switching on, the dropdown stays free after a completed turn and nothing is pinned",async()=>{
  const ui=await open(switchingChat()),model=ui.nodes.get("model");
  assert.equal(vm.runInContext("pinnedConversationModel()",ui.context),null);
  model.value="offline-chat-b";model.events.change();
  assert.equal(model.value,"offline-chat-b","the pick stays");
  assert.equal(ui.nodes.get("model-lock-note").hidden,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false,"changing the dropdown writes nothing");
  // The API may report the flag while the Hosted session still refuses a model change: the refusal keeps its own
  // capture-changed sentence (nothing ran, open a new conversation), not the general text.
  assert.equal(vm.runInContext('friendlyError({code:"NATIVE_CAPTURE_CHANGED",not_dispatched:true})',ui.context),
    "這段對話固定用原本的模型，換模型要開新對話；這則訊息沒有執行。");
  // Off (no model_switch from the server): the #821 pin is back.
  const off=switchingChat();delete off.composer.responses.model_switch;
  ui.render(off);
  assert.equal(vm.runInContext("pinnedConversationModel()",ui.context),"offline-chat-a");
});
test("a send that switches the model says once that this turn is slower; the same model says nothing",async()=>{
  const ui=await open(switchingChat()),model=ui.nodes.get("model");
  const said="已換成 OFFLINE 聊天部署 B；這一回合會比較慢（重新載入對話）。";
  model.value="offline-chat-b";model.events.change();
  typeAttachmentText(ui,"換模型後的問題");
  ui.click("send-instruction");await ui.tick();
  const post=ui.holder.pendingPosts[0];
  assert.equal(post.url,"/api/instructions/draft-rid/submit");
  assert.equal(JSON.parse(ui.calls.find(call=>call.url===post.url).body).model,"offline-chat-b");
  assert.equal(ui.nodes.get("model-switch-note").hidden,false);
  assert.equal(ui.text("model-switch-note"),said);
  const started=copy(ui.holder.current.tasks[0]);
  Object.assign(started,{task_id:"draft-rid",submit_request_id:"draft-rid",instruction:"換模型後的問題",reply:null,
    admission:{...started.admission,turn_number:2,model:"offline-chat-b"},submission:{state:"CONFIRMED"},
    native_receipt:{...started.native_receipt,status:"started"},status:{data:{state:"started"},observed_at:when}});
  post.resolve({task:started});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("model-switch-note").hidden,false,"shown while that turn runs");
  const done=switchingChat();
  done.tasks.push({...copy(started),native_receipt:{...started.native_receipt,status:"completed"},
    status:{data:{state:"completed"},observed_at:when}});
  ui.render(done);
  assert.equal(ui.nodes.get("model-switch-note").hidden,true,"once: gone when the turn ends");
  // A follow-up starts on the model the conversation last ran on (not the project default), so it says nothing.
  const again=await open(switchingChat());
  assert.equal(again.nodes.get("model").value,"offline-chat-a");
  typeAttachmentText(again,"同一個模型再問");
  again.click("send-instruction");await again.tick();
  assert.equal(again.nodes.get("model-switch-note").hidden,true);
});
test("a follow-up after a switched turn that ended unknown stays on that turn's model, as the server does",async()=>{
  // _allocate adopted offline-chat-b for turn 2 whatever its outcome, so the conversation is on B now.
  const data=switchingChat(),unknown=copy(data.tasks[0]);
  Object.assign(unknown,{task_id:"switched-unknown",submit_request_id:"switched-unknown",instruction:"換模型",reply:null,
    admission:{...unknown.admission,turn_number:2,model:"offline-chat-b"},
    native_receipt:{...unknown.native_receipt,status:"unknown"},status:{data:{state:"unknown"},observed_at:when}});
  // A send refused before it was allocated (no turn number) did not change the conversation's model.
  const refused={...copy(unknown),task_id:"refused-a",submit_request_id:"refused-a",
    admission:{...unknown.admission,turn_number:null,model:"offline-chat-a"},not_accepted:true,
    status:{data:{state:"not_accepted"},observed_at:when}};
  data.tasks.push(unknown,refused);
  const ui=await open(data);
  assert.equal(ui.nodes.get("model").value,"offline-chat-b");
  typeAttachmentText(ui,"接著問");
  ui.click("send-instruction");await ui.tick();
  assert.equal(JSON.parse(ui.calls.find(call=>call.url==="/api/instructions/draft-rid/submit").body).model,"offline-chat-b");
  assert.equal(ui.nodes.get("model-switch-note").hidden,true,"no switch, so no notice");
});
test("a switch the conversation is too long for names the new model and says nothing ran",async()=>{
  const data=switchingChat(),refused=copy(nativeChat("unknown").tasks[0]);
  Object.assign(refused,{task_id:"too-long",submit_request_id:"too-long",instruction:"換小模型",requested_model:"offline-chat-b",
    native_receipt:null,admission:null,not_accepted:true,status:{data:{state:"not_accepted"},observed_at:when},
    submission:{state:"REFUSED",code:"NATIVE_MODEL_CONTEXT_EXCEEDED",not_dispatched:true}});
  data.tasks.push(refused);
  const ui=await open(data);
  const said="這段對話太長，OFFLINE 聊天部署 B 放不下；請開新對話或改用原模型。";
  const shown=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.ok(shown.includes(said),"the refused turn says why");
  vm.runInContext('lastError={code:"NATIVE_MODEL_CONTEXT_EXCEEDED",not_dispatched:true,request_id:"too-long"};render()',ui.context);
  assert.equal(ui.text("error-message"),said);
  assert.equal(ui.text("chat-presence"),"需要處理");
});
test("#821 follow-ups: 新對話 passes no model, a failed open takes the pick back, a lock refusal names its own thread",
  async()=>{
    const ui=await open(pinnedChat());
    ui.holder.response={ok:false,status:503,json:async()=>({error:{code:"BACKEND_UNAVAILABLE"}})};
    vm.runInContext('modelDrafts.set(scopeKey(OPENING_DRAFT),"offline-chat-a")',ui.context);
    ui.nodes.get("new-conversation").events.click({type:"click"});await ui.tick();await ui.tick();
    assert.equal(vm.runInContext("modelDrafts.get(scopeKey(OPENING_DRAFT))",ui.context),"offline-chat-a",
      "a click event is not a model");
    await vm.runInContext('openNewConversation("offline-chat-b")',ui.context);
    assert.equal(vm.runInContext("modelDrafts.get(scopeKey(OPENING_DRAFT))",ui.context),"offline-chat-a",
      "the conversation did not open: the earlier pick is back");
    // The refused message belongs to another thread whose last completed turn ran on offline-chat-b.
    const other=copy(ui.holder.current.tasks[0]);
    Object.assign(other,{task_id:"other-done",thread_id:"other-thread",admission:{...other.admission,model:"offline-chat-b"}});
    const refused={...copy(other),task_id:"other-refused",admission:null,not_accepted:true,
      status:{data:{state:"not_accepted"},observed_at:when}};
    const data=pinnedChat();data.tasks.push(other,refused);ui.render(data);
    assert.equal(vm.runInContext('friendlyError({code:"NATIVE_CAPTURE_CHANGED",not_dispatched:true,request_id:"other-refused"})',
      ui.context),"這段對話固定用 OFFLINE 聊天部署 B，換模型要開新對話；這則訊息沒有執行。");
  });

test("definite native refusal exposes only an explicit original-input retry",async()=>{
  const data=nativeChat("unknown"),task=data.tasks[0];
  task.native_receipt=null;task.not_accepted=true;task.admission=null;
  task.status={data:{state:"not_accepted"},observed_at:when};
  task.submission={state:"REFUSED",code:"AGENT_VERSION_FOLLOWING"};
  task.actions["submit-retry"]=action(true);
  const ui=await open(data);
  assert.equal(ui.nodes.get("submission-reconcile-panel").hidden,false);
  assert.equal(ui.nodes.get("submit-retry").disabled,false);
  assert.match(ui.text("submit-retry-reason"),/明確未接單.*同一 RID.*先前拒絕紀錄保留/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  ui.click("submit-retry");await ui.tick();
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,`/api/tasks/${task.task_id}/submit-retry`);
});

test("observed version refusal preserves the unknown submission without offering replay",async()=>{
  const data=nativeChat("unknown"),task=data.tasks[0];
  task.native_receipt=null;task.not_accepted=true;task.admission=null;
  task.status={data:{state:"not_accepted"},observed_at:when};
  task.submission={state:"UNKNOWN",code:"NATIVE_SESSION_CHANGED"};
  task.submission_observation={schema_version:"fa-native-admission-observation-v1",
    state:"not_accepted",retry_allowed:false};
  task.actions["submit-retry"]=action(false);
  const ui=await open(data);
  assert.equal(ui.nodes.get("submit-retry").disabled,true);
  assert.match(ui.text("submit-retry-reason"),/原.*未確認.*保留.*不.*重送/);
  assert.match(ui.text("work-message"),/沒有接單/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an observed unaccepted first message stops polling and the owner can write again",async()=>{
  const data=nativeChat("unknown"),task=data.tasks[0];
  task.native_receipt=null;task.not_accepted=true;task.admission=null;
  task.status={data:{state:"not_accepted"},observed_at:when};
  task.submission={state:"UNKNOWN",code:"NATIVE_SESSION_CHANGED"};
  task.submission_observation={schema_version:"fa-native-admission-observation-v1",state:"not_accepted",
    retry_allowed:false};
  task.actions["submit-retry"]=action(false);
  data.composer.conversation=null;data.composer.conversation_id=null;
  data.composer.follow_up_blocker="NATIVE_SUBMISSION_NOT_ACCEPTED";
  data.composer.can_new_conversation=true;data.composer.progress_available=true;
  // #679: the bridge leaves the composer open; the next message gets a new RID (and, with no CID yet, a new
  // conversation); the turn itself shows it was not accepted.
  data.composer.can_follow_up=true;
  const ui=await open(data);
  assert.equal(vm.runInContext("progressTimer",ui.context),null);
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.nodes.get("new-conversation").disabled,false);
  assert.equal(ui.nodes.get("submit-retry").disabled,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  assert.deepEqual(data.tasks[0].submission,{state:"UNKNOWN",code:"NATIVE_SESSION_CHANGED"});
});

test("an unaccepted later message does not stop polling an older active turn",async()=>{
  const data=nativeChat("started"),original=data.tasks[0];
  const refused={...copy(original),task_id:"not-accepted-rid",submit_request_id:"not-accepted-rid",
    native_receipt:null,not_accepted:true,admission:null,status:{data:{state:"not_accepted"},observed_at:when},
    submission:{state:"UNKNOWN",code:"NATIVE_SESSION_CHANGED"}};
  data.tasks.push(refused);
  const ui=await open(data);
  assert.notEqual(vm.runInContext("progressTimer",ui.context),null);
});

for(const [code,pattern] of [
  ["NATIVE_SESSION_CHANGED",/新版本還沒準備好/],
  ["AGENT_VERSION_FOLLOWING",/系統正在更新/],
  ["NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE",/保存前文.*空白/],
  ["NATIVE_ENVIRONMENT_UNKNOWN",/暫時沒有完成/],
  ["NATIVE_ENVIRONMENT_UNAVAILABLE",/目前不能接續|請聯絡管理員/],
]){
  test(`known native environment blocker ${code} has a specific explanation`,async()=>{
    const data=blockedConversation(code),ui=await open(data);
    assert.match(ui.text("follow-up-detail"),pattern);
    assert.doesNotMatch(ui.text("follow-up-detail"),/原因未提供/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

for(const profile of ["foundry-native-v1","foundry-responses-v1"]){
test(`native states ${profile} retain their meaning without inventing jobs or tool counts`,async()=>{
  const data=nativeChat();data.tasks[0].state_profile=data.composer.state_profile=profile;
  const ui=await open(data);
  assert.match(ui.text("job-state"),/執行中/);
  const panel=inlineProcessing(ui);
  assert.match(panel.children[0].textContent,/正在回覆/);
  assert.equal(panel.children.length,1,"no receipt bookkeeping on the main screen");
  assert.match(ui.text("processing-detail"),/沒有提供工具步驟或用量時，不填成零/);
  assert.doesNotMatch(panel.children[0].textContent,/0 次工具|0 個工具/);
  const unknown=nativeChat("unknown");unknown.tasks[0].state_profile=unknown.composer.state_profile=profile;ui.render(unknown);
  assert.equal(ui.nodes.get("follow-up-blocker").hidden,true,"an unknown end does not lock the conversation");
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});
}

test("Responses Agent view keeps its original thread and cannot show another CID",async()=>{
  const data=nativeChat("completed",true);
  data.tasks[0].state_profile=data.composer.state_profile="foundry-responses-v1";
  const other=copy(data.tasks[0]);other.task_id="another-responses-rid";other.thread_id="another-responses-thread";
  other.agent_text="Another CID private input.";other.reply.data.text="Another CID private output.";
  data.tasks.push(other);
  const ui=await open(data),turn=turnNode(ui);
  assert.equal(turn.children[0].textContent,"Agent 訊息（不是你的原話）");
  assert.equal(shownText(turn.children[3]),data.tasks[0].reply.data.text.trimEnd());
  assert.equal(ui.nodes.get("task").children.length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native unstarted unknown explains a new explicit submission without replaying",async()=>{
  const data=nativeChat("unknown");
  data.tasks[0].native_receipt.failure_code="NATIVE_TURN_NOT_STARTED";
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.equal(panel.open,true);
  assert.match(panel.children[0].textContent,/這次沒有開始執行/);
  assert.equal(panel.children[1].textContent,"請重新送出這則訊息。");
  assert.match(ui.text("processing-detail"),/NATIVE_TURN_NOT_STARTED/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  const ordinary=nativeChat("unknown");ui.render(ordinary);
  assert.equal(inlineProcessing(ui).children[0].textContent,"結果不確定");
  assert.equal(inlineProcessing(ui).children.length,1);
});

test("native message an earlier version left reads as final and asks for a new message",async()=>{
  const data=nativeChat("unknown");
  data.tasks[0].native_receipt.failure_code="NATIVE_EARLIER_VERSION_UNFINISHED";
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.equal(panel.open,true);
  assert.match(panel.children[0].textContent,/較早的版本接下的訊息/);
  assert.equal(panel.children[1].textContent,"這則訊息是較早的版本接下的，目前環境無法接續；請用新訊息再送一次。");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native message an earlier version left and Hosted marked stalled reads with the same notice",async()=>{
  const data=nativeChat("unknown");
  data.tasks[0].native_receipt.failure_code="NATIVE_SOURCE_STALLED";
  data.tasks[0].native_receipt.environment_notice={kind:"earlier_version_unfinished"};
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.equal(panel.open,true);
  assert.match(panel.children[0].textContent,/較早的版本接下的訊息/);
  assert.equal(panel.children[1].textContent,"這則訊息是較早的版本接下的，目前環境無法接續；請用新訊息再送一次。");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native model refusal explains that nothing ran and another model may be asked",async()=>{
  const data=nativeChat("unknown");
  data.tasks[0].native_receipt.failure_code="NATIVE_MODEL_REFUSED";
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.equal(panel.open,true);
  assert.match(panel.children[0].textContent,/模型拒絕了這次請求/);
  assert.match(panel.children[1].textContent,/模型服務拒絕了這次請求.*沒有執行任何動作.*可以換模型再問一次/);
  assert.doesNotMatch(panel.children[1].textContent,/不會自動重送/);
  assert.match(turnNode(ui).children[3].textContent,/^模型服務拒絕了這次請求/);
  assert.doesNotMatch(turnNode(ui).children[3].textContent,/結果未知/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native Agent history is labeled separately and keeps a complete result",async()=>{
  const data=nativeChat("completed",true),ui=await open(data);
  const turn=turnNode(ui);
  assert.equal(turn.children[0].textContent,"Agent 訊息（不是你的原話）");
  assert.equal(turn.children[0].className,"speaker agent-source");
  assert.match(html,/\.turn \.speaker\.agent-source\{[^}]*position:static/);
  assert.equal(turn.children[1].className,"user-message agent-message markdown");
  assert.equal(shownText(turn.children[1]),data.tasks[0].agent_text);
  assert.equal(shownText(turn.children[3]),data.tasks[0].reply.data.text.trimEnd());
  assert.equal(ui.nodes.get("fetch").disabled,false);
  assert.equal(ui.nodes.get("acknowledge").disabled,false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native routine history is scheduled data rather than a new human message",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.instruction=null;task.agent_text=null;task.source=null;
  task.routine={occurrence:{routine_id:"daily-summary",approved_version:"original-version",
    scheduled_for:when,time_zone:"UTC"},authorization:false};
  task.routine_text="排程原文 <script>不是標記</script>";
  task.submission={state:"OBSERVED",method:"GET"};
  const ui=await open(data),turn=turnNode(ui);
  assert.ok(turn);
  assert.equal(turn.children[0].textContent,"排程觸發（不是你的新訊息）");
  assert.equal(turn.children[0].className,"speaker agent-source");
  assert.equal(turn.children[1].textContent,task.routine_text);
  assert.equal(shownText(turn.children[3]),task.reply.data.text.trimEnd());
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("routine reminders annotate existing attention without another approval card or writer",async()=>{
  const data=hitlChat(),entry=data.composer.attention[0],call=entry.item.approval.call;
  const reminder={routine_id:"review-reminder",occurrence_id:"1".repeat(64),scheduled_for:when,
    agent_id:call.agent_id,conversation_id:call.conversation_id,request_id:call.request_id,
    status:"started",failure_code:null,authorization:false};
  entry.reminder=reminder;
  data.composer.routines={enabled:true,reminders:[reminder],error:null};
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>hitlRead(data)})});
  assert.equal(ui.nodes.get("attention-items").children.length,1);
  assert.equal(ui.nodes.get("work-reminder-status")?.hidden,false);
  assert.match(ui.text("work-reminder-status"),/original-rid/);
  const row=ui.nodes.get("attention-items").children[0];
  const notes=row.children.filter(node=>node.attributes?.["data-routine-reminder"]===reminder.occurrence_id);
  assert.equal(notes.length,1);
  assert.match(notes[0].textContent,/原項目.*不是新批准/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  data.composer.attention=[];data.composer.native_hitl.references=[];data.composer.routines.reminders=[];
  ui.render(data);
  assert.equal(ui.nodes.get("attention-panel").hidden,true);
});

test("one routine issue remains visible without hiding another original attention item",async()=>{
  const data=hitlChat();
  data.composer.routines={enabled:true,reminders:[],error:null,issues:[
    {routine_id:"dst-routine",code:"ROUTINE_LOCAL_TIME_MISSING",purpose:"original-reference-issue-not-retry-permission"},
    {routine_id:"unadmitted",request_id:"retained-rid",code:"NATIVE_RID_MISSING",
      purpose:"original-reference-issue-not-retry-permission"},
  ]};
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>hitlRead(data)})});
  assert.equal(ui.nodes.get("attention-items").children.length,1);
  assert.equal(ui.nodes.get("attention-panel").hidden,false);
  assert.match(ui.text("work-reminder-status"),/dst-routine.*ROUTINE_LOCAL_TIME_MISSING.*unadmitted.*NATIVE_RID_MISSING/);
  assert.match(ui.text("work-reminder-status"),/不重送/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("automatic continuation stays separate from the original unknown and never sends a human message",async()=>{
  const data=nativeChat("unknown"),original=data.tasks[0];
  const child=copy(nativeChat("completed").tasks[0]);
  child.task_id=child.submit_request_id="resume-observed";
  child.thread_id=original.thread_id;child.instruction=null;child.agent_text=null;child.source=null;
  child.continuation={continuation_of:original.task_id,root_request_id:original.task_id,attempt:1};
  child.continuation_text="原請求 <script>not executable</script>";
  child.submission={state:"OBSERVED"};
  original.continuation_usage={known_requests:3,unknown_attempts:1};
  data.tasks.push(child);
  const ui=await open(data),turns=ui.nodes.get("conversation-history").children;
  assert.equal(turns.length,2);
  assert.equal(turns[0].children[0].textContent,"你");
  assert.doesNotMatch(turns[0].children.map(node=>node.textContent).join("\n"),/用量/);
  vm.runInContext(`selected=${JSON.stringify(original.task_id)};render()`,ui.context);
  assert.match(ui.text("identifiers"),/"continuation_usage": \{\s*"known_requests": 3,\s*"unknown_attempts": 1/);
  assert.match(turns[1].children[0].textContent,/自動接續.*1.*2.*不是新訊息/);
  assert.equal(turns[1].children[1].textContent,child.continuation_text);
  assert.equal(shownText(turns[1].children[3]),child.reply.data.text.trimEnd());
  assert.equal(original.native_receipt.status,"unknown");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native notification exhaustion is visible without replacing the saved reply",async()=>{
  const data=nativeChat("completed"),ui=await open(data);
  const original=data.tasks[0].reply.data.text;
  data.tasks[0].native_receipt.notification_error="NATIVE_TURN_BUSY";
  ui.render(data);
  const panel=inlineProcessing(ui);
  assert.equal(panel.open,true);
  assert.equal(panel.children[0].textContent,"和另一位助理的協作還沒確認");
  assert.doesNotMatch(panel.children[1].textContent,/NATIVE_TURN_BUSY/);
  assert.match(panel.children[1].textContent,/不要重新交辦/);
  assert.match(ui.text("processing-detail"),/NATIVE_TURN_BUSY/);
  assert.equal(shownText(turnNode(ui).children[3]),original.trimEnd());
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const status of ["not_delivered","unknown"]){
  test(`queued delivery ${status} is visible without resending or rewriting the sender reply`,async()=>{
    const data=nativeChat("completed"),original=data.tasks[0].reply.data.text;
    data.tasks[0].native_receipt.queued_relay={status,reason:"NATIVE_RELAY_WAIT_EXPIRED"};
    const ui=await open(data),panel=inlineProcessing(ui);
    assert.equal(panel.open,true);
    assert.match(panel.children[0].textContent,status==="not_delivered"?/交辦沒有送達/:/還不確定交辦有沒有送達/);
    assert.match(panel.children[1].textContent,/不要重新交辦/);
    assert.match(ui.text("processing-detail"),new RegExp(`交辦狀態：${status}（NATIVE_RELAY_WAIT_EXPIRED）`));
    assert.equal(shownText(turnNode(ui).children[3]),original.trimEnd());
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("a tool source the turn ran without is named plainly under the reply; codes stay in the support details",async()=>{
  // Neal 2026-10-10: 「工具列不出來時，這一回合少那個工具照常跑，並在畫面上講明」.
  const data=nativeChat("completed"),original=data.tasks[0].reply.data.text;
  data.tasks[0].native_receipt.unavailable_sources=[
    {entry_id:"WebIQ",source:"WebIQ",code:"TOOLBOX_LIST_FAILED",reason:"upstream_auth"},
    {entry_id:"Other",source:"Other",code:"TOOLBOX_LIST_FAILED",reason:"service_error"}];
  const ui=await open(data),turn=turnNode(ui);
  const notes=turn.children.filter(node=>node.className==="reason tool-source-unavailable");
  assert.deepEqual(notes.map(node=>node.textContent),[
    "這次沒用到 WebIQ：工具清單讀不到（上游驗證失敗），其他功能照常。",
    "這次沒用到 Other：工具清單讀不到（工具服務暫時無法使用），其他功能照常。"]);
  // Right after the assistant's reply, which stays the original saved text.
  assert.equal(turn.children.indexOf(notes[0]),4);
  assert.equal(shownText(turn.children[3]),original.trimEnd());
  assert.doesNotMatch(notes.map(node=>node.textContent).join("\n"),/TOOLBOX_LIST_FAILED|upstream_auth|NATIVE_/);
  vm.runInContext(`selected=${JSON.stringify(data.tasks[0].task_id)};render()`,ui.context);
  assert.match(ui.text("identifiers"),/"code": "TOOLBOX_LIST_FAILED",\s*"reason": "upstream_auth"/);
  // A turn that had every source shows no such line.
  const plain=nativeChat("completed");ui.render(plain);
  assert.equal(turnNode(ui).children.some(node=>node.className==="reason tool-source-unavailable"),false);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

function nativeDraftCase(){
  const data=nativeChat("completed"),task=data.tasks[0],body="<script>Plain draft text</script>\n";
  task.actions["read-skill-draft"]=action(true);
  const raw=Buffer.from(body),digest=require("node:crypto").createHash("sha256").update(raw).digest("hex");
  const value={agent_id:"configured-agent",conversation_id:task.admission.conversation_id,request_id:task.task_id,
    state:"awaiting_decision",body,receipt:{native_source:{request_id:task.task_id},approved:false,
      artifact_id:"a".repeat(64),version_id:"b".repeat(32),sha256:digest,byte_length:raw.length,
      purpose:{description:"Original draft purpose"}}};
  return {data,body,raw,digest,value};
}
// P10: the draft's original text is in the support details, never inside the conversation.
function supportDraft(ui){
  return ui.nodes.get("skill-draft-body").children.find(node=>node.className==="native-skill-draft");
}

test("native skill draft is read as unapproved text and download never approves it",async(t)=>{
  const {data,body,raw,digest,value}=nativeDraftCase();
  const ui=await open(data,{skillDraftResponse:{ok:true,json:async()=>value},
    skillDraftDownloadResponse:{ok:true,arrayBuffer:async()=>raw,headers:{get:name=>name==="x-content-sha256"?digest:null}}});
  const download=t.mock.method(ui.context,"downloadNativeSkillDraft");
  const button=turnActions(turnNode(ui)).find(node=>node.textContent==="查看技能草稿");
  assert.ok(button);button.click();await ui.tick();await ui.tick();
  assert.equal(turnNode(ui).children.some(node=>node.className==="native-skill-draft"),false);
  assert.doesNotMatch(JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value),
    /Plain draft text/);
  assert.equal(ui.nodes.get("diagnostics").hidden,false);
  assert.equal(ui.nodes.get("skill-draft-support").hidden,false);
  assert.equal(ui.nodes.get("skill-draft-support").open,true);
  const panel=supportDraft(ui);
  assert.ok(panel);assert.match(panel.children[1].textContent,/未批准、未生效/);
  assert.equal(panel.children[3].textContent,body);
  panel.children[4].click();await download.mock.calls[0].result;
  assert.equal(await ui.holder.downloaded.text(),body);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

for(const failure of ["different-bytes","different-hash","denied"]){
  test(`native skill draft download ${failure} cannot save a success-shaped file`,async(t)=>{
    const {data,raw,digest,value}=nativeDraftCase();
    const response={ok:true,arrayBuffer:async()=>failure==="different-bytes"?Buffer.from("wrong draft"):raw,
      headers:{get:name=>name==="x-content-sha256"?(failure==="different-hash"?"0".repeat(64):digest):null}};
    if(failure==="denied"){response.ok=false;response.json=async()=>({error:{code:"BACKEND_DENIED"}})}
    const ui=await open(data,{skillDraftResponse:{ok:true,json:async()=>value},skillDraftDownloadResponse:response});
    const download=t.mock.method(ui.context,"downloadNativeSkillDraft");
    turnActions(turnNode(ui)).find(node=>node.textContent==="查看技能草稿").click();
    await ui.tick();await ui.tick();
    supportDraft(ui).children[4].click();
    await download.mock.calls[0].result;
    assert.equal(ui.holder.downloaded,undefined);
    const panel=supportDraft(ui);
    assert.ok(panel.children.at(-1).textContent);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("native skill draft late download from an earlier login is discarded",async(t)=>{
  const {data,raw,digest,value}=nativeDraftCase();let finish;
  const ui=await open(data,{skillDraftResponse:{ok:true,json:async()=>value},skillDraftDownloadResponse:{
    ok:true,arrayBuffer:()=>new Promise(resolve=>{finish=resolve}),headers:{get:()=>digest}}});
  const download=t.mock.method(ui.context,"downloadNativeSkillDraft");
  turnActions(turnNode(ui)).find(node=>node.textContent==="查看技能草稿").click();
  await ui.tick();await ui.tick();
  supportDraft(ui).children[4].click();
  await ui.tick();
  ui.render({...data,authentication:"LOGIN_EXPIRED"});ui.render(data);
  finish(raw);await download.mock.calls[0].result;
  assert.equal(ui.holder.downloaded,undefined);
  assert.equal(supportDraft(ui),undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native skill draft read rejects a response for another original request",async()=>{
  const {data,value}=nativeDraftCase();value.request_id="different-rid";
  const ui=await open(data,{skillDraftResponse:{ok:true,json:async()=>value}});
  turnActions(turnNode(ui)).find(node=>node.textContent==="查看技能草稿").click();
  await ui.tick();await ui.tick();
  const panel=supportDraft(ui);
  assert.equal(panel.children.length,2);
  assert.equal(ui.holder.downloaded,undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native delivered readback does not erase the original unknown acknowledgement",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.acknowledgment={state:"UNKNOWN",observed_at:when};
  task.native_receipt.delivered=true;
  const ui=await open(data);
  assert.match(ui.text("ack-detail"),/未知紀錄保留/);
  assert.match(ui.text("ack-state"),/已確認收到/);
  assert.equal(task.acknowledgment.state,"UNKNOWN");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("terminal blocked continuation is not advertised as ready or still waiting for a reply",async()=>{
  const data=blockedConversation(),ui=await open(data);
  assert.equal(ui.text("chat-presence"),"需要處理");
  assert.doesNotMatch(ui.nodes.get("instruction").placeholder,/等待/);
  assert.match(ui.text("instruction-reason"),/恢復資料不完整/);
  assert.equal(ui.nodes.get("instruction").disabled,true);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("blocked continuation translates only known public reasons",async()=>{
  for(const [code,reason] of [
    ["SDK_STATE_UNSUPPORTED_FORMAT",/不支援.*恢復格式/],
    ["SDK_STATE_UPLOAD_UNKNOWN",/保存結果未確認/],
    ["SDK_STATE_PRIVATE_MATERIAL",/未通過隱私檢查/],
    ["SDK_NOT_STARTED_RESET_REQUIRED",/未建立可接續的 SDK 狀態/],
    ["CONVERSATION_SESSION_RESET_REQUIRED",/沒有可沿用的工作環境/],
    ["SOP_COPY_OUTCOME_UNKNOWN",/複製操作的結果未確認/],
    ["turn_unresolved",/原工作仍有未結狀態/],
  ]){
    const ui=await open(blockedConversation(code));
    assert.match(ui.text("instruction-reason"),reason);
    assert.match(ui.text("follow-up-detail"),reason);
    assert.equal(ui.nodes.get("send-instruction").disabled,true);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  }
});

test("new conversation opens a blank composer at once and shows the created one from the POST reply, without another read",async()=>{
  const old=nativeChat("completed"),scope=agentChat("agent-one");
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
    old[key]=scope[key];
  const ui=await open(old);
  typeAttachmentText(ui,"舊對話未送草稿");
  ui.click("new-conversation");await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"","The old draft is kept with the old conversation, not shown here");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.match(ui.text("instruction-reason"),/正在開新對話/);
  assert.match(JSON.stringify(ui.nodes.get("conversation-history").children),/正在開新對話/);
  assert.doesNotMatch(JSON.stringify(ui.nodes.get("conversation-history").children),/完整回覆/);
  const next=agentChat("agent-one");next.tasks=[];
  Object.assign(next.composer,{thread_id:"new-thread",request_id:"new-thread",content:"",
    state_profile:"foundry-responses-v1",can_new_conversation:true});
  newConversationPost(ui).resolve(next);await ui.tick();await ui.tick();
  ui.documentEvents.visibilitychange();await ui.tick();
  assert.equal(ui.calls.some(call=>call.url==="/api/state?thread_id=new-thread"),false,"the POST reply is the new state");
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.doesNotMatch(ui.text("instruction-reason"),/正在開新對話/);
  typeAttachmentText(ui,"新對話第一次輸入");
  ui.render(next);await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"新對話第一次輸入");
  assert.equal(vm.runInContext("state.composer.request_id",ui.context),"new-thread");
  assert.equal(vm.runInContext(`inputDrafts.get(scopeKey(${JSON.stringify(old.composer.request_id)}))`,ui.context),"舊對話未送草稿");
  assert.equal(ui.calls.filter(call=>call.url==="/api/instructions/conversations/new"&&call.method==="POST").length,1);
  assertNothingSent(ui);
});

for(const profile of ["foundry-native-v1","foundry-responses-v1"]){
  for(const reply of ["empty","peer-card"]){
    test(`the first new-conversation draft never becomes empty during Team attention reads: ${profile}/${reply}`,async()=>{
      const c=teamAttentionCase(),old=c.data;
      old.composer.state_profile=old.tasks[0].state_profile=profile;
      old.composer.environment_policy="fa-environment-system-policy-20261004";
      let slow=false,finishTeam,environmentThread=old.composer.thread_id;
      const metadata={schema_version:"fa-native-environment-metadata-v1",agent_id:"agent-one",
        hosted_session_id:"current-sid",hosted_agent_version:"53",state:"unverified",observed_sessions:[],
        state_scope:"platform-metadata-only",preparation_verified:false,scope:"entire-agent",
        model_started:false,original_work_replayed:false};
      const empty={...copy(c.page),items:[]};
      const ui=await open(old,{now:Date.parse(when),
        environmentResponse:{ok:true,json:async()=>({thread_id:environmentThread,environment:metadata})},
        hitl:async(url)=>{
          if(!url.includes("/attention"))return {ok:true,json:async()=>copy(c.view)};
          if(slow)return new Promise(resolve=>{finishTeam=resolve});
          return {ok:true,json:async()=>copy(reply==="empty"?empty:c.page)};
        }});
      await ui.tick();await ui.tick();
      const next=agentChat("agent-one");next.tasks=[];
      Object.assign(next.composer,{thread_id:"fresh-thread",request_id:"fresh-thread",content:"",state_profile:profile,
        environment_policy:"fa-environment-system-policy-20261004",
        native_hitl:copy(old.composer.native_hitl),attention:[]});
      ui.click("new-conversation");await ui.tick();
      assert.equal(ui.nodes.get("instruction").value,"");
      assert.equal(ui.nodes.get("send-instruction").disabled,true);
      assert.match(ui.text("instruction-reason"),/正在開新對話/);
      slow=true;environmentThread="fresh-thread";
      ui.holder.pendingPosts[0].resolve(next);await ui.tick();await ui.tick();
      assert.equal(ui.nodes.get("instruction").disabled,false);
      assert.equal(finishTeam,undefined,"a new conversation reuses the Team-wide list instead of rescanning it");
      const background=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===60000);
      ui.timers.delete(background[0]);background[1]();await ui.tick();
      assert.ok(finishTeam,"a background Team read is still in flight");
      typeAttachmentText(ui,"新對話第一次填入；不可被 Team 重繪洗掉");
      const input=ui.nodes.get("instruction"),writes=[];let value=input.value;
      Object.defineProperty(input,"value",{get:()=>value,set:nextValue=>{writes.push(nextValue);value=nextValue}});
      slow=false;finishTeam({ok:true,json:async()=>copy(reply==="empty"?empty:c.page)});
      await ui.tick();await ui.tick();
      for(let interval=0;interval<4;interval++){
        const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===60000);
        assert.ok(timer,"continue four slow background Team refreshes");
        ui.timers.delete(timer[0]);await timer[1]();await ui.tick();await ui.tick();
        assert.equal(input.value,"新對話第一次填入；不可被 Team 重繪洗掉");
      }
      assert.equal(writes.includes(""),false,"even a temporary empty write must be observable");
      assert.equal(ui.nodes.get("send-instruction").disabled,false);
      assert.equal(vm.runInContext("draftId",ui.context),"fresh-thread");
      assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
      assert.equal(ui.calls.some(call=>call.url.endsWith("/submit")||call.url.endsWith("/next")),false);
    });
  }
}

function delayedNewConversation(){
  const old=nativeChat("completed"),scope=agentChat("agent-one");
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
    old[key]=scope[key];
  const next=agentChat("agent-one");next.tasks=[];
  Object.assign(next.composer,{thread_id:"new-thread",request_id:"new-thread",content:"",
    state_profile:"foundry-native-v1",can_new_conversation:true});
  return {old,next};
}
function chooseModel(ui,id){ui.nodes.get("model").value=id;ui.nodes.get("model").events.change()}
// RTT A round 2: text typed after a finished turn takes the conversation's next RID ahead (/next, local, never a send).
// These cases are about other flows, so they look up their own POST and only require that nothing was sent.
function newConversationPost(ui){
  return ui.holder.pendingPosts.filter(post=>post.url==="/api/instructions/conversations/new").at(-1);
}
function assertNothingSent(ui){
  assert.equal(ui.calls.some(call=>call.url.endsWith("/submit")),false);
  assert.ok(ui.calls.filter(call=>call.url.endsWith("/next")).length<=1,"at most the one RID taken ahead");
}

test("typing and a model chosen while a delayed new conversation opens stay in the new conversation",async()=>{
  const {old,next}=delayedNewConversation(),ui=await open(old);
  typeAttachmentText(ui,"舊對話草稿");
  ui.click("new-conversation");await ui.tick();
  assert.equal(ui.nodes.get("instruction").disabled,false,"the blank new composer accepts typing while it opens");
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.nodes.get("model").value,"offline-chat-a");
  assert.equal(ui.nodes.get("conversation").disabled,true);
  typeAttachmentText(ui,"切換延遲期間打的新訊息");
  chooseModel(ui,"offline-chat-b");
  ui.click("send-instruction");
  ui.nodes.get("instruction").events.keydown({key:"Enter",preventDefault(){}});
  for(let page=0;page<3;page++){vm.runInContext("render()",ui.context);await ui.tick()}
  assert.equal(ui.nodes.get("instruction").value,"切換延遲期間打的新訊息");
  assert.equal(ui.nodes.get("model").value,"offline-chat-b");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  let finish;
  ui.holder.threadStateResponse=new Promise(resolve=>{finish=resolve});
  newConversationPost(ui).resolve(next);await ui.tick();await ui.tick();
  vm.runInContext("render()",ui.context);
  assert.equal(ui.nodes.get("instruction").value,"切換延遲期間打的新訊息");
  finish({ok:true,json:async()=>copy(next)});await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("state.composer.thread_id",ui.context),"new-thread");
  assert.equal(ui.nodes.get("instruction").value,"切換延遲期間打的新訊息");
  assert.equal(ui.nodes.get("model").value,"offline-chat-b");
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  assert.equal(ui.nodes.get("conversation").disabled,false);
  assert.equal(vm.runInContext(`inputDrafts.get(scopeKey("offline"))`,ui.context),"舊對話草稿");
  assert.equal(vm.runInContext("inputDrafts.has(scopeKey(OPENING_DRAFT))",ui.context),false);
  assert.equal(ui.calls.filter(call=>call.method==="POST"&&!call.url.endsWith("/next")).length,1);
  assertNothingSent(ui);
});

test("an older read finishing after the new conversation is shown cannot bring the old one back",async()=>{
  const {old,next}=delayedNewConversation(),ui=await open(old);
  let finishOld;
  ui.holder.progressResponse=new Promise(resolve=>{finishOld=resolve});
  const reading=vm.runInContext("readState({progress:true})",ui.context);await ui.tick();
  ui.click("new-conversation");await ui.tick();
  ui.holder.pendingPosts[0].resolve(next);await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("state.composer.thread_id",ui.context),"new-thread");
  typeAttachmentText(ui,"被延遲的舊讀取不可洗掉");
  finishOld({ok:true,json:async()=>copy(old)});await reading;
  for(let i=0;i<4;i++)await ui.tick();
  assert.equal(vm.runInContext("state.composer.thread_id",ui.context),"new-thread");
  assert.equal(ui.nodes.get("instruction").value,"被延遲的舊讀取不可洗掉");
  assert.equal(vm.runInContext("busy",ui.context),false);
  assert.equal(ui.calls.filter(call=>call.url==="/api/state?thread_id=new-thread").length,0);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("a failed new conversation returns to the old draft and keeps what was typed for the next one",async()=>{
  const {old,next}=delayedNewConversation(),ui=await open(old);
  typeAttachmentText(ui,"舊對話草稿");
  let fail;ui.holder.response=new Promise(resolve=>{fail=resolve});
  ui.click("new-conversation");await ui.tick();
  typeAttachmentText(ui,"沒開成也不能丟");
  chooseModel(ui,"offline-chat-b");
  fail({ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE",message:"OFFLINE new conversation failed"}})});
  for(let i=0;i<4;i++)await ui.tick();
  assert.equal(vm.runInContext("state.composer.thread_id",ui.context),old.composer.thread_id);
  assert.equal(ui.nodes.get("instruction").value,"舊對話草稿");
  assert.equal(ui.nodes.get("model").value,"offline-chat-a");
  assert.equal(ui.nodes.get("error").hidden,false);
  assert.match(ui.text("instruction-reason"),/剛才在新對話打的字已保留/);
  ui.holder.response=null;ui.holder.threadStateResponse={ok:true,json:async()=>copy(next)};
  ui.click("new-conversation");await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"沒開成也不能丟");
  assert.equal(ui.nodes.get("model").value,"offline-chat-b");
  newConversationPost(ui).resolve(next);for(let i=0;i<4;i++)await ui.tick();
  assert.equal(vm.runInContext("state.composer.thread_id",ui.context),"new-thread");
  assert.equal(ui.nodes.get("instruction").value,"沒開成也不能丟");
  assert.equal(ui.nodes.get("model").value,"offline-chat-b");
  assert.doesNotMatch(ui.text("instruction-reason"),/已保留/);
  assertNothingSent(ui);
});

test("新對話 waits while a message is still being sent, so the switch cannot silently drop it",async()=>{
  const {old}=delayedNewConversation(),ui=await open(old);
  typeAttachmentText(ui,"正在送出的訊息");
  ui.click("send-instruction");await ui.tick();
  assert.ok(ui.holder.pendingPosts.some(post=>/\/(next|submit)$/.test(post.url)),"the send is still in flight");
  assert.equal(ui.nodes.get("new-conversation").disabled,true);
  ui.click("new-conversation");await ui.tick();
  assert.equal(ui.calls.some(call=>call.url==="/api/instructions/conversations/new"),false);
  assert.equal(ui.nodes.get("instruction").value,"正在送出的訊息");
});

test("empty Team attention pages neither redraw the composer nor rewrite the chosen model",async()=>{
  const c=teamAttentionCase(),reads=[];
  // No known model on the completed turn leaves the choice free (no pin), so a rewrite of the pick would show.
  c.data.tasks[0].admission.model=null;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url)=>{
    if(!url.includes("/attention"))return {ok:true,json:async()=>copy(c.view)};
    reads.push(url);
    const page=Number(/cursor=p(\d+)/.exec(url)?.[1]||0);
    return {ok:true,json:async()=>copy(page<6?{...c.page,items:[],next_cursor:"p"+(page+1)}:c.page)};
  }});
  for(let i=0;i<12;i++)await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,1);
  chooseModel(ui,"offline-chat-b");
  const select=ui.nodes.get("model"),writes=[];let value=select.value;
  Object.defineProperty(select,"value",{get:()=>value,set:nextValue=>{writes.push(nextValue);value=nextValue}});
  const before=reads.length;
  await vm.runInContext("readTeamAttention()",ui.context);await ui.tick();
  assert.equal(reads.length-before,7,"every page is still read");
  assert.deepEqual(writes,[],"no page rewrites the model choice");
  assert.equal(select.value,"offline-chat-b");
  assert.equal(hitlNodes(ui,"data-hitl-card",c.reference.pending_id).length,1);
});

test("a login that expired while a new conversation was opening keeps the expiry and reads the state again",async()=>{
  const old=nativeChat("completed"),ui=await open(old);
  typeAttachmentText(ui,"登入過期也保留的原草稿");
  ui.click("new-conversation");await ui.tick();
  const next=copy(old);
  next.composer.thread_id=next.composer.request_id="new-thread";
  ui.holder.response={ok:false,json:async()=>({error:{code:"LOGIN_EXPIRED"}})};
  await vm.runInContext("refreshAuthentication()",ui.context);
  ui.holder.response=null;
  ui.holder.threadStateResponse={ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE",message:"OFFLINE read failed"}})};
  newConversationPost(ui).resolve(next);await ui.tick();await ui.tick();await ui.tick();
  assert.equal(vm.runInContext("busy",ui.context),false);
  assert.equal(ui.nodes.get("login").disabled,false);
  assert.equal(ui.nodes.get("instruction").disabled,true);
  assert.equal(vm.runInContext("state.authentication",ui.context),"LOGIN_EXPIRED","a reply sent before the expiry is not adopted");
  assert.equal(ui.nodes.get("instruction").value,"登入過期也保留的原草稿");
  assert.equal(ui.calls.filter(call=>call.url==="/api/instructions/conversations/new"&&call.method==="POST").length,1);
  assertNothingSent(ui);
});

for(const stage of ["post","state"]){
  test(`a new-conversation ${stage} reply arriving after an Agent switch preserves the other draft`,async()=>{
    const old=nativeChat("completed"),a=agentChat("agent-one"),b=agentChat("agent-two");
    for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])
      old[key]=a[key];
    b.tasks=[];
    const ui=await open(old,{agentStates:{"agent-two":b}});
    ui.click("new-conversation");await ui.tick();
    const next=copy(a);next.tasks=[];
    next.composer.thread_id=next.composer.request_id="new-agent-one-thread";
    let finish;
    if(stage==="state"){
      ui.holder.threadStateResponse=new Promise(resolve=>{finish=resolve});
      ui.holder.pendingPosts[0].resolve(next);await ui.tick();await ui.tick();
    }
    sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();
    typeAttachmentText(ui,"另一位助理的未送草稿");
    if(stage==="post")ui.holder.pendingPosts[0].resolve(next);
    else finish({ok:true,json:async()=>copy(next)});
    await ui.tick();await ui.tick();
    assert.equal(ui.text("chat-title"),"第二位助理");
    assert.equal(ui.nodes.get("instruction").value,"另一位助理的未送草稿");
    assert.equal(ui.nodes.get("instruction").disabled,false);
    assert.equal(vm.runInContext("busyAgents.has('agent-one')",ui.context),false);
    assert.equal(ui.calls.filter(call=>call.url==="/api/instructions/conversations/new"&&call.method==="POST").length,1);
    assert.equal(ui.calls.some(call=>call.url.endsWith("/submit")||call.url.endsWith("/next")),false);
  });
}

test("version-changed conversation asks for a new conversation without changing identity or sending",async()=>{
  for(const canNew of [false,true]){
    const data=blockedConversation("CONVERSATION_SESSION_VERSION_CHANGED");
    data.composer.can_new_conversation=canNew;
    const original=JSON.stringify(data.composer),ui=await open(data);
    assert.match(ui.text("instruction-reason"),/執行環境已被平台換成新版本/);
    assert.equal(ui.text("instruction-reason").includes("請開新對話"),canNew);
    assert.doesNotMatch(ui.text("instruction-reason"),/重設對話/);
    assert.equal(ui.nodes.get("send-instruction").disabled,true);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
    assert.equal(JSON.stringify(data.composer),original);
    const value=vm.runInContext('friendlyError({code:"CONVERSATION_SESSION_VERSION_CHANGED"})',ui.context);
    assert.match(value,/平台換成新版本.*請開新對話.*不會重送/);
  }
});

test("failed original result still awaiting forwarding is visible without implying completion",async()=>{
  const data=blockedConversation("CONVERSATION_FAILURE_DELIVERY_PENDING");
  data.composer.can_reset=false;
  const ui=await open(data);
  assert.match(ui.text("instruction-reason"),/原工作已失敗.*結果尚待轉交.*更新進度/);
  assert.match(ui.text("follow-up-detail"),/不會重新執行/);
  assert.equal(ui.nodes.get("reset-session").disabled,true);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.text("chat-presence"),"需要處理");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("blocked continuation with an unknown or missing reason never echoes or guesses it",async()=>{
  for(const code of ["PRIVATE_BLOCKER_CANARY","toString",null]){
    const ui=await open(blockedConversation(code));
    assert.equal(ui.text("instruction-reason"),"目前不能接續，原因未提供");
    assert.equal(ui.text("follow-up-detail"),"目前不能接續，原因未提供");
    assert.equal(ui.nodes.get("follow-up-blocker").hidden,false);
    assert.equal(ui.text("chat-presence"),"需要處理");
    if(code)assert.equal([...ui.nodes.values()].some(node=>node.textContent.includes(code)),false);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  }
});

test("blocked continuation hints preserve the original reset and new-conversation flags",async()=>{
  for(const canReset of [false,true]){
    for(const canNew of [false,true]){
      const data=blockedConversation("SDK_NOT_STARTED_RESET_REQUIRED");
      data.composer.can_reset=canReset;data.composer.can_new_conversation=canNew;
      const original=JSON.stringify(data.composer),ui=await open(data);
      const reason=ui.text("instruction-reason");
      assert.equal(reason.includes("「重設對話」"),canReset);
      assert.equal(reason.includes("「新對話 · New session」"),canNew);
      assert.equal(ui.nodes.get("reset-session").disabled,!canReset);
      assert.equal(ui.nodes.get("new-conversation").disabled,!canNew);
      assert.equal(ui.nodes.get("send-instruction").disabled,true);
      assert.equal(JSON.stringify(data.composer),original);
      assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
    }
  }
});

test("blocked continuation preserves the draft and clears only when the backend permits follow-up",async()=>{
  const data=blockedConversation();
  const ready=copy(data);
  ready.composer.can_follow_up=ready.composer.conversation.can_follow_up=true;
  ready.composer.follow_up_blocker=ready.composer.conversation.follow_up_blocker=null;
  const ui=await open(ready);
  ui.nodes.get("instruction").value="Keep this unsent follow-up";
  ui.render(data);
  assert.equal(ui.nodes.get("instruction").value,"Keep this unsent follow-up");
  assert.equal(ui.nodes.get("instruction").disabled,true);
  assert.equal(ui.text("chat-presence"),"需要處理");
  ui.render(ready);
  assert.equal(ui.nodes.get("instruction").value,"Keep this unsent follow-up");
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.text("chat-presence"),"可以聊天");
  assert.equal(ui.nodes.get("instruction").placeholder,"接著問…");
  assert.equal(ui.nodes.get("follow-up-blocker").hidden,true);
  assert.equal(ui.text("follow-up-detail"),"");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("blocked continuation does not borrow a previous turn to classify a new frozen submission",async()=>{
  const data=blockedConversation(null);
  data.composer.request_id="new-frozen-request";
  data.composer.content="A new original submission";
  for(const canSubmit of [true,false]){
    data.composer.can_submit=canSubmit;
    const ui=await open(data);
    assert.equal(ui.nodes.get("follow-up-blocker").hidden,true);
    assert.equal(ui.text("follow-up-detail"),"");
    assert.doesNotMatch(ui.text("instruction-reason"),/目前不能接續/);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  }
});

test("blocked continuation does not replace active work or invent an archived recovery action",async()=>{
  const active=cancellableChat();
  active.composer.follow_up_blocker="turn_unresolved";
  const running=await open(active);
  assert.equal(running.text("chat-presence"),"可以聊天");
  assert.match(running.text("instruction-reason"),/助理處理中/);
  assert.equal(running.nodes.get("instruction").placeholder,"等待助理回覆…");
  assert.doesNotMatch(running.text("follow-up-detail"),/可從選單/);
  const archived=blockedConversation("CONVERSATION_ARCHIVED");
  archived.composer.archived=archived.composer.conversation.archived=true;
  archived.composer.can_reset=archived.composer.can_new_conversation=false;
  const closed=await open(archived);
  assert.equal(closed.text("chat-presence"),"需要處理");
  assert.match(closed.text("instruction-reason"),/已封存/);
  assert.doesNotMatch(closed.text("instruction-reason"),/重設對話|新對話/);
  assert.equal(closed.nodes.get("reset-session").disabled,true);
  assert.equal(closed.nodes.get("new-conversation").disabled,true);
  assert.equal(closed.calls.filter(call=>call.method==="POST").length,0);
});

function unknownCancellationChat(){
  const data=cancellableChat(),task=data.tasks[0];
  task.cancellation={state:"UNKNOWN",intent:{generation:0}};
  task.actions.cancel=action(false);
  task.actions["read-cancel-receipt"]=action(true);
  return data;
}

function unknownSubmission(){
  const data=cancellableChat(),task=data.tasks[0];
  task.submission={state:"UNKNOWN"};task.admission=null;task.actions.cancel=action(false);
  task.actions["submit-reconcile"]=action(true);task.actions["submit-retry"]=action(false);
  data.composer.conversation=null;data.composer.progress_available=false;
  return data;
}

test("original submission retry is manual after reconciliation and double click sends once",async()=>{
  const data=unknownSubmission(),ui=await open(data);
  assert.equal(ui.nodes.get("submit-retry").disabled,true);
  ui.click("submit-retry");assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  ui.click("submit-reconcile");
  assert.equal(ui.calls.at(-1).url,"/api/tasks/offline/submit-reconcile");
  data.tasks[0].actions["submit-retry"]=action(true);
  ui.holder.current=data;ui.holder.pending({task:data.tasks[0]});await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  assert.equal(ui.nodes.get("submit-retry").disabled,false);
  assert.match(ui.nodes.get("conversation-history").children[0].children[3].textContent,/沒有送達.*同一則訊息重試/);
  assert.doesNotMatch(ui.nodes.get("conversation-history").children[0].children[3].textContent,/接單|佇列|未確認紀錄/);
  assert.equal(ui.text("submit-retry"),"以完整原請求重試","the retry action stays in the support details");
  ui.nodes.get("instruction").value="Unrelated later draft";
  ui.click("submit-retry");ui.click("submit-retry");
  const calls=ui.calls.filter(call=>call.url.endsWith("/submit-retry"));
  assert.equal(calls.length,1);
  assert.deepEqual(JSON.parse(calls[0].body),{});
  assert.equal(data.tasks[0].submission.state,"UNKNOWN");
});

test("original submission retry remains disabled for unknown or denied reconciliation",async()=>{
  for(const code of ["WORK_SUBMIT_UNKNOWN","BACKEND_DENIED","SUBMISSION_OBSERVATION_INVALID"]){
    const data=unknownSubmission(),ui=await open(data);
    ui.holder.response={ok:false,json:async()=>({error:{code}})};
    ui.click("submit-reconcile");await ui.tick();await ui.tick();
    assert.equal(ui.nodes.get("submit-retry").disabled,true);
    ui.click("submit-retry");
    assert.equal(ui.calls.some(call=>call.url.endsWith("/submit-retry")),false);
    assert.equal(data.tasks[0].submission.state,"UNKNOWN");
  }
});

test("original submission retry late response cannot switch to its old thread or resend",async()=>{
  const data=unknownSubmission();data.tasks[0].actions["submit-retry"]=action(true);
  const ui=await open(data);ui.click("submit-retry");
  const next=copy(data);next.composer.thread_id="other-thread";next.composer.request_id="other-request";
  next.tasks=[];
  ui.nodes.get("conversation").value="other-thread";ui.holder.current=next;
  await ui.nodes.get("conversation").events.change();
  ui.holder.pending({task:data.tasks[0]});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("conversation").value,"other-thread");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
  assert.equal(ui.nodes.get("submit-retry").disabled,true);
});
function cancellationObservation(task){
  return {schema_version:"fa-cancellation-observation-v1",task_id:task.task_id,job_id:task.job_id,
    generation:0,observed_at:when,read_only:true,original_outcome:"UNKNOWN",
    receipt:{result:{status:"cancelled",cancel_accepted:true,executor_stopped:true},
      control_request_id:"a".repeat(64),control_request_digest:"b".repeat(64)}};
}
function receiptButton(ui){
  return turnActions(ui.nodes.get("conversation-history").children[0])
    .find(button=>button.textContent==="核對原停止回條");
}

function downloadableChat(){
  const data=cancellableChat(),task=data.tasks[0];
  task.reply={data:{text:"Original saved answer"},sha256:"c".repeat(64),byte_length:21};
  task.actions["read-reply"]=action(true);
  task.actions.cancel=action(false);
  data.composer.conversation.turns[0].state="succeeded";
  return data;
}
test("Agent handoff cards show saved status and original work without claiming human delivery",async()=>{
  const data={...cancellableChat(),...Object.fromEntries(Object.entries(agentChat("agent-one"))
    .filter(([key])=>["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"].includes(key)))};
  const task=data.tasks[0];
  const value={schema_version:"fa-agent-message-list-v1",agent_id:"agent-one",binding_id:task.binding_id,
    conversation_id:task.admission.conversation_id,messages:[{
      message_key:"original-message",message_id:"message-one",root_request_id:"root-one",
      sender_agent_id:"agent-one",recipient_agent_id:"agent-two",from_work_id:task.work_id,to_work_id:"peer-work",
      from_conversation_id:task.admission.conversation_id,to_conversation_id:"peer-conversation",
      kind:"handoff",hop:1,status:"queued",decision:{action:"queued",reason:"parent_busy",
        settles_human_obligation:false,model_dispatch_authorized:false},human_obligation_fulfilled:false,
    }]};
  const ui=await open(data,{agentMessagesResponse:()=>({ok:true,json:async()=>copy(value)})});
  const button=()=>turnActions(ui.nodes.get("conversation-history").children[0])
    .find(node=>node.textContent==="查看交接紀錄");
  assert.ok(button());
  button().events.click();await ui.tick();await ui.tick();
  const note=()=>ui.nodes.get("conversation-history").children[0].children
    .find(node=>node.className==="agent-message-status");
  assert.match(note().textContent,/第一位助理.*第二位助理/);
  assert.match(note().textContent,/等待/);
  assert.match(note().textContent,/不表示你已收到回覆/);
  value.messages[0].status="completed";value.messages[0].kind="result";value.messages[0].decision.action="silence";
  button().events.click();await ui.tick();await ui.tick();
  assert.match(note().textContent,/已保存/);
  assert.match(note().textContent,/不表示你已收到回覆/);
  assert.doesNotMatch(note().textContent,/offline-work|peer-work/,"work IDs stay in the support details");
  assert.match(ui.text("identifiers"),/"to_work_id": "peer-work"/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.ok(ui.calls.filter(call=>call.url.endsWith("/agent-messages"))
    .every(call=>call.headers["X-Console-Agent"]==="agent-one"));
});

// Two retained-message projections only; this fixture does not execute the B consumer.
function secondPieceChat(saved=false){
  return {...(saved?downloadableChat():cancellableChat()),
    ...Object.fromEntries(Object.entries(agentChat("agent-one"))
      .filter(([key])=>["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"].includes(key)))};
}
function secondHopProjection(data,kind="result",status="completed"){
  const task=data.tasks[0],cid=task.admission.conversation_id;
  const handoff={
    message_key:"original-handoff",message_id:"handoff-one",root_request_id:"original-root",
    sender_agent_id:"agent-one",recipient_agent_id:"agent-two",from_work_id:task.work_id,
    to_work_id:"peer-work-without-local-submit",from_conversation_id:cid,to_conversation_id:"peer-conversation",
    kind:"handoff",hop:1,status:"completed",decision:{action:"queued",reason:"original_handoff_consumed",
      settles_human_obligation:false,model_dispatch_authorized:false},human_obligation_fulfilled:false,
  };
  const returned={...handoff,message_key:"original-return",message_id:"return-two",
    sender_agent_id:"agent-two",recipient_agent_id:"agent-one",from_work_id:handoff.to_work_id,to_work_id:task.work_id,
    from_conversation_id:handoff.to_conversation_id,to_conversation_id:cid,kind,hop:2,status,
    decision:{action:kind==="result"?"silence":"needs_attention",
      reason:kind==="result"?"original_result_saved":"original_peer_blocked",
      settles_human_obligation:false,model_dispatch_authorized:false}};
  return {schema_version:"fa-agent-message-list-v1",agent_id:"agent-one",binding_id:task.binding_id,
    conversation_id:cid,messages:[handoff,returned]};
}
function handoffButton(ui){
  return turnActions(ui.nodes.get("conversation-history").children[0])
    .find(node=>node.textContent==="查看交接紀錄");
}
function handoffPanel(ui){
  return ui.nodes.get("conversation-history").children[0]?.children
    .find(node=>node.className==="agent-message-status");
}

for(const saved of [false,true]){
  test(`second-hop completed metadata preserves A's ${saved?"saved final":"pending reply"} without human receipt`,async()=>{
    const data=secondPieceChat(saved),projection=secondHopProjection(data);
    const ui=await open(data,{agentMessagesResponse:{ok:true,json:async()=>copy(projection)}});
    const answer=()=>ui.nodes.get("conversation-history").children[0].children
      .find(node=>node.className?.startsWith("assistant-message")).textContent;
    const originalAnswer=answer(),originalAck=ui.text("ack-detail"),draft=ui.nodes.get("instruction").value;
    handoffButton(ui).events.click();await ui.tick();await ui.tick();
    const panel=handoffPanel(ui);
    assert.match(panel.textContent,/第一位助理 → 第二位助理：交接／紀錄已處理/);
    assert.match(panel.textContent,/第二位助理 → 第一位助理：結果／結果已保存/);
    assert.match(panel.textContent,/不表示你已收到回覆/);
    assert.equal(answer(),originalAnswer);
    assert.equal(ui.text("ack-detail"),originalAck);
    const peerLinks=panel.children.filter(node=>node.textContent==="打開第二位助理的原對話");
    assert.equal(peerLinks.length,2);
    for(const link of peerLinks){assert.equal(link.disabled,true);await link.events.click()}
    const originalLink=panel.children.find(node=>node.textContent==="打開第一位助理的原對話");
    assert.equal(originalLink.disabled,false);await originalLink.events.click();
    assert.equal(ui.text("chat-title"),"第一位助理");
    assert.equal(ui.nodes.get("instruction").value,draft);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
    assert.ok(ui.calls.filter(call=>call.url.endsWith("/agent-messages"))
      .every(call=>call.headers["X-Console-Agent"]==="agent-one"));
  });
}

for(const [status,label] of [["needs_attention","需要處理"],["unknown","結果還不確定"]]){
  test(`second-hop blocked ${status} is neither FYI silence nor A success and never resends`,async()=>{
    const data=secondPieceChat(),projection=secondHopProjection(data,"blocked",status);
    const ui=await open(data,{agentMessagesResponse:{ok:true,json:async()=>copy(projection)}});
    const presence=ui.text("chat-presence"),ack=ui.text("ack-detail");
    handoffButton(ui).events.click();await ui.tick();await ui.tick();
    handoffButton(ui).events.click();await ui.tick();await ui.tick();
    assert.match(handoffPanel(ui).textContent,new RegExp(`第二位助理 → 第一位助理：阻礙／${label}`));
    assert.doesNotMatch(handoffPanel(ui).textContent,/不另提醒|通知／|結果已保存|工作成功/);
    assert.equal(ui.text("chat-presence"),presence);
    assert.equal(ui.text("ack-detail"),ack);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/agent-messages")).length,2);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("second-hop late A projection cannot populate B or another A conversation or move either draft",async()=>{
  const a=secondPieceChat(),b=agentChat("agent-two");
  a.composer.threads=[{thread_id:a.composer.thread_id,label:"原 A 對話"},
    {thread_id:"other-a-thread",label:"另一段 A 對話"}];
  b.tasks=[];
  const otherA=agentChat("agent-one");
  otherA.tasks=[];otherA.composer.thread_id="other-a-thread";otherA.composer.request_id="other-a-draft";
  otherA.composer.threads=copy(a.composer.threads);
  let finish;
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},
    agentMessagesResponse:new Promise(resolve=>{finish=resolve})});
  handoffButton(ui).events.click();await ui.tick();
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  ui.nodes.get("instruction").value="B 的原草稿";ui.nodes.get("instruction").events.input();
  assert.equal(handoffPanel(ui),undefined);
  agentButton(ui,0).events.click();await ui.tick();await ui.tick();
  ui.holder.agentStates["agent-one"]=otherA;
  ui.nodes.get("conversation").value="other-a-thread";
  await ui.nodes.get("conversation").events.change();await ui.tick();
  ui.nodes.get("instruction").value="另一段 A 的原草稿";ui.nodes.get("instruction").events.input();
  finish({ok:true,json:async()=>secondHopProjection(a)});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"另一段 A 的原草稿");
  assert.equal(handoffPanel(ui),undefined);
  assert.equal(ui.nodes.get("conversation").value,"other-a-thread");
  assert.equal(sidebar(ui,"agent-two").badge,undefined,"an old CID must not introduce a new activity hint");
  agentButton(ui,1).events.click();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"B 的原草稿");
  assert.equal(handoffPanel(ui),undefined);
  const reads=ui.calls.filter(call=>call.url.endsWith("/agent-messages"));
  assert.equal(reads.length,1);
  assert.equal(reads[0].headers["X-Console-Agent"],"agent-one");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

function delegationChat(){
  const data=cancellableChat();
  data.tasks[0].actions["read-delegation"]=action(true);
  data.composer.conversation_id=data.tasks[0].admission.conversation_id;
  return data;
}
function delegationObservation(data,status="running",extra={}){
  const task=data.tasks[0],admission=task.admission;
  return {task_id:task.task_id,job_id:task.job_id,conversation_id:admission.conversation_id,
    turn_number:admission.turn_number,read_at:when,delegation:{
      schema_version:"fa-delegation-reply-v1",parent_job_id:task.job_id,sdk_session_id:"original-sdk",
      delegation_id:"original-delegation",tool_call_id:"original-tool",input_sha256:"a".repeat(64),
      child_agent_id:"original-child",status,result:status==="completed"?"<script>CHILD IS NOT THE PARENT ANSWER</script>":null,
      failure_code:status==="failed"?"CHILD_FAILED":status==="incomplete"?"DELEGATION_RESULT_INCOMPLETE":null,
      fresh:false,cancel_requested:false,child_cancelled:status==="cancelled",parent_idle:status==="cancelled",
      stop_confirmed:status==="cancelled",...extra}};
}
const delegationResponse=value=>({ok:true,json:async()=>copy(value)});
function childNote(ui,index=0){
  return ui.nodes.get("conversation-history").children[index]?.children
    .find(node=>node.className==="reason child-status")?.textContent||"";
}

test("delegation projection shows original child states without a child answer or POST",async()=>{
  for(const [status,label] of [["running","處理中"],["completed","已完成"],["failed","沒有完成"],
      ["unknown","還不確定"],["incomplete","還不確定"],["cancelled","已取消"]]){
    const data=delegationChat(),before=JSON.stringify(data);
    const ui=await open(data,{delegationResponse:delegationResponse(delegationObservation(data,status))});
    await ui.tick();
    assert.match(childNote(ui),new RegExp(label));
    assert.match(childNote(ui),/助理會整合子題結果再回覆你/);
    assert.doesNotMatch(childNote(ui),/original-delegation|original-child|CHILD_FAILED|DELEGATION_RESULT_INCOMPLETE/);
    assert.match(ui.text("identifiers"),/"delegation_id": "original-delegation"/);
    assert.equal(JSON.stringify(data),before);
    const nodes=ui.nodes.get("conversation-history").children[0].children;
    assert.equal(nodes.filter(node=>node.textContent==="助理").length,1);
    assert.equal(nodes.some(node=>node.textContent.includes("CHILD IS NOT THE PARENT ANSWER")),false);
    assert.equal(nodes.at(-1).children.some(button=>/批准|委派|重試子題/.test(button.textContent)),false);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/delegation")).length,1);
    assert.equal(ui.text("chat-presence"),"可以聊天");
  }
});

test("delegation cancellation intent and either single stop observation are never confirmed cancellation",async()=>{
  for(const flags of [{},{child_cancelled:true},{parent_idle:true}]){
    const data=delegationChat();
    const value=delegationObservation(data,"unknown",{cancel_requested:true,...flags});
    const ui=await open(data,{delegationResponse:delegationResponse(value)});
    assert.match(childNote(ui),/還不確定.*已要求取消，正在等它停下來/);
    assert.doesNotMatch(childNote(ui),/已取消/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  }
});

test("delegation absent observation is silent but unavailable and unknown remain explicit",async()=>{
  const data=delegationChat(),absent=delegationObservation(data);
  absent.delegation=null;
  const ui=await open(data,{delegationResponse:delegationResponse(absent)});
  assert.equal(childNote(ui),"");
  for(const [code,expected] of [["DELEGATION_UNAVAILABLE",/讀不到子題狀態/],["DELEGATION_UNKNOWN",/還不確定/],
      ["BACKEND_DENIED",/讀不到子題狀態/]]){
    ui.holder.delegationResponse={ok:false,json:async()=>({error:{code}})};
    await ui.context.refreshDelegations(true);
    assert.match(childNote(ui),expected);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  }
});

test("delegation progress refresh changes only the child note and retains the parent answer",async()=>{
  const data=delegationChat();
  data.tasks[0].reply={data:{text:"Saved parent answer."}};
  const ui=await open(data,{delegationResponse:delegationResponse(delegationObservation(data))});
  const answer=ui.text("answer"),ack=ui.text("ack-state");
  ui.holder.delegationResponse=delegationResponse(delegationObservation(data,"completed"));
  ui.click("refresh-progress");await ui.tick();await ui.tick();
  assert.match(childNote(ui),/子題：已完成/);
  assert.equal(ui.text("answer"),answer);assert.equal(ui.text("ack-state"),ack);
  const original=ui.nodes.get("conversation-history").children[0];
  await ui.context.refreshDelegations(true);
  assert.equal(ui.nodes.get("conversation-history").children[0],original);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("delegation mismatched task turn or parent never fills another parent card",async()=>{
  for(const fault of ["task","turn","parent","schema","stop"]){
    const data=delegationChat(),value=delegationObservation(data,"completed");
    if(fault==="task")value.task_id="other-task";
    if(fault==="turn")value.turn_number=2;
    if(fault==="parent")value.delegation.parent_job_id="other-parent";
    if(fault==="schema")value.delegation.schema_version="future";
    if(fault==="stop")value.delegation.stop_confirmed=true;
    const ui=await open(data,{delegationResponse:delegationResponse(value)});
    assert.match(childNote(ui),/讀不到子題狀態/);
    assert.doesNotMatch(childNote(ui),/子題：已完成/);
  }
});

for(const boundary of ["thread","login","reconnect"]){
  test(`delegation late result error and finally are isolated across ${boundary}`,async()=>{
    for(const failed of [false,true]){
      const data=delegationChat();
      let oldResolve,newResolve;
      const old=new Promise(resolve=>{oldResolve=resolve});
      const ui=await open(data,{delegationResponse:old});
      const next=copy(data);
      if(boundary==="thread"){
        next.tasks[0].thread_id=next.composer.thread_id="next-thread";
        next.tasks[0].task_id=next.tasks[0].submit_request_id=next.composer.request_id="next-parent";
        next.tasks[0].job_id="next-job";
        next.tasks[0].admission.conversation_id=next.composer.conversation_id="next-cid";
        next.composer.conversation.turns[0].request_id="next-parent";
      }else if(boundary==="login")ui.render({...data,authentication:"LOGIN_EXPIRED"});
      ui.holder.delegationResponse=new Promise(resolve=>{newResolve=resolve});
      if(boundary==="reconnect"){
        ui.holder.response={ok:true,json:async()=>({})};
        ui.click("login");await ui.tick();await ui.tick();
      }else{
        ui.render(next);ui.context.refreshDelegations(true);await ui.tick();
      }
      oldResolve(failed?{ok:false,json:async()=>({error:{code:"DELEGATION_UNKNOWN"}})}:
        delegationResponse(delegationObservation(data,"completed")));
      await ui.tick();
      assert.equal(childNote(ui),"");
      assert.equal(vm.runInContext("delegationReads.size",ui.context),1);
      newResolve(delegationResponse(delegationObservation(next,"running")));
      await ui.tick();
      assert.match(childNote(ui),/子題：處理中/);
      assert.equal(vm.runInContext("delegationReads.size",ui.context),0);
    }
  });
}

test("delegation late observation stays under its original turn after a new draft RID",async()=>{
  const data=delegationChat();
  let finish;
  const ui=await open(data,{delegationResponse:new Promise(resolve=>{finish=resolve})});
  const next=copy(data);
  next.composer.request_id="new-draft";next.composer.content=null;next.composer.editable=true;
  next.composer.can_submit=true;next.composer.can_follow_up=false;
  next.tasks.push({...copy(data.tasks[0]),task_id:"later-parent",submit_request_id:"later-parent",job_id:"later-job",
    instruction:"A later turn",admission:null,actions:{}});
  ui.render(next);
  finish(delegationResponse(delegationObservation(data,"completed")));await ui.tick();
  assert.match(childNote(ui,0),/子題：已完成/);assert.equal(childNote(ui,1),"");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

function answerButton(ui){
  return turnActions(ui.nodes.get("conversation-history").children[0])
    .find(button=>button.textContent==="下載答案文字");
}
function answerResponse(text){
  return {ok:true,headers:{get:name=>({
      "content-type":"text/plain; charset=utf-8","cache-control":"no-store",
      "content-disposition":'attachment; filename="fa-reply-1.txt"'
    }[name]||null)},arrayBuffer:async()=>new TextEncoder().encode(text).buffer};
}

test("skill draft outcome shows saved but unapproved only for its original saved turn without a POST",async()=>{
  const data=downloadableChat(),task=data.tasks[0],turn=data.composer.conversation.turns[0];
  const original=copy(task),ui=await open(data);
  const answer=ui.text("answer"),acknowledgment=ui.text("ack-state");
  const notes=()=>ui.nodes.get("conversation-history").children[0].children
    .filter(node=>node.textContent.includes("技能草稿，還沒批准"));
  assert.equal(notes().length,0);
  for(const stage of ["create_unknown","created","upload_unknown"]){
    turn.skill_draft_outcome={schema_version:"fa-skill-draft-outcome-v1",receipt_id:"a".repeat(64),
      intent_sha256:"b".repeat(64),stage};
    ui.render(data);assert.equal(notes().length,0);
  }
  turn.skill_draft_outcome.stage="saved";ui.render(data);
  assert.equal(notes().length,1);
  assert.equal(notes()[0].textContent,"助理存了一份技能草稿，還沒批准。");
  assert.equal(ui.text("answer"),answer);assert.equal(ui.text("ack-state"),acknowledgment);
  assert.deepEqual(data.tasks[0],original);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  const controls=turnActions(ui.nodes.get("conversation-history").children[0]);
  assert.equal(controls.some(button=>/批准|approve/i.test(button.textContent)),false);
  turn.request_id="another-request";ui.render(data);assert.equal(notes().length,0);
  turn.request_id=task.submit_request_id;data.authentication="LOGIN_REQUIRED";ui.render(data);
  assert.equal(notes().length,0);
});

test("saved answer download is manual and preserves exact UTF8 without a post or receipt",async()=>{
  const data=downloadableChat(),ui=await open(data),text="  原答案🙂\r\n<b>純文字</b>\n  ";
  const answer=ui.text("answer"),ack=ui.text("ack-state");
  ui.holder.answerResponse=answerResponse(text);
  answerButton(ui).events.click();answerButton(ui).events.click();
  await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply.txt")).length,1);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(await ui.holder.downloaded.text(),text);
  assert.equal(ui.nodes.get("reply-text-download").download,"fa-reply-1.txt");
  assert.equal(ui.nodes.get("reply-text-download").clickCount,1);
  assert.equal(ui.text("answer"),answer);assert.equal(ui.text("ack-state"),ack);
});

for(const failure of ["denied","wrong-type","wrong-name","empty"]){
  test(`answer download ${failure} never creates a success-shaped file`,async()=>{
    const ui=await open(downloadableChat());
    const response=answerResponse(failure==="empty"?"":"not an authorized answer");
    if(failure==="denied"){
      response.ok=false;response.json=async()=>({error:{code:"BACKEND_DENIED"}});
    }else if(failure!=="empty"){
      const original=response.headers.get;
      response.headers.get=name=>failure==="wrong-type"&&name==="content-type"?"application/json":
        failure==="wrong-name"&&name==="content-disposition"?'attachment; filename="../wrong.txt"':original(name);
    }
    ui.holder.answerResponse=response;
    answerButton(ui).events.click();await ui.tick();await ui.tick();
    assert.equal(ui.holder.downloaded,undefined);
    assert.equal(ui.nodes.get("reply-text-download").clickCount,undefined);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
    assert.equal(answerButton(ui).disabled,false);
  });
}

test("answer download from an earlier login cannot trigger a late file save",async()=>{
  const data=downloadableChat(),ui=await open(data);
  let finishOld,finishNew;
  ui.holder.answerResponse={...answerResponse(""),arrayBuffer:()=>new Promise(resolve=>{finishOld=resolve})};
  answerButton(ui).events.click();await ui.tick();
  ui.render({...data,authentication:"LOGIN_EXPIRED"});ui.render(data);
  assert.equal(answerButton(ui).disabled,false);
  ui.holder.answerResponse={...answerResponse(""),arrayBuffer:()=>new Promise(resolve=>{finishNew=resolve})};
  answerButton(ui).events.click();await ui.tick();
  finishOld(new TextEncoder().encode("stale answer").buffer);await ui.tick();await ui.tick();
  assert.equal(ui.holder.downloaded,undefined);
  assert.equal(answerButton(ui).disabled,true);
  finishNew(new TextEncoder().encode("current authorized answer").buffer);await ui.tick();await ui.tick();
  assert.equal(await ui.holder.downloaded.text(),"current authorized answer");
  assert.equal(ui.nodes.get("reply-text-download").clickCount,1);
});

function screenshotChat(delivered=true){
  const data=downloadableChat(),turn=data.composer.conversation.turns[0];
  turn.progress={events:[
    {seq:1,at:when,kind:"tool_started",tool_name:"playwright_browser_take_screenshot",tool_start_seq:1,status:"started"},
    {seq:2,at:when,kind:"tool_completed",tool_name:"playwright_browser_take_screenshot",tool_start_seq:1,status:"succeeded"},
    ...(delivered?[
      {seq:3,at:when,kind:"tool_started",tool_name:"deliver_image",tool_start_seq:3,status:"started"},
      {seq:4,at:when,kind:"tool_completed",tool_name:"deliver_image",tool_start_seq:3,status:"succeeded"}]:[])]};
  return data;
}
const imagesListing=(count=1)=>({ok:true,json:async()=>({entries:Array.from({length:count},(_,index)=>({
  version_id:String(index).repeat(32),number:index+1,sha256:"d".repeat(64),byte_length:4,media_type:"image/png"}))})});
const imageBytes=()=>({ok:true,headers:{get:name=>name==="content-type"?"image/png":null},
  arrayBuffer:async()=>new Uint8Array([137,80,78,71]).buffer});
function turnNode(ui){return ui.nodes.get("conversation-history").children[0]}
function screenshotButton(ui){return turnActions(turnNode(ui)).find(button=>button.textContent==="查看截圖")}
function gallery(ui){return turnNode(ui).children.find(node=>node.className==="turn-images")}
const settle=async ui=>{for(let i=0;i<6;i++)await ui.tick()};

function nativeScreenshotCase(status="completed"){
  const data=nativeChat(status);data.tasks[0].actions["read-images"]=action(true);
  const raw=Uint8Array.from(Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==","base64"));
  const digest=require("node:crypto").createHash("sha256").update(raw).digest("hex");
  const entry={version_id:digest.slice(0,32),number:1,sha256:digest,byte_length:raw.length,media_type:"image/png"};
  return {data,raw,entry,imagesResponse:{ok:true,json:async()=>({entries:[entry]})},imageResponse:{
    ok:true,headers:{get:name=>name==="content-type"?"image/png":name==="x-content-sha256"?digest:null},
    arrayBuffer:async()=>raw.buffer}};
}

for(const status of ["completed","unknown"]){
  test(`native screenshot ${status} uses the original image reader without inventing a delivery event`,async()=>{
    const value=nativeScreenshotCase(status),ui=await open(value.data,value);
    await settle(ui);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/images")).length,status==="completed"?1:0);
    assert.ok(screenshotButton(ui),"native image read is not gated on a legacy progress event or final answer");
    if(status==="unknown"){screenshotButton(ui).events.click();await settle(ui)}
    const deadline=Date.now()+1000;
    while(!gallery(ui)?.children.some(node=>node.children?.[0]?.src==="blob:offline-template")&&Date.now()<deadline)
      await new Promise(resolve=>setTimeout(resolve,1));
    assert.ok(gallery(ui).children.some(node=>node.children?.[0]?.src==="blob:offline-template"));
    assert.deepEqual(new Uint8Array(await ui.holder.downloaded.arrayBuffer()),value.raw);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

for(const damage of ["bytes","hash","five-images","missing-list"]){
  test(`native screenshot ${damage} never creates an unchecked preview`,async()=>{
    const value=nativeScreenshotCase();
    if(damage==="bytes")value.imageResponse={...value.imageResponse,arrayBuffer:async()=>new Uint8Array([0]).buffer};
    if(damage==="hash")value.imageResponse={...value.imageResponse,headers:{
      get:name=>name==="content-type"?"image/png":"0".repeat(64)}};
    if(damage==="five-images")value.imagesResponse={ok:true,json:async()=>({entries:Array(5).fill(value.entry)})};
    if(damage==="missing-list")value.imagesResponse={ok:true,json:async()=>({})};
    const ui=await open(value.data,value);await settle(ui);
    assert.equal(ui.holder.downloaded,undefined);
    assert.ok(gallery(ui).children[0].textContent);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("Responses screenshot retains the native digest check instead of falling back to legacy preview",async()=>{
  const value=nativeScreenshotCase("unknown");
  value.data.tasks[0].state_profile=value.data.composer.state_profile="foundry-responses-v1";
  const ui=await open(value.data,{...value,imageResponse:{...value.imageResponse,
    arrayBuffer:async()=>new Uint8Array([137,80,78,71]).buffer}});
  screenshotButton(ui).events.click();await settle(ui);
  assert.equal(ui.holder.downloaded,undefined);
  assert.ok(gallery(ui).children[0].textContent);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("native screenshot from an earlier login is discarded before creating its preview",async()=>{
  const value=nativeScreenshotCase();let finish;
  const ui=await open(value.data,{...value,imageResponse:{...value.imageResponse,
    arrayBuffer:()=>new Promise(resolve=>{finish=resolve})}});
  screenshotButton(ui).events.click();await ui.tick();
  ui.render({...value.data,authentication:"LOGIN_EXPIRED"});ui.render(value.data);
  finish(value.raw.buffer);await settle(ui);
  assert.equal(ui.holder.downloaded,undefined);
  assert.equal(gallery(ui),undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("an empty native image listing stays quiet and is not read again on repaint",async()=>{
  const value=nativeScreenshotCase(),ui=await open(value.data,{
    ...value,imagesResponse:{ok:true,json:async()=>({entries:[]})}});
  await settle(ui);ui.render(value.data);await settle(ui);
  assert.equal(gallery(ui),undefined);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/images")).length,1);
  assert.equal(ui.holder.downloaded,undefined);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a reply that delivered a screenshot shows its thumbnail without a click and reads it once",async()=>{
  const data=screenshotChat(),ui=await open(data,{imagesResponse:imagesListing(),imageResponse:imageBytes()});
  await settle(ui);
  const shown=gallery(ui);
  assert.ok(shown,"gallery is rendered after the automatic read");
  const link=shown.children.find(node=>node.children?.[0]?.src);
  assert.equal(link.children[0].src,"blob:offline-template");
  assert.equal(link.href,"blob:offline-template");
  assert.ok(shown.children.some(node=>node.download==="fa-screenshot-1.png"));
  ui.render(copy(data));ui.render(copy(data));await settle(ui);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/images")).length,1);
  assert.equal(ui.calls.filter(call=>call.url.includes("/images/")).length,1);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("a failed automatic screenshot read shows its error, never retries itself, and the button retries once",async()=>{
  const data=screenshotChat();
  const ui=await open(data,{imagesResponse:{ok:false,json:async()=>({error:{code:"RESULT_INVALID"}})}});
  await settle(ui);
  assert.ok(gallery(ui),"the failure is shown under the reply");
  assert.equal(gallery(ui).children[0].textContent,vm.runInContext('errorHeadline(friendlyError({code:"RESULT_INVALID"}))',ui.context));
  ui.render(copy(data));await settle(ui);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/images")).length,1);
  ui.holder.imagesResponse=imagesListing();ui.holder.imageResponse=imageBytes();
  assert.equal(screenshotButton(ui).disabled,false);
  screenshotButton(ui).events.click();await settle(ui);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/images")).length,2);
  assert.ok(gallery(ui).children.some(node=>node.children?.[0]?.src==="blob:offline-template"),"the button shows the gallery");
});

test("replies without a delivered screenshot have no screenshot button and read nothing",async()=>{
  const ui=await open(screenshotChat(false),{imagesResponse:imagesListing(),imageResponse:imageBytes()});
  await settle(ui);
  assert.equal(screenshotButton(ui),undefined);
  assert.equal(gallery(ui),undefined);
  assert.equal(ui.calls.filter(call=>call.url.includes("/images")).length,0);
});

test("a trimmed progress record keeps the screenshot button without an automatic read",async()=>{
  const data=screenshotChat(false),turn=data.composer.conversation.turns[0];
  turn.progress.events=turn.progress.events.map(event=>({...event,seq:event.seq+40,tool_start_seq:event.tool_start_seq+40}));
  const ui=await open(data,{imagesResponse:imagesListing(),imageResponse:imageBytes()});
  await settle(ui);
  assert.equal(ui.calls.filter(call=>call.url.includes("/images")).length,0);
  screenshotButton(ui).events.click();await settle(ui);
  assert.ok(gallery(ui).children.some(node=>node.children?.[0]?.src==="blob:offline-template"));
});

test("provisional preview without a saved reply never gets a download action",async()=>{
  const data=cancellableChat();
  data.composer.conversation.turns[0].stream={text:"Only preview",durable:false,complete:false};
  const ui=await open(data);
  assert.equal(answerButton(ui),undefined);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply.txt")).length,0);
});

test("original cancellation receipt lookup is a manual GET and preserves unknown history",async()=>{
  const data=unknownCancellationChat(),task=data.tasks[0],original=JSON.stringify(task.cancellation);
  const ui=await open(data);
  ui.holder.cancelReceiptResponse={ok:true,json:async()=>cancellationObservation(task)};
  receiptButton(ui).events.click();receiptButton(ui).events.click();
  await ui.tick();await ui.tick();
  const reads=ui.calls.filter(call=>call.url.endsWith("/cancel-receipt"));
  assert.equal(reads.length,1);
  assert.equal(reads[0].url,"/api/instructions/offline/cancel-receipt");
  assert.equal(reads[0].cache,"no-store");
  assert.equal(ui.text("cancellation-note"),"已停止。");
  assert.equal(JSON.stringify(task.cancellation),original);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,true);
  assert.equal(ui.text("ack-state"),"未確認");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  const reloaded=await open(data);
  assert.match(reloaded.text("cancellation-note"),/還不確定停止請求有沒有送到/);
  assert.equal(reloaded.calls.length,1);
});

test("wrong-work cancellation receipt cannot replace the original unknown observation",async()=>{
  const data=unknownCancellationChat(),task=data.tasks[0],ui=await open(data);
  ui.holder.cancelReceiptResponse={ok:true,json:async()=>({...cancellationObservation(task),job_id:"other-job"})};
  receiptButton(ui).events.click();await ui.tick();await ui.tick();
  assert.match(ui.text("cancellation-note"),/還不確定停止請求有沒有送到/);
  assert.match(ui.nodes.get("conversation-history").children[0].children.map(node=>node.textContent).join("\n"),/尚未核對成功/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("missing cancellation receipt stays unknown without automatically querying or cancelling again",async()=>{
  const data=unknownCancellationChat(),ui=await open(data);
  ui.holder.cancelReceiptResponse={ok:false,json:async()=>({error:{code:"CANCELLATION_RECEIPT_UNKNOWN"}})};
  receiptButton(ui).events.click();await ui.tick();await ui.tick();
  assert.match(ui.text("cancellation-note"),/還不確定停止請求有沒有送到/);
  assert.match(ui.nodes.get("conversation-history").children[0].children.map(node=>node.textContent).join("\n"),/仍未取得原取消回條/);
  assert.doesNotMatch(ui.nodes.get("conversation-history").children[0].children.map(node=>node.textContent).join("\n"),/不允許重送/);
  assert.match(ui.text("identifiers"),/CANCELLATION_RECEIPT_UNKNOWN/);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/cancel-receipt")).length,1);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/cancel-receipt")).length,1);
});

test("old-login cancellation receipt response cannot unlock or overwrite a newer read",async()=>{
  const data=unknownCancellationChat(),task=data.tasks[0],ui=await open(data);
  let oldRead,newRead;
  ui.holder.cancelReceiptResponse={ok:true,json:()=>new Promise(resolve=>{oldRead=resolve})};
  receiptButton(ui).events.click();await ui.tick();
  ui.render({...data,authentication:"LOGIN_EXPIRED"});
  ui.render(data);
  ui.holder.cancelReceiptResponse={ok:true,json:()=>new Promise(resolve=>{newRead=resolve})};
  assert.equal(receiptButton(ui).disabled,false);
  receiptButton(ui).events.click();await ui.tick();
  oldRead(cancellationObservation(task));await ui.tick();await ui.tick();
  assert.equal(receiptButton(ui).disabled,true);
  assert.doesNotMatch(ui.text("cancellation-note"),/已停止/);
  newRead(cancellationObservation(task));await ui.tick();await ui.tick();
  assert.equal(receiptButton(ui).disabled,false);
  assert.equal(ui.text("cancellation-note"),"已停止。");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

// The stop is confirmed in the page like every other action (the harness fails on any native dialog).
const STOP_QUESTION="送出停止這次回覆的請求？原對話與已保存資料會保留；是否已停止仍以後端狀態為準。";

test("stopping a reply sends the original task cancellation once and preserves the draft",async()=>{
  const data=cancellableChat(),task=data.tasks[0];
  task.reply={data:{text:"Original saved answer"},sha256:"a".repeat(64),byte_length:21};
  const ui=await open(data),answer=ui.text("answer");
  ui.nodes.get("instruction").value="A draft to keep";
  assert.equal(ui.nodes.get("cancel-instruction").hidden,false);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,false);
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  ui.click("cancel-instruction");ui.click("cancel-instruction");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0,"the first click only asks");
  assert.equal(ui.nodes.get("page-confirm").hidden,false);
  assert.equal(ui.text("page-confirm-message"),STOP_QUESTION);
  assert.equal(ui.nodes.get("page-confirm-cancel").focused,true,"focus starts on 取消, so Enter cannot confirm");
  assert.notEqual(ui.nodes.get("page-confirm-ok").focused,true);
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await ui.tick();
  const sent=ui.calls.filter(call=>call.method==="POST");
  assert.equal(sent.length,1);
  assert.equal(sent[0].url,"/api/instructions/offline/cancel");
  assert.deepEqual(JSON.parse(sent[0].body),{});
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(ui.text("cancellation-note"),"正在送出停止請求…");
  task.cancellation={state:"CONFIRMED",receipt:{result:{status:"running",cancel_accepted:true,executor_stopped:false}}};
  task.actions.cancel=action(false);
  ui.holder.current=data;ui.holder.pending({task});
  await ui.tick();await ui.tick();
  assert.match(ui.text("cancellation-note"),/已要求停止，正在等它停下來/);
  assert.doesNotMatch(ui.text("cancellation-note"),/已停止|已保存|已收件/);
  assert.equal(ui.nodes.get("instruction").value,"A draft to keep");
  assert.equal(ui.text("answer"),answer);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,true);
  assert.equal(ui.text("ack-state"),"未確認");
  ui.click("cancel-instruction");ui.click("page-confirm-ok");await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

// The bridge still held the native turn as running when 「停止這次回覆」 was pressed, but it had already finished: the API
// answered with the original receipt. The finished turn says no stop was needed, not 「還不確定停止請求有沒有送到」.
test("a native stop the API answered for an already finished turn says no stop was needed",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.cancellation={state:"CONFIRMED",receipt:{result:{cancel_accepted:false,status:"completed"}},automatic_retry:false};
  const ui=await open(data);
  const shown=ui.nodes.get("conversation-history").children[0].children.map(node=>node.textContent).join("\n");
  assert.match(shown,/工作已經結束，不需要停止。/);
  assert.doesNotMatch(shown,/還不確定停止請求有沒有送到/);
  assert.doesNotMatch(ui.text("cancellation-note"),/還不確定停止請求有沒有送到/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("dismissing the stop confirmation has no cancellation or other side effects",async()=>{
  const ui=await open(cancellableChat());
  ui.click("cancel-instruction");await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,false);
  ui.click("page-confirm-cancel");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(ui.nodes.get("cancel-instruction").hidden,false);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,false);
  assert.equal(ui.nodes.get("cancel-instruction").focused,true);
  assert.equal(ui.text("cancellation-note"),"");
  ui.click("page-confirm-ok");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0,"a dismissed confirmation cannot be used later");
});

test("Escape closes the stop confirmation without sending, inside the dialog or elsewhere on the page",async()=>{
  const ui=await open(cancellableChat());
  ui.click("cancel-instruction");
  let stopped=false;
  ui.nodes.get("page-confirm").events.keydown({key:"Escape",stopPropagation(){stopped=true}});
  await ui.tick();
  assert.equal(stopped,true,"the page's own Escape handling (closing settings, menus) does not also run");
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(ui.nodes.get("cancel-instruction").focused,true);
  ui.click("cancel-instruction");
  assert.equal(ui.nodes.get("page-confirm").hidden,false);
  ui.documentEvents.keydown({key:"Escape"});await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  ui.click("page-confirm-ok");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("a newer in-page question answers the older one 取消; only the open one can be confirmed",async()=>{
  const ui=await open(cancellableChat());
  const answers=[];
  ui.context.confirmInPage("first").then(value=>answers.push(["first",value]));
  ui.context.confirmInPage("second").then(value=>answers.push(["second",value]));
  await ui.tick();
  assert.deepEqual(answers,[["first",false]]);
  assert.equal(ui.text("page-confirm-message"),"second");
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await ui.tick();
  assert.deepEqual(answers,[["first",false],["second",true]]);
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(ui.text("page-confirm-message"),"");
});

test("the page behind an open in-page question is inert, and comes back on every way it closes",async()=>{
  // aria-modal alone does not stop Tab or a click from reaching the page behind; #app-shell is the dialog's only sibling.
  assert.match(html,/<\/main>\s*<\/div>\s*<div id="page-confirm" class="page-confirm" hidden>/);
  assert.match(html,/<body>\s*<div class="app-shell" id="app-shell">/);
  const first=cancellableChat(),ui=await open(first),shell=ui.nodes.get("app-shell");
  assert.ok(!shell.inert);
  ui.click("cancel-instruction");
  assert.equal(shell.inert,true,"open: the page behind is inert");
  ui.nodes.get("cancel-instruction").focused=false;
  ui.click("page-confirm-cancel");await ui.tick();
  assert.equal(shell.inert,false,"取消");
  assert.equal(ui.nodes.get("cancel-instruction").focused,true,"focus returns to the button");
  ui.click("cancel-instruction");
  ui.context.confirmInPage("a newer question");
  assert.equal(shell.inert,true,"a newer question keeps the page behind inert");
  ui.nodes.get("page-confirm").events.keydown({key:"Escape",stopPropagation(){},preventDefault(){}});
  assert.equal(shell.inert,false,"Esc");
  ui.click("cancel-instruction");
  assert.equal(shell.inert,true);
  const second=cancellableChat();
  second.tasks[0]={...second.tasks[0],task_id:"other",submit_request_id:"other",job_id:"other-job",thread_id:"other-thread"};
  second.composer={...second.composer,thread_id:"other-thread",request_id:"other"};
  second.composer.conversation.turns[0].request_id="other";
  ui.render(second);
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(shell.inert,false,"closed because the question no longer applies");
  ui.click("cancel-instruction");ui.click("page-confirm-ok");await ui.tick();
  assert.equal(shell.inert,false,"確定");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("focus returns only to an element still on the page",async()=>{
  const ui=await open(cancellableChat());
  const gone={isConnected:false,focus(){this.focused=true}},here={isConnected:true,focus(){this.focused=true}};
  ui.context.confirmInPage("detached",{returnFocus:gone});
  ui.click("page-confirm-cancel");await ui.tick();
  assert.equal(gone.focused,undefined,"a list re-rendered while the question was open leaves a detached node");
  assert.equal(ui.nodes.get("app-shell").inert,false);
  ui.context.confirmInPage("attached",{returnFocus:here});
  ui.click("page-confirm-cancel");await ui.tick();
  assert.equal(here.focused,true);
});

test("a stop confirmation left open closes when the conversation changes and cannot stop the other one",async()=>{
  const first=cancellableChat(),ui=await open(first);
  ui.click("cancel-instruction");
  assert.equal(ui.nodes.get("page-confirm").hidden,false);
  const second=cancellableChat();
  second.tasks[0]={...second.tasks[0],task_id:"other",submit_request_id:"other",job_id:"other-job",thread_id:"other-thread"};
  second.composer={...second.composer,thread_id:"other-thread",request_id:"other"};
  second.composer.conversation.turns[0].request_id="other";
  ui.render(second);
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  ui.click("page-confirm-ok");await ui.tick();
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  ui.click("cancel-instruction");ui.click("page-confirm-ok");await ui.tick();
  const sent=ui.calls.filter(call=>call.method==="POST");
  assert.equal(sent.length,1);
  assert.equal(sent[0].url,"/api/instructions/other/cancel");
});

test("unknown stop response remains unknown after terminal progress and page reconstruction",async()=>{
  const data=cancellableChat(),task=data.tasks[0];
  task.cancellation={state:"UNKNOWN",automatic_retry:false};
  task.actions.cancel=action(false);
  data.composer.conversation.turns[0].state="cancelled";
  for(const ui of [await open(data),await open(copy(data))]){
    assert.match(ui.text("cancellation-note"),/還不確定停止請求有沒有送到/);
    assert.equal(ui.text("chat-presence"),"需要處理");
    assert.equal(ui.nodes.get("instruction").placeholder,"回覆已取消，請從選單查看接續方式");
    assert.equal(ui.nodes.get("cancel-instruction").disabled,true);
    ui.click("cancel-instruction");await ui.tick();
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
    assert.equal(ui.text("ack-state"),"未確認");
  }
});

for(const source of ["turn","status"]){
  for(const [state,label] of [["cancelled","已停止"],["succeeded","已完成"],["failed","沒有完成"]]){
    test(`active cancellation note reflects later ${state} from its original ${source}`,async()=>{
      const data=cancellableChat(),task=data.tasks[0];
      task.cancellation={state:"CONFIRMED",
        receipt:{result:{status:"running",cancel_accepted:true,executor_stopped:false}}};
      task.actions.cancel=action(false);
      const receipt=JSON.stringify(task.cancellation),ui=await open(data);
      assert.match(ui.text("cancellation-note"),/正在等它停下來/);
      if(source==="turn")data.composer.conversation.turns[0].state=state;
      else task.status={data:{state},observed_at:when};
      ui.render(data);
      const reloaded=await open(copy(data));
      for(const current of [ui,reloaded]){
        assert.match(current.text("cancellation-note"),new RegExp(`已要求停止；最後狀態：${label}`));
        assert.doesNotMatch(current.text("cancellation-note"),/正在等它停下來/);
        assert.match(current.nodes.get("conversation-history").children[0].children.map(node=>node.textContent).join("\n"),
          new RegExp(label));
        assert.equal(current.calls.filter(call=>call.method==="POST").length,0);
        assert.equal(current.text("ack-state"),"未確認");
      }
      assert.equal(JSON.stringify(task.cancellation),receipt);
    });
  }
}

test("another task's terminal progress cannot settle this task's accepted stop request",async()=>{
  const data=cancellableChat(),task=data.tasks[0];
  task.cancellation={state:"CONFIRMED",
    receipt:{result:{status:"running",cancel_accepted:true,executor_stopped:false}}};
  task.actions.cancel=action(false);
  data.composer.conversation.turns.push({request_id:"different-task",state:"cancelled"});
  const ui=await open(data);
  assert.match(ui.text("cancellation-note"),/正在等它停下來/);
  assert.doesNotMatch(ui.text("cancellation-note"),/最後狀態/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("late stop response cannot change another conversation or its draft",async()=>{
  const first=cancellableChat(),ui=await open(first);
  ui.click("cancel-instruction");ui.click("page-confirm-ok");await ui.tick();
  const second=cancellableChat();
  second.tasks[0]={...second.tasks[0],task_id:"other",submit_request_id:"other",job_id:"other-job",thread_id:"other-thread"};
  second.composer={...second.composer,thread_id:"other-thread",request_id:"other",content:null,editable:true,can_submit:true};
  second.composer.conversation.turns[0].request_id="other";
  second.tasks[0].actions.cancel=action(false);
  ui.render(second);ui.nodes.get("instruction").value="Other conversation draft";
  ui.holder.pending({task:{...first.tasks[0],cancellation:{state:"CONFIRMED"}}});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("cancellation-note"),"");
  assert.equal(ui.nodes.get("instruction").value,"Other conversation draft");
  assert.equal(ui.nodes.get("task").value,"other");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1);
});

test("fixed work and unauthenticated chat cannot expose an actionable stop button",async()=>{
  const fixed=await open(fixture());
  assert.equal(fixed.nodes.get("cancel-instruction").hidden,true);
  fixed.click("cancel-instruction");
  assert.equal(fixed.nodes.get("page-confirm").hidden,true);
  fixed.click("page-confirm-ok");await fixed.tick();
  const data=cancellableChat();data.authentication="LOGIN_REQUIRED";
  const ui=await open(data);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,true);
  ui.click("cancel-instruction");
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  ui.click("page-confirm-ok");await ui.tick();
  assert.equal(fixed.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

const inspectedTemplate=()=>({
  schema_version:"fa-agent-template-inspection-v1",template_sha256:"a".repeat(64),
  role_matches:true,settings_match:true,selection_matches:true,references_match:true,
  skills:[{name:"receipt-guide",status:"matches"}],
  required_at_destination:["connector-accounts-and-tool-grants"],authorization:false,activated:false
});

const selectableTemplate=()=>({
  schema_version:"fa-agent-template-v1",
  role:{profile:"fa-instruction-v1",prefix_sha256:"b".repeat(64),capability_catalog_version:"fa-capabilities-v1"},
  settings:{sdk_version:"1.0.13",runtime_version:"1.0.83",mode:"empty",
    checkpoint_profile:null,sdk_start_profile:null,model_capacity_profile:null,
    text_attachment_profile:"fa-source-text-attachments-v1"},
  skills:[],unavailable_skills:[],authorization:false,
});
const selectionRead=(current={selection_id:null,applies_to:"new-conversations-only",permissions_changed:false},selections=[])=>({
  ok:true,json:async()=>({current,read_at:when,selections}),
});
async function readyTemplateSelection(ui,candidate=selectableTemplate(),current,hash="a".repeat(64)){
  const raw=JSON.stringify(candidate);
  ui.nodes.get("template-file").files=[{size:new TextEncoder().encode(raw).length,text:async()=>raw}];
  ui.nodes.get("template-file").events.change();
  ui.holder.templateResponses={
    "/api/agent-template/inspect":{ok:true,json:async()=>({...inspectedTemplate(),
      template_sha256:hash,role_matches:false,references_match:false,skills:[]})},
    "/api/agent-template/selection":selectionRead(current),
  };
  ui.click("inspect-template");await ui.tick();
  ui.click("read-template-selection");await ui.tick();
}

test("template selection requires a fresh read and human confirmation even when inspection differs",async()=>{
  const data=fixture();data.template_tools_available=true;data.template_selections=[];
  const ui=await open(data),candidate=selectableTemplate();
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/agent-template")),false);
  await readyTemplateSelection(ui,candidate);
  assert.equal(ui.nodes.get("select-template").disabled,false);
  assert.match(ui.text("template-target"),/fa-instruction-v1/);
  assert.match(ui.text("template-target"),/text_attachment_profile/);
  ui.click("select-template");
  const question=await ui.answer(false);
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/select"),false);
  assert.match(question,/只影響新對話/);
  assert.match(question,/不是新增操作權限/);
  ui.click("select-template");ui.click("select-template");await ui.tick();
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await ui.tick();
  const posts=ui.calls.filter(call=>call.url==="/api/agent-template/select");
  assert.equal(posts.length,1);
  const command=JSON.parse(posts[0].body);
  assert.match(command.request_id,/^ui-template-/);
  assert.equal(command.expected_selection_id,null);
  assert.deepEqual(command.template,candidate);
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.equal(ui.calls.some(call=>/prepare|approve-operation|execute|reinstall/.test(call.url)),false);
});

function selectedTemplateResponse(command,{state="CONFIRMED",reconciliation=false,hash="a".repeat(64)}={}){
  const receipt={result:{selection_id:"c".repeat(64),applies_to:"new-conversations-only",template_sha256:hash,
    permissions_changed:false,existing_conversations_changed:false},
    control_request_id:"d".repeat(64),control_request_digest:"e".repeat(64)};
  const entry={request_id:command.request_id,expected_selection_id:command.expected_selection_id??null,
    template_sha256:hash,state,receipt:state==="CONFIRMED"?receipt:null,reconciliation:reconciliation?receipt:null,
    created_at:when,historical:true,original_request_resent:false};
  return {request_id:command.request_id,receipt,selections:[entry],original_request_resent:false};
}
function templateData(){
  const data=fixture();data.template_tools_available=true;data.template_selections=[];return data;
}
function templateReconcileButton(ui){
  return ui.nodes.get("template-selections").children[0].children[2];
}

test("template selection success keeps the draft and old result and requires a new current read",async()=>{
  const data=templateData(),ui=await open(data),candidate=selectableTemplate();
  await readyTemplateSelection(ui,candidate);
  const oldAnswer=ui.text("answer"),oldAck=ui.text("ack-state");
  ui.nodes.get("instruction").value="Keep my unsent draft";
  ui.click("select-template");await ui.answer(true);
  const post=ui.calls.find(call=>call.url==="/api/agent-template/select"),command=JSON.parse(post.body);
  const value=selectedTemplateResponse(command);
  ui.holder.pending(value);await ui.tick();await ui.tick();
  assert.match(ui.text("template-note"),/新對話設定已選定.*不是新增操作權限/);
  assert.equal(ui.nodes.get("instruction").value,"Keep my unsent draft");
  assert.equal(ui.text("answer"),oldAnswer);assert.equal(ui.text("ack-state"),oldAck);
  assert.equal(ui.nodes.get("select-template").disabled,true);
  const current={...value.receipt.result,template:candidate};
  ui.holder.templateResponses["/api/agent-template/selection"]=selectionRead(current,value.selections);
  ui.click("read-template-selection");await ui.tick();
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.match(ui.text("template-selection-reason"),/已是目前選擇/);
  const next=copy(candidate);next.role.profile="fa-instruction-skill-resources-v1";
  await readyTemplateSelection(ui,next,current,"f".repeat(64));
  assert.equal(ui.nodes.get("select-template").disabled,false);
  ui.click("select-template");await ui.answer(true);
  const posts=ui.calls.filter(call=>call.url==="/api/agent-template/select");
  assert.equal(posts.length,2);
  assert.equal(JSON.parse(posts[1].body).expected_selection_id,current.selection_id);
  assert.notEqual(JSON.parse(posts[1].body).request_id,command.request_id);
});

test("template confirmation invalidated by another file cannot post the old candidate",async()=>{
  const ui=await open(templateData());await readyTemplateSelection(ui);
  // Another file is chosen while the in-page question is still open; 確定 afterwards cannot post the old candidate.
  ui.click("select-template");await ui.tick();
  ui.nodes.get("template-file").files=[{size:2,text:async()=>"{}"}];
  ui.nodes.get("template-file").events.change();
  await ui.answer(true);
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/select"),false);
  assert.equal(ui.text("template-target"),"");
});

test("template response loss exposes the saved original and reconciles only its request id",async()=>{
  const data=templateData(),ui=await open(data);await readyTemplateSelection(ui);
  let lost;
  ui.holder.templateResponses["/api/agent-template/select"]=new Promise(resolve=>{lost=resolve});
  ui.click("select-template");await ui.answer(true);
  const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
  const retained=selectedTemplateResponse(command,{state:"UNKNOWN"});
  data.template_selections=retained.selections;ui.holder.current=data;
  lost({ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})});await ui.tick();await ui.tick();
  assert.match(ui.nodes.get("template-selections").children[0].children[0].textContent,/未確認.*原 command/);
  assert.equal(ui.nodes.get("select-template").disabled,true);
  let read;
  ui.holder.templateResponses["/api/agent-template/selection-reconcile"]=new Promise(resolve=>{read=resolve});
  const button=templateReconcileButton(ui);button.events.click();button.events.click();await ui.tick();
  const calls=ui.calls.filter(call=>call.url==="/api/agent-template/selection-reconcile");
  assert.equal(calls.length,1);
  assert.deepEqual(JSON.parse(calls[0].body),{request_id:command.request_id});
  read({ok:true,json:async()=>selectedTemplateResponse(command,{state:"UNKNOWN",reconciliation:true})});
  await ui.tick();await ui.tick();
  assert.match(ui.nodes.get("template-selections").children[0].children[0].textContent,/UNKNOWN 保留.*另已核對.*沒有重送/);
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/select").length,1);
});

test("template unknown survives a reload and current pointer read without becoming that request receipt",async()=>{
  const data=templateData(),command={request_id:"saved-original",expected_selection_id:null};
  data.template_selections=selectedTemplateResponse(command,{state:"UNKNOWN"}).selections;
  const ui=await open(data);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  assert.equal(templateReconcileButton(ui).disabled,false);
  await readyTemplateSelection(ui);
  ui.holder.templateResponses["/api/agent-template/selection"]=selectionRead({
    selection_id:"c".repeat(64),applies_to:"new-conversations-only",permissions_changed:false,
    existing_conversations_changed:false,template_sha256:"a".repeat(64),template:selectableTemplate(),
  },data.template_selections);
  ui.click("read-template-selection");await ui.tick();
  assert.match(ui.nodes.get("template-selections").children[0].children[0].textContent,/原選擇回應未確認/);
  assert.equal(ui.nodes.get("select-template").disabled,true);
});

test("an unavailable template selection reader never falls back to another endpoint or enables selection",async()=>{
  const ui=await open(templateData());await readyTemplateSelection(ui);
  ui.holder.templateResponses["/api/agent-template/selection"]={
    ok:false,json:async()=>({error:{code:"TEMPLATE_SELECTION_UNAVAILABLE",upstream_status:404}}),
  };
  const start=ui.calls.length;
  ui.click("read-template-selection");await ui.tick();
  assert.match(ui.text("template-note"),/尚未提供.*不改用其他入口或啟用 flags/);
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.equal(ui.nodes.get("inspect-template").disabled,false);
  assert.deepEqual(ui.calls.slice(start).map(call=>call.url),["/api/agent-template/selection"]);
});

for(const outcome of ["success","http-error","rejection"]){
  test(`late template selection ${outcome} cannot finish a newer file's current read`,async()=>{
    const ui=await open(templateData());await readyTemplateSelection(ui);
    let finish,fail,nextRead;
    ui.holder.templateResponses["/api/agent-template/select"]=new Promise((resolve,reject)=>{finish=resolve;fail=reject});
    ui.click("select-template");await ui.answer(true);
    const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
    const next=selectableTemplate();next.role.profile="NEW-FILE";
    await readyTemplateSelection(ui,next);
    ui.holder.templateResponses["/api/agent-template/selection"]=new Promise(resolve=>{nextRead=resolve});
    ui.click("read-template-selection");await ui.tick();
    const count=ui.calls.length;
    if(outcome==="success")finish({ok:true,json:async()=>selectedTemplateResponse(command)});
    else if(outcome==="http-error")finish({ok:false,json:async()=>({error:{code:"PROFILE_SELECTION_CONFLICT"}})});
    else fail(new Error("OFFLINE late response"));
    await ui.tick();await ui.tick();
    assert.equal(ui.calls.length,count);
    assert.equal(ui.nodes.get("read-template-selection").disabled,true);
    assert.equal(ui.nodes.get("select-template").disabled,true);
    assert.match(ui.text("template-target"),/NEW-FILE/);
    assert.doesNotMatch(ui.text("template-note"),/已選定|CAS|沒有回應/);
    nextRead(selectionRead());await ui.tick();
    assert.equal(ui.nodes.get("select-template").disabled,false);
  });
}

test("late template current read after reconnect cannot restore selection or replace a fresh observation",async()=>{
  const data=templateData(),ui=await open(data);await readyTemplateSelection(ui);
  let oldRead,freshRead;
  ui.holder.templateResponses["/api/agent-template/selection"]=new Promise(resolve=>{oldRead=resolve});
  ui.click("read-template-selection");await ui.tick();
  ui.holder.response={ok:true,json:async()=>({})};ui.click("login");await ui.tick();await ui.tick();
  ui.holder.templateResponses["/api/agent-template/selection"]=new Promise(resolve=>{freshRead=resolve});
  ui.click("read-template-selection");await ui.tick();
  oldRead(selectionRead({selection_id:"OLD-POINTER",applies_to:"new-conversations-only",permissions_changed:false}));
  await ui.tick();await ui.tick();
  assert.doesNotMatch(ui.text("template-current"),/OLD-POINTER/);
  assert.equal(ui.nodes.get("read-template-selection").disabled,true);
  freshRead(selectionRead());await ui.tick();
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.equal(ui.text("template-target"),"");
});

test("late template reconcile error after reconnect cannot clear a newer template read",async()=>{
  const data=templateData(),command={request_id:"retained-original"};
  data.template_selections=selectedTemplateResponse(command,{state:"UNKNOWN"}).selections;
  const ui=await open(data);let failed,current;
  ui.holder.templateResponses={"/api/agent-template/selection-reconcile":new Promise((_,reject)=>{failed=reject})};
  templateReconcileButton(ui).events.click();await ui.tick();
  ui.holder.response={ok:true,json:async()=>({})};ui.click("login");await ui.tick();await ui.tick();
  ui.holder.templateResponses["/api/agent-template/selection"]=new Promise(resolve=>{current=resolve});
  ui.click("read-template-selection");await ui.tick();
  failed(new Error("OFFLINE old reconcile"));await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("read-template-selection").disabled,true);
  assert.doesNotMatch(ui.text("template-note"),/沒有回應/);
  current(selectionRead(undefined,data.template_selections));await ui.tick();
  assert.match(ui.nodes.get("template-selections").children[0].children[0].textContent,/未確認/);
});

test("losing login clears template candidate and suppresses a late selection receipt",async()=>{
  const data=templateData(),ui=await open(data);await readyTemplateSelection(ui);
  let finish;
  ui.holder.templateResponses["/api/agent-template/select"]=new Promise(resolve=>{finish=resolve});
  ui.click("select-template");await ui.answer(true);
  const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
  data.authentication="LOGIN_REQUIRED";ui.render(data);
  finish({ok:true,json:async()=>selectedTemplateResponse(command)});await ui.tick();await ui.tick();
  assert.equal(ui.text("template-target"),"");
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.doesNotMatch(ui.text("template-note"),/已選定/);
  assert.equal(ui.nodes.get("template-selections").children.length,0);
});

test("login loss during template error history refresh also clears the visible observation",async()=>{
  const data=templateData(),ui=await open(data);await readyTemplateSelection(ui);
  data.authentication="LOGIN_EXPIRED";ui.holder.current=data;
  ui.holder.templateResponses["/api/agent-template/select"]={ok:false,json:async()=>({error:{code:"LOGIN_EXPIRED"}})};
  ui.click("select-template");await ui.answer(true);await ui.tick();
  assert.equal(ui.text("template-target"),"");
  assert.equal(ui.nodes.get("read-template-selection").disabled,true);
  assert.equal(ui.nodes.get("select-template").disabled,true);
});

test("late template history finally cannot overwrite a new file's observation or release its read",async()=>{
  const ui=await open(templateData());await readyTemplateSelection(ui);
  let oldHistory,newRead;
  ui.holder.stateResponse=new Promise(resolve=>{oldHistory=resolve});
  ui.holder.templateResponses["/api/agent-template/select"]={ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})};
  ui.click("select-template");await ui.answer(true);
  assert.equal(ui.calls.at(-1).url,"/api/state");
  await readyTemplateSelection(ui);
  ui.holder.templateResponses["/api/agent-template/selection"]=new Promise(resolve=>{newRead=resolve});
  ui.click("read-template-selection");await ui.tick();
  oldHistory({ok:true,json:async()=>({...templateData(),authentication:"LOGIN_EXPIRED",
    template_selections:selectedTemplateResponse({request_id:"OLD-HISTORY"},{state:"UNKNOWN"}).selections})});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("read-template-selection").disabled,true);
  assert.match(ui.text("template-target"),/fa-instruction-v1/);
  assert.equal(ui.nodes.get("template-selections").children.length,0);
  newRead(selectionRead());await ui.tick();
  assert.equal(ui.nodes.get("select-template").disabled,false);
});

test("template download is user initiated and keeps the original bytes, without a submission",async()=>{
  const data=fixture();data.template_tools_available=true;
  const ui=await open(data),raw='{"schema_version":"fa-agent-template-v1","authorization":false}\n';
  assert.equal(ui.calls.length,1);
  ui.holder.templateResponse={ok:true,arrayBuffer:async()=>new TextEncoder().encode(raw).buffer};
  ui.click("export-template");ui.click("export-template");await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template").length,1);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(await ui.holder.downloaded.text(),raw);
  assert.equal(ui.nodes.get("template-download").clickCount,1);
  assert.match(ui.text("template-note"),/送交瀏覽器下載.*不包含授權/);
});

test("template inspection sends only the selected file and never displays a matching template as activated",async()=>{
  const data=fixture();data.template_tools_available=true;
  const ui=await open(data),raw='{"schema_version":"fa-agent-template-v1","authorization":false}';
  ui.nodes.get("template-file").files=[{size:raw.length,text:async()=>raw}];
  ui.nodes.get("template-file").events.change();
  ui.holder.templateResponse={ok:true,json:async()=>inspectedTemplate()};
  const answer=ui.text("answer"),ack=ui.text("ack-state");
  ui.click("inspect-template");ui.click("inspect-template");await ui.tick();await ui.tick();
  const sent=ui.calls.filter(call=>call.method==="POST");
  assert.equal(sent.length,1);
  assert.equal(sent[0].url,"/api/agent-template/inspect");
  assert.equal(sent[0].body,raw);
  assert.equal(sent[0].headers["X-Console-CSRF"],"offline");
  assert.match(ui.text("template-comparison"),/參照相符；沒有啟用或授權/);
  assert.match(ui.text("template-comparison"),/Connector.*獨立授權/);
  assert.equal(ui.text("answer"),answer);assert.equal(ui.text("ack-state"),ack);
});

test("oversized template files are rejected before reading or sending their contents",async()=>{
  const data=fixture();data.template_tools_available=true;
  const ui=await open(data);
  ui.nodes.get("template-file").files=[{size:8193,text:async()=>{throw Error("Must not read oversized input")}}];
  ui.nodes.get("template-file").events.change();
  ui.click("inspect-template");await ui.tick();
  assert.equal(ui.calls.length,1);
  assert.match(ui.text("template-note"),/8192 bytes/);
  assert.equal(ui.text("template-comparison"),"");
});

test("reconnecting invalidates an older template comparison without locking the new file read",async()=>{
  const data=fixture();data.template_tools_available=true;
  const ui=await open(data);
  let finish;
  ui.nodes.get("template-file").files=[{size:2,text:async()=>"{}"}];
  ui.nodes.get("template-file").events.change();
  ui.holder.templateResponse={ok:true,json:()=>new Promise(resolve=>{finish=resolve})};
  ui.click("inspect-template");await ui.tick();
  assert.equal(ui.nodes.get("inspect-template").disabled,true);
  ui.holder.response={ok:true,json:async()=>data};
  ui.click("login");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("inspect-template").disabled,false);
  finish(inspectedTemplate());await ui.tick();await ui.tick();
  assert.equal(ui.text("template-comparison"),"");
  assert.equal(ui.nodes.get("inspect-template").disabled,false);
});

test("turn context is an explicit GET and keeps bytes, unknown tokens and receipt authority separate",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Original context task";task.actions["read-context"]=action(true);
  const before=JSON.stringify(data),ui=await open(data),record=contextRecord(task);
  assert.equal(ui.calls.length,1);
  assert.equal(ui.nodes.get("turn-context-panel").hidden,false);
  ui.holder.contextResponse={ok:true,json:async()=>({task_id:task.task_id,context:record})};
  ui.click("read-context");ui.click("read-context");await ui.tick();await ui.tick();
  const reads=ui.calls.filter(call=>call.url.endsWith("/context"));
  assert.equal(reads.length,1);
  assert.equal(reads[0].url,"/api/instructions/offline/context");
  assert.equal(reads[0].cache,"no-store");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.match(ui.text("turn-context-summary"),/SDK state JSON：576 UTF-8 bytes；inline zlib：未知/);
  assert.match(ui.text("turn-context-summary"),/回覆 JSON：400 UTF-8 bytes；本文：33 UTF-8 bytes；儲存 zlib：100 bytes/);
  assert.match(ui.text("turn-context-summary"),/provider tokens：未知/);
  assert.match(ui.text("turn-context-summary"),/原回合模型容量：未記錄.*不取目前模型清單補值/);
  assert.match(ui.text("turn-context-note"),/不是目前操作許可或收件證明/);
  assert.equal(ui.text("ack-state"),"未確認");
  assert.equal(JSON.stringify(data),before);
  assert.equal(ui.nodes.get("turn-context-data").innerHTML,undefined);
});

test("turn context displays historical declared capacity instead of the current model list",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Historical capacity";task.actions["read-context"]=action(true);
  data.composer={request_id:task.task_id,content:task.instruction,editable:false,can_submit:false,
    can_follow_up:false,input_capacity:{maximum:8192},models:{
      default_model:"offline-chat-a",models:[{...models.models[0],context_window_tokens:99999,max_output_tokens:9999}]
    }};
  const ui=await open(data),record=contextRecord(task);
  record.model_capacity={model:record.model,source:"server-curated-model-facts",
    limits:{context_window_tokens:16000,max_prompt_tokens:14000,max_output_tokens:512}};
  ui.holder.contextResponse={ok:true,json:async()=>({task_id:task.task_id,context:record})};
  ui.click("read-context");await ui.tick();await ui.tick();
  assert.match(ui.text("turn-context-summary"),/原回合模型 offline-chat-a.*context 16000 tokens；input 14000 tokens；output 512 tokens/);
  assert.doesNotMatch(ui.text("turn-context-summary"),/99999|9999/);
  assert.match(ui.text("turn-context-summary"),/不是實際用量或 provider 已強制的上限/);
  assert.match(ui.text("turn-context-summary"),/provider tokens：未知/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("historical capacity with unknown input never borrows context or output as input usage",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Partially known capacity";task.actions["read-context"]=action(true);
  const ui=await open(data),record=contextRecord(task);
  record.model_capacity={model:record.model,source:"server-curated-model-facts",
    limits:{context_window_tokens:12000,max_prompt_tokens:null,max_output_tokens:1024}};
  ui.holder.contextResponse={ok:true,json:async()=>({task_id:task.task_id,context:record})};
  ui.click("read-context");await ui.tick();await ui.tick();
  assert.match(ui.text("turn-context-summary"),/context 12000 tokens；input 未知；output 1024 tokens/);
  assert.doesNotMatch(ui.text("turn-context-summary"),/10976|input 0/);
  assert.equal(ui.text("ack-state"),"未確認");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("late turn-context reads stay with their original task and logout clears only the local metadata cache",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Original task";task.actions["read-context"]=action(true);
  const second={...copy(task),task_id:"second",job_id:"second-job",label:"Second task"};
  data.tasks.push(second);
  const ui=await open(data);
  let completeRead;
  ui.holder.contextResponse={ok:true,json:()=>new Promise(resolve=>{completeRead=resolve})};
  ui.click("read-context");await ui.tick();
  ui.nodes.get("task").value="second";ui.nodes.get("task").events.change();
  completeRead({task_id:task.task_id,context:contextRecord(task)});await ui.tick();await ui.tick();
  assert.equal(ui.text("turn-context-data"),"");
  assert.match(ui.text("turn-context-summary"),/尚未取得/);
  ui.nodes.get("task").value=task.task_id;ui.nodes.get("task").events.change();
  assert.match(ui.text("turn-context-data"),/saved-turn-metadata/);
  data.authentication="LOGIN_REQUIRED";
  for(const item of data.tasks)item.actions["read-context"]=action(false);
  ui.render(data);
  assert.equal(ui.text("turn-context-data"),"");
  assert.equal(ui.nodes.get("read-context").disabled,true);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("late context from an earlier login cannot repopulate cleared metadata",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Original task";task.actions["read-context"]=action(true);
  const ui=await open(data),answer=ui.text("answer"),ack=ui.text("ack-state");
  let finishOld;
  ui.holder.contextResponse={ok:true,json:()=>new Promise(resolve=>{finishOld=resolve})};
  ui.click("read-context");await ui.tick();
  ui.render({...data,authentication:"LOGIN_EXPIRED"});
  ui.render(data);
  finishOld({task_id:task.task_id,context:contextRecord(task)});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("turn-context-data"),"");
  assert.match(ui.text("turn-context-summary"),/尚未取得/);
  assert.equal(ui.text("answer"),answer);
  assert.equal(ui.text("ack-state"),ack);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

for(const oldOutcome of ["success","refusal","read-error"]){
  test(`fresh context reads survive an earlier login's late ${oldOutcome}`,async()=>{
    const data=fixture(),task=data.tasks[0];
    task.instruction="Original task";task.actions["read-context"]=action(true);
    const ui=await open(data),answer=ui.text("answer"),ack=ui.text("ack-state");
    let finishOld,failOld,finishNew;
    ui.holder.contextResponse={ok:oldOutcome!=="refusal",
      json:()=>new Promise((resolve,reject)=>{finishOld=resolve;failOld=reject})};
    ui.click("read-context");await ui.tick();
    ui.render({...data,authentication:"LOGIN_REQUIRED"});
    ui.render(data);
    assert.equal(ui.nodes.get("read-context").disabled,false);
    ui.holder.contextResponse={ok:true,json:()=>new Promise(resolve=>{finishNew=resolve})};
    ui.click("read-context");await ui.tick();
    if(oldOutcome==="read-error")failOld(new Error("old read failed"));
    else finishOld(oldOutcome==="refusal"?{error:{code:"BACKEND_DENIED"}}:
      {task_id:task.task_id,context:contextRecord(task)});
    await ui.tick();await ui.tick();
    assert.equal(ui.text("turn-context-data"),"");
    assert.equal(ui.nodes.get("read-context").disabled,true);
    assert.match(ui.text("turn-context-note"),/正在純讀/);
    const fresh={...contextRecord(task),read_at:"2026-01-02T00:00:00Z"};
    finishNew({task_id:task.task_id,context:fresh});
    await ui.tick();await ui.tick();
    assert.match(ui.text("turn-context-data"),/2026-01-02T00:00:00Z/);
    assert.equal(ui.nodes.get("read-context").disabled,false);
    assert.equal(ui.text("answer"),answer);
    assert.equal(ui.text("ack-state"),ack);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
    assert.equal(ui.calls.filter(call=>call.url.endsWith("/context")).length,2);
  });
}

test("explicit reconnect invalidates context reads even while authentication still appears available",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Original task";task.actions["read-context"]=action(true);
  const ui=await open(data),answer=ui.text("answer"),ack=ui.text("ack-state");
  let finishOld;
  ui.holder.contextResponse={ok:true,json:()=>new Promise(resolve=>{finishOld=resolve})};
  ui.click("read-context");await ui.tick();
  ui.click("login");await ui.tick();
  assert.equal(ui.holder.pendingPosts.at(-1).url,"/api/authenticate");
  ui.holder.pending();
  await ui.tick();await ui.tick();
  finishOld({task_id:task.task_id,context:contextRecord(task)});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("turn-context-data"),"");
  assert.equal(ui.nodes.get("read-context").disabled,false);
  assert.equal(ui.text("answer"),answer);
  assert.equal(ui.text("ack-state"),ack);
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),["/api/authenticate"]);
});

test("a wrong-turn context response cannot replace the displayed snapshot or change the saved answer",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Original task";task.actions["read-context"]=action(true);
  const ui=await open(data),record=contextRecord(task);
  ui.holder.contextResponse={ok:true,json:async()=>({task_id:task.task_id,context:record})};
  ui.click("read-context");await ui.tick();await ui.tick();
  const answer=ui.text("answer"),ack=ui.text("ack-state");
  ui.holder.contextResponse={ok:true,json:async()=>({task_id:task.task_id,context:{...record,job_id:"another-job"}})};
  ui.click("read-context");await ui.tick();await ui.tick();
  assert.equal(ui.text("turn-context-data"),"");
  assert.match(ui.text("turn-context-note"),/未通過核對/);
  assert.equal(ui.text("answer"),answer);
  assert.equal(ui.text("ack-state"),ack);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("inherited checkpoint observations are not displayed as a new compaction in this turn",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="Original task";task.actions["read-context"]=action(true);
  const ui=await open(data),record=contextRecord(task);
  record.compaction={source:"inherited-checkpoint-observation",observation_job_id:"earlier-job",checkpoint_number:1};
  ui.holder.contextResponse={ok:true,json:async()=>({task_id:task.task_id,context:record})};
  ui.click("read-context");await ui.tick();await ui.tick();
  assert.match(ui.text("turn-context-summary"),/沿用較早回合的 compaction 觀察/);
  assert.doesNotMatch(ui.text("turn-context-summary"),/本回合保存的 compaction/);
  assert.match(ui.text("turn-context-summary"),/不是此次重新驗證壓縮或恢復/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("cancelled saved result is plain language; raw data and operation hashes stay in details",async()=>{
  const data=fixture(),before=JSON.stringify(data),ui=await open(data);
  assert.match(ui.text("answer"),/工作已取消/);
  assert.match(ui.text("answer"),/已保存資料/);
  assert.match(ui.text("answer"),/模型結果仍未明/);
  assert.match(ui.text("answer"),/並非正常完成/);
  assert.match(ui.text("answer"),/預算未退回/);
  assert.doesNotMatch(ui.text("answer"),/model_intents|artifact_stage|request_sha256/);
  assert.equal(ui.text("job-state"),"工作已取消");
  assert.match(ui.text("effects"),/3/);
  assert.doesNotMatch(ui.text("effects"),/[a-f0-9]{64}/);
  assert.equal(ui.text("raw-content"),data.tasks[0].result.content);
  assert.equal(JSON.stringify(data),before);
  assert.match(html,/<details[^>]*>[\s\S]*?<pre id="raw-content"/);
});

test("logged out and submitted restart do not suggest another submission or send requests",async()=>{
  const data=fixture(),t=data.tasks[0];
  data.authentication="LOGIN_REQUIRED";t.result=null;
  for(const value of Object.values(t.actions)){value.enabled=false;value.reason="請先讀取既有登入。"}
  const ui=await open(data);
  assert.equal(ui.text("auth-state"),"尚未登入");
  assert.match(ui.text("notice"),/交辦指令.*指定的助理/);
  assert.match(ui.text("work-message"),/已交辦.*不會再次送出/);
  assert.equal(ui.nodes.get("login").disabled,false);
  for(const action of ["status","fetch","acknowledge","reconcile","submit-reconcile","submit-retry"]){
    assert.equal(ui.nodes.get(action).disabled,true);ui.click(action);
  }
  assert.equal(ui.calls.length,1);
  assert.equal(ui.calls[0].url,"/api/state");
  assert.match(html,/<textarea id="instruction"/);
  assert.doesNotMatch(html,/先通過固定工作|不提供自由文字聊天/);
});

test("original failure ACK reconciliation keeps its explicit recovery and human-read boundary",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.mode="original_failure";t.acknowledgment={state:"UNKNOWN"};
  t.actions.acknowledge=action(false);t.actions.reconcile=action(true);
  const ui=await open(data);
  assert.equal(ui.nodes.get("reconcile-panel").hidden,false);
  t.reconciliation={state:"CONFIRMED",confirmation_level:"client_received",historical:true,observed_at:when};
  t.actions.reconcile=action(false);ui.render(data);
  assert.equal(ui.text("ack-state"),"已核對收到");
  assert.match(ui.text("ack-detail"),/原收件回應遺失.*確認收到.*未重送/);
  assert.equal(ui.nodes.get("reconcile-panel").hidden,true);
  assert.match(ui.text("receipt-code"),/acknowledgment=UNKNOWN.*reconciliation=CONFIRMED/);
  assert.match(html,/收件不代表真人已讀/);
});

test("metrics answer uses only verified returned integers, never saved cancelled data as an answer",async()=>{
  const data=fixture(),t=data.tasks[0],ui=await open(data);
  t.result.work_state="succeeded";t.result.message_kind="final";t.result.unresolved_operations=[];
  t.result.content=JSON.stringify({item_count:37,completed_count:9,remaining_count:28});
  ui.render(data);
  assert.equal(ui.text("answer"),"總項目：37\n已完成：9\n剩餘：28");
  for(const invalid of [
    {item_count:37,completed_count:9,remaining_count:7},
    {item_count:37,completed_count:true,remaining_count:36},
    {item_count:37.5,completed_count:9,remaining_count:28.5},
    {item_count:37,completed_count:9,remaining_count:28,other:"unknown shape"}
  ]){
    t.result.content=JSON.stringify(invalid);ui.render(data);
    assert.match(ui.text("answer"),/不是目前可整理的格式/);
    assert.equal(ui.text("raw-content"),t.result.content);
  }
  t.result.content="12 / 5 / 7";t.result.verified_bytes=false;ui.render(data);
  assert.doesNotMatch(ui.text("answer"),/12|5|7/);
  const cancelled=fixture();cancelled.tasks[0].result.content=JSON.stringify({item_count:12,completed_count:5,remaining_count:7});
  ui.render(cancelled);assert.match(ui.text("answer"),/工作已取消/);
  assert.doesNotMatch(ui.text("answer"),/12|5|7|總項目/);
});

test("verified model limit failure displays its fixed reason without enabling a resend",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.result.work_state="failed";task.result.message_kind="failure";
  task.result.content=JSON.stringify({schema_version:"fa-assistant-notice-v1",state:"failed",
    sdk_reply_available:false,code:"MODEL_CALL_LIMIT_REACHED"});
  const ui=await open(data);
  assert.match(ui.text("answer"),/模型呼叫上限.*MODEL_CALL_LIMIT_REACHED/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  task.result.verified_bytes=false;ui.render(data);
  assert.doesNotMatch(ui.text("answer"),/MODEL_CALL_LIMIT_REACHED/);
});

test("wrong-work or malformed cancellation content cannot assert saved model or refund facts",async()=>{
  const data=fixture(),t=data.tasks[0],ui=await open(data);
  const content=JSON.parse(t.result.content);content.work_id="other-work";t.result.content=JSON.stringify(content);
  ui.render(data);
  assert.match(ui.text("answer"),/取消內容無法整理/);
  assert.doesNotMatch(ui.text("answer"),/已保存資料|預算未退回|模型結果仍未明/);
  t.result.content="{malformed";ui.render(data);
  assert.match(ui.text("answer"),/無法整理/);
  assert.equal(ui.text("raw-content"),"{malformed");
  t.result.work_state="succeeded";t.result.message_kind="final";
  t.result.content="<img src=x onerror=alert(1)>";ui.render(data);
  assert.equal(ui.text("answer"),t.result.content);
  assert.equal(ui.nodes.get("answer").innerHTML,undefined);
  assert.doesNotMatch(html,/\.innerHTML|insertAdjacentHTML|eval\(/);
});

test("server error has plain main copy and untouched details, without retry or enabling ACK",async()=>{
  const data=fixture(),ui=await open(data);
  ui.holder.response={ok:false,json:async()=>({error:{
    code:"BACKEND_OFFLINE",message:"OFFLINE transport diagnostic original-id",automatic_retry:false}})};
  ui.click("status");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("error").hidden,false);
  // P01: one plain sentence above the composer; the full text and raw error only in the support details.
  assert.equal(ui.text("error-message"),"暫時無法取得服務回應。");
  assert.doesNotMatch(ui.text("error-message"),/BACKEND_OFFLINE|original-id|不會自動重送/);
  assert.match(ui.text("error-request"),/offline/,"the read on the task's own route carries its ID");
  assert.equal(ui.nodes.get("error-details").hidden,false);
  assert.match(ui.text("error-full"),/仍未確認，不會自動重送/);
  assert.match(ui.text("error-code"),/BACKEND_OFFLINE.*|original-id/);
  ui.click("open-error-details");
  assert.equal(ui.nodes.get("diagnostics").hidden,false);
  assert.equal(ui.nodes.get("error-details").open,true);
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,1);
  const count=ui.calls.length;await ui.tick();assert.equal(ui.calls.length,count);
});

test("switching tasks does not carry results, receipt, errors or recovery controls to another work",async()=>{
  const data=fixture(),second=copy(data.tasks[0]);
  second.task_id="second";second.label="OFFLINE 第二筆";second.result=null;
  second.reply=null;second.progress={data:{state:"queued",reply_available:false,delivery_id:null},observed_at:when};
  second.submission={state:"NOT_SENT"};second.fetch={state:"NOT_SENT"};second.acknowledgment={state:"NOT_SENT"};
  second.actions.acknowledge=action(false);second.actions["confirm-reply"]=action(false);
  data.tasks.push(second);
  const ui=await open(data);ui.nodes.get("task").value="second";ui.nodes.get("task").events.change();
  assert.equal(ui.nodes.get("verified").hidden,true);
  assert.equal(ui.text("ack-state"),"未確認");
  assert.equal(ui.nodes.get("acknowledge").disabled,true);
  assert.equal(ui.calls.length,1);
});

test("unconfigured preview and expired login never claim a recent permission",async()=>{
  const ui=await open({mode:"PREVIEW_ONLY",environment:"NOT_RUN",reason:"本機預覽；尚未連接服務。",
    tasks:[],authentication:"LOGIN_REQUIRED",automatic_polling:false});
  assert.match(ui.text("work-message"),/尚未設定工作/);
  assert.equal(ui.nodes.get("status").disabled,true);
  assert.equal(ui.nodes.get("login").disabled,true);
  const data=fixture();data.authentication="LOGIN_EXPIRED";
  for(const a of Object.values(data.tasks[0].actions)){a.enabled=false;a.reason="登入已過期。"}
  ui.render(data);
  assert.equal(ui.text("auth-state"),"登入已過期");
  assert.equal(ui.nodes.get("acknowledge").disabled,true);
  ui.click("acknowledge");assert.equal(ui.calls.length,1);
});

test("composer preserves exact original text and stable message ID on doubleclick",async()=>{
  const data=fixture(),t=data.tasks[0];
  data.composer={request_id:"offline-text-id",content:null,editable:true,can_submit:true,can_follow_up:false,input_capacity:{maximum:8192},models};
  const ui=await open(data);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  const text="  請解釋這個概念。\n保留原文🙂 <script>  ";
  ui.nodes.get("instruction").value=text;ui.nodes.get("instruction").events.input();
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  ui.click("send-instruction");ui.click("send-instruction");
  const sent=ui.calls.filter(call=>call.method==="POST");
  assert.equal(sent.length,1);
  assert.equal(sent[0].url,"/api/instructions/offline-text-id/submit");
  assert.deepEqual(JSON.parse(sent[0].body),{content:text,model:"offline-chat-a"});
  data.composer={...data.composer,content:text,editable:false,can_submit:false};
  const accepted={...copy(t),task_id:"offline-text-id",submit_request_id:"offline-text-id",
    instruction:text,submission:{state:"CONFIRMED"},result:null,reply:null};
  data.tasks.push(accepted);
  ui.holder.current=data;ui.holder.pending({task:accepted});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.nodes.get("conversation-history").children.at(-1).children[1].textContent,text);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
});

test("composer UTF8 quoted limit is not a character whitelist; server text reply stays textContent",async()=>{
  const data=fixture(),t=data.tasks[0];
  data.composer={request_id:"offline-draft",content:null,editable:true,can_submit:true,can_follow_up:false,input_capacity:{maximum:8192},models};
  t.instruction="請回覆我的任意文字指令";
  t.result.work_state="succeeded";t.result.message_kind="final";
  t.result.content=JSON.stringify({schema_version:"fa-assistant-text-result-v1",text:"真正回覆\n<b>不是 HTML</b>",
    sdk_session_id:"OFFLINE-server-session",conversation_id:"OFFLINE-server-conversation",turn_number:1});
  const ui=await open(data);
  assert.equal(ui.text("answer"),"真正回覆\n<b>不是 HTML</b>");
  for(const text of [" ","🙂".repeat(2048),"a".repeat(8191)]){
    ui.nodes.get("instruction").value=text;ui.nodes.get("instruction").events.input();
    assert.equal(ui.nodes.get("send-instruction").disabled,true);
  }
  ui.nodes.get("instruction").value="a".repeat(8190);ui.nodes.get("instruction").events.input();
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  assert.equal(ui.nodes.get("input-capacity").hidden,true);
  const turn=ui.nodes.get("conversation-history").children[0];
  assert.equal(turn.children[1].textContent,t.instruction);
  assert.equal(turn.children[3].textContent,"真正回覆\n<b>不是 HTML</b>");
});

test("new instruction progress updates are GET-only and stop for hidden or completed conversations",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="OFFLINE progress fixture";t.result=null;
  data.composer={request_id:t.submit_request_id,content:t.instruction,editable:false,can_submit:false,
    can_follow_up:false,progress_available:true,conversation:{
      can_follow_up:false,turns:[{request_id:t.submit_request_id,state:"running"}]}};
  const ui=await open(data);
  assert.equal(ui.timers.size,1);
  const callback=[...ui.timers.values()][0];ui.timers.clear();
  t.progress={data:{state:"running"},observed_at:when};ui.holder.current=data;
  await callback();
  assert.equal(ui.calls.at(-1).url,"/api/instructions/progress");
  assert.equal(ui.calls.at(-1).method,undefined);
  assert.equal(ui.text("job-state"),"處理中");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  ui.context.document.hidden=true;ui.documentEvents.visibilitychange();
  assert.equal(ui.timers.size,0);
  ui.context.document.hidden=false;
  data.composer.conversation.can_follow_up=true;data.composer.conversation.turns[0].state="succeeded";
  t.reply={data:{text:"OFFLINE saved reply"}};
  ui.render(data);assert.equal(ui.timers.size,0);
});

test("selected deployment is sent unchanged and unknown capacities are not infinite",async()=>{
  const data=fixture();
  data.composer={request_id:"offline-model-choice",content:null,editable:true,can_submit:true,can_follow_up:false,
    input_capacity:{maximum:8192},models};
  const ui=await open(data);
  assert.match(ui.text("model-capacity"),/未知（不代表無限）/);
  assert.equal(ui.nodes.get("model").children.length,2);
  ui.nodes.get("model").value="offline-chat-b";ui.nodes.get("model").events.change();
  ui.nodes.get("instruction").value="測試真正傳遞所選模型";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");
  assert.equal(JSON.parse(ui.calls.find(call=>call.method==="POST").body).model,"offline-chat-b");
  ui.holder.current={...data,composer:{...data.composer,editable:false,can_submit:false}};
  ui.holder.pending();await ui.tick();await ui.tick();
  assert.doesNotMatch(ui.text("model-capacity"),/gpt-5|o3|無限 tokens/);
});

test("process snapshot shows only public step and tool projection, never private reasoning",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.progress={data:{state:"running",progress:{
    events:[{seq:1,at:when,kind:"tool_started",tool_name:"get_context",summary:"OFFLINE 已遮罩摘要",source:"OFFLINE source"},
      {seq:2,at:when,kind:"tool_completed",tool_name:"get_context",status:"succeeded"},
      {seq:3,at:when,kind:"reasoning_delta",summary:"PRIVATE_REASONING_MUST_NOT_RENDER"}],
    reasoning_summary:{availability:"not_provided",source:"copilot-sdk-1.0.13",text:"PRIVATE_REASONING_MUST_NOT_RENDER"}}},
    observed_at:when};
  const ui=await open(data);
  assert.equal(ui.text("reasoning-summary"),"此模型未提供推理摘要。");
  const process=ui.nodes.get("processing-events").children.map(node=>node.textContent).join("\n");
  assert.match(process,/工具開始.*get_context.*已遮罩摘要/);
  assert.match(process,/工具完成.*get_context.*succeeded/);
  assert.match(process,/2026-01-01T00:00:00Z/);
  assert.doesNotMatch(process,/PRIVATE_REASONING/);
  assert.doesNotMatch([...ui.nodes.values()].map(node=>node.textContent).join("\n"),/PRIVATE_REASONING/);
  assert.equal(ui.calls.length,1);
});

test("tool start sequences pair interleaved results without rendering raw identifiers",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.progress={data:{state:"running",progress:{
    events:[
      {seq:1,at:when,kind:"tool_started",tool_name:"read_context",tool_start_seq:1},
      {seq:2,at:when,kind:"tool_started",tool_name:"read_context",tool_start_seq:2},
      {seq:3,at:when,kind:"tool_completed",tool_name:"read_context",tool_start_seq:2,status:"failed"},
      {seq:4,at:when,kind:"tool_completed",tool_name:"read_context",tool_start_seq:1,status:"succeeded"},
      {seq:5,at:when,kind:"tool_completed",tool_name:"read_context",tool_start_seq:"PRIVATE_REF",tool_call_id:"PRIVATE_RAW"},
      {seq:6,at:when,kind:"stage",tool_start_seq:1},
      {seq:7,at:when,kind:"tool_completed",tool_start_seq:8}
    ],reasoning_summary:{availability:"not_provided"}}},observed_at:when};
  const ui=await open(data);
  const lines=ui.nodes.get("processing-events").children.map(node=>node.textContent);
  assert.match(lines[0],/工具開始.*工具關聯 #1.*read_context/);
  assert.match(lines[1],/工具開始.*工具關聯 #2.*read_context/);
  assert.match(lines[2],/工具完成.*工具關聯 #2.*failed/);
  assert.match(lines[3],/工具完成.*工具關聯 #1.*succeeded/);
  for(const line of lines.slice(4))assert.doesNotMatch(line,/工具關聯 #/);
  assert.doesNotMatch(lines.join("\n"),/PRIVATE_/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

const progressAt=seconds=>new Date(Date.parse(when)+seconds*1000).toISOString();
function liveProcessing(){
  const data=fixture(),task=data.tasks[0];
  task.instruction="OFFLINE public progress";task.thread_id="progress-thread";task.result=null;task.reply=null;
  const progress={events:[
    {seq:1,at:progressAt(0),kind:"stage",status:"started",summary:"SDK session started"},
    {seq:2,at:progressAt(1),kind:"tool_started",tool_name:"WebIQ_MCP_web",tool_start_seq:2,status:"started"},
  ]};
  task.progress={data:{state:"running",progress},observed_at:progressAt(4)};
  data.composer={thread_id:task.thread_id,request_id:task.submit_request_id,progress_available:true,
    editable:false,can_follow_up:false,conversation:{can_follow_up:false,turns:[
      {request_id:task.submit_request_id,state:"running",progress}
    ]}};
  return data;
}
function inlineProcessing(ui,index=0){
  return ui.nodes.get("conversation-history").children[index].children.find(node=>node.className==="turn-processing");
}
const processingText=panel=>panel.children[1].children.map(node=>node.textContent).join("\n");

test("native model refusal names the model and the provider's sanitized reason from the public step",async()=>{
  // 2026-10-04 stage A: Azure Responses rejected gpt-6-luna's reasoning.effort before any output.
  const data=nativeChat("unknown"),task=data.tasks[0];
  task.native_receipt.failure_code="NATIVE_MODEL_REFUSED";
  task.admission={conversation_id:"native-cid",turn_number:1,model:"gpt-6-luna"};
  delete data.composer.conversation.turns[0].progress;
  task.progress={data:{state:"unknown",progress:{events:[
    {seq:1,at:progressAt(0),kind:"stage",status:"started",summary:"SDK session started"},
    {seq:2,at:progressAt(1),kind:"error",status:"failed",
      summary:"Model provider refused: status=400 code=unsupported_parameter param=reasoning.effort"},
  ]}},recorded:true,finished:true,truncated:false,byte_length:600};
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.match(panel.children[0].textContent,/^模型拒絕了這次請求/);
  assert.match(processingText(panel),/模型服務拒絕了這次請求（不支援 reasoning\.effort）/);
  assert.equal(turnNode(ui).children[3].textContent,
    "模型服務拒絕了這次請求（gpt-6-luna：不支援 reasoning.effort），沒有執行任何動作。可以換模型再問一次。");
  // The same provider step on a turn that may have acted (e.g. after a tool call) keeps the unknown wording.
  const ordinary=copy(data);ordinary.tasks[0].native_receipt.failure_code="NATIVE_TURN_UNKNOWN";ui.render(ordinary);
  assert.match(inlineProcessing(ui).children[0].textContent,/^結果不確定/);
  assert.equal(turnNode(ui).children[3].textContent,"這次回覆的結果還不確定。");
  assert.match(ui.text("processing-detail"),/NATIVE_TURN_UNKNOWN/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Release 30 dev test (2026-10-14): Hosted saves a stopped turn as unknown with NATIVE_TURN_CANCELLED; the page said
// 「這次回覆的結果還不確定」 under 「已要求停止這次回覆」. Tools may have run before the stop, so it says so.
test("a stopped native turn says it stopped and may have acted, also after 更新回覆",async()=>{
  const data=nativeChat("started"),stopped=copy(data);
  data.tasks[0].native_receipt.cancel_requested=true;data.tasks[0].actions.cancel=action(false);
  const task=stopped.tasks[0];
  task.native_receipt={...task.native_receipt,status:"unknown",cancel_requested:true,failure_code:"NATIVE_TURN_CANCELLED"};
  task.status.data.state=stopped.composer.conversation.turns[0].state="unknown";task.actions.cancel=action(false);
  const ui=await open(data);
  const text="已停止。停止前可能已經做了部分動作。";
  const shown=()=>JSON.stringify(turnNode(ui),(key,value)=>key==="events"?undefined:value);
  assert.match(shown(),/已要求停止這次回覆/);
  assert.equal(ui.nodes.get("refresh-progress").disabled,false);
  ui.holder.progressResponse=()=>({ok:true,json:async()=>copy(stopped)});
  ui.click("refresh-progress");for(let i=0;i<4;i++)await ui.tick();
  assert.equal(turnNode(ui).children[3].textContent,text);
  assert.equal(shown().split(text).length-1,1,"said once in the turn");
  assert.doesNotMatch(shown(),/結果還不確定|已要求停止/);
  assert.match(inlineProcessing(ui).children[0].textContent,/^已停止/);
  assert.equal(vm.runInContext("cancellationMessage(state.tasks[0])",ui.context),text);
  // A stopped turn whose collaboration is unconfirmed keeps saying so in the summary.
  const unconfirmed=copy(stopped);unconfirmed.tasks[0].native_receipt.notification_error="NATIVE_TURN_BUSY";ui.render(unconfirmed);
  assert.equal(inlineProcessing(ui).children[0].textContent,"已停止；和另一位助理的協作還沒確認");
  assert.match(inlineProcessing(ui).children[1].textContent,/不要重新交辦/);
  // Any other unknown keeps the uncertain wording.
  const other=copy(stopped);other.tasks[0].native_receipt.failure_code="NATIVE_TURN_UNKNOWN";ui.render(other);
  assert.equal(turnNode(ui).children[3].textContent,"這次回覆的結果還不確定。");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a stopped native turn with tool steps says 已停止 in the summary row, not 結果不確定",async()=>{
  const stopped=nativeChat("unknown"),task=stopped.tasks[0];
  task.native_receipt.failure_code="NATIVE_TURN_CANCELLED";task.native_receipt.cancel_requested=true;
  delete stopped.composer.conversation.turns[0].progress;
  task.progress={data:{state:"unknown",progress:{events:[
    {seq:1,at:progressAt(0),kind:"tool_started",tool_name:"read_skill",status:"started"},
    {seq:2,at:progressAt(20),kind:"tool_completed",tool_name:"read_skill",tool_start_seq:1,status:"succeeded"}]}},
    recorded:true,finished:true,truncated:false,byte_length:400};
  const ui=await open(stopped),row=()=>inlineProcessing(ui).children[0].textContent;
  assert.equal(row(),"已停止・用了 1 個工具・20 秒");
  const unconfirmed=copy(stopped);unconfirmed.tasks[0].native_receipt.notification_error="NATIVE_TURN_BUSY";ui.render(unconfirmed);
  assert.equal(row(),"已停止；和另一位助理的協作還沒確認・用了 1 個工具・20 秒");
  assert.match(inlineProcessing(ui).children[2].textContent,/不要重新交辦/);
  // Any other unknown keeps the uncertain wording.
  const other=copy(stopped);other.tasks[0].native_receipt.failure_code="NATIVE_TURN_UNKNOWN";ui.render(other);
  assert.equal(row(),"結果不確定・用了 1 個工具・20 秒");
});

for(const lane of ["foundry-native-v1","foundry-responses-v1"]){
  test(`native relay queue progress says not delivered on ${lane}`,async()=>{
    const data=nativeChat("started"),task=data.tasks[0];
    task.state_profile=data.composer.state_profile=lane;
    task.progress={data:{state:"started",progress:{events:[{
      seq:1,at:when,kind:"stage",status:"started",
      summary:"已排隊，助理 B 換版完成後處理；尚未確認送達，不會重送。"}]}},
      recorded:true,finished:false,truncated:false,byte_length:400};
    const ui=await open(data),panel=inlineProcessing(ui);
    assert.match(processingText(panel),/已排隊，助理 B 換版完成後處理；尚未確認送達/);
    assert.doesNotMatch(processingText(panel),/已交給|已送達/);
    assert.equal(task.native_receipt.status,"started");
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });

  test(`native public tool steps and summaries remain visible after completion on ${lane}`,async()=>{
    const data=nativeChat("started"),task=data.tasks[0],snapshot=liveProcessing().tasks[0].progress.data.progress;
    data.composer.state_profile=task.state_profile=lane;
    snapshot.events.push(
      {seq:3,at:progressAt(2),kind:"tool_completed",tool_name:"WebIQ_MCP_web",tool_start_seq:2,status:"succeeded",
        summary:"Tool returned a result"},
      {seq:4,at:progressAt(3),kind:"stage",status:"started",summary:"OpenAI reasoning summary",
        reasoning_summary:"先核對公開來源，再整理答案。"});
    snapshot.reasoning_summary={availability:"provided",source:"openai-responses",text:"先核對公開來源，再整理答案。"};
    task.progress={data:{state:"started",progress:snapshot},recorded:true,finished:false,truncated:false,byte_length:1200};
    const ui=await open(data,{now:Date.parse(progressAt(4))});
    const running=inlineProcessing(ui);
    assert.match(processingText(running),/完成：網路搜尋/);
    assert.match(processingText(running),/思考摘要：先核對公開來源/);
    assert.doesNotMatch(processingText(running),/推理摘要|OpenAI|工具關聯|SDK/);
    assert.match(ui.text("reasoning-summary"),/OpenAI 已提供公開推理摘要/);
    assert.match(ui.nodes.get("processing-events").children.map(node=>node.textContent).join("\n"),
      /工具關聯 #2[\s\S]*推理摘要（OpenAI，節錄）：先核對公開來源/);
    assert.equal(running.open,true);
    const done=copy(data);
    done.tasks[0].native_receipt.status="completed";done.tasks[0].status.data.state="completed";
    done.tasks[0].reply={data:{text:"原本完整答案"},observed_at:progressAt(5)};
    done.tasks[0].progress.data.state="completed";done.tasks[0].progress.finished=true;
    done.composer.conversation.turns[0].state="completed";
    ui.render(done);
    assert.match(processingText(inlineProcessing(ui)),/完成：網路搜尋/);
    assert.match(processingText(inlineProcessing(ui)),/先核對公開來源/);
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}

test("native public activity marks truncated or unconfirmed reads without hiding the original answer",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.progress={data:{state:"completed",progress:liveProcessing().tasks[0].progress.data.progress},
    recorded:true,finished:false,truncated:true,byte_length:8192};
  task.progress_error="NATIVE_STATE_STORE_UNKNOWN";
  const ui=await open(data),panel=inlineProcessing(ui);
  assert.match(processingText(panel),/使用工具：網路搜尋/);
  assert.equal(panel.children[2].textContent,"","no progress bookkeeping on the main screen");
  assert.match(ui.text("processing-detail"),/已截斷/);
  assert.match(ui.text("processing-detail"),/未確認.*保留/);
  const text=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(text,/完整回覆/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a slow native activity read never delays the original reply or resends work",async()=>{
  const data=nativeChat("started");
  data.tasks[0].actions["read-progress"]=action(true);
  let release;
  const waiting=new Promise(resolve=>{release=resolve});
  const ui=await open(data,{nativeProgressResponse:()=>waiting});
  const panel=inlineProcessing(ui);panel.children[0].click();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/progress")).length,1);
  const done=copy(data);done.tasks[0].native_receipt.status="completed";
  done.tasks[0].reply={data:{text:"答案先完整顯示"},observed_at:when};
  done.composer.conversation.turns[0].state="completed";
  ui.holder.progressResponse={ok:true,json:async()=>copy(done)};
  await vm.runInContext("updateProgress()",ui.context);
  const text=()=>JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.match(text(),/答案先完整顯示/);
  const snapshot=liveProcessing().tasks[0].progress.data.progress;
  release({ok:true,json:async()=>({request_id:data.tasks[0].task_id,progress:{
    data:{state:"completed",progress:snapshot},recorded:true,finished:false,truncated:false,byte_length:600}})});
  await ui.tick();await ui.tick();
  assert.match(processingText(inlineProcessing(ui)),/使用工具：網路搜尋/);
  assert.match(text(),/答案先完整顯示/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("a native activity response from an earlier console configuration is ignored",async()=>{
  const data=nativeChat("started");
  data.tasks[0].actions["read-progress"]=action(true);
  let release;
  const ui=await open(data,{nativeProgressResponse:()=>new Promise(resolve=>{release=resolve})});
  const panel=inlineProcessing(ui);panel.children[0].click();await ui.tick();
  const newer=copy(data);newer.console_id="new-console-configuration";
  ui.render(newer);
  release({ok:true,json:async()=>({request_id:data.tasks[0].task_id,progress:{
    data:{state:"started",progress:{events:[{seq:1,at:when,kind:"stage",summary:"OLD-CONSOLE-DETAIL"}]}},
    recorded:true,finished:true,truncated:false,byte_length:700}})});
  await ui.tick();await ui.tick();
  const text=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  assert.doesNotMatch(text,/OLD-CONSOLE-DETAIL/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("opening a completed native conversation reads its saved activity when its steps are opened",async()=>{
  const initial=nativeChat("completed"),ui=await open(initial);
  const saved=copy(initial);
  saved.composer.thread_id="past-conversation";
  saved.tasks[0].thread_id="past-conversation";
  saved.tasks[0].actions["read-progress"]=action(true);
  const snapshot=liveProcessing().tasks[0].progress.data.progress;
  ui.holder.current=saved;
  ui.holder.nativeProgressResponse={ok:true,json:async()=>({request_id:saved.tasks[0].task_id,progress:{
    data:{state:"completed",progress:snapshot},recorded:true,finished:true,truncated:false,byte_length:700}})};
  await vm.runInContext('switchThread("past-conversation")',ui.context);
  await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/progress")).length,0,"opening reads no per-turn steps");
  const panel=inlineProcessing(ui);panel.children[0].click();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/progress")).length,1);
  assert.match(processingText(inlineProcessing(ui)),/使用工具：網路搜尋/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("inline processing opens below the pending reply with live plain labels and no new requests",async()=>{
  const data=liveProcessing(),ui=await open(data,{now:Date.parse(progressAt(4))});
  const panel=inlineProcessing(ui),turn=ui.nodes.get("conversation-history").children[0];
  assert.equal(turn.children[4],panel);
  assert.equal(panel.open,true);
  assert.match(turn.children[3].textContent,/正在準備回覆/);
  assert.equal(panel.children[0].textContent,"正在讀取・用了 1 個工具・4 秒");
  assert.match(processingText(panel),/開始處理/);
  assert.doesNotMatch(processingText(panel),/第 \d+ 秒 ·|工具關聯|SDK|推理摘要/);
  assert.match(processingText(panel),/使用工具：網路搜尋（進行中 3 秒）/);
  assert.match(html,/<aside[^>]*id="diagnostics"[^>]*hidden/);
  assert.equal(ui.nodes.get("close-diagnostics").focused,undefined);
  assert.equal(ui.calls.length,1);
  assert.equal(ui.timers.size,1);
});

test("inline processing seconds update in place and preserve manual collapse and reader scroll",async()=>{
  const data=liveProcessing(),ui=await open(data,{now:Date.parse(progressAt(4))});
  const history=ui.nodes.get("conversation-history"),turn=history.children[0],panel=inlineProcessing(ui);
  const summary=panel.children[0],line=panel.children[1].children[1],scroll=ui.nodes.get("chat-scroll");
  panel.open=false;scroll.scrollHeight=1000;scroll.clientHeight=300;scroll.scrollTop=100;
  ui.holder.now=Date.parse(progressAt(7));ui.render(copy(data));
  assert.equal(history.children[0],turn);assert.equal(inlineProcessing(ui),panel);
  assert.equal(panel.children[0],summary);assert.equal(panel.children[1].children[1],line);
  assert.match(line.textContent,/進行中 6 秒/);
  assert.equal(panel.open,false);assert.equal(scroll.scrollTop,100);
  data.tasks[0].progress.data.progress.events.push(
    {seq:3,at:progressAt(6),kind:"tool_completed",tool_name:"WebIQ_MCP_web",tool_start_seq:2,status:"succeeded"});
  ui.render(data);
  assert.equal(inlineProcessing(ui),panel);assert.equal(panel.open,false);
  assert.match(processingText(panel),/完成：網路搜尋（5 秒）/);
  assert.equal(scroll.scrollTop,100);assert.equal(ui.calls.length,1);
});

test("inline processing finishes with a stable tool count and elapsed summary then can reopen",async()=>{
  const data=liveProcessing(),ui=await open(data,{now:Date.parse(progressAt(4))});
  const panel=inlineProcessing(ui),task=data.tasks[0],turn=data.composer.conversation.turns[0];
  turn.progress.events.push(
    {seq:3,at:progressAt(6),kind:"tool_completed",tool_name:"WebIQ_MCP_web",tool_start_seq:2,status:"succeeded"},
    {seq:4,at:progressAt(9),kind:"tool_started",tool_name:"read_skill",tool_start_seq:4,status:"started"},
    {seq:5,at:progressAt(12),kind:"tool_completed",tool_name:"read_skill",tool_start_seq:4,status:"succeeded"},
    {seq:6,at:progressAt(13),kind:"stage",status:"started",summary:"SDK turn started"},
    {seq:7,at:progressAt(41),kind:"stage",status:"succeeded",summary:"SDK turn ended"});
  turn.state=task.progress.data.state="succeeded";
  task.reply={data:{text:"Saved answer"}};
  data.composer.conversation.can_follow_up=true;
  ui.holder.now=Date.parse(progressAt(50));ui.render(data);
  assert.equal(inlineProcessing(ui),panel);assert.equal(panel.open,false);
  assert.equal(panel.children[0].textContent,"已完成・用了 2 個工具・41 秒");
  assert.match(processingText(panel),/使用工具：讀取 skill/);
  assert.match(processingText(panel),/整理回覆中/);
  assert.doesNotMatch(processingText(panel),/進行中/);
  panel.open=true;ui.holder.now=Date.parse(progressAt(100));ui.render(copy(data));
  assert.equal(panel.open,true);
  assert.equal(panel.children[0].textContent,"已完成・用了 2 個工具・41 秒");
  assert.equal(ui.timers.size,0);assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

for(const [code,label] of [
  ["MODEL_CALL_LIMIT_REACHED",/模型呼叫上限/],
  ["AGENT_MESSAGE_RESULT_UNCONFIRMED",/已接收.*B 的結果尚未確認/],
]){
  test(`inline processing explains ${code} without a generic unconfirmed reason or resend`,async()=>{
    const data=liveProcessing(),turn=data.composer.conversation.turns[0];
    turn.progress.events.push({seq:3,at:progressAt(8),kind:"error",
      status:code==="MODEL_CALL_LIMIT_REACHED"?"failed":"unknown",summary:code});
    turn.state=data.tasks[0].progress.data.state="failed";
    const ui=await open(data),panel=inlineProcessing(ui);
    assert.match(processingText(panel),label);
    assert.doesNotMatch(processingText(panel),new RegExp(code));
    assert.match(ui.nodes.get("processing-events").children.map(node=>node.textContent).join("\n"),new RegExp(code));
    assert.doesNotMatch(processingText(panel),/（結果未確認）|不會自動重送/);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  });
}

for(const status of ["failed","cancelled"]){
  test(`inline processing retains reached steps on ${status} without claiming an unfinished tool succeeded`,async()=>{
    const data=liveProcessing(),ui=await open(data,{now:Date.parse(progressAt(4))});
    const panel=inlineProcessing(ui),turn=data.composer.conversation.turns[0];
    turn.progress.events.push({seq:3,at:progressAt(8),kind:"error",status:"unknown",summary:"SDK reported an unresolved error"});
    turn.state=data.tasks[0].progress.data.state=status;ui.render(data);
    assert.equal(panel.open,false);assert.equal(panel.children[1].children.length,3);
    assert.match(panel.children[0].textContent,status==="failed"?/^沒有完成/:/^已停止/);
    assert.match(processingText(panel),/沒有完成紀錄/);assert.match(processingText(panel),/處理遇到問題/);
    assert.doesNotMatch(processingText(panel),/進行中|完成：|SDK/);
    assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  });
}

test("inline processing pairs concurrent same-name calls and marks pruned or legacy snapshots honestly",async()=>{
  const data=liveProcessing(),turn=data.composer.conversation.turns[0];
  turn.progress.events.push(
    {seq:3,at:progressAt(2),kind:"tool_started",tool_name:"WebIQ_MCP_web",tool_start_seq:3,status:"started"},
    {seq:4,at:progressAt(4),kind:"tool_completed",tool_name:"WebIQ_MCP_web",tool_start_seq:3,status:"succeeded"});
  const ui=await open(data,{now:Date.parse(progressAt(6))}),panel=inlineProcessing(ui);
  assert.match(panel.children[0].textContent,/用了 2 個工具/);
  assert.match(panel.children[1].children[1].textContent,/進行中 5 秒/);
  assert.match(panel.children[1].children[2].textContent,/完成 2 秒/);
  assert.doesNotMatch(processingText(panel),/#\d/);
  assert.match(ui.nodes.get("processing-events").children.map(node=>node.textContent).join("\n"),/工具關聯 #2[\s\S]*工具關聯 #3/);
  turn.progress.events.splice(0,3);ui.render(data);
  assert.match(panel.children[0].textContent,/用了 1 個工具/);
  assert.equal(panel.children[2].textContent,"");
  assert.match(ui.text("processing-detail"),/部分步驟/);
  assert.doesNotMatch(processingText(panel),/耗時/);
  turn.progress.events=[
    {seq:1,at:progressAt(1),kind:"tool_started",tool_name:"read_skill"},
    {seq:2,at:progressAt(2),kind:"tool_completed",tool_name:"read_skill",status:"succeeded"}];
  ui.render(data);
  assert.match(panel.children[0].textContent,/用了 1 個工具/);
  assert.match(processingText(panel),/使用工具：讀取 skill（已開始）/);
  assert.doesNotMatch(processingText(panel),/進行中|工具關聯/);
});

test("inline processing filters private material and clears panels across threads and authentication",async()=>{
  const data=liveProcessing(),turn=data.composer.conversation.turns[0];
  turn.progress.events.push({seq:3,at:progressAt(3),kind:"reasoning_delta",summary:"PRIVATE_REASONING"});
  turn.progress.reasoning_summary={availability:"not_provided",text:"PRIVATE_REASONING"};
  const ui=await open(data,{now:Date.parse(progressAt(4))}),panel=inlineProcessing(ui);
  assert.doesNotMatch(processingText(panel),/PRIVATE_REASONING/);
  assert.equal(panel.children[2].textContent,"");
  assert.match(ui.text("reasoning-summary"),/此模型未提供推理摘要/);
  data.authentication="LOGIN_EXPIRED";ui.render(data);
  assert.equal(inlineProcessing(ui),undefined);
  assert.equal(vm.runInContext("processingPanels.size",ui.context),0);
  data.authentication="AVAILABLE";data.composer.thread_id=data.tasks[0].thread_id="another-thread";ui.render(data);
  assert.notEqual(inlineProcessing(ui),panel);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("inline processing handles an empty snapshot or paused update without inventing activity",async()=>{
  const data=liveProcessing(),ui=await open(data,{now:Date.parse(progressAt(7))});
  const panel=inlineProcessing(ui);
  vm.runInContext('progressPaused=true;lastError={phase:"progress",code:"LOGIN_REQUIRED"}',ui.context);
  ui.render(data);
  assert.match(panel.children[0].textContent,/^正在等候更新/);
  assert.doesNotMatch(processingText(panel),/進行中/);
  data.composer.conversation.turns[0].progress.events=[];ui.render(data);
  assert.equal(panel.children[1].children.length,0);
  assert.equal(panel.children[2].textContent,"");
  assert.match(ui.text("processing-detail"),/尚未取得可公開的處理步驟/);
  assert.doesNotMatch(panel.children[0].textContent,/NaN|undefined/);
});

test("inline processing renders safe tool summaries and public assistant excerpts only as text steps",async()=>{
  const data=liveProcessing(),progress=data.composer.conversation.turns[0].progress;
  progress.events[1].summary="Search query: Foundry hosted agents <not-html>";
  progress.events.push(
    {seq:3,at:progressAt(2),kind:"stage",status:"started",summary:"Assistant note: 我先查找官方資料，再整理答案。"},
    {seq:4,at:progressAt(3),kind:"tool_started",tool_name:"playwright_browser_navigate",tool_start_seq:4,
      summary:"Open URL: https://example.org/docs"},
    {seq:5,at:progressAt(4),kind:"tool_started",tool_name:"read_skill",tool_start_seq:5,summary:"Skill name: formal-report"});
  const ui=await open(data,{now:Date.parse(progressAt(6))}),panel=inlineProcessing(ui);
  const lines=processingText(panel);
  assert.match(lines,/搜尋關鍵字：Foundry hosted agents <not-html>/);
  assert.match(lines,/助理說明（節錄）：我先查找官方資料，再整理答案。/);
  assert.match(lines,/開啟網址：https:\/\/example.org\/docs/);
  assert.match(lines,/skill 名稱：formal-report/);
  assert.equal(panel.children[1].children[1].children.length,0);
  assert.match(ui.nodes.get("conversation-history").children[0].children[3].textContent,/正在準備回覆/);
  assert.doesNotMatch(ui.nodes.get("conversation-history").children[0].children[3].textContent,/我先查找/);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  assert.equal(ui.text("reasoning-summary"),"此模型未提供推理摘要。");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("inline processing shows the explicit OpenAI summary in steps without replacing the reply",async()=>{
  const data=liveProcessing(),progress=data.composer.conversation.turns[0].progress;
  progress.events.push({seq:3,at:progressAt(2),kind:"stage",status:"started",summary:"OpenAI reasoning summary",
    reasoning_summary:"先核對公開來源，再比較兩個結果。"});
  progress.reasoning_summary={availability:"provided",source:"openai-responses",text:"先核對公開來源，再比較兩個結果。"};
  const ui=await open(data,{now:Date.parse(progressAt(4))}),panel=inlineProcessing(ui);
  assert.match(processingText(panel),/思考摘要：先核對公開來源，再比較兩個結果。/);
  assert.doesNotMatch(panel.children[2].textContent,/未提供/);
  assert.doesNotMatch(ui.text("reasoning-summary"),/未提供|先核對/);
  assert.match(ui.nodes.get("conversation-history").children[0].children[3].textContent,/正在準備回覆/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

test("a rejected progress read quietly keeps the original interval without reposting the work",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="OFFLINE instruction";t.thread_id="thread";t.submit_request_id="request";t.reply=null;
  data.composer={thread_id:"thread",request_id:"request",progress_available:true,can_follow_up:false,
    conversation:{turns:[{request_id:"request",state:"running"}],can_follow_up:false}};
  const ui=await open(data);
  ui.holder.progressResponse={ok:false,status:409,json:async()=>({error:{
    code:"RESULT_UNKNOWN",upstream_status:412,message:"OFFLINE read conflict",automatic_retry:false}})};
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(vm.runInContext("progressPaused",ui.context),false);
  assert.equal(vm.runInContext("lastError",ui.context),null);
  assert.equal(vm.runInContext("progressRetryMs",ui.context),1000);
  assert.equal(ui.nodes.get("error").hidden,true);
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(vm.runInContext("progressRetryMs",ui.context),1000);
  assert.ok(ui.timers.size>0);
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,0);
  ui.holder.progressResponse=null;
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(vm.runInContext("lastError",ui.context),null);
  vm.runInContext('lastError={code:"RESULT_UNKNOWN",phase:"submit"}',ui.context);
  ui.holder.progressResponse={ok:false,status:409,json:async()=>({error:{code:"RESULT_UNKNOWN",upstream_status:412}})};
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(vm.runInContext("lastError.phase",ui.context),"submit");
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,0);
});

test("temporary progress transport failure recovers automatically but authentication refusal pauses",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="OFFLINE instruction";t.thread_id="thread";t.submit_request_id="request";t.reply=null;
  data.composer={thread_id:"thread",request_id:"request",progress_available:true,can_follow_up:false,
    conversation:{turns:[{request_id:"request",state:"running"}],can_follow_up:false}};
  const ui=await open(data);
  for(const error of [
    {status:503,code:"BACKEND_OFFLINE"},
    {status:409,code:"BACKEND_UNAVAILABLE",upstream_status:503},
    {status:409,code:"BACKEND_LIMIT",upstream_status:429},
  ]){
    ui.holder.progressResponse={ok:false,status:error.status,json:async()=>({error})};
    await vm.runInContext("updateProgress()",ui.context);
    assert.equal(vm.runInContext("progressPaused",ui.context),false);
    assert.ok(ui.timers.size>0);
  }
  ui.holder.progressResponse={ok:false,status:409,json:async()=>({error:{code:"BACKEND_DENIED",upstream_status:403}})};
  await vm.runInContext("updateProgress()",ui.context);
  assert.equal(vm.runInContext("progressPaused",ui.context),true);
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,0);
});

test("reset needs explicit confirmation and never duplicates a pending reset",async()=>{
  const data=fixture();
  data.composer={thread_id:"old-thread",request_id:"old-request",can_reset:true,can_new_conversation:true,
    hosted_session_id:"fa-chat-original",content:"old",editable:false,can_submit:false,models};
  const ui=await open(data);
  assert.match(ui.text("hosted-session"),/fa-chat-original.*沿用/);
  ui.click("reset-session");
  assert.equal(await ui.answer(false),"封存目前對話並開新 session？舊紀錄與檔案會保留；執行中或未知的工作不會被丟棄。");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0,"取消 sends nothing");
  ui.click("reset-session");ui.click("reset-session");await ui.tick();
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await ui.tick();
  ui.click("reset-session");await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"a reset in flight is not asked again");
  const posts=ui.calls.filter(call=>call.method==="POST");
  assert.deepEqual(posts.map(call=>call.url),["/api/instructions/old-thread/reset"]);
  assert.deepEqual(JSON.parse(posts[0].body),{});
  const fresh=copy(data);fresh.composer={...fresh.composer,thread_id:"new-thread",request_id:"new-request",
    can_reset:false,hosted_session_id:null,content:null,editable:true};
  ui.holder.current=fresh;
  ui.holder.pending({composer:fresh.composer,reset:{session_stop:"confirmed"}});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("reset-session").disabled,true);
  assert.match(ui.text("hosted-session"),/送出第一則/);
});

test("a pending send in one conversation does not block or replace another conversations draft",async()=>{
  const first=fixture();
  first.composer={thread_id:"thread-A",request_id:"request-A",content:null,editable:true,can_submit:true,
    can_follow_up:false,can_new_conversation:true,input_capacity:{maximum:8192},models,
    threads:[{thread_id:"thread-A",label:"A"},{thread_id:"thread-B",label:"B"}]};
  const ui=await open(first);
  ui.nodes.get("instruction").value="A 的原文";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");
  const second=copy(first);second.composer.thread_id="thread-B";second.composer.request_id="request-B";
  ui.render(second);
  ui.nodes.get("instruction").value="B 的原文";ui.nodes.get("instruction").events.input();
  assert.equal(ui.nodes.get("send-instruction").disabled,false);
  ui.click("send-instruction");
  assert.deepEqual(ui.holder.pendingPosts.map(p=>p.url),
    ["/api/instructions/request-A/submit","/api/instructions/request-B/submit"]);
  ui.holder.current=second;
  ui.holder.pendingPosts[0].resolve({task:{task_id:"request-A",thread_id:"thread-A",submission:{state:"CONFIRMED"}}});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"B 的原文");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  ui.holder.current={...second,composer:{...second.composer,content:"B 的原文",editable:false,can_submit:false}};
  ui.holder.pendingPosts[1].resolve({task:{task_id:"request-B",thread_id:"thread-B",submission:{state:"CONFIRMED"}}});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,2);
});

test("free instruction presentation does not reinstate legacy budget or deadline panels",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="OFFLINE free instruction";
  t.execution={connection:"OBSERVED",blocking_code:null,message:"",observation:{
    budget:{unit:"USD-micro",remaining:50000000,required:0},observed_at:when,execution_deadline:null,
    standing:{already_admitted:true,capacity_available:false,queue_deadline:null,
      monetary_budget:{cumulative_ceiling_microusd:110000000,safety_reserve_microusd:10000000,
        observed_high_water_microusd:45000000,reserved_microusd:5000000}}}};
  const ui=await open(data);
  assert.equal(ui.nodes.has("work-budget"),false);
  assert.equal(ui.nodes.has("work-deadline"),false);
  assert.doesNotMatch(ui.text("work-message"),/預算|期限|倒數/);
});

test("locally saved text with denied preflight is not displayed as agent admission",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="OFFLINE not sent";t.result=null;t.submission={state:"NOT_SENT"};t.admission=null;
  const ui=await open(data);
  const reply=ui.nodes.get("conversation-history").children[0].children[3].textContent;
  assert.equal(reply,"這則訊息還沒送出。");
  assert.doesNotMatch(reply,/已接單；|接單|本機/);
  assert.equal(ui.text("submission-state"),"尚未交辦","the original state stays in the support details");
});

test("delivery reference never replaces the full pure-GET answer on screen",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="OFFLINE long answer";
  t.result.work_state="succeeded";t.result.message_kind="final";
  t.result.content=JSON.stringify({schema_version:"fa-assistant-text-reference-v1",
    reply_id:"opaque-reference",sha256:"a".repeat(64),byte_length:30000});
  t.reply={data:{text:"完整回答".repeat(2500)},sha256:"a".repeat(64),byte_length:30000,observed_at:when};
  const ui=await open(data);
  assert.equal(ui.text("answer"),t.reply.data.text);
  assert.equal(shownText(ui.nodes.get("conversation-history").children[0].children[3]),t.reply.data.text);
  assert.match(ui.text("payload-download"),/完整回覆/);
  assert.equal(ui.text("ack-state"),"未確認");
  assert.match(ui.text("raw-content"),/fa-assistant-text-reference-v1/);
});

test("sidebar lists actual agents, not conversations, with secondary diagnostics closed",async()=>{
  const data=fixture();
  data.composer={thread_id:"thread-A",request_id:"draft",content:null,editable:true,can_submit:true,
    models,input_capacity:{maximum:8192},threads:[{thread_id:"thread-A",label:"A"},{thread_id:"thread-B",label:"B"}]};
  const ui=await open(data);
  assert.equal(ui.nodes.get("agent-list").children.length,1);
  assert.equal(agentButton(ui,0).children[1].children[0].textContent,"FA 助理");
  assert.equal(ui.nodes.get("conversation").children.length,2);
  assert.match(html,/<aside class="diagnostics" id="diagnostics"[^>]* hidden>/);
  assert.doesNotMatch(html,/0[1-4] \/ (交辦|處理狀態|結果|是否收到)/);
  agentButton(ui,0).events.click();
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,0);
  ui.click("open-diagnostics");assert.equal(ui.nodes.get("diagnostics").hidden,false);
  ui.click("close-diagnostics");assert.equal(ui.nodes.get("diagnostics").hidden,true);
});

test("typing a follow-up sends next then the new immutable request exactly once",async()=>{
  const data=fixture(),first=data.tasks[0];
  Object.assign(first,{task_id:"first",submit_request_id:"first",thread_id:"thread",
    instruction:"First question",reply:{data:{text:"First answer"}},result:null});
  data.composer={thread_id:"thread",request_id:"first",content:"First question",editable:false,can_submit:false,
    can_follow_up:true,model:"offline-chat-a",models,input_capacity:{maximum:8192},
    conversation:{can_follow_up:true,turns:[{request_id:"first",state:"succeeded"}]}};
  const ui=await open(data);
  assert.equal(ui.nodes.get("instruction").disabled,false);
  assert.equal(ui.nodes.get("instruction").value,"");
  ui.nodes.get("instruction").value="  Follow up\nexactly.  ";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");ui.click("send-instruction");
  assert.deepEqual(ui.holder.pendingPosts.map(p=>p.url),["/api/instructions/thread/next"]);
  const next=copy(data);next.composer={...next.composer,request_id:"second",content:null,editable:true,can_submit:true,can_follow_up:false};
  ui.holder.current=next;ui.holder.pendingPosts[0].resolve(next);await ui.tick();await ui.tick();
  assert.deepEqual(ui.holder.pendingPosts.map(p=>p.url),
    ["/api/instructions/thread/next","/api/instructions/second/submit"]);
  assert.equal(JSON.parse(ui.calls.find(c=>c.url==="/api/instructions/second/submit").body).content,"  Follow up\nexactly.  ");
  const second={...copy(first),task_id:"second",submit_request_id:"second",instruction:"  Follow up\nexactly.  ",
    reply:null,submission:{state:"CONFIRMED"}};
  next.tasks.push(second);next.composer={...next.composer,content:second.instruction,editable:false,can_submit:false};
  ui.holder.current=next;ui.holder.pendingPosts[1].resolve({task:second});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,2);
  assert.equal(ui.nodes.get("conversation-history").children.length,2);
  assert.ok(ui.calls.every(c=>!c.url.includes("acknowledge")));
});

test("failed follow-up preparation does not submit and keeps typed text",async()=>{
  const data=fixture();
  data.composer={thread_id:"thread",request_id:"first",content:"Original",editable:false,can_submit:false,
    can_follow_up:true,models,input_capacity:{maximum:8192}};
  const ui=await open(data);
  ui.holder.response={ok:false,status:409,json:async()=>({error:{code:"CONVERSATION_TURN_UNRESOLVED"}})};
  ui.nodes.get("instruction").value="Keep this draft";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");await ui.tick();await ui.tick();
  assert.deepEqual(ui.calls.filter(c=>c.method==="POST").map(c=>c.url),["/api/instructions/thread/next"]);
  assert.equal(ui.nodes.get("instruction").value,"Keep this draft");
});

test("Enter sends, Shift+Enter and IME confirmation never submit",async()=>{
  const data=fixture();
  data.composer={thread_id:"thread",request_id:"draft",content:null,editable:true,can_submit:true,
    can_follow_up:false,models,input_capacity:{maximum:8192}};
  const ui=await open(data),input=ui.nodes.get("instruction");
  input.value="你好";input.events.input();
  const key=(extra={})=>({key:"Enter",preventDefault(){this.prevented=true},...extra});
  input.events.keydown(key({shiftKey:true}));
  input.events.keydown(key({isComposing:true}));
  input.events.compositionstart();input.events.keydown(key());input.events.compositionend();
  input.events.keydown(key({keyCode:229}));
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,0);
  const enter=key();input.events.keydown(enter);input.events.keydown(key());
  assert.equal(enter.prevented,true);
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,1);
});

test("unconfirmed HTTP success keeps the draft and never retries",async()=>{
  const data=fixture();
  data.composer={thread_id:"thread",request_id:"draft",content:null,editable:true,can_submit:true,
    can_follow_up:false,models,input_capacity:{maximum:8192}};
  const ui=await open(data);ui.nodes.get("instruction").value="Keep until confirmed";ui.nodes.get("instruction").events.input();
  ui.click("send-instruction");ui.holder.pending({task:{task_id:"draft",submission:{state:"UNKNOWN"}}});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("instruction").value,"Keep until confirmed");
  assert.equal(ui.calls.filter(c=>c.method==="POST").length,1);
});

test("unchanged polling does not rebuild messages or force a reader to the bottom",async()=>{
  const data=fixture(),t=data.tasks[0];
  t.instruction="Read this";t.result=null;t.reply={data:{text:"A long retained answer"}};
  const ui=await open(data),history=ui.nodes.get("conversation-history"),scroll=ui.nodes.get("chat-scroll");
  const turn=history.children[0];
  scroll.scrollHeight=1000;scroll.clientHeight=300;scroll.scrollTop=100;
  ui.render(copy(data));
  assert.equal(history.children[0],turn);
  assert.equal(scroll.scrollTop,100);
  t.reply.data.text+="\nUpdated content";
  ui.render(data);
  assert.equal(scroll.scrollTop,100);
});

test("live text stays provisional and cannot become a saved result or receipt",async()=>{
  const data=fixture(),task=data.tasks[0];
  task.instruction="OFFLINE streaming question";task.result=null;task.reply=null;
  data.composer={thread_id:"stream-thread",request_id:task.task_id,content:task.instruction,
    editable:false,can_follow_up:false,conversation:{turns:[{request_id:task.submit_request_id,
      state:"running",stream:{text:"First words",sequence:1,durable:false,complete:false}}]}};
  task.thread_id="stream-thread";
  const ui=await open(data);
  const reply=()=>ui.nodes.get("conversation-history").children[0].children[3],message=()=>shownText(reply());
  assert.match(message(),/First words.*尚未確認保存/);
  assert.equal(reply().children.at(-1).textContent,"回覆中，尚未確認保存。");
  assert.equal(reply().children.some(node=>node.className==="message-meta"),false,"unsaved text gets no time or copy");
  assert.equal(ui.nodes.get("send-instruction").disabled,true);
  data.composer.conversation.turns[0].stream.text="First words continued";
  data.composer.conversation.turns[0].stream.sequence=2;
  ui.render(data);assert.match(message(),/continued/);
  data.composer.conversation.turns[0].state="failed";
  ui.render(data);assert.doesNotMatch(message(),/First words/);
  assert.match(message(),/沒有完成/);
});

test("retained answers survive another turn's preview eviction and page reconstruction",async()=>{
  const data=fixture(),first=data.tasks[0];
  const original="Retained answer: <not-html>\nExact original line.";
  Object.assign(first,{task_id:"saved-turn",submit_request_id:"saved-turn",thread_id:"retained-thread",
    instruction:"Original question",result:null,reply:{data:{text:original}}});
  const current={...copy(first),task_id:"pending-turn",submit_request_id:"pending-turn",
    instruction:"Current question",reply:null,acknowledgment:{state:"NOT_SENT"}};
  data.tasks.push(current);
  data.composer={thread_id:"retained-thread",request_id:"pending-turn",content:current.instruction,
    editable:false,can_follow_up:false,conversation:{turns:[
      {request_id:"saved-turn",state:"succeeded",reply_available:true},
      {request_id:"pending-turn",state:"running",reply_available:false,
        stream:{text:"Temporary preview",sequence:1,durable:false,complete:false}}
    ]}};
  const ui=await open(data);
  const answer=(panel,index)=>shownText(panel.nodes.get("conversation-history").children[index].children[3]);
  assert.equal(answer(ui,0),original);
  assert.match(answer(ui,1),/Temporary preview/);
  delete data.composer.conversation.turns[1].stream;
  ui.render(data);
  assert.equal(answer(ui,0),original);
  assert.doesNotMatch(answer(ui,1),/Temporary preview/);
  assert.equal(ui.nodes.get("send-instruction").disabled,true);

  const reopened=await open(data);
  assert.equal(answer(reopened,0),original);
  assert.doesNotMatch(answer(reopened,1),/Temporary preview/);
  assert.equal(reopened.nodes.get("send-instruction").disabled,true);
  current.reply={data:{text:"The later durable answer."}};
  data.composer.conversation.turns[1]={request_id:"pending-turn",state:"succeeded",reply_available:true};
  data.composer.can_follow_up=true;
  reopened.render(data);
  assert.equal(answer(reopened,0),original);
  assert.equal(answer(reopened,1),"The later durable answer.");
  assert.equal(current.acknowledgment.state,"NOT_SENT");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(reopened.calls.filter(call=>call.method==="POST").length,0);
});

test("template options add approved skills and capabilities, then use the same inspect and confirmed select",async()=>{
  const ui=await open(templateData()),base=selectableTemplate();
  base.role.profile="fa-instruction-history-skill-draft-v1";
  const guide={name:"receipt-guide",version_id:"1".repeat(32),sha256:"2".repeat(64),metadata_sha256:"3".repeat(64)};
  const skills={entry_id:"fa-skills-v1",entry_sha256:"4".repeat(64),kind:"fa-builtin",tools:["read_skill"],vision:false};
  ui.holder.templateResponses={
    "/api/agent-template":{ok:true,json:async()=>base},
    "/api/agent-template/options":{ok:true,json:async()=>({schema_version:"fa-agent-template-options-v1",
      skills:[guide],more_skills:false,capabilities:[skills],
      composable_profiles:["fa-instruction-history-skill-draft-v1","fa-instruction-history-v1"],
      authorization:false,activated:false})},
    "/api/agent-template/inspect":{ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"5".repeat(64),
      role_matches:true,settings_match:false,references_match:false})},
    "/api/agent-template/selection":selectionRead(),
  };
  assert.equal(ui.nodes.get("build-template").disabled,true);
  ui.click("load-template-options");await ui.tick();await ui.tick();
  const inputs=vm.runInContext("templateOptionInputs",ui.context);
  assert.equal(JSON.stringify(inputs.map(input=>[input.faKind,input.checked])),JSON.stringify([["skill",false],["capability",false]]));
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  for(const index of [0,1]){
    const current=vm.runInContext("templateOptionInputs",ui.context)[index];current.checked=true;current.events.change();
  }
  assert.equal(ui.nodes.get("build-template").disabled,false);
  ui.click("build-template");await ui.tick();await ui.tick();
  const inspected=ui.calls.filter(call=>call.url==="/api/agent-template/inspect");
  assert.equal(inspected.length,1);
  const built=JSON.parse(inspected[0].body);
  assert.deepEqual(built.skills,[guide]);
  assert.deepEqual(built.settings.capabilities,[{entry_id:"fa-skills-v1",entry_sha256:"4".repeat(64)}]);
  assert.deepEqual(built.unavailable_skills,[]);
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/select"),false);
  ui.click("read-template-selection");await ui.tick();
  ui.click("select-template");await ui.answer(true);
  const posts=ui.calls.filter(call=>call.url==="/api/agent-template/select");
  assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].body).template,built);
});

for(const enabled of [true,false]){
  test(`Responses template choice follows server eligibility ${enabled}`,async()=>{
    const ui=await open(templateData()),base=selectableTemplate();
    ui.holder.templateResponses={
      "/api/agent-template":{ok:true,json:async()=>base},
      "/api/agent-template/options":{ok:true,json:async()=>({
        schema_version:"fa-agent-template-options-v1",skills:[],capabilities:[],more_skills:false,
        composable_profiles:[],responses:{enabled,history_source:"conversation"},authorization:false,activated:false})},
      "/api/agent-template/inspect":{ok:true,json:async()=>({...inspectedTemplate(),skills:[]})},
    };
    await ui.nodes.get("load-template-options").events.click();
    const inputs=vm.runInContext("templateOptionInputs",ui.context);
    const choice=inputs.find(input=>input.faKind==="state-profile");
    assert.equal(Boolean(choice),enabled);
    if(enabled){
      choice.checked=true;choice.events.change();
      await ui.nodes.get("build-template").events.click();
      const request=ui.calls.find(call=>call.url==="/api/agent-template/inspect");
      assert.equal(JSON.parse(request.body).settings.state_profile,"foundry-responses-v1");
    }
    assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/select"),false);
  });
}

test("imported Responses template cannot be selected without server eligibility",async()=>{
  const ui=await open(templateData()),candidate=selectableTemplate();
  candidate.settings.state_profile="foundry-responses-v1";
  await readyTemplateSelection(ui,candidate);
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.match(ui.text("template-selection-reason"),/Responses.*資格/);
});

test("changing a template option after building requires a new comparison before selection",async()=>{
  const ui=await open(templateData()),base=selectableTemplate();
  base.role.profile="fa-instruction-history-skill-draft-v1";
  const skills={entry_id:"fa-skills-v1",entry_sha256:"4".repeat(64),kind:"fa-builtin",tools:["read_skill"],vision:false};
  ui.holder.templateResponses={
    "/api/agent-template":{ok:true,json:async()=>base},
    "/api/agent-template/options":{ok:true,json:async()=>({schema_version:"fa-agent-template-options-v1",
      skills:[],more_skills:false,capabilities:[skills],composable_profiles:["fa-instruction-history-skill-draft-v1"],
      authorization:false,activated:false})},
    "/api/agent-template/inspect":{ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"5".repeat(64),
      settings_match:false,references_match:false,skills:[]})},
    "/api/agent-template/selection":selectionRead(),
  };
  ui.click("load-template-options");await ui.tick();await ui.tick();
  const first=vm.runInContext("templateOptionInputs",ui.context)[0];first.checked=true;first.events.change();
  ui.click("build-template");await ui.tick();await ui.tick();
  ui.click("read-template-selection");await ui.tick();
  assert.equal(ui.nodes.get("select-template").disabled,false);
  const again=vm.runInContext("templateOptionInputs",ui.context)[0];again.checked=false;again.events.change();
  assert.equal(ui.nodes.get("select-template").disabled,true);
  assert.equal(ui.text("template-target"),"");
});

const ONBOARDING_CAP=(entry_id,kind,tools)=>({entry_id,entry_sha256:entry_id[0].repeat(64),kind,tools,vision:false});
const ONBOARDING_SKILL=name=>({name,version_id:"1".repeat(32),sha256:"a".repeat(64),metadata_sha256:"b".repeat(64)});
const ONBOARDING_OPTIONS={
  options:{schema_version:"fa-agent-template-options-v1",authorization:false,activated:false,more_skills:false,
    composable_profiles:[],skills:[ONBOARDING_SKILL("receipt-guide"),ONBOARDING_SKILL("tone")],
    capabilities:[ONBOARDING_CAP("lookup","mcp-api",["search"]),ONBOARDING_CAP("fa-skills-v1","fa-builtin",["read_skill"]),
      ONBOARDING_CAP("fa-files-v1","fa-builtin",["read_file"])]},
  current:{capabilities:[{entry_id:"fa-skills-v1",entry_sha256:"f".repeat(64)},{entry_id:"lookup",entry_sha256:"l".repeat(64)}],
    skills:[ONBOARDING_SKILL("receipt-guide")]},
  unsupported:[],
};
function onboardingServer(replies){
  const seen=[];
  return {seen,handle:(url,options={})=>{
    seen.push({url,method:options.method||"GET",body:options.body?JSON.parse(options.body):undefined});
    if(!options.method&&url==="/api/agents/onboarding/options")
      return {ok:replies.options!==null,json:async()=>copy(replies.options===null?{error:{code:"BACKEND_OFFLINE"}}:replies.options||ONBOARDING_OPTIONS)};
    if(!options.method)return {ok:true,json:async()=>({onboardings:replies.list||[]})};
    const next=replies.posts.shift();
    return {ok:next.ok!==false,json:async()=>copy(next.value)};
  }};
}

test("a completed native reply automatically displays its verified screenshot thumbnail",async()=>{
  const value=nativeScreenshotCase(),ui=await open(value.data,value);
  await settle(ui);
  const deadline=Date.now()+1000;
  while(!gallery(ui)?.children.some(node=>node.children?.[0]?.src)&&Date.now()<deadline)
    await new Promise(resolve=>setTimeout(resolve,1));
  const shown=gallery(ui);
  assert.ok(shown,"The delivered image must not require 查看截圖");
  assert.ok(shown.children.some(node=>node.children?.[0]?.src==="blob:offline-template"&&node.target==="_blank"));
  ui.render(value.data);await settle(ui);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/images")).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});
// The Team-approved tools and skills are read when 「＋ 新增助理」 is opened, not on page load.
async function openOnboarding(ui){
  const panel=ui.nodes.get("onboarding-panel");panel.open=true;panel.events.toggle();await ui.tick();await ui.tick();
}
function fillOnboarding(ui,name,role){
  ui.nodes.get("onboarding-name").value=name;ui.nodes.get("onboarding-role").value=role;
  ui.nodes.get("onboarding-role").events.input();
}
const onboardingView=(extra)=>({request_id:"onboard-"+"a".repeat(24),display_name:"研究員",restart_required:false,
  max_team_agents:16,...extra});

test("onboarding panel counts role bytes and shows the server quota limit without linking anything",async()=>{
  const server=onboardingServer({posts:[{value:onboardingView({state:"REFUSED",code:"AGENT_QUOTA_REACHED",limit:16})}]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await openOnboarding(ui);
  assert.equal(ui.nodes.get("onboarding-panel").hidden,false);
  fillOnboarding(ui,"研究員","é".repeat(1025));
  assert.equal(ui.text("onboarding-remaining"),"角色說明剩餘 -2 bytes");
  assert.equal(ui.nodes.get("onboarding-submit").disabled,true);
  fillOnboarding(ui,"研究員","找資料");
  assert.equal(ui.nodes.get("onboarding-submit").disabled,false);
  ui.click("onboarding-submit");await ui.tick();await ui.tick();
  const posts=server.seen.filter(call=>call.method==="POST");
  assert.deepEqual(posts.map(call=>[call.url,Object.keys(call.body).sort()]),
    [["/api/agents/onboarding",["capabilities","display_name","role_prompt","skills"]]]);
  assert.match(ui.text("onboarding-status"),/上限 16 位/);
  assert.equal(ui.nodes.get("onboarding-continue").hidden,true);
});

test("onboarding selection stale shows the inner cause's own recovery and keeps the original request ID",async()=>{
  const rid="onboard-"+"a".repeat(24);
  for(const [cause,wording] of [["CAPABILITY_SOURCES_CHANGED",/讀不到其中一個勾選的工具（組織工具目錄或 Team MCP）/],
    ["PROFILE_SELECTION_SOURCES_CHANGED",/skill 已不是 Team 目前批准的版本/],
    ["SKILL_CATALOG_BUDGET_TOO_SMALL",/模型容量放不下/],["PROFILE_SELECTION_DENIED",/（PROFILE_SELECTION_DENIED）.*部署線/]]){
    const server=onboardingServer({posts:[{value:onboardingView({state:"UNKNOWN",code:"AGENT_ONBOARDING_SELECTION_STALE",cause})}]});
    const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
    await openOnboarding(ui);
    fillOnboarding(ui,"研究員","找資料");
    ui.click("onboarding-submit");await ui.tick();await ui.tick();
    const status=ui.text("onboarding-status");
    assert.match(status,wording);
    assert.match(status,new RegExp(`已建立並登錄.*用原編號 ${rid} 續做，不要另建`));
    assert.doesNotMatch(status,/恢復原目錄/);
  }
});

test("a missing chosen tool names its own owner: Team MCP to the Team owner, the org catalog to the deployment",async()=>{
  const owner=/請 Team 主人恢復/,deployment=/請部署管理者確認組織目錄/;
  for(const [capability_ids,team,org] of [[["team-crm"],true,false],[["fa-skills-v1"],false,true],
    [["fa-skills-v1","team-crm"],true,true]]){
    const server=onboardingServer({posts:[{value:onboardingView({state:"UNKNOWN",
      code:"AGENT_ONBOARDING_SELECTION_STALE",cause:"CAPABILITY_SOURCES_CHANGED",capability_ids})}]});
    const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
    await openOnboarding(ui);
    fillOnboarding(ui,"研究員","找資料");
    ui.click("onboarding-submit");await ui.tick();await ui.tick();
    const status=ui.text("onboarding-status");
    (team?assert.match:assert.doesNotMatch)(status,owner);
    (org?assert.match:assert.doesNotMatch)(status,deployment);
    assert.match(status,/如果工具都還在，請部署線確認服務端版本/);
  }
});

test("onboarding setup still pending continues the same request every 30 seconds, not every 2",async()=>{
  const rid="onboard-"+"a".repeat(24);
  const server=onboardingServer({posts:[
    {value:onboardingView({state:"SERVER",server_state:"creating",reason:"AGENT_SETUP_PENDING"})},
    {value:onboardingView({state:"SERVER",server_state:"creating"})},
  ]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await openOnboarding(ui);
  fillOnboarding(ui,"研究員","找資料");
  ui.click("onboarding-submit");await ui.tick();await ui.tick();
  assert.match(ui.text("onboarding-status"),/已建立，角色補設中：正在補設對話入口與權限.*這是正常的.*每 30 秒自動用同一個編號續做，不會另建/);
  const id=vm.runInContext("onboarding.timer",ui.context);
  assert.equal(ui.timerDelays.get(id),30000);
  const step=ui.timers.get(id);
  ui.timers.delete(id);step();await ui.tick();await ui.tick();
  assert.deepEqual(server.seen.filter(call=>call.method==="POST").map(call=>call.url),
    ["/api/agents/onboarding","/api/agents/onboarding/"+rid+"/continue"]);
  assert.match(ui.text("onboarding-status"),/建立中（creating）/);
});

test("onboarding progress continues only the original request ID and ends with the restart hint",async()=>{
  const rid="onboard-"+"a".repeat(24),sha="f".repeat(64);
  const server=onboardingServer({posts:[
    {value:onboardingView({state:"SERVER",server_state:"creating"})},
    {value:onboardingView({state:"CONFIRMED",server_state:"ready",agent_id:"agent-new",manifest_sha256:sha,restart_required:true})},
  ]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await openOnboarding(ui);
  fillOnboarding(ui,"研究員","找資料");
  ui.click("onboarding-submit");await ui.tick();await ui.tick();
  assert.match(ui.text("onboarding-status"),/建立中（creating）/);
  assert.equal(ui.nodes.get("onboarding-submit").disabled,true);
  const [id,step]=[...ui.timers.entries()].find(([key])=>ui.timerDelays.get(key)===2000);
  ui.timers.delete(id);step();await ui.tick();await ui.tick();
  const posts=server.seen.filter(call=>call.method==="POST").map(call=>call.url);
  assert.deepEqual(posts,["/api/agents/onboarding","/api/agents/onboarding/"+rid+"/continue"]);
  assert.match(ui.text("onboarding-status"),/還沒準備對話環境/);assert.doesNotMatch(ui.text("onboarding-status"),/重啟 8765/);
  assert.equal(ui.nodes.get("onboarding-prepare").hidden,false);
  assert.equal(ui.nodes.get("onboarding-prepare").textContent,"準備對話環境");
  assert.equal(vm.runInContext("onboarding.timer",ui.context),null);
  // No restart: right after linking, the page reads the Agent directory again so the new Agent can be listed.
  assert.ok(ui.calls.some(call=>call.url==="/api/agents"));
});

test("a linked new Agent retries its native prepare only with the same ID and says when it can chat",async()=>{
  const rid="onboard-"+"a".repeat(24),sha="f".repeat(64);
  const linked=extra=>onboardingView({state:"CONFIRMED",server_state:"ready",agent_id:"agent-new",manifest_sha256:sha,
    restart_required:true,ready_to_chat:false,...extra});
  const server=onboardingServer({list:[linked({prepare:{request_id:"prepare-1",state:"FAILED",code:"NATIVE_BOOTSTRAP_SOURCE_ACTIVE"}})],
    posts:[{value:linked({prepare:{request_id:"prepare-1",state:"UNKNOWN",code:"BACKEND_OFFLINE"}})},
           {value:linked({prepare:{request_id:"prepare-1",state:"READY",hosted_agent_version:"7"},ready_to_chat:true})}]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("onboarding-prepare").hidden,false);
  assert.equal(ui.nodes.get("onboarding-prepare").textContent,"重試準備");
  assert.match(ui.text("onboarding-status"),/舊工作階段還在使用中.*同一個準備編號/);
  ui.click("onboarding-prepare");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("onboarding-prepare").textContent,"再查一次");
  assert.match(ui.text("onboarding-status"),/尚未確認.*只查詢、不重送/);
  ui.click("onboarding-prepare");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("onboarding-prepare").hidden,true);
  assert.match(ui.text("onboarding-status"),/可以在左側選用並開始對話/);
  const posts=server.seen.filter(call=>call.method==="POST").map(call=>[call.url,call.body]);
  assert.deepEqual(JSON.parse(JSON.stringify(posts)),[["/api/agents/onboarding/"+rid+"/prepare",{}],
    ["/api/agents/onboarding/"+rid+"/prepare",{}]]);
});

test("a newer onboarding never hides an earlier Agent that still needs its native prepare",async()=>{
  const older="onboard-"+"b".repeat(24),sha="e".repeat(64);
  const server=onboardingServer({list:[
    onboardingView({request_id:"onboard-"+"c".repeat(24),display_name:"第二位",state:"REFUSED",code:"AGENT_QUOTA_REACHED",limit:16}),
    onboardingView({request_id:older,display_name:"第一位",state:"CONFIRMED",server_state:"ready",agent_id:"agent-new",
      manifest_sha256:sha,restart_required:true,ready_to_chat:false,
      prepare:{request_id:"prepare-1",state:"UNKNOWN",code:"AGENT_VERSION_FOLLOWING"}}),
  ],posts:[]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("onboarding-prepare").hidden,false);
  assert.equal(ui.nodes.get("onboarding-prepare").textContent,"再查一次");
  assert.match(ui.text("onboarding-status"),/「第一位」建立完成，對話環境的準備結果尚未確認/);
  assert.equal(vm.runInContext("onboarding.current.request_id",ui.context),older);
});

test("an earlier unprepared Agent is shown with the newest manifest, which also lists every later Agent",async()=>{
  const newest="d".repeat(64),older="c".repeat(64);
  const server=onboardingServer({list:[
    onboardingView({request_id:"onboard-"+"d".repeat(24),display_name:"第二位",state:"CONFIRMED",server_state:"ready",
      agent_id:"agent-d",manifest_sha256:newest,restart_required:true,ready_to_chat:true,
      prepare:{request_id:"prepare-d",state:"READY",hosted_agent_version:"7"}}),
    onboardingView({request_id:"onboard-"+"c".repeat(24),display_name:"第一位",state:"CONFIRMED",server_state:"ready",
      agent_id:"agent-c",manifest_sha256:older,restart_required:true,ready_to_chat:false,
      prepare:{request_id:"prepare-c",state:"FAILED",code:"NATIVE_TURNS_DISABLED"}}),
  ],posts:[{value:onboardingView({request_id:"onboard-"+"c".repeat(24),display_name:"第一位",state:"CONFIRMED",
      server_state:"ready",agent_id:"agent-c",manifest_sha256:older,restart_required:true,ready_to_chat:true,
      prepare:{request_id:"prepare-c",state:"READY",hosted_agent_version:"7"}})}]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await ui.tick();await ui.tick();
  assert.match(ui.text("onboarding-status"),/「第一位」建立完成，但對話環境準備失敗/);
  assert.doesNotMatch(ui.text("onboarding-status"),new RegExp(older));
  ui.click("onboarding-prepare");await ui.tick();await ui.tick();
  assert.match(ui.text("onboarding-status"),/可以在左側選用並開始對話/);assert.doesNotMatch(ui.text("onboarding-status"),/重啟 8765/);
});

test("a not-ready native prepare is retried every 30 seconds while the server allows it, never after a 403",async()=>{
  const rid="onboard-"+"a".repeat(24),sha="f".repeat(64);
  const linked=prepare=>onboardingView({state:"CONFIRMED",server_state:"ready",agent_id:"agent-new",manifest_sha256:sha,
    restart_required:true,ready_to_chat:prepare.state==="READY",prepare:{request_id:"prepare-1",...prepare}});
  const server=onboardingServer({list:[linked({state:"FAILED",code:"AGENT_VERSION_FOLLOWING",auto_retry:true})],
    posts:[{value:linked({state:"FAILED",code:"NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",upstream_status:403,auto_retry:false})}]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await ui.tick();await ui.tick();
  assert.match(ui.text("onboarding-status"),/每 30 秒自動再試，最多 5 分鐘/);
  const timer=[...ui.timers.entries()].find(([key])=>ui.timerDelays.get(key)===30000);
  assert.ok(timer,"a 30 second prepare retry is scheduled");
  ui.timers.delete(timer[0]);timer[1]();await ui.tick();await ui.tick();
  const posts=server.seen.filter(call=>call.method==="POST").map(call=>call.url);
  assert.deepEqual(posts,["/api/agents/onboarding/"+rid+"/prepare"]);
  assert.match(ui.text("onboarding-status"),/需要部署線替這位助理補角色/);
  assert.equal([...ui.timers.keys()].some(key=>ui.timerDelays.get(key)===30000),false);
  assert.equal(ui.nodes.get("onboarding-prepare").hidden,false);
});

test("stage: a 403 while new roles take effect is retried with the same ID every 10 minutes",async()=>{
  const rid="onboard-"+"a".repeat(24),sha="f".repeat(64);
  const linked=prepare=>onboardingView({state:"CONFIRMED",server_state:"ready",agent_id:"agent-new",manifest_sha256:sha,
    restart_required:false,ready_to_chat:prepare.state==="READY",prepare:{request_id:"prepare-1",...prepare}});
  const waiting={state:"FAILED",code:"NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",upstream_status:403,permission_wait:true,
    auto_retry:true,retry_seconds:600};
  const server=onboardingServer({list:[linked(waiting)],
    posts:[{value:linked({...waiting,auto_retry:false})}]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await ui.tick();await ui.tick();
  assert.match(ui.text("onboarding-status"),/角色已自動指派，正在等權限生效.*每 10 分鐘.*最多 2 小時/);
  const timer=[...ui.timers.entries()].find(([key])=>ui.timerDelays.get(key)===600000);
  assert.ok(timer,"a 10 minute prepare retry is scheduled");
  ui.timers.delete(timer[0]);timer[1]();await ui.tick();await ui.tick();
  assert.deepEqual(server.seen.filter(call=>call.method==="POST").map(call=>call.url),
    ["/api/agents/onboarding/"+rid+"/prepare"]);
  assert.match(ui.text("onboarding-status"),/等了 2 小時權限仍未生效/);
  assert.equal([...ui.timers.keys()].some(key=>ui.timerDelays.get(key)===600000),false);
  assert.equal(ui.nodes.get("onboarding-prepare").hidden,false);
});

test("onboarding tools and skills come from the Team catalog with the default Team choice preselected",async()=>{
  const server=onboardingServer({posts:[{value:onboardingView({state:"SERVER",server_state:"admitted"})}]});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await openOnboarding(ui);
  await ui.tick();await ui.tick();
  const inputs=vm.runInContext("onboarding.inputs",ui.context);
  const shown=JSON.parse(JSON.stringify(inputs.map(input=>[input.faKind,input.faRef.entry_id||input.faRef.name,input.checked])));
  assert.deepEqual(shown,[["capability","lookup",true],["capability","fa-skills-v1",true],
    ["capability","fa-files-v1",false],["skill","receipt-guide",true],["skill","tone",false]]);
  const groups=ui.nodes.get("onboarding-capabilities").children.filter(node=>node.className==="muted")
    .map(node=>node.textContent).slice();
  assert.deepEqual(groups,["MCP","內建工具"]);
  fillOnboarding(ui,"研究員","找資料");
  assert.equal(ui.text("onboarding-choice-note"),"");
  // Dropping fa-skills-v1 while a skill stays checked is caught before sending.
  inputs[1].checked=false;inputs[1].events.change();
  assert.match(ui.text("onboarding-choice-note"),/fa-skills-v1/);
  assert.equal(ui.nodes.get("onboarding-submit").disabled,true);
  inputs[1].checked=true;inputs[1].events.change();inputs[4].checked=true;inputs[4].events.change();
  assert.equal(ui.nodes.get("onboarding-submit").disabled,false);
  ui.click("onboarding-submit");await ui.tick();await ui.tick();
  const post=server.seen.find(call=>call.method==="POST");
  assert.deepEqual(post.body,{display_name:"研究員",role_prompt:"找資料",
    capabilities:[{entry_id:"fa-skills-v1",entry_sha256:"f".repeat(64)},{entry_id:"lookup",entry_sha256:"l".repeat(64)}],
    skills:[ONBOARDING_SKILL("receipt-guide"),ONBOARDING_SKILL("tone")]});
  assert.equal(inputs.every(input=>input.disabled),true);
});

test("onboarding never preselects or sends a tool the agent-role profile cannot compose",async()=>{
  const skillify=ONBOARDING_CAP("fa-skillify-v1","fa-builtin",["save_skill_draft"]);
  const options={options:{...ONBOARDING_OPTIONS.options,capabilities:[...ONBOARDING_OPTIONS.options.capabilities,skillify],skills:[]},
    current:{capabilities:[{entry_id:"lookup",entry_sha256:"l".repeat(64)}],skills:[]},unsupported:["fa-skillify-v1"]};
  const server=onboardingServer({posts:[{value:onboardingView({state:"SERVER",server_state:"admitted"})}],options});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await openOnboarding(ui);
  await ui.tick();await ui.tick();
  const inputs=vm.runInContext("onboarding.inputs",ui.context);
  const blocked=inputs.find(input=>input.faRef.entry_id==="fa-skillify-v1");
  assert.equal(blocked.checked,false);assert.equal(blocked.disabled,true);
  const texts=node=>[node.textContent||"",...(node.children||[]).flatMap(texts)];
  assert.ok(texts(ui.nodes.get("onboarding-capabilities")).some(value=>/fa-skillify-v1.*agent-role 範本不支援/.test(value)));
  fillOnboarding(ui,"研究員","找資料");
  assert.equal(ui.nodes.get("onboarding-submit").disabled,false);
  ui.click("onboarding-submit");await ui.tick();await ui.tick();
  const post=server.seen.find(call=>call.method==="POST");
  assert.deepEqual(post.body.capabilities,[{entry_id:"lookup",entry_sha256:"l".repeat(64)}]);
  const missing=onboardingServer({posts:[],options:{options:ONBOARDING_OPTIONS.options,current:ONBOARDING_OPTIONS.current}});
  const other=await open(agentChat("agent-one"),{onboarding:missing.handle});
  await openOnboarding(other);
  await other.tick();await other.tick();
  fillOnboarding(other,"研究員","找資料");
  assert.equal(other.nodes.get("onboarding-submit").disabled,true);
  assert.equal(missing.seen.some(call=>call.method==="POST"),false);
});

test("onboarding blocks more tools or skills than the server accepts and waits for the catalog",async()=>{
  const many=Array.from({length:13},(_,i)=>ONBOARDING_CAP("tool-"+String(i).padStart(2,"0"),"mcp-api",["t"]));
  const skills=Array.from({length:5},(_,i)=>ONBOARDING_SKILL("skill-"+i));
  const options={options:{...ONBOARDING_OPTIONS.options,capabilities:many,skills},current:{capabilities:[],skills:[]},
    unsupported:[]};
  const server=onboardingServer({posts:[],options});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await openOnboarding(ui);
  await ui.tick();await ui.tick();
  fillOnboarding(ui,"研究員","找資料");
  const inputs=vm.runInContext("onboarding.inputs",ui.context);
  for(const input of inputs.filter(input=>input.faKind==="capability")){input.checked=true;input.events.change()}
  assert.match(ui.text("onboarding-choice-note"),/工具最多 12 項/);
  assert.equal(ui.nodes.get("onboarding-submit").disabled,true);
  const offline=onboardingServer({posts:[],options:null});
  const other=await open(agentChat("agent-one"),{onboarding:offline.handle});
  await openOnboarding(other);
  await other.tick();await other.tick();
  fillOnboarding(other,"研究員","找資料");
  assert.equal(other.nodes.get("onboarding-submit").disabled,true);
  assert.notEqual(other.text("onboarding-choice-note"),"");
  assert.equal(offline.seen.some(call=>call.method==="POST"),false);
});


test("page load reads the onboarding record but the Team catalog only when 「＋ 新增助理」 is opened",async()=>{
  const server=onboardingServer({posts:[],options:null});
  const ui=await open(agentChat("agent-one"),{onboarding:server.handle});
  await ui.tick();await ui.tick();
  const reads=()=>server.seen.map(call=>call.url);
  assert.deepEqual(reads(),["/api/agents/onboarding"],"a half-finished creation still continues from page load");
  await openOnboarding(ui);
  fillOnboarding(ui,"研究員","找資料");
  assert.deepEqual(reads(),["/api/agents/onboarding","/api/agents/onboarding/options"]);
  assert.notEqual(ui.text("onboarding-choice-note"),"");
  const panel=ui.nodes.get("onboarding-panel");
  panel.open=false;panel.events.toggle();await openOnboarding(ui);
  assert.deepEqual(reads(),["/api/agents/onboarding","/api/agents/onboarding/options","/api/agents/onboarding/options"],
    "a failed catalog read is retried on the next opening, not on every render");
  assert.equal(server.seen.some(call=>call.method==="POST"),false);
});
test("the desktop panel shows grey text instead of a black screen and disables 連線 without the desktop capability",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  for(const data of [a,b]){data.template_tools_available=true;data.composer.hosted_session_id="hosted-session-1"}
  const capabilities={"agent-one":[{entry_id:"fa-desktop-v1",entry_sha256:"e".repeat(64)}],
    "agent-two":[{entry_id:"playwright",entry_sha256:"f".repeat(64)}]};
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},agentTemplates:agent=>({ok:true,
    json:async()=>({schema_version:"fa-agent-template-v1",settings:{capabilities:capabilities[agent]}})})});
  const node=id=>ui.nodes.get(id),reads=()=>ui.calls.filter(call=>call.url==="/api/agent-template");
  // Not connected: the canvas is hidden and the same explanation shows in grey; a closed panel reads nothing.
  assert.equal(node("desktop-panel").hidden,false);
  assert.equal(node("desktop-screen").hidden,true);assert.equal(node("desktop-idle").hidden,false);
  assert.equal(node("desktop-note").hidden,true);assert.equal(ui.text("desktop-idle"),ui.text("desktop-note"));
  assert.equal(node("desktop-connect").disabled,false);assert.equal(reads().length,0);
  node("desktop-panel").open=true;node("desktop-panel").events.toggle();await ui.tick();await ui.tick();
  assert.equal(node("desktop-connect").disabled,false);assert.equal(ui.text("desktop-idle"),ui.text("desktop-note"));
  sidebar(ui,"agent-two").button.events.click();await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.text("desktop-idle"),"這位助理沒有開啟桌面能力。");
  assert.equal(node("desktop-connect").disabled,true);
  assert.deepEqual(reads().map(call=>call.headers["X-Console-Agent"]),["agent-one","agent-two"]);
  // A template that cannot be read leaves 連線 to the server's answer instead of guessing, and is not re-read per render.
  ui.holder.agentTemplates=()=>({ok:false,json:async()=>({error:{code:"TEMPLATE_READ_FAILED"}})});
  node("desktop-panel").events.toggle();await ui.tick();await ui.tick();
  assert.equal(node("desktop-connect").disabled,false);assert.equal(ui.text("desktop-idle"),ui.text("desktop-note"));
  for(let index=0;index<3;index++){ui.render(b);await ui.tick()}
  assert.equal(reads().length,3);
  // A new template applies to new conversations: a new conversation reads it again.
  capabilities["agent-two"].push({entry_id:"fa-desktop-v1",entry_sha256:"e".repeat(64)});
  ui.holder.agentTemplates=agent=>({ok:true,
    json:async()=>({schema_version:"fa-agent-template-v1",settings:{capabilities:capabilities[agent]}})});
  const next=copy(b);next.composer.conversation_id="conversation-2";ui.render(next);await ui.tick();await ui.tick();
  assert.equal(reads().length,4);assert.equal(node("desktop-connect").disabled,false);
  assert.equal(ui.text("desktop-idle"),ui.text("desktop-note"));
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});
// Neal 2026-10-07 13:12「桌面無法連線」: the bridge now names the VM the running turn is on (native.running_session).
// A viewer still on the conversation's old VM closes and says the conversation moved; the next 連線 uses the new VM.
test("the live desktop follows the conversation to another VM and asks to connect again",async()=>{
  const a=agentChat("agent-one");a.composer.hosted_session_id="session-before-follow";
  const ui=await open(a,{agentStates:{"agent-one":a}});
  const node=id=>ui.nodes.get(id),viewer=()=>vm.runInContext("desktopView",ui.context);
  const moved="這個對話換到另一台 VM 執行，桌面連線已中斷；按「連線」看新的桌面。";
  ui.click("desktop-connect");
  assert.equal(viewer().session,"session-before-follow");assert.equal(viewer().thread,a.composer.thread_id);
  const followed=copy(a);followed.composer.hosted_session_id="session-after-follow";ui.render(followed);
  assert.equal(viewer(),null);assert.equal(ui.text("desktop-note"),moved);assert.equal(ui.text("desktop-idle"),moved);
  assert.equal(node("desktop-connect").disabled,false);
  ui.click("desktop-connect");
  assert.equal(viewer().session,"session-after-follow");
  // Another conversation keeps the original note; the same VM never disconnects.
  ui.render(followed);assert.equal(viewer().session,"session-after-follow");
  const other=copy(followed);other.composer.thread_id="thread-other";other.composer.hosted_session_id="session-other";
  ui.render(other);
  assert.equal(viewer(),null);assert.equal(ui.text("desktop-note"),"已切換助理或對話，桌面連線已中斷。");
  assert.match(html,/DESKTOP_VM_NOT_RUNNING:"[^"]*送出訊息、助理開始回合後再按「連線」。"/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Settings page (S1): one Agent's new-conversation skills and capabilities, plus the moved Team rules panel.
const settingsBase=()=>{const base=selectableTemplate();base.settings.capabilities=[{entry_id:"fa-shell-v1",entry_sha256:"5".repeat(64)}];return base};
const settingsSkill={name:"receipt-guide",version_id:"1".repeat(32),sha256:"2".repeat(64),metadata_sha256:"3".repeat(64)};
const settingsOptions=()=>({schema_version:"fa-agent-template-options-v1",skills:[copy(settingsSkill)],more_skills:false,
  capabilities:[
    {entry_id:"WebIQ",entry_sha256:"4".repeat(64),kind:"mcp-toolbox",tools:["WebIQ-web","WebIQ-browse"],vision:false},
    {entry_id:"fa-shell-v1",entry_sha256:"5".repeat(64),kind:"fa-builtin",tools:["run_command"],vision:false},
    {entry_id:"fa-skills-v1",entry_sha256:"6".repeat(64),kind:"fa-builtin",tools:["read_skill"],vision:false}],
  composable_profiles:["fa-instruction-v1"],responses:{enabled:false,history_source:null},authorization:false,activated:false});
const settingsCurrent={selection_id:"7".repeat(64),applies_to:"new-conversations-only",permissions_changed:false};
async function settingsUi(options=settingsOptions(),base=settingsBase(),selections=[]){
  return open(templateData(),{templateResponses:{
    "/api/agent-template":{ok:true,json:async()=>copy(base)},
    "/api/agent-template/options":{ok:true,json:async()=>copy(options)},
    "/api/agent-template/selection":selectionRead(settingsCurrent,selections),
    "/api/agent-connectors":{ok:true,json:async()=>agentConnectorsView()},
  }});
}
// One Agent's own settings open from its ⚙ in the Agent list.
async function openSettingsPage(options=settingsOptions(),base=settingsBase(),selections=[]){
  const ui=await settingsUi(options,base,selections);
  agentGear(ui,"configured-agent").events.click();await ui.tick();await ui.tick();
  return ui;
}
// The Team's shared settings open from the ⚙ menu at the bottom left (Team MCP first).
async function openTeamSettings(ui,tab=null){
  ui.click("open-settings");
  if(tab)ui.click("settings-tab-"+tab);
  await settleReviews(ui);
  return ui;
}
const AGENT_TABS=["installed","skills","mcp","plugins"],TEAM_TABS=["team-mcp","team-connectors","team-skills","rules"];
const shownTabs=ui=>[...AGENT_TABS,"routines",...TEAM_TABS].filter(tab=>!ui.nodes.get("settings-tab-"+tab).hidden);
const settingsRows=ui=>ui.nodes.get("settings-list").children.filter(row=>row.children.length===2);
const settingsRow=(ui,title)=>settingsRows(ui).find(row=>row.children[0].children[0].textContent.includes(title));
function settingsSwitch(ui,title,on){
  const toggle=settingsRow(ui,title).children[1];toggle.checked=on;toggle.events.change();return toggle;
}

test("an Agent's ⚙ opens its own settings and lists its skills, MCP and built-in plugins by tab",async()=>{
  const ui=await openSettingsPage();
  assert.equal(ui.nodes.get("settings-page").hidden,false);
  assert.equal(ui.text("settings-title"),"FA 助理 的設定");
  assert.equal(ui.nodes.get("settings-page").getAttribute("aria-label"),"FA 助理 的設定");
  assert.equal(agentGear(ui,"configured-agent").getAttribute("aria-label"),"FA 助理 的設定");
  assert.deepEqual(shownTabs(ui),AGENT_TABS,"no Team tab on an Agent's page");
  assert.equal(ui.nodes.get("settings-tab-installed").getAttribute("aria-selected"),"true");
  assert.deepEqual(["/api/agent-template","/api/agent-template/options","/api/agent-template/selection"]
    .map(path=>ui.calls.filter(call=>call.url===path).length),[1,1,1]);
  assert.deepEqual(settingsRows(ui).map(row=>row.children[0].children[0].textContent),["在自己的 sandbox 執行命令"]);
  assert.equal(ui.text("settings-count"),"1 項");
  assert.equal(settingsRow(ui,"在自己的 sandbox 執行命令").children[0].children[2].textContent,"使用中");
  // P10: the role and the skills in use, in words; no version IDs, entry IDs or hashes on this page.
  assert.equal(ui.nodes.get("settings-role").hidden,false);
  assert.match(ui.text("settings-role"),/^角色：/);
  ui.click("settings-tab-skills");
  assert.equal(settingsRow(ui,"receipt-guide").children[0].children[1].textContent,"Team 已批准的 skill");
  assert.equal(ui.nodes.get("settings-role").hidden,true);
  assert.doesNotMatch(JSON.stringify(ui.nodes.get("settings-list").children,(key,value)=>key==="events"?undefined:value),
    /1111|版本 |fa-shell-v1|fa-skills-v1/);
  ui.click("settings-tab-skills");
  assert.deepEqual(settingsRows(ui).map(row=>row.children[0].children[0].textContent),["receipt-guide"]);
  assert.match(ui.text("settings-intro"),/要審核、批准、停用或自己寫 skill，請到左下 ⚙ 的「Agent Team 設定」→ Team Skills/);
  assert.equal(ui.nodes.get("settings-skill-reviews").hidden,true,"Team skill review is not on an Agent's page");
  ui.click("settings-tab-mcp");
  assert.deepEqual(settingsRows(ui).map(row=>row.children[0].children[0].textContent),["WebIQ"]);
  assert.match(settingsRow(ui,"WebIQ").children[0].children[1].textContent,/WebIQ-web、WebIQ-browse/);
  assert.match(ui.text("settings-intro"),/金鑰不會經過這個頁面/);
  assert.equal(ui.nodes.get("settings-team-mcp").hidden,true,"Team MCP is not on an Agent's page");
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/team-")),false,"an Agent's page reads nothing of the Team");
  ui.click("settings-tab-plugins");
  assert.equal(settingsRows(ui).length,2);
  assert.match(ui.text("settings-intro"),/不能在這裡新增或移除/);
  ui.nodes.get("settings-search").value="SKILL";ui.nodes.get("settings-search").events.input();
  assert.deepEqual(settingsRows(ui).map(row=>row.children[0].children[0].textContent),["讀取已批准的 skill"]);
  assert.equal(ui.text("settings-changes"),"沒有變更。");
  assert.equal(ui.nodes.get("settings-apply").disabled,true);
  ui.click("close-settings");
  assert.equal(ui.nodes.get("settings-page").hidden,true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("settings stages switches and sends one inspected selection that changes only skills and capabilities",async()=>{
  const ui=await openSettingsPage();
  ui.click("settings-tab-skills");
  settingsSwitch(ui,"receipt-guide",true);
  assert.match(ui.text("settings-changes"),/開啟：receipt-guide、讀取已批准的 skill/);
  ui.click("settings-tab-plugins");
  const reader=settingsRow(ui,"讀取已批准的 skill");
  assert.equal(reader.children[1].checked,true);assert.equal(reader.children[1].disabled,true);
  assert.match(reader.children[0].children[2].textContent,/先關掉那些項目/);
  settingsSwitch(ui,"在自己的 sandbox 執行命令",false);
  assert.match(ui.text("settings-changes"),/關閉：在自己的 sandbox 執行命令/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  ui.holder.templateResponses["/api/agent-template/inspect"]={ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"a".repeat(64)})};
  ui.click("settings-apply");ui.click("settings-apply");
  const question=await ui.answer(true);await ui.tick();
  ui.click("settings-apply");await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"an apply in flight is not asked again");
  assert.match(question,/只影響之後的新對話；舊對話和已開始的工作不變，也不是新增操作權限/);
  assert.match(question,/關閉：在自己的 sandbox 執行命令/);
  const inspects=ui.calls.filter(call=>call.url==="/api/agent-template/inspect");
  const selects=ui.calls.filter(call=>call.url==="/api/agent-template/select");
  assert.equal(inspects.length,1);assert.equal(selects.length,1);
  const command=JSON.parse(selects[0].body),expected=settingsBase();
  expected.skills=[settingsSkill];expected.settings.capabilities=[{entry_id:"fa-skills-v1",entry_sha256:"6".repeat(64)}];
  assert.match(command.request_id,/^ui-settings-/);
  assert.equal(command.expected_selection_id,settingsCurrent.selection_id);
  assert.deepEqual(command.template,expected);
  assert.deepEqual(JSON.parse(inspects[0].body),expected);
  assert.match(ui.text("settings-note"),/正在送出一次；回應不明時不會重送/);
  ui.holder.pending(selectedTemplateResponse(command));await ui.tick();await ui.tick();await ui.tick();await ui.tick();
  assert.match(ui.text("settings-note"),/已套用：之後的新對話會使用這份設定/);
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template").length,2);
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),
    ["/api/agent-template/inspect","/api/agent-template/select"]);
});

test("settings cancel or a changed draft sends nothing; a lost select is only reconciled by its original ID",async()=>{
  const ui=await openSettingsPage();
  ui.click("settings-tab-mcp");settingsSwitch(ui,"WebIQ",true);
  ui.holder.templateResponses["/api/agent-template/inspect"]={ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"a".repeat(64)})};
  ui.click("settings-apply");await ui.answer(false);await ui.tick();
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/select"),false);
  assert.equal(ui.text("settings-note"),"沒有送出任何變更。");
  // The draft changes while the in-page question is open: 確定 afterwards sends nothing.
  ui.click("settings-apply");await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,false);
  settingsSwitch(ui,"WebIQ",false);settingsSwitch(ui,"WebIQ",true);
  await ui.answer(true);await ui.tick();
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/select"),false);
  let lost;
  ui.holder.templateResponses["/api/agent-template/select"]=new Promise(resolve=>{lost=resolve});
  ui.click("settings-apply");await ui.answer(true);await ui.tick();
  const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
  const retained=selectedTemplateResponse(command,{state:"UNKNOWN"});
  ui.holder.current.template_selections=retained.selections;
  ui.holder.templateResponses["/api/agent-template/selection"]=selectionRead(settingsCurrent,retained.selections);
  lost({ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})});
  for(let n=0;n<6;n++)await ui.tick();
  const unknown=ui.nodes.get("settings-selections").children[0];
  assert.match(unknown.children[0].textContent,/結果未確認.*不會換編號或重送/);
  assert.notEqual(ui.text("settings-note"),"");
  let read;
  ui.holder.templateResponses["/api/agent-template/selection-reconcile"]=new Promise(resolve=>{read=resolve});
  unknown.children[1].events.click();unknown.children[1].events.click();await ui.tick();
  const reconciles=ui.calls.filter(call=>call.url==="/api/agent-template/selection-reconcile");
  assert.equal(reconciles.length,1);
  assert.deepEqual(JSON.parse(reconciles[0].body),{request_id:command.request_id});
  read({ok:true,json:async()=>selectedTemplateResponse(command,{state:"UNKNOWN",reconciliation:true})});
  for(let n=0;n<6;n++)await ui.tick();
  assert.match(ui.text("settings-note"),/已核對原請求.*沒有重送/);
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/select").length,1);
});

test("settings only lets an unavailable item be switched off and blocks capabilities on a non-composable role",async()=>{
  const options=settingsOptions();options.capabilities=options.capabilities.filter(entry=>entry.entry_id!=="fa-shell-v1");
  const ui=await openSettingsPage(options);
  const shell=settingsRow(ui,"在自己的 sandbox 執行命令");
  assert.equal(shell.children[1].checked,true);assert.equal(shell.children[1].disabled,false);
  assert.match(shell.children[0].children[1].textContent,/目前環境未提供/);
  settingsSwitch(ui,"在自己的 sandbox 執行命令",false);
  ui.click("settings-tab-plugins");
  assert.equal(settingsRow(ui,"在自己的 sandbox 執行命令").children[1].disabled,true);
  assert.match(settingsRow(ui,"在自己的 sandbox 執行命令").children[0].children[2].textContent,/只能關閉/);
  const locked=settingsOptions();locked.composable_profiles=[];
  const other=await openSettingsPage(locked);
  other.click("settings-tab-mcp");
  assert.equal(settingsRow(other,"WebIQ").children[1].disabled,true);
  assert.match(settingsRow(other,"WebIQ").children[0].children[2].textContent,/角色版本不能組合/);
  assert.equal(ui.calls.some(call=>call.method==="POST")||other.calls.some(call=>call.method==="POST"),false);
});

test("settings can close unavailable MCP selections in one local edit without changing other choices",async()=>{
  const base=settingsBase();
  base.settings.capabilities.push({entry_id:"team-MsLearn",entry_sha256:"a".repeat(64)});
  const ui=await openSettingsPage(settingsOptions(),base);
  assert.equal(ui.nodes.get("settings-apply").disabled,true);
  const clear=ui.nodes.get("settings-clear-unavailable-mcp");
  assert.ok(clear);
  assert.equal(clear.hidden,false);
  ui.click("settings-clear-unavailable-mcp");await ui.tick();
  assert.equal(settingsRow(ui,"team-MsLearn").children[1].checked,false);
  assert.equal(settingsRow(ui,"在自己的 sandbox 執行命令").children[1].checked,true);
  assert.equal(ui.nodes.get("settings-apply").disabled,false);
  assert.equal(clear.hidden,true);
  assert.match(ui.text("settings-changes"),/尚未套用|將關閉/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("Team rules moved into settings while approval and question cards stay on the chat page",async()=>{
  const settings=html.indexOf('id="settings-page"'),rules=html.indexOf('id="settings-rules"');
  const panel=html.indexOf('id="hitl-rules-panel"'),chat=html.indexOf('id="chat-scroll"');
  assert.ok(settings<rules&&rules<panel&&panel<chat);
  assert.ok(html.indexOf('id="attention-panel"')>chat&&html.indexOf('id="hitl-status"')>chat);
  const data=hitlChat(),view=hitlRead(data);
  const ui=await open(data,{now:Date.parse(when),hitl:async()=>({ok:true,json:async()=>({rules:copy(view.rules),commands:[],can_approve:true,read_at:when})})});
  assert.equal(ui.text("hitl-rules-note"),"尚未讀取，不以空清單當成沒有規則。");
  ui.click("open-hitl-rules");
  assert.equal(ui.nodes.get("settings-page").hidden,false);
  assert.equal(ui.text("settings-title"),"Agent Team 設定");
  assert.deepEqual(shownTabs(ui),TEAM_TABS);
  assert.equal(ui.nodes.get("settings-tab-rules").getAttribute("aria-selected"),"true");
  assert.equal(ui.nodes.get("settings-rules").hidden,false);assert.equal(ui.nodes.get("settings-catalog").hidden,true);
  assert.equal(ui.nodes.get("hitl-rules-panel").open,true);assert.equal(ui.text("settings-rules-note"),"");
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  assert.match(ui.text("hitl-rules-note"),/已讀規則版本/);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  ui.documentEvents.keydown({key:"Escape"});
  assert.equal(ui.nodes.get("settings-page").hidden,true);
});

test("another Agent's ⚙ selects that Agent first and opens its settings; remote re-sign-in stays in the account menu",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  for(const data of [a,b]){data.template_tools_available=true;data.template_selections=[]}
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},templateResponses:{
    "/api/agent-template":{ok:true,json:async()=>settingsBase()},
    "/api/agent-template/options":{ok:true,json:async()=>settingsOptions()},
    "/api/agent-template/selection":selectionRead(settingsCurrent,[]),
  }});
  assert.equal(ui.nodes.get("settings-relogin").hidden,true);
  assert.equal(ui.nodes.get("settings-agent-select"),undefined,"no Agent picker on the settings page");
  const reads=agent=>ui.calls.filter(call=>call.url==="/api/agent-template/options"&&call.headers?.["X-Console-Agent"]===agent).length;
  agentGear(ui,"agent-two").events.click();
  assert.equal(ui.text("settings-note"),"正在讀取這位助理…","while the switched Agent loads, no misleading refusal");
  for(let n=0;n<8;n++)await ui.tick();
  assert.equal(vm.runInContext("selectedAgent",ui.context),"agent-two");
  assert.equal(ui.nodes.get("settings-page").hidden,false);
  assert.equal(ui.text("settings-title"),"第二位助理 的設定");
  assert.deepEqual(shownTabs(ui),AGENT_TABS);
  assert.equal(reads("agent-two"),1);assert.equal(reads("agent-one"),0,"nothing of the previous Agent is read");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  const remote=await open({...fixture(),authentication:"AVAILABLE",authentication_refresh_at:null,remote_user:"neal@example.org"},
    {location:{protocol:"https:"}});
  await remote.tick();
  assert.equal(remote.nodes.get("login").hidden,true);
  assert.equal(remote.nodes.get("settings-relogin").hidden,false);
  remote.click("settings-relogin");
  assert.equal(remote.context.window.location.href,"/.auth/login/aad?post_login_redirect_uri=%2F");
  assert.equal(remote.calls.some(call=>call.method==="POST"),false);
});

test("the selected Agent's ⚙ opens its settings without the row's own click, switch or reread",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  for(const data of [a,b]){data.template_tools_available=true;data.template_selections=[]}
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},templateResponses:{
    "/api/agent-template":{ok:true,json:async()=>settingsBase()},
    "/api/agent-template/options":{ok:true,json:async()=>settingsOptions()},
    "/api/agent-template/selection":selectionRead(settingsCurrent,[]),
  }});
  const stateReads=()=>ui.calls.filter(call=>call.url.startsWith("/api/state")).length,before=stateReads();
  const gear=agentGear(ui,"agent-one"),row=ui.nodes.get("agent-list").children[0];
  assert.equal(gear.tagName,"BUTTON");assert.equal(gear.getAttribute("aria-label"),"第一位助理 的設定");
  assert.equal(row.children[0].contains(gear),false,"the ⚙ is not inside the row button");
  ui.nodes.get("instruction").focused=false;
  gear.events.click();for(let n=0;n<4;n++)await ui.tick();
  assert.equal(vm.runInContext("selectedAgent",ui.context),"agent-one");
  assert.equal(ui.text("settings-title"),"第一位助理 的設定");
  assert.equal(stateReads(),before,"no switch, so no reread of the conversation");
  assert.equal(ui.nodes.get("instruction").focused,false,"the row's click (focus the composer) did not run");
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/options"&&call.headers?.["X-Console-Agent"]==="agent-one").length,1);
  ui.click("close-settings");
  assert.equal(ui.nodes.get("settings-page").hidden,true);
  assert.equal(agentGear(ui,"agent-one").focused,true,"closing an Agent's settings returns focus to its ⚙");
  const off=agentChat("agent-one");off.agents[1].enabled=false;
  const locked=await open(off);
  assert.equal(agentGear(locked,"agent-two").disabled,true,"an unavailable Agent's ⚙ is off like its row");
  vm.runInContext("openAgentSettings('agent-two')",locked.context);
  assert.equal(locked.nodes.get("settings-page").hidden,true);
  assert.equal(vm.runInContext("selectedAgent",locked.context),"agent-one");
});

test("settings follows the offered version of an installed item and blocks applying while a withdrawn one stays on",async()=>{
  const base=settingsBase();base.settings.capabilities=[{entry_id:"fa-shell-v1",entry_sha256:"9".repeat(64)}];
  const ui=await openSettingsPage(settingsOptions(),base);
  assert.equal(settingsRow(ui,"在自己的 sandbox 執行命令").children[0].children[2].textContent,"使用中；套用後改用目前批准的版本");
  assert.match(ui.text("settings-changes"),/改用目前批准的版本：在自己的 sandbox 執行命令/);
  assert.equal(ui.nodes.get("settings-apply").disabled,false);
  ui.holder.templateResponses["/api/agent-template/inspect"]={ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"a".repeat(64)})};
  ui.click("settings-apply");await ui.answer(true);await ui.tick();
  const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
  assert.deepEqual(command.template.settings.capabilities,[{entry_id:"fa-shell-v1",entry_sha256:"5".repeat(64)}]);
  const withdrawn=settingsOptions();withdrawn.capabilities=withdrawn.capabilities.filter(entry=>entry.entry_id!=="fa-shell-v1");
  const other=await openSettingsPage(withdrawn);
  other.click("settings-tab-mcp");settingsSwitch(other,"WebIQ",true);
  assert.equal(other.nodes.get("settings-apply").disabled,true);
  assert.match(other.text("settings-changes"),/目前環境未提供：在自己的 sandbox 執行命令；請先關閉才能套用/);
  other.click("settings-tab-installed");
  assert.equal(settingsRow(other,"在自己的 sandbox 執行命令").children[0].children[2].textContent,"使用中；目前環境未提供，套用前請先關閉");
  settingsSwitch(other,"在自己的 sandbox 執行命令",false);
  assert.equal(other.nodes.get("settings-apply").disabled,false);
  assert.equal(other.calls.some(call=>call.url==="/api/agent-template/select"),false);
});

test("settings work in flight is dropped when the page is closed and another Agent is chosen",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  for(const data of [a,b]){data.template_tools_available=true;data.template_selections=[]}
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},templateResponses:{
    "/api/agent-template":{ok:true,json:async()=>settingsBase()},
    "/api/agent-template/options":{ok:true,json:async()=>settingsOptions()},
    "/api/agent-template/selection":selectionRead(settingsCurrent,[]),
    "/api/agent-template/inspect":{ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"a".repeat(64)})},
  }});
  agentGear(ui,"agent-one").events.click();await ui.tick();await ui.tick();
  ui.click("settings-tab-mcp");settingsSwitch(ui,"WebIQ",true);
  ui.click("settings-apply");await ui.answer(true);await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/select").length,1);
  ui.click("close-settings");
  sidebar(ui,"agent-two").button.events.click();for(let n=0;n<6;n++)await ui.tick();
  ui.holder.pending({error:{code:"BACKEND_OFFLINE"}});for(let n=0;n<4;n++)await ui.tick();
  sidebar(ui,"agent-one").button.events.click();for(let n=0;n<6;n++)await ui.tick();
  const reads=ui.calls.filter(call=>call.url==="/api/agent-template/options"&&call.headers?.["X-Console-Agent"]==="agent-one").length;
  agentGear(ui,"agent-one").events.click();for(let n=0;n<4;n++)await ui.tick();
  assert.doesNotMatch(ui.text("settings-note"),/正在送出/);
  assert.equal(ui.nodes.get("settings-reload").disabled,false);
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/options"&&call.headers?.["X-Console-Agent"]==="agent-one").length,reads+1);
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/select").length,1);
});

test("settings keeps an installed skill applicable while a newer version of it awaits approval",async()=>{
  const base=settingsBase();base.skills=[copy(settingsSkill)];
  base.settings.capabilities=[{entry_id:"fa-skills-v1",entry_sha256:"6".repeat(64)}];
  const pending=settingsOptions();pending.skills=[];
  const ui=await openSettingsPage(pending,base);
  assert.equal(settingsRow(ui,"receipt-guide").children[0].children[2].textContent,"使用中");
  ui.click("settings-tab-mcp");settingsSwitch(ui,"WebIQ",true);
  assert.equal(ui.nodes.get("settings-apply").disabled,false);
  assert.doesNotMatch(ui.text("settings-changes"),/目前環境未提供/);
  ui.holder.templateResponses["/api/agent-template/inspect"]={ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"a".repeat(64)})};
  ui.click("settings-apply");await ui.answer(true);await ui.tick();
  const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
  assert.deepEqual(command.template.skills,[settingsSkill]);
});

// S2: owner skill review in the settings page (approve, reject, deactivate, replace).
const skillApproval=id=>({decision_id:id,version_id:"1".repeat(32),sha256:"e".repeat(64),decision:"approve",
  metadata_sha256:"f".repeat(64),approver_oid:"owner",decided_at:when});
const skillVersion=(approval=null)=>({version_id:"1".repeat(32),number:1,sha256:"e".repeat(64),metadata_sha256:"f".repeat(64),
  created_at:when,filename:"SKILL.md",media_type:"text/markdown",
  skill_purpose:{schema_version:"fa-skill-purpose-v1",description:"整理收據"},...(approval?{approval}:{})});
function skillDoc(id,title,{approval=null,effective=false,deactivations=[]}={}){
  return {artifact_id:id,team:"team",kind:"skill",title,owner:"agent-a",revision:1,versions:[skillVersion(approval)],
    pending:null,effective_version_id:effective?"1".repeat(32):null,deactivations};
}
const reviewEntry=(id,title,status)=>({artifact_id:id,title,owner:"agent-a",revision:1,status,
  latest_version:{version_id:"1".repeat(32),number:1,sha256:"e".repeat(64),metadata_sha256:"f".repeat(64),created_at:when,
    decision:status==="effective"?"approve":null},effective_version_id:status==="effective"?"1".repeat(32):null,deactivated_version_ids:[]});
const reply=value=>({ok:true,json:async()=>copy(value)});
function skillReviewResponses(ui,{newDoc=skillDoc("new-guide","新指南"),oldDoc=skillDoc("old-guide","舊指南",{approval:skillApproval("0".repeat(32)),effective:true})}={}){
  Object.assign(ui.holder.templateResponses,{
    "/api/team-skills/reviews":reply({schema_version:"fa-skill-review-list-v1",team:"team",after:null,next_after:null,
      entries:[reviewEntry("new-guide","新指南","awaiting_decision"),reviewEntry("old-guide","舊指南","effective")]}),
    "/api/team-skills/skills/new-guide":reply({artifact:newDoc,etag:"etag-new"}),
    ["/api/team-skills/skills/new-guide/versions/"+"1".repeat(32)]:reply({version:skillVersion(),text:"# 新指南\n步驟一"}),
    "/api/team-skills/skills/old-guide":reply({artifact:oldDoc,etag:"etag-old"}),
    ["/api/team-skills/skills/old-guide/versions/"+"1".repeat(32)]:reply({version:skillVersion(skillApproval("0".repeat(32))),text:"# 舊指南"}),
  });
}
const reviewRows=ui=>ui.nodes.get("settings-skill-review-list").children;
const reviewRow=(ui,title)=>reviewRows(ui).find(row=>row.children[0]?.textContent?.startsWith(title));
const reviewButton=(row,label)=>{
  for(const node of [row,...row.children,...row.children.flatMap(child=>child.children||[])])
    if(node.textContent===label&&node.events?.click)return node;
};
async function settleReviews(ui,n=8){for(let i=0;i<n;i++)await ui.tick()}
async function openReviews(options,base){
  const ui=await settingsUi(options,base);
  skillReviewResponses(ui);
  Object.assign(ui.holder.templateResponses,{"/api/team-mcp/servers":reply(teamMcpView()),
    "/api/team-mcp/connections":reply(teamMcpConnections)});
  return openTeamSettings(ui,"team-skills");
}

test("skill review: content must be viewed first, then one approval with a fixed decision ID is sent once",async()=>{
  const ui=await openReviews();
  assert.equal(ui.calls.filter(call=>call.url==="/api/team-skills/reviews").length,1);
  assert.equal(ui.nodes.get("settings-skill-reviews").hidden,false);
  let row=reviewRow(ui,"新指南");
  assert.match(row.children[0].textContent,/待審核/);
  assert.equal(reviewButton(row,"批准").disabled,true);
  reviewButton(row,"查看內容").events.click();await settleReviews(ui);
  row=reviewRow(ui,"新指南");
  assert.ok(row.children.some(node=>node.textContent==="# 新指南\n步驟一"));
  assert.ok(row.children.some(node=>node.textContent==="用途：整理收據"));
  assert.equal(reviewButton(row,"批准").disabled,false);
  reviewButton(row,"批准").events.click();
  assert.match(await ui.answer(false),/^批准 skill「新指南」第 1 版？\n/);await settleReviews(ui);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/decisions")).length,0,"取消 sends nothing");
  row=reviewRow(ui,"新指南");
  reviewButton(row,"批准").events.click();reviewButton(row,"批准").events.click();await ui.tick();
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await settleReviews(ui);
  const posts=ui.calls.filter(call=>call.url==="/api/team-skills/skills/new-guide/decisions");
  assert.equal(posts.length,1);
  const body=JSON.parse(posts[0].body);
  assert.match(body.decision_id,/^[a-f0-9]{32}$/);
  assert.deepEqual({...body,decision_id:null},{decision_id:null,version_id:"1".repeat(32),sha256:"e".repeat(64),
    metadata_sha256:"f".repeat(64),decision:"approve",etag:"etag-new"});
  ui.holder.pending({artifact:skillDoc("new-guide","新指南",{approval:skillApproval(body.decision_id),effective:true}),
    etag:"etag-2",decision_id:body.decision_id,original_request_resent:false});
  await settleReviews(ui);
  assert.ok(reviewRow(ui,"新指南").children.some(node=>node.textContent==="決定已記錄。"));
  assert.equal(ui.calls.filter(call=>call.url==="/api/team-skills/reviews").length,2);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/decisions")).length,1);
});

test("skill review: a failed decision is read back first and only resent with the same decision ID after confirmation",async()=>{
  const ui=await openReviews();
  reviewButton(reviewRow(ui,"新指南"),"查看內容").events.click();await settleReviews(ui);
  ui.holder.templateResponses["/api/team-skills/skills/new-guide/decisions"]={ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})};
  reviewButton(reviewRow(ui,"新指南"),"拒絕").events.click();
  assert.match(await ui.answer(true),/^拒絕 skill「新指南」第 1 版？\n/);await settleReviews(ui);
  const first=JSON.parse(ui.calls.find(call=>call.url.endsWith("/decisions")).body);
  assert.equal(first.decision,"reject");
  const reads=()=>ui.calls.filter(call=>call.url==="/api/team-skills/skills/new-guide").length;
  assert.equal(reads(),2);
  let row=reviewRow(ui,"新指南");
  assert.ok(row.children.some(node=>/決定沒有確認/.test(node.textContent)));
  reviewButton(row,"重新讀取並核對").events.click();
  assert.match(await ui.answer(false),/用同一個決定編號再送一次/);await settleReviews(ui);
  assert.equal(reads(),3);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/decisions")).length,1);
  delete ui.holder.templateResponses["/api/team-skills/skills/new-guide/decisions"];
  reviewButton(reviewRow(ui,"新指南"),"重新讀取並核對").events.click();await ui.answer(true);await settleReviews(ui);
  const posts=ui.calls.filter(call=>call.url.endsWith("/decisions"));
  assert.equal(posts.length,2);
  assert.equal(JSON.parse(posts[1].body).decision_id,first.decision_id);
});

test("skill review: deactivation names the effective version and keeps the original",async()=>{
  const ui=await openReviews();
  reviewButton(reviewRow(ui,"舊指南"),"停用").events.click();
  assert.match(await ui.answer(true),/原內容和批准紀錄保留，不刪除/);await settleReviews(ui);
  const posts=ui.calls.filter(call=>call.url==="/api/team-skills/skills/old-guide/deactivations");
  assert.equal(posts.length,1);
  const body=JSON.parse(posts[0].body);
  assert.deepEqual({...body,decision_id:null},{schema_version:"fa-skill-deactivation-v1",decision_id:null,
    version_id:"1".repeat(32),sha256:"e".repeat(64),metadata_sha256:"f".repeat(64),etag:"etag-old"});
});

test("skill review: replace approves the new skill, deactivates the old one, then swaps them in one confirmed selection",async()=>{
  const base=settingsBase();
  base.skills=[{name:"old-guide",version_id:"1".repeat(32),sha256:"e".repeat(64),metadata_sha256:"f".repeat(64)}];
  base.settings.capabilities=[{entry_id:"fa-skills-v1",entry_sha256:"6".repeat(64)}];
  const ui=await openReviews(settingsOptions(),base);
  reviewButton(reviewRow(ui,"新指南"),"查看內容").events.click();await settleReviews(ui);
  const after=settingsOptions();after.skills=[{name:"new-guide",version_id:"1".repeat(32),sha256:"e".repeat(64),metadata_sha256:"f".repeat(64)}];
  ui.holder.templateResponses["/api/agent-template/options"]=reply(after);
  reviewButton(reviewRow(ui,"新指南"),"批准並取代").events.click();
  const confirmations=[await ui.answer(true)];await settleReviews(ui);
  assert.match(confirmations[0],/1\. 批准新 skill.*2\. 停用舊 skill.*3\./s);
  const approval=JSON.parse(ui.calls.find(call=>call.url.endsWith("new-guide/decisions")).body);
  ui.holder.pending({artifact:skillDoc("new-guide","新指南",{approval:skillApproval(approval.decision_id),effective:true}),
    etag:"etag-2",decision_id:approval.decision_id,original_request_resent:false});
  await settleReviews(ui);
  const deactivation=JSON.parse(ui.calls.find(call=>call.url.endsWith("old-guide/deactivations")).body);
  // As the API does after deactivation: the template lists the old skill only under unavailable_skills.
  const reloaded=copy(base);reloaded.skills=[];reloaded.unavailable_skills=["old-guide"];
  ui.holder.templateResponses["/api/agent-template"]=reply(reloaded);
  ui.holder.pending({artifact:skillDoc("old-guide","舊指南",{approval:skillApproval("0".repeat(32)),
    deactivations:[{...deactivation,etag:undefined,deactivated_by_oid:"owner",deactivated_at:when,applied_revision:2}]}),
    etag:"etag-3",decision_id:deactivation.decision_id,original_request_resent:false});
  await settleReviews(ui,12);
  assert.match(ui.text("settings-note"),/已批准「新指南」並停用「舊指南」/);
  // Step 3 changes the selected Agent's own settings, so it is shown on that Agent's page, not on the Team page.
  assert.equal(ui.text("settings-title"),"FA 助理 的設定");
  assert.equal(ui.nodes.get("settings-tab-skills").getAttribute("aria-selected"),"true");
  assert.equal(ui.nodes.get("settings-catalog").hidden,false);assert.equal(ui.nodes.get("settings-skill-reviews").hidden,true);
  assert.match(confirmations[0],/3\. 打開目前選中的助理「FA 助理」的設定/);
  ui.holder.templateResponses["/api/agent-template/inspect"]=reply({...inspectedTemplate(),template_sha256:"a".repeat(64)});
  for(const [id,fn] of [...ui.timers])if(ui.timerDelays.get(id)===0){ui.timers.delete(id);fn()}
  confirmations.push(await ui.answer(true));await settleReviews(ui);
  assert.match(confirmations[1],/^確定把「FA 助理」的新對話設定改成下列內容？\n/);
  const selects=ui.calls.filter(call=>call.url==="/api/agent-template/select");
  assert.equal(selects.length,1);
  assert.deepEqual(JSON.parse(selects[0].body).template.skills.map(ref=>ref.name),["new-guide"]);
  assert.deepEqual(JSON.parse(selects[0].body).template.unavailable_skills,[]);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/decisions")||call.url.endsWith("/deactivations")).length,2);
});

test("skill review: a failed read at replace step 3 stays on the Team page, where 繼續取代 resumes without resending",async()=>{
  const base=settingsBase();
  base.skills=[{name:"old-guide",version_id:"1".repeat(32),sha256:"e".repeat(64),metadata_sha256:"f".repeat(64)}];
  base.settings.capabilities=[{entry_id:"fa-skills-v1",entry_sha256:"6".repeat(64)}];
  const ui=await openReviews(settingsOptions(),base);
  reviewButton(reviewRow(ui,"新指南"),"查看內容").events.click();await settleReviews(ui);
  ui.holder.templateResponses["/api/agent-template/options"]={ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})};
  reviewButton(reviewRow(ui,"新指南"),"批准並取代").events.click();await ui.answer(true);await settleReviews(ui);
  const approval=JSON.parse(ui.calls.find(call=>call.url.endsWith("new-guide/decisions")).body);
  ui.holder.pending({artifact:skillDoc("new-guide","新指南",{approval:skillApproval(approval.decision_id),effective:true}),
    etag:"etag-2",decision_id:approval.decision_id,original_request_resent:false});
  await settleReviews(ui);
  const deactivation=JSON.parse(ui.calls.find(call=>call.url.endsWith("old-guide/deactivations")).body);
  const reloaded=copy(base);reloaded.skills=[];reloaded.unavailable_skills=["old-guide"];
  ui.holder.templateResponses["/api/agent-template"]=reply(reloaded);
  ui.holder.pending({artifact:skillDoc("old-guide","舊指南",{approval:skillApproval("0".repeat(32)),
    deactivations:[{...deactivation,etag:undefined,deactivated_by_oid:"owner",deactivated_at:when,applied_revision:2}]}),
    etag:"etag-3",decision_id:deactivation.decision_id,original_request_resent:false});
  await settleReviews(ui,12);
  assert.equal(ui.text("settings-title"),"Agent Team 設定");
  assert.equal(ui.nodes.get("settings-tab-team-skills").getAttribute("aria-selected"),"true");
  assert.match(ui.text("settings-skill-review-note"),/取代「舊指南」停在第 3 步；按「繼續取代」/);
  const resume=reviewRows(ui).map(row=>reviewButton(row,"繼續取代")).find(Boolean);
  assert.ok(resume,"繼續取代 is on the page the owner is looking at");
  const after=settingsOptions();after.skills=[{name:"new-guide",version_id:"1".repeat(32),sha256:"e".repeat(64),metadata_sha256:"f".repeat(64)}];
  ui.holder.templateResponses["/api/agent-template/options"]=reply(after);
  resume.events.click();await settleReviews(ui,12);
  assert.equal(ui.text("settings-title"),"FA 助理 的設定");
  assert.match(ui.text("settings-note"),/已批准「新指南」並停用「舊指南」。下方已把舊的換成新的/);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/decisions")||call.url.endsWith("/deactivations")).length,2,
    "resuming step 3 resends neither decision");
});

test("skill review: replace refuses to approve a newer version than the one the owner viewed",async()=>{
  const ui=await openReviews();
  reviewButton(reviewRow(ui,"新指南"),"查看內容").events.click();await settleReviews(ui);
  const newer=skillDoc("new-guide","新指南");
  newer.versions.push({...skillVersion(),version_id:"2".repeat(32),number:2,sha256:"9".repeat(64)});
  ui.holder.templateResponses["/api/team-skills/skills/new-guide"]=reply({artifact:newer,etag:"etag-new-2"});
  ui.holder.templateResponses["/api/team-skills/skills/new-guide/versions/"+"2".repeat(32)]=
    reply({version:{...skillVersion(),version_id:"2".repeat(32),number:2,sha256:"9".repeat(64)},text:"# 新指南 v2"});
  reviewButton(reviewRow(ui,"新指南"),"批准並取代").events.click();await settleReviews(ui);
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"no confirmation may be asked");
  assert.equal(ui.calls.some(call=>call.url.endsWith("/decisions")||call.url.endsWith("/deactivations")),false);
  assert.equal(ui.text("settings-skill-review-note"),"這份 skill 已有新的草稿版本；請重新查看內容後再決定。");
  assert.ok(reviewRow(ui,"新指南").children.some(node=>node.textContent==="# 新指南 v2"));
});

test("settings: a selection the API rejected before writing shows the truth and no reconcile row",async()=>{
  const ui=await openSettingsPage();
  ui.click("settings-tab-mcp");settingsSwitch(ui,"WebIQ",true);
  ui.holder.templateResponses["/api/agent-template/inspect"]={ok:true,json:async()=>({...inspectedTemplate(),template_sha256:"a".repeat(64)})};
  let refuse;
  ui.holder.templateResponses["/api/agent-template/select"]=new Promise(resolve=>{refuse=resolve});
  ui.click("settings-apply");await ui.answer(true);await ui.tick();
  const command=JSON.parse(ui.calls.find(call=>call.url==="/api/agent-template/select").body);
  const rejected={...selectedTemplateResponse(command,{state:"UNKNOWN"}).selections[0],state:"REJECTED",code:"PROFILE_SELECTION_CONFLICT"};
  ui.holder.current.template_selections=[rejected];
  ui.holder.templateResponses["/api/agent-template/selection"]=selectionRead(settingsCurrent,[rejected]);
  refuse({ok:false,json:async()=>({error:{code:"PROFILE_SELECTION_CONFLICT"}})});
  for(let n=0;n<6;n++)await ui.tick();
  assert.equal(ui.text("settings-note"),"這位助理的新對話設定剛被改過，這次沒有套用。已讀回最新設定，請確認後重新選擇。");
  assert.equal(ui.nodes.get("settings-selections").children.length,0);
  assert.equal(ui.text("settings-changes"),"沒有變更。");
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/select").length,1);
  const data=templateData();data.template_selections=[rejected];
  const diagnostics=await open(data);
  const record=diagnostics.nodes.get("template-selections").children[0];
  assert.match(record.children[0].textContent,/寫入前拒絕了這次選擇，沒有生效；不需要核對/);
  assert.equal(record.children.some(node=>node.textContent==="核對這筆原選擇回條"),false);
});

// Q4: the owner writes a skill draft in the Skills tab; the API keeps it unapproved and the same content maps to one draft.
const ownerEntry=(status="awaiting_decision")=>({...reviewEntry("owner-draft","收據指南",status),owner:"team-owner",
  native_source:{kind:"team-owner",tenant_id:"t",team_id:"team",owner_oid:"owner",submit_sha256:"a".repeat(64)}});
function fillSkillNew(ui,{title="收據指南",purpose="整理收據時使用",body="# 收據指南\n步驟一"}={}){
  for(const [id,value] of [["skill-new-title",title],["skill-new-purpose",purpose],["skill-new-body",body]]){
    ui.nodes.get(id).value=value;ui.nodes.get(id).events.input();
  }
}
const draftPosts=ui=>ui.calls.filter(call=>call.url==="/api/team-skills/drafts");

test("owner skill draft: one confirmed save, then the list shows the owner's draft and opens it for review",async()=>{
  const ui=await openReviews();
  assert.equal(ui.nodes.get("skill-new-save").disabled,true,"an empty form cannot be saved");
  fillSkillNew(ui);
  assert.equal(ui.nodes.get("skill-new-save").disabled,false);
  ui.click("skill-new-save");ui.click("skill-new-save");
  const question=await ui.answer(true);await settleReviews(ui);
  assert.equal(draftPosts(ui).length,1);
  assert.match(question,/只建立未批准的草稿，不會生效.*相同內容只會建立一次/s);
  assert.deepEqual(JSON.parse(draftPosts(ui)[0].body),{schema_version:"fa-team-owner-skill-draft-v1",title:"收據指南",
    arguments:{purpose:"整理收據時使用",body:"# 收據指南\n步驟一"}});
  assert.equal(ui.text("skill-new-note"),"正在保存草稿一次；回應不明時，用相同內容再送只會對到同一份草稿。");
  Object.assign(ui.holder.templateResponses,{
    "/api/team-skills/reviews":reply({schema_version:"fa-skill-review-list-v1",team:"team",after:null,next_after:null,
      entries:[ownerEntry(),reviewEntry("old-guide","舊指南","effective")]}),
    "/api/team-skills/skills/owner-draft":reply({artifact:{...skillDoc("owner-draft","收據指南"),owner:"team-owner"},etag:"etag-owner"}),
    ["/api/team-skills/skills/owner-draft/versions/"+"1".repeat(32)]:reply({version:skillVersion(),text:"# 收據指南\n步驟一"}),
  });
  ui.holder.pending({entry:ownerEntry(),original_request_resent:false});
  await settleReviews(ui,12);
  assert.equal(ui.text("skill-new-note"),"已保存草稿「收據指南」，待審核。請在下方查看內容後決定是否批准。");
  assert.equal(ui.nodes.get("skill-new-title").value,"");
  assert.equal(ui.nodes.get("skill-new-body").value,"");
  const row=reviewRow(ui,"收據指南");
  assert.match(row.children[1].textContent,/起草：你（Team 主人）/);
  assert.ok(row.children.some(node=>node.textContent==="# 收據指南\n步驟一"),"the new draft is opened for review");
  assert.equal(reviewButton(row,"批准").disabled,false);
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/decisions")).length,0,"saving a draft never approves it");
});

test("owner skill draft: a lost reply keeps the form and resending sends the identical content",async()=>{
  const ui=await openReviews();
  fillSkillNew(ui);
  ui.holder.templateResponses["/api/team-skills/drafts"]={ok:false,json:async()=>({error:{code:"SKILL_DRAFT_OUTCOME_UNKNOWN"}})};
  ui.click("skill-new-save");await ui.answer(true);await settleReviews(ui);
  assert.match(ui.text("skill-new-note"),/草稿保存結果未確認；內容還在表單裡.*不會另建/);
  assert.equal(ui.nodes.get("skill-new-title").value,"收據指南");
  ui.holder.templateResponses["/api/team-skills/drafts"]={ok:false,json:async()=>({error:{code:"LOCAL_OFFLINE"}})};
  ui.click("skill-new-save");await ui.answer(true);await settleReviews(ui);
  assert.match(ui.text("skill-new-note"),/表單內容保留；相同內容再送一次只會對到同一份草稿/);
  ui.holder.templateResponses["/api/team-skills/drafts"]=reply({entry:ownerEntry("effective"),original_request_resent:false});
  ui.click("skill-new-save");await ui.answer(true);await settleReviews(ui);
  const bodies=draftPosts(ui).map(call=>call.body);
  assert.equal(bodies.length,3);
  assert.equal(new Set(bodies).size,1,"every resend carries exactly the same content");
  assert.match(ui.text("skill-new-note"),/相同內容先前已建立過「收據指南」（目前：生效中），沒有另建/);
});

test("owner skill draft: invalid fields, files and a dismissed confirmation send nothing",async()=>{
  const ui=await openReviews();
  fillSkillNew(ui,{title:"兩\n行"});
  assert.equal(ui.nodes.get("skill-new-save").disabled,true);
  assert.equal(ui.text("skill-new-note"),"還需填好：名稱。");
  fillSkillNew(ui,{purpose:"x".repeat(513)});
  assert.equal(ui.text("skill-new-note"),"還需填好：用途。");
  fillSkillNew(ui);
  const files=ui.nodes.get("skill-new-files"),pick=(name,text)=>({name,size:Buffer.byteLength(text),text:async()=>text});
  files.files=[pick("a.md","1"),pick("b.md","2"),pick("c.md","3"),pick("d.md","4"),pick("e.md","5")];
  await files.events.change();
  assert.equal(ui.text("skill-new-files-note"),"附檔最多 4 個；請重新選擇。");
  assert.equal(ui.nodes.get("skill-new-save").disabled,true);
  files.files=[pick("../x.md","1")];await files.events.change();
  assert.match(ui.text("skill-new-files-note"),/只收 \.md／\.txt／\.json/);
  files.files=[pick("notes.exe","1")];await files.events.change();
  assert.match(ui.text("skill-new-files-note"),/不能用/);
  files.files=[pick("big.md","測".repeat(2731))];await files.events.change();
  assert.match(ui.text("skill-new-files-note"),/「big\.md」.*8 KiB/,"the limit is 8192 UTF-8 bytes, not characters");
  files.files=[pick("empty.md","  ")];await files.events.change();
  assert.match(ui.text("skill-new-files-note"),/是空的或超過 8 KiB/);
  files.files=[pick("fits.md","測".repeat(2730))];await files.events.change();
  assert.equal(ui.text("skill-new-files-note"),"已選 1 個附檔：fits.md");
  files.files=[pick("notes.md","附檔內容"),pick("data.json","{}")];await files.events.change();
  assert.equal(ui.text("skill-new-files-note"),"已選 2 個附檔：notes.md、data.json");
  assert.equal(ui.nodes.get("skill-new-save").disabled,false);
  ui.click("skill-new-save");await ui.answer(false);await settleReviews(ui);
  assert.equal(draftPosts(ui).length,0);
  ui.click("skill-new-save");await ui.answer(true);await settleReviews(ui);
  assert.deepEqual(JSON.parse(draftPosts(ui)[0].body).arguments.resources,[
    {name:"notes.md",media_type:"text/markdown",text:"附檔內容"},{name:"data.json",media_type:"application/json",text:"{}"}]);
});

// Team MCP servers in the Team's own Foundry toolbox: the default version is the record; one request ID per change,
// resent only as the same body when its outcome is unknown.
const teamMcpServer=(label="Docs",version="v1",approval="always")=>({server_label:label,
  server_url:"https://mcp.example.com/"+label.toLowerCase(),allowed_tools:["read","search"],require_approval:approval,version});
const teamMcpView=({version=null,servers=[],other=0,create=false,manage=true}={})=>({schema_version:"fa-team-mcp-v2",
  team_id:"team",toolbox:"fa-team-team",default_version:version,single_team:true,servers,other_tools:other,
  hidden_connections:0,can_create_connection:create,can_manage:manage});
const teamMcpConnections={schema_version:"fa-team-mcp-connections-v2",single_team:false,hidden_secret_connections:2,
  connections:[{name:"public-mi",connection_type:"RemoteTool",credential_type:"ProjectManagedIdentity",team_owned:false}]};
async function openTeamMcp(view=teamMcpView()){
  const ui=await settingsUi();
  let current=copy(view);
  Object.defineProperty(ui.holder.templateResponses,"/api/team-mcp/servers",{configurable:true,enumerable:true,
    get:()=>reply(current)});
  ui.holder.templateResponses["/api/team-mcp/connections"]=reply(teamMcpConnections);
  ui.setTeamMcp=value=>{current=copy(value)};
  return openTeamSettings(ui);
}
const teamMcpRows=ui=>ui.nodes.get("team-mcp-list").children;
const teamMcpPosts=ui=>ui.calls.filter(call=>call.method==="POST"&&call.url.startsWith("/api/team-mcp/"));
const teamMcpAction=(ui,row,label)=>teamMcpRows(ui)[row].children[2].children.find(node=>node.textContent===label);
function fillTeamMcp(ui,{label="Docs",url="https://mcp.example.com/docs",tools="search, read"}={}){
  for(const [id,value] of [["team-mcp-label",label],["team-mcp-url",url],["team-mcp-tools",tools]]){
    ui.nodes.get(id).value=value;ui.nodes.get(id).events.input();
  }
}

test("team MCP: one confirmed add asks every call by default and reloads the Agent options",async()=>{
  const ui=await openTeamMcp();
  assert.equal(ui.nodes.get("settings-team-mcp").hidden,false);
  assert.equal(ui.text("team-mcp-note"),"Team 目前沒有自己的 MCP。");
  assert.deepEqual(ui.nodes.get("team-mcp-connection").children.map(node=>node.textContent),
    ["（不需要驗證）","public-mi（ProjectManagedIdentity）"]);
  assert.match(ui.text("team-mcp-connection-note"),/只能選既有的 connection.*另有 2 個帶金鑰的 connection 沒有列出/);
  assert.equal(ui.nodes.get("team-mcp-approval").value,"always");
  assert.equal(ui.nodes.get("team-mcp-save").disabled,true);
  fillTeamMcp(ui,{url:"http://insecure.example.com"});
  assert.equal(ui.text("team-mcp-form-note"),"還需填好：網址。");
  fillTeamMcp(ui);
  assert.equal(ui.nodes.get("team-mcp-save").disabled,false);
  const optionReads=()=>ui.calls.filter(call=>call.url==="/api/agent-template/options").length,before=optionReads();
  ui.click("team-mcp-save");ui.click("team-mcp-save");ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.equal(teamMcpPosts(ui).length,1);
  assert.match(ui.holder.confirmation,/新增 MCP「Docs」？\n呼叫前：每次呼叫都要我批准。.*之後要到每位助理的設定（助理清單右側的 ⚙）→ MCP 打開並套用/s);
  const command=JSON.parse(teamMcpPosts(ui)[0].body);
  assert.match(command.request_id,/^[a-f0-9]{32}$/);
  assert.deepEqual({...command,request_id:null},{schema_version:"fa-team-mcp-change-v2",request_id:null,
    expected_version:null,operation:"add",server_label:"Docs",server:{server_label:"Docs",
      server_url:"https://mcp.example.com/docs",project_connection_id:null,allowed_tools:["read","search"],
      require_approval:"always"}});
  const applied=teamMcpView({version:"v1",servers:[teamMcpServer()]});ui.setTeamMcp(applied);
  ui.holder.pending({schema_version:"fa-team-mcp-change-result-v2",request_id:command.request_id,state:"applied",
    toolbox_version:"v1",view:applied,original_request_resent:false});
  await settleReviews(ui,12);
  assert.match(ui.text("team-mcp-note"),/已新增「Docs」；要給助理用，請到那位助理的設定（助理清單右側的 ⚙）→ MCP 打開並套用到新對話（toolbox 版本 v1）。/);
  assert.match(teamMcpRows(ui)[0].children[0].textContent,/Docs · https:\/\/mcp\.example\.com\/docs/);
  assert.match(teamMcpRows(ui)[0].children[1].textContent,/呼叫前：每次呼叫都要我批准 · 版本 v1/);
  assert.equal(ui.nodes.get("team-mcp-label").value,"");
  assert.ok(optionReads()>before,"the Agent options are read again after the change");
  assert.equal(teamMcpPosts(ui).length,1);
});

test("team MCP: an unknown disable outcome is resent once as the same body and request ID",async()=>{
  const ui=await openTeamMcp(teamMcpView({version:"v2",servers:[teamMcpServer("Docs","v2"),teamMcpServer("Wiki","v2")]}));
  ui.holder.templateResponses["/api/team-mcp/changes"]={ok:false,json:async()=>({error:{code:"TEAM_MCP_OUTCOME_UNKNOWN"}})};
  teamMcpAction(ui,0,"停用").events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.match(ui.holder.confirmation,/停用後，所有對話（含進行中的）從下一回合起都不能再用這個工具；舊紀錄保留，不重送。/);
  const first=JSON.parse(teamMcpPosts(ui)[0].body);
  assert.deepEqual({...first,request_id:null},{schema_version:"fa-team-mcp-change-v2",request_id:null,
    expected_version:"v2",operation:"disable",server_label:"Docs",server:null});
  assert.equal(ui.text("team-mcp-note"),"MCP 變更結果未確認；請按「用原編號重送」，不會建立第二個版本。");
  assert.equal(ui.nodes.get("team-mcp-retry").hidden,false);
  fillTeamMcp(ui,{label:"Other"});
  assert.equal(ui.nodes.get("team-mcp-save").disabled,true,"no new change while one is unconfirmed");
  assert.equal(teamMcpAction(ui,1,"修改").disabled,true);assert.equal(teamMcpAction(ui,1,"停用").disabled,true);
  const done=teamMcpView({version:"v3",servers:[teamMcpServer("Wiki","v3")]});
  ui.holder.templateResponses["/api/team-mcp/changes"]=reply({schema_version:"fa-team-mcp-change-result-v2",
    request_id:first.request_id,state:"applied",toolbox_version:"v3",view:done,original_request_resent:false});
  ui.setTeamMcp(done);
  ui.click("team-mcp-retry");
  assert.match(await ui.answer(true),/^用原編號重送上一個變更（Docs）？/);await settleReviews(ui,12);
  const posts=teamMcpPosts(ui);
  assert.equal(posts.length,2);
  assert.equal(posts[1].body,posts[0].body,"the resend is the original body with its request ID");
  assert.match(ui.text("team-mcp-note"),/已停用「Docs」（toolbox 版本 v3）/);
  assert.equal(ui.nodes.get("team-mcp-retry").hidden,true);
});

test("team MCP: the Team's last server cannot be disabled and says what to do instead",async()=>{
  const ui=await openTeamMcp(teamMcpView({version:"v4",servers:[teamMcpServer("Docs","v4")]}));
  assert.equal(teamMcpAction(ui,0,"停用").disabled,true);
  assert.match(teamMcpRows(ui)[0].children[3].textContent,/最後一個 MCP，不能停用.*請先新增另一個 MCP，再停用這個/);
  ui.setTeamMcp(teamMcpView({version:"v4",servers:[teamMcpServer("Docs","v4")],other:1}));
  ui.click("team-mcp-read");await settleReviews(ui);
  assert.equal(teamMcpAction(ui,0,"停用").disabled,false,"another tool keeps the version non-empty");
  assert.match(teamMcpRows(ui)[1].textContent,/另有 1 個不是由這裡管理的工具；這裡不顯示也不修改/);
  assert.equal(teamMcpPosts(ui).length,0);
});

test("team MCP: a new connection key is sent once in the change body and never kept or shown",async()=>{
  const key="OFFLINE-team-key-2c41";
  const ui=await openTeamMcp(teamMcpView({version:"v1",servers:[teamMcpServer("Wiki")],create:true}));
  assert.ok(ui.nodes.get("team-mcp-connection").children.some(node=>node.textContent==="建立新的 connection（輸入金鑰）"));
  assert.equal(ui.nodes.get("team-mcp-new-connection").hidden,true);
  fillTeamMcp(ui);
  ui.nodes.get("team-mcp-connection").value="__new__";ui.nodes.get("team-mcp-connection").events.change();
  assert.equal(ui.nodes.get("team-mcp-new-connection").hidden,false);
  assert.equal(ui.text("team-mcp-form-note"),"還需填好：金鑰。");
  ui.nodes.get("team-mcp-secret").value=key;ui.nodes.get("team-mcp-secret").events.input();
  ui.click("team-mcp-save");ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.match(ui.holder.confirmation,/金鑰只送給 Foundry/);assert.doesNotMatch(ui.holder.confirmation,new RegExp(key));
  assert.equal(ui.nodes.get("team-mcp-secret").value,"","the key field is cleared as soon as it is sent");
  const command=JSON.parse(teamMcpPosts(ui)[0].body);
  assert.deepEqual(command.new_connection,{header_name:"x-apikey",secret:key});
  assert.equal(command.server.project_connection_id,null);
  const applied=teamMcpView({version:"v2",servers:[teamMcpServer("Docs","v2"),teamMcpServer("Wiki","v2")],create:true});
  ui.setTeamMcp(applied);
  ui.holder.pending({schema_version:"fa-team-mcp-change-result-v2",request_id:command.request_id,state:"applied",
    toolbox_version:"v2",connection:"t0123456789-Docs",view:applied,original_request_resent:false});
  await settleReviews(ui,12);
  assert.match(ui.text("team-mcp-note"),/connection t0123456789-Docs/);
  assert.doesNotMatch(JSON.stringify(ui.nodes.get("settings-team-mcp")),new RegExp(key));
});

test("team MCP: an environment without the routes hides the form and sends nothing",async()=>{
  const ui=await settingsUi();
  ui.holder.templateResponses["/api/team-mcp/servers"]={ok:false,json:async()=>({error:{code:"TEAM_MCP_UNAVAILABLE"}})};
  await openTeamSettings(ui);
  assert.equal(ui.text("team-mcp-note"),"這個環境還沒開放從設定頁管理 MCP；新增 MCP 請找部署線。");
  assert.equal(ui.nodes.get("team-mcp-form").hidden,true);
  ui.click("settings-tab-team-skills");ui.click("settings-tab-team-mcp");await settleReviews(ui);
  assert.equal(ui.calls.filter(call=>call.url==="/api/team-mcp/servers").length,1,"unavailable is not re-asked on every render");
  assert.equal(teamMcpPosts(ui).length,0);
  ui.holder.templateResponses["/api/team-mcp/servers"]=reply(teamMcpView());
  ui.holder.templateResponses["/api/team-mcp/connections"]=reply(teamMcpConnections);
  ui.click("team-mcp-read");await settleReviews(ui);
  assert.equal(ui.text("team-mcp-note"),"Team 目前沒有自己的 MCP。","a reread clears the earlier unavailable state");
  assert.equal(ui.nodes.get("team-mcp-form").hidden,false);
});

test("team MCP: only the Team owner sees the section, and only a login that may change it sees the form",async()=>{
  const ui=await settingsUi();
  ui.holder.templateResponses["/api/team-mcp/servers"]={ok:false,json:async()=>({error:{code:"TEAM_MCP_OWNER_ONLY"}})};
  await openTeamSettings(ui);
  assert.equal(ui.nodes.get("settings-team-mcp").hidden,true);
  assert.match(ui.text("settings-agent"),/Team MCP 只有 Team 主人看得到；這個登入看不到。$/,"the tab says why it is empty");
  ui.click("settings-tab-team-skills");ui.click("settings-tab-team-mcp");await settleReviews(ui);
  assert.equal(ui.calls.filter(call=>call.url==="/api/team-mcp/servers").length,1);
  const reader=await openTeamMcp(teamMcpView({version:"v1",servers:[teamMcpServer(),teamMcpServer("Wiki")],manage:false}));
  assert.equal(reader.nodes.get("team-mcp-form").hidden,true);
  assert.equal(teamMcpRows(reader)[0].children.length,2,"no change buttons for a read-only login");
  assert.equal(teamMcpPosts(reader).length,0);
});

test("team MCP: editing keeps the server's own connection and its approval setting",async()=>{
  const server={...teamMcpServer("Docs","v3","never"),project_connection_id:"old-key"};
  const ui=await openTeamMcp(teamMcpView({version:"v3",servers:[server,teamMcpServer("Wiki","v3")]}));
  teamMcpAction(ui,0,"修改").events.click();
  assert.equal(ui.nodes.get("team-mcp-connection").value,"old-key");
  assert.match(ui.nodes.get("team-mcp-connection").children.at(-1).textContent,/old-key（目前設定，未在清單中）/);
  assert.equal(ui.nodes.get("team-mcp-approval").value,"never");
  assert.equal(ui.nodes.get("team-mcp-label").disabled,true);
  ui.click("team-mcp-save");ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.match(ui.holder.confirmation,/修改 MCP「Docs」？\n呼叫前：不用批准。所有對話（含進行中的）從下一回合起改用新設定/);
  const command=JSON.parse(teamMcpPosts(ui)[0].body);
  assert.equal(command.operation,"modify");assert.equal(command.server.project_connection_id,"old-key");
  assert.equal(command.server.require_approval,"never");assert.equal(command.expected_version,"v3");
});

test("Agent Team 設定 holds only the Team tabs, reads no Agent settings and names the Agent it goes through",async()=>{
  const ui=await settingsUi();
  assert.match(html,/<summary id="settings-button" aria-label="Agent Team 設定" title="Agent Team 設定">⚙<\/summary>/);
  assert.match(html,/<button id="open-settings">Agent Team 設定<\/button>/);
  Object.assign(ui.holder.templateResponses,{"/api/team-mcp/servers":reply(teamMcpView()),
    "/api/team-mcp/connections":reply(teamMcpConnections)});
  skillReviewResponses(ui);
  await openTeamSettings(ui);
  assert.equal(ui.text("settings-title"),"Agent Team 設定");
  assert.equal(ui.nodes.get("settings-page").getAttribute("aria-label"),"Agent Team 設定");
  assert.deepEqual(shownTabs(ui),TEAM_TABS,"no Agent tab on the Team page");
  assert.equal(ui.nodes.get("settings-tab-team-mcp").getAttribute("aria-selected"),"true");
  assert.match(ui.text("settings-agent"),/^整個 Team 共用，不屬於任何一位助理。這頁經由目前選中的助理「FA 助理」讀寫你的 Team。$/);
  assert.equal(ui.nodes.get("settings-catalog").hidden,true);
  assert.equal(ui.nodes.get("settings-team-mcp").hidden,false);assert.equal(ui.nodes.get("settings-skill-reviews").hidden,true);
  ui.click("settings-tab-team-skills");await settleReviews(ui);
  assert.equal(ui.nodes.get("settings-skill-reviews").hidden,false);assert.equal(ui.nodes.get("settings-team-mcp").hidden,true);
  assert.equal(ui.nodes.get("settings-catalog").hidden,true);
  ui.click("settings-tab-rules");
  assert.equal(ui.nodes.get("settings-rules").hidden,false);
  assert.equal(ui.nodes.get("settings-skill-reviews").hidden,true);assert.equal(ui.nodes.get("settings-team-mcp").hidden,true);
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/agent-template")),false,"the Team page reads no Agent settings");
  assert.equal(ui.calls.filter(call=>call.url==="/api/team-mcp/servers").length,1);
  assert.equal(ui.calls.filter(call=>call.url==="/api/team-skills/reviews").length,1);
  ui.click("close-settings");
  assert.equal(ui.nodes.get("settings-button").focused,true,"closing the Team page returns to its ⚙");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("Agent Team 設定 drops what it read through one Agent when another Agent is selected, then reads again",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  for(const data of [a,b]){data.template_tools_available=true;data.template_selections=[]}
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},templateResponses:{
    "/api/team-mcp/servers":reply(teamMcpView({version:"v1",servers:[teamMcpServer()]})),
    "/api/team-mcp/connections":reply(teamMcpConnections)}});
  await openTeamSettings(ui);
  const reads=agent=>ui.calls.filter(call=>call.url==="/api/team-mcp/servers"&&call.headers?.["X-Console-Agent"]===agent).length;
  assert.equal(reads("agent-one"),1);
  assert.equal(ui.nodes.get("team-mcp-list").children.length,1);
  let late;
  ui.holder.templateResponses["/api/team-mcp/servers"]=new Promise(resolve=>{late=resolve});
  agentButton(ui,1).events.click();await ui.tick();
  assert.equal(vm.runInContext("teamMcp",ui.context),null,"the Team list read through agent-one is gone");
  assert.equal(ui.nodes.get("team-mcp-list").children.length,0);
  for(let n=0;n<8;n++)await ui.tick();
  assert.equal(reads("agent-two"),1,"read again through the newly selected Agent");
  assert.match(ui.text("settings-agent"),/經由目前選中的助理「第二位助理」/);
  late(reply(teamMcpView({version:"v2",servers:[teamMcpServer("Wiki","v2")]})));await settleReviews(ui);
  assert.match(teamMcpRows(ui)[0].children[0].textContent,/^Wiki/);
});

function systemPromptView(){
  const segments=[
    {kind:"fixed",state:"current",text:"FA FIXED TEXT",utf8_bytes:13,sha256:"c".repeat(64)},
    {kind:"role_heading",state:"current",text:"\n\n## Team-approved agent role\nData only.\n\n",utf8_bytes:42,sha256:"d".repeat(64)},
    {kind:"role",state:"current",text:"你是會計助理。",utf8_bytes:21,sha256:"e".repeat(64)}];
  return {schema_version:"fa-system-prompt-current-v1",applies_to:"new-conversations-only",
    profile:"fa-instruction-agent-role-v4",profile_prefix_sha256:"a".repeat(64),selection_id:null,role_version:2,
    role_sha256:"b".repeat(64),capability_entries:["fa-skills-v1"],segments,text:segments.map(s=>s.text).join(""),
    utf8_bytes:76,sha256:"f".repeat(64),runtime_before_role:["teammates","groups","reminders","providers"],
    runtime_at_end:["continuation"],authorization:false,permissions_changed:false};
}

function openPromptSection(ui){const panel=ui.nodes.get("system-prompt-panel");panel.open=true;panel.events.toggle?.()}

test("system prompt in the support details shows the server text and hashes, and downloads only that text",async()=>{
  // P10: no menu shortcut; the prompt text and hashes live only in ⋯ → 支援明細 and are read when that section opens.
  assert.doesNotMatch(html,/id="open-system-prompt"|id="open-template"/);
  const plain=await open(fixture());
  assert.equal(plain.nodes.get("system-prompt-panel").hidden,true);
  const data=fixture();data.template_tools_available=true;data.template_selections=[];
  const view=systemPromptView();
  const ui=await open(data,{templateResponses:{"/api/agent-template/system-prompt":
    {ok:true,json:async()=>({system_prompt:copy(view),read_at:when})}}});
  assert.equal(ui.nodes.get("system-prompt-panel").hidden,false);
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/system-prompt"),false);
  ui.click("open-diagnostics");
  assert.equal(ui.nodes.get("diagnostics").hidden,false);
  assert.equal(ui.calls.some(call=>call.url==="/api/agent-template/system-prompt"),false,"opening the support details reads nothing");
  openPromptSection(ui);await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/system-prompt").length,1);
  const segments=ui.nodes.get("system-prompt-segments").children;
  assert.equal(segments.length,3);
  assert.match(segments[0].children[0].textContent,/FA 固定指示（fa-instruction-agent-role-v4）：13 bytes；SHA-256 c{64}/);
  assert.equal(segments[0].children[1].textContent,"FA FIXED TEXT");
  assert.match(segments[2].children[0].textContent,/Team 核准的角色說明原文（第 2 版）/);
  assert.equal(segments[2].children[1].textContent,"你是會計助理。");
  assert.match(ui.text("system-prompt-summary"),/全文：76 UTF-8 bytes；SHA-256 f{64}/);
  assert.match(ui.text("system-prompt-summary"),/角色說明之前加入：同事名冊與傳話說明、群組討論說明、工作提醒、外掛工具說明/);
  assert.match(ui.text("system-prompt-summary"),/在最後加入：接續原工作說明。/);
  assert.match(ui.text("system-prompt-note"),/只適用之後開的新對話/);
  openPromptSection(ui);await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url==="/api/agent-template/system-prompt").length,1);
  ui.click("download-system-prompt");
  assert.equal(await ui.holder.downloaded.text(),view.text);
  assert.equal(ui.nodes.get("system-prompt-download").download,"fa-system-prompt-fa-instruction-agent-role-v4.txt");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("system prompt refusals show no text and never fall back to a local copy",async()=>{
  for(const [code,pattern] of [["NATIVE_TURNS_DISABLED",/沒有開啟原生回合/],
      ["SYSTEM_PROMPT_UNRECONSTRUCTABLE",/不拿其他版本代替/],["SYSTEM_PROMPT_UNAVAILABLE",/API 版本較舊/]]){
    const data=fixture();data.template_tools_available=true;data.template_selections=[];
    const ui=await open(data,{templateResponses:{"/api/agent-template/system-prompt":
      {ok:false,json:async()=>({error:{code}})}}});
    openPromptSection(ui);await ui.tick();await ui.tick();
    assert.match(ui.text("system-prompt-note"),pattern);
    assert.equal(ui.nodes.get("system-prompt-segments").children.length,0);
    assert.equal(ui.text("system-prompt-summary"),"");
    assert.equal(ui.nodes.get("download-system-prompt").disabled,true);
  }
  const data=fixture();data.template_tools_available=true;data.template_selections=[];
  const granted={...systemPromptView(),authorization:true};
  const ui=await open(data,{templateResponses:{"/api/agent-template/system-prompt":
    {ok:true,json:async()=>({system_prompt:granted,read_at:when})}}});
  openPromptSection(ui);await ui.tick();await ui.tick();
  assert.match(ui.text("system-prompt-note"),/SHA-256 核對不符/);
  assert.equal(ui.nodes.get("system-prompt-segments").children.length,0);
});

test("a native turn's system prompt shows only hash-checked text and says when it cannot be rebuilt",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.mode="native_turn";task.actions["read-system-prompt"]=action(true);
  const path=`/api/instructions/${task.task_id}/system-prompt`;
  const base={schema_version:"fa-system-prompt-turn-v1",agent_id:"agent-a",request_id:task.task_id,
    conversation_id:"cancel-conversation",turn_number:4,profile:"fa-instruction-agent-role-v4",
    profile_prefix_sha256:"a".repeat(64),role_version:2,role_sha256:"b".repeat(64),capability_entries:[],
    authorization:false};
  const blocked={...base,state:"unreconstructable",reasons:["turn_sections_not_saved"],saved_utf8_bytes:9000,
    saved_sha256:"9".repeat(64),text:null,utf8_bytes:null,sha256:null,
    runtime_sections:[{name:"teammates",state:"included",utf8_bytes:300,sha256:"8".repeat(64)}],
    segments:[{kind:"fixed",state:"verified",text:"FA FIXED TEXT",utf8_bytes:13,sha256:"c".repeat(64)},
      {kind:"role_heading",state:"unverified"},
      {kind:"role",state:"verified",text:"你是會計助理。",utf8_bytes:21,sha256:"e".repeat(64)}]};
  const ui=await open(data,{templateResponses:{[path]:{ok:true,json:async()=>({task_id:task.task_id,
    system_prompt:copy(blocked),read_at:when})}}});
  assert.equal(ui.nodes.get("turn-context-panel").hidden,false);
  assert.equal(ui.nodes.get("read-turn-system-prompt").hidden,false);
  assert.equal(ui.nodes.get("read-turn-system-prompt").disabled,false);
  ui.click("read-turn-system-prompt");await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url===path).length,1);
  assert.match(ui.text("turn-system-prompt-note"),/無法重建這回合的全文；不拿目前版本代替/);
  assert.match(ui.text("turn-system-prompt-summary"),/FA 只保存大小和 SHA-256，沒有保存原文/);
  assert.match(ui.text("turn-system-prompt-summary"),/同事名冊與傳話說明：300 bytes；SHA-256 8{64}（只存 hash，沒有原文）/);
  const segments=ui.nodes.get("turn-system-prompt-segments").children;
  assert.equal(segments[1].children[1].textContent,"");
  assert.match(segments[1].children[0].textContent,/無法重建/);
  assert.equal(segments[2].children[1].textContent,"你是會計助理。");
  assert.equal(ui.nodes.get("download-turn-system-prompt").hidden,true);

  const rebuilt={...base,state:"rebuilt",reasons:[],saved_utf8_bytes:34,saved_sha256:"7".repeat(64),
    text:"FA FIXED TEXT你是會計助理。",utf8_bytes:34,sha256:"7".repeat(64),runtime_sections:[],
    segments:[{kind:"fixed",state:"verified",text:"FA FIXED TEXT",utf8_bytes:13,sha256:"c".repeat(64)},
      {kind:"role",state:"verified",text:"你是會計助理。",utf8_bytes:21,sha256:"e".repeat(64)}]};
  ui.holder.templateResponses[path]={ok:true,json:async()=>({task_id:task.task_id,system_prompt:copy(rebuilt),read_at:when})};
  ui.click("read-turn-system-prompt");await ui.tick();await ui.tick();
  assert.match(ui.text("turn-system-prompt-note"),/整段 SHA-256 與保存值一致/);
  assert.match(ui.text("turn-system-prompt-summary"),/全文：34 UTF-8 bytes；SHA-256 7{64}/);
  assert.equal(ui.nodes.get("download-turn-system-prompt").hidden,false);
  ui.click("download-turn-system-prompt");
  assert.equal(await ui.holder.downloaded.text(),rebuilt.text);
  assert.equal(ui.nodes.get("system-prompt-download").download,"fa-system-prompt-turn-4.txt");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// 2026-10-04 (Neal): workbench chat UI requests.
const treeOf=node=>typeof node==="string"?[]:[node,...(node.children||[]).flatMap(treeOf)];
const metaOf=node=>node.children.find(child=>child.className==="message-meta");
function markdownChat(text){
  const data=nativeChat("completed");data.tasks[0].reply.data.text=text;return data;
}

test("open menus close on an outside click or a chosen action, but not inside their nested pickers",async()=>{
  const ui=await open(),menu=ui.nodes.get("chat-menu"),settings=ui.nodes.get("settings-menu");
  const nested=["conversation-picker","conversation","hosted-session"].map(id=>ui.nodes.get(id));
  menu.children.push(ui.nodes.get("menu-button"),...nested,ui.nodes.get("new-conversation"),ui.nodes.get("open-diagnostics"));
  settings.children.push(ui.nodes.get("settings-button"),ui.nodes.get("open-settings"));
  const click=target=>ui.documentEvents.click({target});
  menu.open=true;
  for(const target of nested){click(target);assert.equal(menu.open,true,"nested picker keeps the menu open")}
  click(ui.nodes.get("chat-scroll"));
  assert.equal(menu.open,false,"a click outside closes the menu");
  menu.open=true;click(ui.nodes.get("open-diagnostics"));
  assert.equal(menu.open,false,"choosing an action closes the menu");
  menu.open=settings.open=true;click(ui.nodes.get("settings-button"));
  assert.equal(menu.open,false);
  assert.equal(settings.open,true,"a menu's own summary is left to the native toggle");
  click(ui.nodes.get("open-settings"));
  assert.equal(settings.open,false);
  menu.open=true;ui.context.document.activeElement=ui.nodes.get("conversation");
  ui.documentEvents.keydown({key:"Escape"});
  assert.equal(menu.open,false);
  assert.equal(ui.nodes.get("menu-button").focused,true,"Escape returns focus to the menu button");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("Agent replies render Markdown as DOM nodes: headings, bold, tables, links, lists, code, quotes and rules",async()=>{
  const source=["## 結果摘要","","**重點**：見 [官方文件](https://learn.microsoft.com/azure \"Learn\") 與 https://example.com/a_b。",
    "","| 項目 | 數量 |","|:--|--:|","| **CPU** | 4 |","| 記憶體 | 16 GiB |","",
    "1. 第一步","2. 第二步","   - 子項 `code`","","> 引用文字","","```bash","echo <b>hi</b>","```","","---","",
    "最後一段","第二行 ~~刪除~~ *斜體*"].join("\n");
  const ui=await open(markdownChat(source)),reply=turnNode(ui).children[3],nodes=treeOf(reply);
  const tags=new Set(nodes.map(node=>node.tagName));
  assert.equal(reply.className,"assistant-message markdown");
  for(const tag of ["H2","STRONG","A","TABLE","THEAD","TBODY","TH","TD","OL","UL","LI","CODE","BLOCKQUOTE","PRE","HR",
    "BR","DEL","EM"])assert.ok(tags.has(tag),tag);
  const [head,body]=nodes.filter(node=>node.tagName==="TABLE")[0].children;
  assert.deepEqual(head.children[0].children.map(shownText),["項目","數量"]);
  assert.deepEqual(head.children[0].children.map(cell=>cell.style.textAlign),["left","right"]);
  assert.deepEqual(body.children.map(row=>row.children.map(shownText)),[["CPU","4"],["記憶體","16 GiB"]]);
  assert.equal(body.children[0].children[0].children[0].tagName,"STRONG");
  const links=nodes.filter(node=>node.tagName==="A");
  assert.deepEqual(links.map(link=>[link.href,shownText(link),link.target,link.rel]),[
    ["https://learn.microsoft.com/azure","官方文件","_blank","noopener noreferrer"],
    ["https://example.com/a_b","https://example.com/a_b","_blank","noopener noreferrer"]]);
  assert.equal(links[0].title,"Learn");
  const ordered=nodes.find(node=>node.tagName==="OL");
  assert.equal(ordered.children.length,2);
  assert.equal(treeOf(ordered.children[1]).find(node=>node.tagName==="CODE").textContent,"code");
  assert.equal(nodes.find(node=>node.tagName==="PRE").children[0].textContent,"echo <b>hi</b>");
  assert.match(shownText(nodes.find(node=>node.tagName==="BLOCKQUOTE")),/引用文字/);
  assert.equal(ui.text("answer"),source,"the saved answer text itself is unchanged");
});

test("Markdown never runs or loads model HTML, and only http(s) links become anchors",async()=>{
  const evil=['<img src=x onerror="alert(1)">','<script>alert(2)</script>','[點我](javascript:alert(3))',
    '[資料](data:text/html;base64,PHNjcmlwdD4=)','![追蹤](https://evil.example/pixel.png)','[相對](/api/state)',
    '<a href="javascript:alert(4)">x</a>','<javascript:alert(5)>','<iframe src="https://evil.example"></iframe>'].join("\n\n");
  const ui=await open(markdownChat(evil)),reply=turnNode(ui).children[3],nodes=treeOf(reply);
  assert.deepEqual(nodes.filter(node=>["IMG","SCRIPT","IFRAME","OBJECT","EMBED","STYLE","LINK"].includes(node.tagName)),[]);
  const links=nodes.filter(node=>node.tagName==="A");
  assert.ok(links.every(link=>/^https:\/\/evil\.example/.test(link.href)&&link.rel==="noopener noreferrer"));
  assert.deepEqual(links.filter(link=>shownText(link).startsWith("圖片：")).map(link=>link.href),
    ["https://evil.example/pixel.png"],"a Markdown image is only a link and is never loaded");
  const shown=shownText(reply);
  for(const literal of ['<img src=x onerror="alert(1)">','<script>alert(2)</script>','[點我](javascript:alert(3))',
    '[資料](data:text/html;base64,PHNjcmlwdD4=)','[相對](/api/state)','<a href="javascript:alert(4)">x</a>',
    '<javascript:alert(5)>','<iframe src="https://evil.example"></iframe>'])assert.ok(shown.includes(literal),literal);
  assert.doesNotMatch(html,/\.innerHTML|outerHTML|insertAdjacentHTML|DOMParser|createContextualFragment|document\.write/);
  const copied=[];ui.context.navigator={clipboard:{writeText:async text=>{copied.push(text)}}};
  metaOf(reply).children.at(-1).events.click();await ui.tick();
  assert.deepEqual(copied,[evil],"copy is the original Markdown source");
});

test("Markdown rendering stays bounded on hostile input and very long replies fall back to plain text",async()=>{
  const ui=await open(markdownChat("ok"));
  // Review cases: padded table cells, regex backtracking on long spaces, failed link scans, URL ")" trimming.
  for(const hostile of ["*".repeat(60000),"[".repeat(60000)+"](","`a".repeat(30000),"_x ".repeat(20000),
    "> ".repeat(5000)+"deep","- ".repeat(5000)+"deep","# a"+" ".repeat(150000)+"b","a|b\n|-"+" ".repeat(150000)+"x|",
    "-"+" ".repeat(150000)+"x","[a](".repeat(49999),"[a](<".repeat(39999),'[a](b "'.repeat(28000),
    (" https://a"+")".repeat(2030)).repeat(97),"|".repeat(2001)+"\n"+"-|".repeat(2000)+"\n"+"x\n".repeat(2000),
    "a\n".repeat(70000),"- a\n".repeat(45000),"> a\n".repeat(45000)]){
    const started=Date.now(),nodes=ui.context.markdownNodes(hostile);
    assert.ok(Array.isArray(nodes));
    assert.ok(Date.now()-started<1500,`slow on ${JSON.stringify(hostile.slice(0,8))}`);
    assert.ok(treeOf({children:nodes}).length<=hostile.length,`more nodes than characters for ${JSON.stringify(hostile.slice(0,8))}`);
  }
  assert.equal(ui.context.markdownNodes("|".repeat(66)+"\n"+"-|".repeat(65)).some(node=>node.className==="table-wrap"),false,
    "more than 64 columns is not a table");
  assert.equal(ui.context.markdownNodes("|a".repeat(64)+"|\n"+"|-".repeat(64)+"|\n"+"x\n".repeat(400)),null,
    "more than 20,000 table cells shows the reply as plain text");
  assert.equal(ui.context.markdownNodes("x".repeat(200001)),null);
  ui.render(markdownChat("a\n".repeat(70000)));
  assert.equal(turnNode(ui).children[3].className,"assistant-message markdown",
    "many short lines render without exceeding the engine's argument limit");
  const long="**不是粗體的超長回覆**\n".repeat(15000);
  ui.render(markdownChat(long));
  const reply=turnNode(ui).children[3];
  assert.equal(reply.className,"assistant-message");
  assert.equal(reply.textContent,long);
});

test("code spans bind before emphasis and may end with a space",async()=>{
  const ui=await open(markdownChat("ok"));
  const [paragraph]=ui.context.markdownNodes("**Pattern `**/*.ts`** ok, `` ` `` and `a `");
  const [strong]=paragraph.children;
  assert.equal(strong.tagName,"STRONG");
  assert.deepEqual(treeOf(strong).filter(node=>node.tagName==="CODE").map(node=>node.textContent),["**/*.ts"]);
  assert.deepEqual(treeOf(paragraph).filter(node=>node.tagName==="CODE").map(node=>node.textContent),["**/*.ts","`","a "]);
  assert.equal(shownText(paragraph),"Pattern **/*.ts ok, ` and a ");
  const [escaped]=ui.context.markdownNodes("\\\\`a` *b* \\`");
  assert.deepEqual(treeOf(escaped).filter(node=>node.tagName==="CODE").map(node=>node.textContent),["a"]);
  assert.deepEqual(treeOf(escaped).filter(node=>node.tagName==="EM").map(shownText),["b"],
    "an escaped backtick does not open a code span that would hide *b*");
});

test("each saved message shows its saved Taiwan time and copies its original text",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.instruction="請整理 **重點**";
  task.submission={state:"CONFIRMED",observed_at:"2026-10-04T13:59:00Z",intent:{created_at:"2026-10-04T13:58:30Z"}};
  task.reply.data.text="**粗體**與 [連結](https://example.com)";
  task.progress={recorded:true,finished:true,truncated:false,byte_length:0,data:{state:"completed",progress:{events:[
    {seq:1,kind:"stage",summary:"SDK turn started",at:"2026-10-04T13:58:31Z"},
    {seq:2,kind:"stage",summary:"SDK turn ended",at:"2026-10-04T14:01:05Z"}]}}};
  const ui=await open(data,{now:Date.parse("2026-10-04T14:10:00Z")}),copied=[];
  ui.context.navigator={clipboard:{writeText:async text=>{copied.push(text)}}};
  const turn=turnNode(ui),[userTime,userCopy]=metaOf(turn.children[1]).children;
  const [replyTime,replyCopy]=metaOf(turn.children[3]).children;
  assert.equal(userTime.tagName,"TIME");assert.equal(userTime.dateTime,"2026-10-04T13:58:30Z");
  assert.equal(userTime.textContent,"21:58");
  assert.match(userTime.title,/^送出時間：.*21:58:30（台灣時間）$/);
  assert.equal(replyTime.textContent,"22:01");
  assert.match(replyTime.title,/^Agent 記錄的回合結束時間：.*22:01:05（台灣時間）$/);
  assert.equal(shownText(turn.children[1]),task.instruction,"the user's own words stay plain text");
  userCopy.events.click();await ui.tick();
  replyCopy.events.click();await ui.tick();
  assert.deepEqual(copied,[task.instruction,task.reply.data.text]);
  assert.equal(replyCopy.textContent,"已複製");
  for(const [id,delay] of ui.timerDelays)if(delay===1500){ui.timers.get(id)();ui.timers.delete(id)}
  assert.equal(replyCopy.textContent,"複製");
  delete ui.context.navigator;replyCopy.events.click();await ui.tick();
  assert.equal(replyCopy.textContent,"無法複製");

  const later=await open(data,{now:Date.parse("2027-01-02T03:00:00Z")});
  assert.equal(metaOf(turnNode(later).children[1]).children[0].textContent,"2026/10/04 21:58");
  const nextDay=await open(data,{now:Date.parse("2026-10-05T03:00:00Z")});
  assert.equal(metaOf(turnNode(nextDay).children[1]).children[0].textContent,"10/04 21:58");
  const midnight=await open(data,{now:Date.parse("2026-10-04T15:50:00Z")});
  assert.equal(metaOf(turnNode(midnight).children[1]).children[0].textContent,"21:58");
  midnight.holder.now=Date.parse("2026-10-04T16:10:00Z");midnight.render(data);
  assert.equal(metaOf(turnNode(midnight).children[1]).children[0].textContent,"10/04 21:58",
    "the next refresh after Taiwan midnight adds the date");
});

test("a message without a saved time shows no time at all, never the current time",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  task.instruction="沒有保存時間的訊息";task.submission={state:"CONFIRMED"};task.progress=null;
  task.reply.observed_at=task.status.observed_at=when;
  const ui=await open(data,{now:Date.parse(when)}),turn=turnNode(ui);
  assert.equal(treeOf(ui.nodes.get("conversation-history")).some(node=>node.tagName==="TIME"),false);
  for(const bubble of [turn.children[1],turn.children[3]]){
    const meta=metaOf(bubble);
    assert.deepEqual(meta.children.map(node=>node.tagName),["BUTTON"]);
    assert.equal(meta.children[0].textContent,"複製");
  }
});

test("the sidebar scrolls as a whole, keeps the agent list's own scroll and a reachable footer",()=>{
  assert.match(html,/\.sidebar\{[^}]*overflow-y:auto/);
  assert.match(html,/\.agent-list\{[^}]*overflow-y:auto;min-height:120px/);
  assert.match(html,/\.sidebar-footer\{position:sticky;bottom:0/);
  assert.match(html,/@media\(max-width:900px\)\{[\s\S]*?\.sidebar\{position:absolute;inset:0 auto 0 0;/,
    "the narrow-screen drawer is the same sidebar with top and bottom pinned, so it scrolls too");
});

// 2026-10-06 (Neal): "縮小視窗時，網頁layout沒有跟著縮小，很多功能和資訊會被改調". Layout only: read/send logic unchanged.
const css=html.slice(html.indexOf("<style>"),html.indexOf("</style>"));
const cssRule=selector=>{
  const match=css.match(new RegExp("(?:^|[\\n}])"+selector.replace(/[.#()]/g,"\\$&")+"\\{([^}]*)\\}"));
  assert.ok(match,selector);return match[1];
};
test("the page fills the window and only the conversation scrolls; nothing in the chat column has a fixed height",()=>{
  assert.match(cssRule(".app-shell"),/--sidebar-column:clamp\(220px,22vw,280px\);[^}]*height:100dvh;display:grid;grid-template-columns:var\(--sidebar-column\) minmax\(0,1fr\)/);
  assert.match(cssRule(".chat-pane"),/display:flex;flex-direction:column;min-height:0/);
  assert.match(cssRule(".chat-scroll"),/flex:1 1 auto;min-height:0;overflow:auto/);
  for(const selector of [".chat-pane",".chat-header",".chat-scroll","#conversation-history",".composer-area",".composer-wrap",
    ".composer-surface",".composer-status",".composer-meta"])
    assert.doesNotMatch(cssRule(selector),/(?:^|;)height:/,selector+" must follow the window, not a fixed height");
  // The input row never leaves the window: the area under it is capped and scrolls, and short windows tighten the chrome.
  assert.match(cssRule(".composer-area"),/flex-shrink:0;max-height:calc\(100% - 48px\);overflow-y:auto/);
  assert.match(cssRule(".composer-status"),/max-height:clamp\(3\.6em,16dvh,14em\);overflow-y:auto/);
  assert.match(css,/@media\(max-height:640px\)\{[\s\S]*?\.composer-status\{max-height:max\(3em,10dvh\)\}/);
  assert.match(css,/@media\(max-width:900px\)\{[\s\S]*?#sidebar-toggle\{display:grid\}/);
  // Popups and panels stay inside the window and scroll inside.
  assert.match(cssRule(".menu-items"),/width:min\(290px,calc\(100vw - 24px\)\);max-height:calc\(100dvh - 72px\);overflow-y:auto/);
  assert.match(cssRule(".diagnostics"),/top:0;bottom:0;[^}]*overflow:auto/);
  assert.match(cssRule(".settings-page"),/inset:0;[^}]*overflow:auto/);
  assert.match(cssRule(".desktop-screen"),/width:100%;aspect-ratio:var\(--desktop-ratio,1280 \/ 800\)/);
  assert.match(cssRule(".turn-tools"),/flex-wrap:wrap/);
  // Neal 2026-10-06 10:24: the model picker stays beside the input at every size; narrow screens may only shrink it.
  for(const match of css.matchAll(/#model\{([^}]*)\}/g))assert.doesNotMatch(match[1],/display:none|visibility:hidden|position:absolute/);
  const at=needle=>html.indexOf(needle);
  assert.ok(at('class="composer-actions"')<at('id="model"')&&at('id="model"')<at('id="send-instruction"'));
});

test("the status lines under the input show only what needs attention, and the checks stay reachable there",async()=>{
  const at=id=>html.indexOf('id="'+id+'"');
  for(const id of ["cancellation-note","instruction-reason","input-capacity"])
    assert.ok(at("send-instruction")<at("composer-status")&&at("composer-status")<at(id)&&at(id)<at("diagnostics"),id);
  // A pending check comes first, so a short window still shows it before the general explanation.
  assert.ok(at("cancellation-note")<at("instruction-reason"));
  assert.match(css,/#instruction-reason:empty\{display:none\}/);
  const quiet=await open(nativeChat("completed"));
  for(const id of ["cancellation-note","input-capacity"])
    assert.equal(quiet.nodes.get(id).hidden,true,id+" is not shown when nothing needs handling");
  assert.equal(quiet.text("instruction-reason"),"");
  const unknown=await open(unknownCancellationChat());
  assert.equal(unknown.nodes.get("cancellation-note").hidden,false);
  assert.notEqual(unknown.text("cancellation-note"),"");
});

test("each reply keeps 詳細 in its row and its other actions in its ⋯ menu, all still working",async()=>{
  const data=downloadableChat();data.tasks[0].actions["read-skill-draft"]=action(true);
  const ui=await open(data);
  const row=()=>turnNode(ui).children.at(-1),menu=()=>turnMenu(turnNode(ui));
  assert.equal(row().className,"turn-tools");
  assert.deepEqual(row().children.map(node=>node.tagName),["BUTTON","DETAILS"]);
  assert.equal(row().children[0].textContent,"詳細");
  assert.equal(menu().className,"turn-more");
  const [summary,items]=menu().children;
  assert.equal(summary.tagName,"SUMMARY");assert.equal(summary.textContent,"⋯");
  assert.equal(summary.getAttribute("aria-label"),"這則的更多動作");
  assert.equal(items.className,"turn-more-items");
  assert.deepEqual(items.children.map(node=>node.textContent),["下載答案文字","查看技能草稿"]);
  assert.deepEqual(turnActions(turnNode(ui)).map(node=>node.textContent),["詳細","下載答案文字","查看技能草稿"]);
  // Outside clicks and Escape close it like the header menus; Escape returns focus to its ⋯.
  menu().open=true;ui.documentEvents.click({target:summary});assert.equal(menu().open,true);
  ui.documentEvents.click({target:ui.nodes.get("chat-scroll")});assert.equal(menu().open,false);
  menu().open=true;ui.context.document.activeElement=items.children[1];
  ui.documentEvents.keydown({key:"Escape"});
  assert.equal(menu().open,false);assert.equal(summary.focused,true);
  // A redraw rebuilds every turn (each second while a turn runs); an open menu stays open until it is closed.
  const first=menu();first.open=true;first.events.toggle();
  const redrawn=copy(data);redrawn.tasks[0].reply.data.text="Original saved answer, redrawn";ui.render(redrawn);
  assert.notEqual(menu(),first);assert.equal(menu().open,true);
  ui.documentEvents.click({target:ui.nodes.get("chat-scroll")});assert.equal(menu().open,false);
  ui.render(data);assert.equal(Boolean(menu().open),false,"a closed menu is not reopened by the next redraw");
  // A chosen action closes the menu and does exactly what the row button did.
  ui.holder.answerResponse=answerResponse("原答案");
  const chosen=turnActions(turnNode(ui)).find(node=>node.textContent==="下載答案文字");
  menu().open=true;ui.documentEvents.click({target:chosen});assert.equal(menu().open,false);
  chosen.events.click();await ui.tick();await ui.tick();
  assert.equal(ui.calls.filter(call=>call.url.endsWith("/reply.txt")).length,1);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Principle audit P11: checks are kept and reachable, but not shown under every reply; the ⋯ is marked while one waits.
test("checks for unknown results sit in the reply's ⋯ menu, which is marked while a check is waiting",async()=>{
  const rowButtons=ui=>turnNode(ui).children.at(-1).children.filter(node=>node.tagName==="BUTTON").map(node=>node.textContent);
  const menuButtons=ui=>(turnMenu(turnNode(ui))?.children[1].children||[]).map(node=>node.textContent);
  const marked=ui=>{const menu=turnMenu(turnNode(ui));
    return menu.className==="turn-more check-waiting"&&menu.children[0].getAttribute("aria-label")==="這則的更多動作（有待核對）"};
  const stop=await open(unknownCancellationChat());
  assert.equal(rowButtons(stop).includes("核對原停止回條"),false);
  assert.ok(menuButtons(stop).includes("核對原停止回條"));assert.ok(marked(stop));
  assert.match(stop.text("cancellation-note"),/這則訊息下方「⋯」裡的「核對原停止回條」/,"the prompt says where the check now is");
  const native=nativeChat("started");native.tasks[0].cancellation={state:"UNKNOWN"};
  assert.match((await open(native)).text("cancellation-note"),/這則訊息下方「⋯」裡的「核對原停止回條」/);
  const appended=status=>{
    const data=appendChat();
    data.tasks[0].appends=[{append_id:"append-offline-a",request_id:data.composer.request_id,content:"補充",status,delivery:null}];
    return data;
  };
  for(const [status,waiting] of [["unknown",true],["applied",false]]){
    const ui=await open(appended(status));
    assert.equal(rowButtons(ui).includes("核對追加"),false,status);assert.ok(menuButtons(ui).includes("核對追加"),status);
    assert.equal(marked(ui),waiting,status);
  }
  const pending=appended("applied");pending.composer.append_target.pending=true;
  const ui=await open(pending);
  assert.ok(menuButtons(ui).includes("核對追加"));assert.ok(marked(ui));
  assert.match(ui.text("instruction-reason"),/這則訊息下方「⋯」裡的「核對追加」/);
});

// 2026-10-06 12:39 (Neal): "可否讓左邊的panel可以往右或往左縮放大小，縮放的過程中，即時桌面也需要等比例的縮放".
test("the sidebar handle resizes it by drag or keys, double-click restores, and the width survives a reload",async()=>{
  const storage=memoryStorage(),ui=await open(fixture(),{storage,innerWidth:1440});
  const handle=ui.nodes.get("sidebar-resizer"),shell=ui.nodes.get("app-shell");
  // A focusable splitter (WAI-ARIA APG window splitter) named like the pane it sizes.
  const tag=html.match(/<div id="sidebar-resizer"[^>]*>/)?.[0]||"";
  for(const attribute of ['role="separator"','aria-orientation="vertical"','aria-controls="sidebar"','aria-label="Agent 清單"','tabindex="0"'])
    assert.ok(tag.includes(attribute),attribute);
  assert.match(html,/<aside class="sidebar" id="sidebar" aria-label="Agent 清單">/);
  // Default: clamp(220px,22vw,280px) at 1440px is 280; the most is min(50vw,640px) = 640.
  assert.equal(shell.style["--sidebar-column"],undefined);
  assert.deepEqual(["aria-valuemax","aria-valuenow"].map(name=>handle.getAttribute(name)),["640","280"]);
  assert.ok(tag.includes('aria-valuemin="220"'));
  let captured=null;handle.setPointerCapture=id=>{captured=id};
  handle.events.pointerdown({pointerId:7,clientX:300,button:0});
  assert.equal(captured,7);assert.equal(shell.getAttribute("data-sidebar-dragging"),"true");
  handle.events.pointermove({pointerId:7,clientX:350});
  assert.equal(handle.getAttribute("aria-valuenow"),"330","the width follows the pointer while dragging");
  assert.equal(storage.getItem("fa-sidebar-width"),null,"saved once, when the drag ends");
  handle.events.pointermove({pointerId:9,clientX:900});assert.equal(handle.getAttribute("aria-valuenow"),"330");
  handle.events.pointermove({pointerId:7,clientX:1200});assert.equal(handle.getAttribute("aria-valuenow"),"640");
  handle.events.pointermove({pointerId:7,clientX:420});handle.events.pointerup({pointerId:7,clientX:420});
  assert.equal(shell.getAttribute("data-sidebar-dragging"),"false");
  assert.equal(shell.style["--sidebar-column"],"clamp(220px,400px,max(220px,min(50vw,640px)))");
  assert.equal(handle.getAttribute("aria-valuenow"),"400");assert.equal(storage.getItem("fa-sidebar-width"),"400");
  // A click without moving keeps the saved width; arrows step 16px, Home/End go to the limits.
  handle.events.pointerdown({pointerId:8,clientX:10,button:0});handle.events.pointerup({pointerId:8,clientX:10});
  assert.equal(storage.getItem("fa-sidebar-width"),"400");
  let prevented=0;const key=name=>handle.events.keydown({key:name,preventDefault(){prevented++}});
  key("ArrowRight");assert.equal(handle.getAttribute("aria-valuenow"),"416");
  key("ArrowLeft");key("ArrowLeft");assert.equal(handle.getAttribute("aria-valuenow"),"384");
  key("Home");assert.equal(handle.getAttribute("aria-valuenow"),"220");key("End");assert.equal(handle.getAttribute("aria-valuenow"),"640");
  key("Tab");assert.equal(prevented,5,"other keys keep their normal behaviour");
  key("ArrowLeft");assert.equal(storage.getItem("fa-sidebar-width"),"624");
  // Reloading the page (same browser storage) keeps the width.
  const reloaded=await open(fixture(),{storage,innerWidth:1440});
  assert.equal(reloaded.nodes.get("app-shell").style["--sidebar-column"],"clamp(220px,624px,max(220px,min(50vw,640px)))");
  assert.equal(reloaded.nodes.get("sidebar-resizer").getAttribute("aria-valuenow"),"624");
  // On a narrower window the saved width is shown clamped (CSS clamps too) and kept for wider windows.
  const narrower=await open(fixture(),{storage,innerWidth:1000});
  assert.equal(narrower.nodes.get("sidebar-resizer").getAttribute("aria-valuenow"),"500");
  assert.equal(storage.getItem("fa-sidebar-width"),"624");
  reloaded.nodes.get("sidebar-resizer").events.dblclick();
  assert.equal(reloaded.nodes.get("app-shell").style["--sidebar-column"],undefined);
  assert.equal(reloaded.nodes.get("sidebar-resizer").getAttribute("aria-valuenow"),"280");
  assert.equal(storage.getItem("fa-sidebar-width"),null);
  // A stored value that is not a width is ignored.
  storage.setItem("fa-sidebar-width","9999");
  assert.equal((await open(fixture(),{storage})).nodes.get("app-shell").style["--sidebar-column"],undefined);
  // Layout: the grid column and the handle position use the same width; the drawer has no handle.
  assert.match(cssRule(".app-shell"),/--sidebar-column:clamp\(220px,22vw,280px\);[^}]*grid-template-columns:var\(--sidebar-column\) minmax\(0,1fr\)/);
  assert.match(cssRule(".sidebar-resizer"),/position:absolute;top:0;bottom:0;left:calc\(var\(--sidebar-column\) - 4px\);[^}]*cursor:col-resize;touch-action:none/);
  assert.match(css,/@media\(max-width:900px\)\{[\s\S]*?\.sidebar-resizer\{display:none\}/);
  assert.match(css,/@media\(pointer:coarse\)\{\.sidebar-resizer::before\{[^}]*left:-8px;right:-8px\}/,"a wider target for fingers");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("the live desktop box keeps the VM screen's proportions at every sidebar width",async()=>{
  // Width follows the sidebar; height comes from the ratio, so the picture is never stretched or cropped.
  for(const selector of [".desktop-screen",".desktop-idle"]){
    assert.match(cssRule(selector),/width:100%;aspect-ratio:var\(--desktop-ratio,1280 \/ 800\)/,selector);
    assert.doesNotMatch(cssRule(selector),/(?:^|;)(?:max-)?height:/,selector);
  }
  // noVNC (scaleViewport) sizes its canvas to the remote screen; the box takes that ratio and keeps it.
  // It is set on the panel, so the not-connected box (a sibling) keeps the last screen's shape too.
  const ui=await open(),screen=ui.nodes.get("desktop-screen"),panel=ui.nodes.get("desktop-panel");
  const canvas={width:1280,height:800};screen.querySelector=selector=>selector==="canvas"?canvas:null;
  ui.context.fitDesktopRatio();assert.equal(panel.style["--desktop-ratio"],"1280 / 800");
  canvas.width=1920;canvas.height=1080;ui.context.fitDesktopRatio();assert.equal(panel.style["--desktop-ratio"],"1920 / 1080");
  canvas.width=0;ui.context.fitDesktopRatio();assert.equal(panel.style["--desktop-ratio"],"1920 / 1080","no frame yet keeps the last ratio");
  assert.ok(html.indexOf('id="desktop-panel"')<html.indexOf('id="desktop-screen"')&&
    html.indexOf('id="desktop-screen"')<html.indexOf('id="desktop-idle"')&&html.indexOf('id="desktop-idle"')<html.indexOf("</details>",html.indexOf('id="desktop-panel"')));
  assert.match(html,/view\.rfb\.scaleViewport=true;/);
  assert.doesNotMatch(html,/resizeSession\s*=\s*true/,"only the display scales; the VM resolution is never changed");
});

// Neal 2026-10-06 09:58／10:00：「FA portal為什麼要控制user能否傳送訊息、能否續問? 有遵守我們的原則嗎」「FA portal還有那些部分是沒有遵守的?」
// P01／P08／P10: the main screen shows plain states only; digests, request IDs, receipts, UNKNOWN details, SDK stage
// names and tool correlation numbers live in ⋯ → 支援明細, and every check or retry action is still there.
const ENGINEERING_TEXT=/SHA-?256|sha256|RID|UNKNOWN|回條|digest|工具關聯|SDK|不會自動重送|改稱成功|已核對服務接點|候選不是讀取權限|metadata|接單|NATIVE_/;
function mainScreenText(ui){
  const tree=JSON.stringify(ui.nodes.get("conversation-history").children,(key,value)=>key==="events"?undefined:value);
  return [tree,...["chat-presence","instruction-reason","error-message","cancellation-note"].map(id=>ui.text(id))].join("\n");
}
function engineeringChat(){
  const data=nativeChat("completed"),task=data.tasks[0];
  task.user_files=[{name:"notes.txt",utf8_bytes:12,sha256:"f".repeat(64)}];
  task.user_images=[{name:"shot.png",byte_length:2048,sha256:"e".repeat(64)}];
  task.continuation_usage={known_requests:2,unknown_attempts:1};
  task.progress={data:{state:"completed",progress:{events:[
    {seq:1,at:progressAt(0),kind:"stage",status:"started",summary:"SDK session started"},
    {seq:2,at:progressAt(1),kind:"tool_started",tool_name:"WebIQ_MCP_web",tool_start_seq:2,status:"started"},
    {seq:3,at:progressAt(2),kind:"tool_completed",tool_name:"WebIQ_MCP_web",tool_start_seq:2,status:"succeeded",
      summary:"Tool returned a result"},
    {seq:4,at:progressAt(3),kind:"stage",status:"succeeded",summary:"SDK turn ended"}]}},
    recorded:true,finished:true,truncated:false,byte_length:900};
  const unsent=copy(task);
  unsent.task_id=unsent.submit_request_id="unsent-rid";unsent.instruction="第二則訊息";unsent.reply=null;
  unsent.admission=null;unsent.native_receipt=null;unsent.status=null;unsent.progress=null;
  unsent.user_files=[];unsent.user_images=[];unsent.continuation_usage=null;
  unsent.submission={state:"UNKNOWN"};
  unsent.actions={...unsent.actions,"submit-reconcile":action(true),"submit-retry":action(true),cancel:action(false)};
  data.tasks.push(unsent);
  return {data,task,unsent};
}

test("P01: the main screen keeps plain words; the support details keep the evidence and every action",async()=>{
  const {data,task}=engineeringChat(),ui=await open(data);
  const main=mainScreenText(ui);
  assert.doesNotMatch(main,ENGINEERING_TEXT);
  assert.match(main,/附檔：notes\.txt/);assert.match(main,/附圖：shot\.png/);
  assert.match(main,/這則訊息沒有送達。可以在「詳細」裡用同一則訊息重試。/);
  assert.match(main,/完成：網路搜尋/);
  assert.equal(ui.text("chat-presence"),"可以聊天");
  assert.match(html,/<aside[^>]*id="diagnostics"[^>]*aria-label="支援明細"[^>]*hidden/);
  assert.match(html,/<button id="open-diagnostics">支援明細<\/button>/);
  assert.doesNotMatch(html,/技術明細/);
  // The unknown message's original-request check and retry stay reachable in the support details, once each.
  assert.equal(ui.nodes.get("submission-reconcile-panel").hidden,false);
  assert.equal(ui.nodes.get("submit-reconcile").disabled,false);
  assert.equal(ui.nodes.get("submit-retry").disabled,false);
  ui.click("submit-retry");ui.click("submit-retry");await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),["/api/tasks/unsent-rid/submit-retry"]);
  // The completed turn's evidence: digests, usage and tool correlation numbers.
  ui.holder.pendingPosts[0].resolve({});await ui.tick();await ui.tick();
  vm.runInContext(`selected=${JSON.stringify(task.task_id)};render()`,ui.context);
  assert.match(ui.text("identifiers"),new RegExp(`"sha256": "${"f".repeat(64)}"`));
  assert.match(ui.text("identifiers"),new RegExp(`"sha256": "${"e".repeat(64)}"`));
  assert.match(ui.text("identifiers"),/"known_requests": 2/);
  assert.match(ui.nodes.get("processing-events").children.map(node=>node.textContent).join("\n"),/工具關聯 #2/);
  assert.match(ui.nodes.get("processing-events").children.map(node=>node.textContent).join("\n"),/SDK turn ended/);
  assert.match(ui.text("processing-detail"),/原回條/);
  assert.match(ui.text("reasoning-summary"),/推理摘要/);
  assert.match(ui.text("hosted-session"),/native-sid/,"Session 與模型容量 is in the support details");
  assert.ok(html.indexOf('id="hosted-session"')>html.indexOf('id="diagnostics"'));
  assert.ok(html.indexOf('id="model-capacity"')>html.indexOf('id="diagnostics"'));
});

test("P01: an error above the composer is one plain sentence with a copyable request ID",async()=>{
  const data=attachmentChat(),ui=await open(data);
  assert.ok(html.indexOf('id="error"')>html.indexOf('class="composer-wrap"')&&
    html.indexOf('id="error"')<html.indexOf('class="composer-surface"'),"the notification sits right above the input");
  ui.holder.response={ok:false,json:async()=>({error:{code:"BACKEND_DENIED",automatic_retry:false}})};
  typeAttachmentText(ui,"會被拒絕的訊息");
  ui.click("send-instruction");await ui.tick();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("error").hidden,false);
  assert.equal(ui.text("error-message"),"服務拒絕這次操作，可能是權限或有效期限已改變。");
  assert.doesNotMatch(ui.text("error-message"),ENGINEERING_TEXT);
  assert.equal(ui.nodes.get("copy-error-request").hidden,false);
  const copied=[];ui.context.navigator={clipboard:{writeText:async text=>{copied.push(text)}}};
  await ui.nodes.get("copy-error-request").events.click();
  assert.deepEqual(copied,["attachment-draft"]);
  assert.equal(ui.text("copy-error-request"),"已複製");
  assert.match(ui.text("error-full"),/沒有重新送出/);
  assert.match(ui.text("error-request"),/attachment-draft/);
  assert.match(ui.text("error-code"),/BACKEND_DENIED/);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1,"no automatic resend");
});

test("P12: assistants the server marks unavailable stay out of the list and only show in the support details",async()=>{
  const data=agentChat("agent-one");
  data.hidden_agents=[{id:"stage-c",name:"fa-stage-c",reason:"AGENT_SCOPE_MISSING"}];
  const ui=await open(data);
  const names=ui.nodes.get("agent-list").children.map(node=>JSON.stringify(node,(key,value)=>key==="events"?undefined:value)).join("\n");
  assert.doesNotMatch(names,/fa-stage-c|服務尚未就緒/);
  assert.equal(ui.nodes.get("hidden-agents-panel").hidden,false);
  assert.match(ui.nodes.get("hidden-agents").children[0].textContent,/fa-stage-c（stage-c）：AGENT_SCOPE_MISSING/);
  const plain=await open(agentChat("agent-one"));
  assert.equal(plain.nodes.get("hidden-agents-panel").hidden,true);
});
// Two-axis review of the plain main screen (2026-10-06).
test("P01 review: before sign-in the multi-assistant header asks to connect instead of reporting a problem",async()=>{
  const data=agentChat("agent-one");data.authentication="LOGIN_REQUIRED";data.agents=[];
  const ui=await open(data);
  assert.equal(ui.text("chat-presence"),"需要處理");
  const incomplete=attachmentChat();incomplete.composer.command_incomplete=true;
  assert.equal((await open(incomplete)).text("chat-presence"),"需要處理");
  const noModels=attachmentChat();noModels.composer.models={default_model:null,models:[]};
  assert.equal((await open(noModels)).text("chat-presence"),"需要處理");
  assert.equal((await open(attachmentChat())).text("chat-presence"),"可以聊天");
});

test("P01 review: the one-sentence error keeps the next step and drops only the reassurance",async()=>{
  const ui=await open(fixture()),headline=code=>vm.runInContext(`errorHeadline(friendlyError({code:"${code}"}))`,ui.context);
  assert.equal(headline("SERVER_FOLLOWING_TIMEOUT"),"換版還沒完成，訊息已保留。可稍後以原內容重送。");
  assert.equal(headline("PLATFORM_REQUEST_REJECTED"),"雲端登入層拒絕了這個請求，沒有送到工作台，也沒有自動重送。請重新整理頁面。");
  assert.equal(headline("INSTRUCTION_PENDING"),"原對話尚有未確認或未完成的工作。請更新進度或核對原接單。");
  assert.equal(headline("BACKEND_DENIED"),"服務拒絕這次操作，可能是權限或有效期限已改變。");
  assert.match(headline("REMOTE_SESSION_CHANGED"),/^遠端工作台已重新啟動，沒有重送任何操作。.*請先複製內容，再重新整理頁面。$/,"the step that protects an unsent draft stays");
});

test("P01 review: only a failed action on the message's own route offers that message's request ID",async()=>{
  const ui=await open(fixture());
  ui.holder.response={ok:false,json:async()=>({error:{code:"BACKEND_DENIED"}})};
  await vm.runInContext('run("/api/tasks/offline/status",{},"offline")',ui.context);
  assert.equal(ui.nodes.get("copy-error-request").hidden,false);
  assert.match(ui.text("error-request"),/offline/);
  await vm.runInContext('run("/api/instructions/thread-x/reset",{},"offline")',ui.context);
  assert.equal(ui.nodes.get("copy-error-request").hidden,true,"a conversation action is not that message's request");
});

test("P12 review: a hidden assistant that is still selected keeps its name and reason",async()=>{
  const data=agentChat("agent-three");
  data.hidden_agents=[{id:"agent-three",name:"第三位助理",reason:"WORK_POLICY_REVOKED"}];
  const ui=await open(data);
  assert.equal(ui.text("chat-title"),"第三位助理");
  assert.equal(ui.nodes.get("agent-directory-note").hidden,false);
  assert.equal(vm.runInContext('groupName("agent-three")',ui.context),"第三位助理");
  assert.equal(ui.text("chat-presence"),"發生問題");
});

test("P10 review: a collapsed skill draft in the support details stays collapsed across refreshes",async()=>{
  const {data,value}=nativeDraftCase();
  const ui=await open(data,{skillDraftResponse:{ok:true,json:async()=>value}});
  turnActions(turnNode(ui)).find(node=>node.textContent==="查看技能草稿").click();
  await ui.tick();await ui.tick();
  const panel=supportDraft(ui);panel.open=false;
  ui.render(data);ui.render(data);
  assert.equal(supportDraft(ui),panel);assert.equal(panel.open,false);
});

// Neal 2026-10-07 08:29: a card's button sends at once (no confirm dialog); the card becomes "助理問／你的回答" where the
// question was, the turn stops waiting in the same redraw, and the assistant's next work follows below it.
function inlineQuestionChat(lane="foundry-responses-v1"){
  const data=nativeChat("started"),task=data.tasks[0];
  task.state_profile=data.composer.state_profile=lane;
  data.composer.native_hitl={enabled:true,can_approve:true,references:[],commands:[],error:null};
  data.composer.native_questions={commands:[],error:null};
  const entry=questionItem(data.composer.thread_id);
  Object.assign(entry.item.question.call,{conversation_id:task.admission.conversation_id,request_id:task.task_id});
  data.composer.attention=[entry];
  return {data,task,entry,qid:entry.item.item_id};
}
const shownNode=node=>JSON.stringify(node,(key,value)=>key==="events"?undefined:value);
// stage 2026-10-10: a turn held by an approval card kept saying 「正在準備回覆…」 as if it were simply running.
for(const kind of ["approval","question"]){
  test(`a running turn held by the owner's ${kind} card says so in its reply`,async()=>{
    let data;
    if(kind==="question")data=inlineQuestionChat("foundry-native-v1").data;
    else{
      data=nativeChat("started");const task=data.tasks[0],pid="a".repeat(64);
      const approval={pending_id:pid,pending_digest:"b".repeat(64),call:{agent_id:"configured-agent",
        conversation_id:task.admission.conversation_id,request_id:task.task_id,sdk_session_id:"sdk",tool_call_id:"call"}};
      data.composer.native_hitl={enabled:true,can_approve:true,references:[approval],commands:[],error:null};
      data.composer.attention=[{thread_id:data.composer.thread_id,label:"原工作",item:{
        schema_version:"fa-native-attention-v1",type:"approval_required",item_id:pid,approval,
        created_at:when,expires_at:"2026-01-01T00:10:00Z",purpose:"attention-only-not-approval"}}];
    }
    const ui=await open(data,{now:Date.parse(when)});
    const reply=turnNode(ui).children.find(node=>String(node.className).startsWith("assistant-message"));
    assert.equal(reply.textContent,kind==="approval"?"這個回合正在等你批准":"這個回合正在等你回答");
    const running=nativeChat("started");ui.render(running);
    assert.equal(turnNode(ui).children.find(node=>String(node.className).startsWith("assistant-message")).textContent,
      "正在準備回覆…","without a card it is the usual running line");
    assert.equal(ui.calls.some(call=>call.method==="POST"),false);
  });
}
test("HITL 10-07: an answered card in a running turn becomes its record above the reply and the turn stops waiting at once",async()=>{
  const {data,entry,qid}=inlineQuestionChat(),streams=[],posts=[];let finish;
  const ui=await open(data,{now:Date.parse(when),
    streamResponse:url=>{const stream=liveStream();streams.push({url,...stream});return stream.response},
    hitl:(url,options)=>{posts.push(JSON.parse(options.body));
      return new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>answerReply(entry.item,posts[0])})})}});
  await liveSettle(ui);
  assert.equal(streams.length,1,"the running turn's live text is open while the card waits");
  streams[0].send({type:"response.output_item.added",sequence_number:2,item_id:"msg_c",phase:"commentary"});
  streams[0].send({type:"response.output_text.delta",sequence_number:3,item_id:"msg_c",delta:"先查一下。"});
  await liveSettle(ui);
  let turn=turnNode(ui);
  const slot=turn.children.findIndex(node=>node.className==="turn-attention");
  assert.ok(slot>1&&slot<turn.children.findIndex(node=>String(node.className).startsWith("assistant-message")),
    "the card sits after the owner's message and before the assistant's reply");
  assert.match(shownNode(turn),/正在等你回答/);
  assert.equal(ui.nodes.get("attention-panel").hidden,false);
  assert.equal(ui.text("chat-presence"),"需要處理");
  hitlNodes(ui,"data-question-choice",qid)[0].events.click();
  // Same redraw, before the bridge answers: the record replaces the card and the turn no longer waits.
  turn=turnNode(ui);
  assert.equal(ui.nodes.get("page-confirm").hidden,true);
  assert.equal(hitlNodes(ui,"data-question-card",qid).length,0);
  const record=hitlNodes(ui,"data-hitl-record",qid)[0];
  assert.ok(turn.children[slot].children.includes(record),"the record stays where the question was");
  assert.deepEqual(record.children.map(node=>node.textContent),["助理問：要寄給 <b>誰</b>？","你的回答：Ann","送出中…"]);
  assert.doesNotMatch(shownNode(turn),/正在等你回答/);
  assert.match(shownNode(turn),/正在回覆/);
  assert.equal(ui.nodes.get("attention-panel").hidden,true,"nothing is waiting in 需要處理 any more");
  assert.notEqual(ui.text("chat-presence"),"需要處理","the header stops asking too");
  finish();await liveSettle(ui);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",qid)[0].children.map(node=>node.textContent),
    ["助理問：要寄給 <b>誰</b>？","你的回答：Ann"]);
  // The assistant's work after the answer keeps arriving on the same stream.
  streams[0].send({type:"response.output_text.delta",sequence_number:4,item_id:"msg_c",delta:"收到，寄給 Ann。"});
  await liveSettle(ui);
  assert.match(shownNode(turnNode(ui)),/先查一下。收到，寄給 Ann。/);
  assert.equal(streams.length,1,"the stream is neither closed nor reopened by the answer");
  assert.equal(posts.length,1);
});

test("HITL 10-07: answered records stay in their own turn after the card leaves the list, a reload or a conversation switch",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0],thread=data.composer.thread_id;
  data.composer.native_hitl={enabled:true,can_approve:true,references:[],commands:[],error:null};
  data.composer.attention=[];
  const call=id=>({agent_id:"configured-agent",conversation_id:task.admission.conversation_id,request_id:task.task_id,
    sdk_session_id:"sdk",tool_call_id:id});
  const question=(id,display,created_at)=>({request_id:id.repeat(64),kind:"answer",state:"CONFIRMED",thread_id:thread,
    code:null,observation:null,command:{kind:"answer",request_id:id.repeat(64),question_id:id.repeat(64),
      question_digest:"f".repeat(64),answer:"Bo",was_freeform:false},
    selection:{question_id:id.repeat(64),question_digest:"f".repeat(64),call:call("ask-"+id)},
    ...(display?{display,created_at}:{})});
  data.composer.native_questions={error:null,commands:[question("1",{question:"要寄給誰？"},"2026-01-01T00:02:00+00:00"),
    question("2",null,null)]};
  data.composer.native_hitl.commands=[{request_id:"d".repeat(64),kind:"decision",state:"CONFIRMED",thread_id:thread,code:null,
    receipt:null,observation:null,command:{kind:"decision",request_id:"d".repeat(64),pending_id:"3".repeat(64),
      pending_digest:"b".repeat(64),choice:"deny",rule:null,expected_rules_revision:0,expected_rules_etag:null},
    selection:{pending_id:"3".repeat(64),pending_digest:"b".repeat(64),call:call("shell")},
    display:{tool:"run_command",category:"shell",target:"/work",command:"rm -rf /tmp/x",toolbox:false},
    created_at:"2026-01-01T00:01:00+00:00"}];
  const check=ui=>{
    const turn=turnNode(ui),slot=turn.children.find(node=>node.className==="turn-attention");
    assert.ok(slot&&!slot.hidden);
    assert.ok(turn.children.indexOf(slot)<turn.children.findIndex(node=>String(node.className).startsWith("assistant-message")));
    assert.deepEqual(slot.children.map(node=>node.children.map(line=>line.textContent)),[
      ["助理問了你一個問題","你的回答：Bo"],
      ["助理要執行：rm -rf /tmp/x","你拒絕了"],
      ["助理問：要寄給誰？","你的回答：Bo"]],"oldest first; a record saved before this change has no question text");
    assert.equal(ui.nodes.get("attention-panel").hidden,true);
  };
  const ui=await open(data,{now:Date.parse(when)});
  check(ui);
  const other=copy(data);other.composer.thread_id="other-thread";other.tasks[0].thread_id="other-thread";
  other.tasks[0].task_id=other.tasks[0].submit_request_id="other-rid";
  ui.render(other);
  assert.equal(hitlNodes(ui,"data-hitl-record","1".repeat(64)).length,0,"another conversation shows none of them");
  ui.render(data);
  check(ui);
  check(await open(copy(data),{now:Date.parse(when)}));
  const off=copy(data);off.composer.native_hitl.enabled=false;ui.render(off);
  assert.equal(hitlNodes(ui,"data-hitl-record","1".repeat(64)).length,0);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("HITL 10-07: a refused answer opens the card again with the reason and sends nothing more by itself",async()=>{
  const {data,qid}=inlineQuestionChat("foundry-native-v1"),posts=[];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    posts.push(JSON.parse(options.body));
    return {ok:false,status:410,json:async()=>({error:{code:"NATIVE_HITL_EXPIRED"}})};
  }});
  hitlNodes(ui,"data-question-choice",qid)[1].events.click();
  assert.equal(hitlNodes(ui,"data-hitl-record",qid).length,1);
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-record",qid).length,0);
  const card=hitlNodes(ui,"data-question-card",qid)[0];
  assert.ok(card,"the card is back");
  assert.match(shownNode(card),/期限/);
  assert.match(shownNode(turnNode(ui)),/正在等你回答/);
  assert.equal(hitlNodes(ui,"data-question-choice",qid).every(button=>!button.disabled),true);
  await liveSettle(ui);
  assert.equal(posts.length,1);
});

test("HITL 10-07: an unconfirmed decision record only reconciles its original request",async()=>{
  const data=nativeChat("started"),task=data.tasks[0],thread=data.composer.thread_id,rid="d".repeat(64),posts=[];
  data.composer.native_hitl={enabled:true,can_approve:true,references:[],error:null,commands:[{request_id:rid,
    kind:"decision",state:"UNKNOWN",thread_id:thread,code:"LOCAL_OFFLINE",receipt:null,observation:null,
    command:{kind:"decision",request_id:rid,pending_id:"3".repeat(64),pending_digest:"b".repeat(64),choice:"allow_once",
      rule:null,expected_rules_revision:0,expected_rules_etag:null},
    selection:{pending_id:"3".repeat(64),pending_digest:"b".repeat(64),call:{agent_id:"configured-agent",
      conversation_id:task.admission.conversation_id,request_id:task.task_id,sdk_session_id:"sdk",tool_call_id:"shell"}},
    display:{tool:"run_command",category:"shell",target:"/work",command:"free -h",toolbox:false},
    created_at:"2026-01-01T00:01:00+00:00"}]};
  data.composer.attention=[];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    posts.push({url,body:JSON.parse(options.body)});
    // Like the bridge (native_hitl.reconcile): the original stays UNKNOWN and gains the checked receipt.
    return {ok:true,json:async()=>({request_id:rid,original_request_resent:false,
      purpose:"readback-only-not-execution-permission",commands:[{...data.composer.native_hitl.commands[0],
        observation:{control_request_id:"e".repeat(64),control_request_digest:"f".repeat(64),
          result:{request_id:rid,kind:"decision",status:"allow",pending_id:"3".repeat(64)}}}]})};
  }});
  const record=hitlNodes(ui,"data-hitl-record","3".repeat(64))[0];
  assert.deepEqual(record.children.slice(0,3).map(node=>node.textContent),
    ["助理要執行：free -h","你允許了這次","還不確定決定有沒有送到；只核對原決定，不會重送。"]);
  hitlNodes(ui,"data-hitl-record-check",rid)[0].events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.deepEqual(posts.map(post=>post.url),[`/api/native-hitl/${thread}/reconcile`]);
  assert.deepEqual(posts[0].body,{request_id:rid});
  assert.deepEqual(hitlNodes(ui,"data-hitl-record","3".repeat(64))[0].children.map(node=>node.textContent),
    ["助理要執行：free -h","你允許了這次"],"the read-back receipt settles it");
});

test("HITL 10-07: a failed decision reads the saved originals at once and a call shows its one confirmed decision",async()=>{
  const data=hitlChat(),view=hitlRead(data),pid=view.readback.approval.pending_id,posts=[];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    if(options.method!=="POST")return {ok:true,json:async()=>copy(view)};
    posts.push(JSON.parse(options.body));
    return {ok:false,status:502,json:async()=>({error:{code:"BACKEND_UNAVAILABLE"}})};
  }});
  await ui.tick();await ui.tick();
  for(const [id,delay] of ui.timerDelays)if(delay===0)ui.timers.delete(id);
  hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(posts.length,1);
  assert.ok(hitlNodes(ui,"data-hitl-card",pid)[0],"the card opens again with the reason");
  const read=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
  assert.ok(read,"the saved originals are read right away");
  const decision=(request_id,choice,state)=>({request_id,kind:"decision",state,thread_id:data.composer.thread_id,code:null,
    receipt:null,observation:null,selection:view.readback.approval,display:{tool:"run_command",category:"shell",
      target:"w",command:"echo hi",toolbox:false},created_at:"2026-01-01T00:01:00+00:00",
    command:{kind:"decision",request_id,pending_id:pid,pending_digest:view.readback.approval.pending_digest,choice,rule:null,
      expected_rules_revision:0,expected_rules_etag:null}});
  const saved=copy(data);saved.composer.native_hitl.commands=[decision("d".repeat(64),"allow_once","UNKNOWN")];
  ui.holder.progressResponse={ok:true,json:async()=>copy(saved)};
  ui.timers.delete(read[0]);await read[1]();await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-card",pid).length,0);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",pid)[0].children.slice(0,3).map(node=>node.textContent),
    ["助理要執行：echo hi","你允許了這次","還不確定決定有沒有送到；只核對原決定，不會重送。"]);
  saved.composer.native_hitl.commands=[decision("d".repeat(64),"allow_once","UNKNOWN"),decision("e".repeat(64),"deny","CONFIRMED")];
  ui.render(saved);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",pid)[0].children.map(node=>node.textContent),
    ["助理要執行：echo hi","你拒絕了"]);
  assert.equal(posts.length,1);
});

test("HITL 10-07: question options are one list the keyboard can walk with the arrow keys",async()=>{
  const {data,qid}=inlineQuestionChat("foundry-native-v1");
  const ui=await open(data,{now:Date.parse(when)});
  const [ann,bo]=hitlNodes(ui,"data-question-choice",qid),group=hitlNodes(ui,"role","group")[0];
  assert.equal(group.getAttribute("aria-label"),"選項");
  assert.deepEqual(group.children,[ann,bo]);
  let prevented=0;const key=name=>({key:name,preventDefault(){prevented++}});
  ui.context.document.activeElement=ann;group.events.keydown(key("ArrowDown"));
  assert.equal(bo.focused,true);
  ui.context.document.activeElement=bo;group.events.keydown(key("ArrowDown"));
  assert.equal(ann.focused,true);
  group.events.keydown(key("Tab"));
  assert.equal(prevented,2);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

// Routine from chat (10-07): the assistant drafts a daily schedule; only the owner's click on its card saves it, and
// the 排程 settings tab lists this Agent's schedules and disables one against the exact version shown.
const ROUTINE_DRAFT={local_time:"09:00",time_zone:"Asia/Taipei",content:"檢查 Foundry 有沒有新文章"};
const ROUTINE_TEXT="每天 09:00（Asia/Taipei）在這個對話執行：「檢查 Foundry 有沒有新文章」";
test("routine from chat: a schedule card shows the exact draft, says only the owner's click counts, and has no rule choice",async()=>{
  const data=hitlAgent("agent-one"),view=hitlRead(data),pid=view.readback.approval.pending_id,posts=[];
  const action=view.readback.pending.command.action;
  Object.assign(action,{category:"routine",tool:"propose_routine",target:"conversation:"+action.call.conversation_id,
    arguments:copy(ROUTINE_DRAFT)});
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>{
    if(options.method==="POST"){
      posts.push(JSON.parse(options.body));
      return {ok:true,json:async()=>hitlCommandReply(posts.at(-1))};
    }
    return {ok:true,json:async()=>copy(view)};
  }});
  await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-summary",pid)[0].textContent,"要排程："+ROUTINE_TEXT);
  assert.match(hitlNodes(ui,"data-hitl-reason",pid)[0].textContent,/助理說的話或你在對話裡的回覆都不算批准/);
  assert.match(hitlNodes(ui,"data-hitl-reason",pid)[0].textContent,/要停用請到「助理清單右側的 ⚙ → 排程」/);
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_rule").length,0,"a schedule never becomes a Team rule");
  hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,true,"the card already shows the exact schedule");
  assert.equal(posts.length,1);assert.equal(posts[0].choice,"allow_once");assert.equal(posts[0].rule,null);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",pid)[0].children.map(node=>node.textContent),
    ["助理要排程："+ROUTINE_TEXT,"你允許了這次"]);
});

test("routine from chat: a failed schedule decision points the owner to settings to check it",async()=>{
  const data=hitlAgent("agent-one"),view=hitlRead(data),pid=view.readback.approval.pending_id;
  const action=view.readback.pending.command.action;
  Object.assign(action,{category:"routine",tool:"propose_routine",target:"conversation:"+action.call.conversation_id,
    arguments:copy(ROUTINE_DRAFT)});
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>options.method==="POST"?
    {ok:false,status:503,json:async()=>({error:{code:"BACKEND_OFFLINE"}})}:{ok:true,json:async()=>copy(view)}});
  await ui.tick();await ui.tick();
  hitlNodes(ui,"data-hitl-choice","allow_once")[0].events.click();
  for(let i=0;i<6;i++)await ui.tick();
  const card=ui.nodes.get("attention-items").children.find(node=>node.attributes?.["data-hitl-card"]===pid)
    ||hitlNodes(ui,"data-hitl-card",pid)[0];
  assert.ok(card,"the card is shown again");
  assert.match(card.children[1].textContent,/請到「助理清單右側的 ⚙ → 排程」核對這個排程是否已建立/);
});

test("routine from chat: a saved schedule decision reads back as what its card showed",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0],thread=data.composer.thread_id;
  data.composer.native_hitl={enabled:true,can_approve:true,references:[],commands:[],error:null};
  data.composer.attention=[];
  data.composer.native_hitl.commands=[{request_id:"d".repeat(64),kind:"decision",state:"CONFIRMED",thread_id:thread,code:null,
    receipt:null,observation:null,command:{kind:"decision",request_id:"d".repeat(64),pending_id:"3".repeat(64),
      pending_digest:"b".repeat(64),choice:"allow_once",rule:null,expected_rules_revision:0,expected_rules_etag:null},
    selection:{pending_id:"3".repeat(64),pending_digest:"b".repeat(64),call:{agent_id:"configured-agent",
      conversation_id:task.admission.conversation_id,request_id:task.task_id,sdk_session_id:"sdk",tool_call_id:"routine"}},
    display:{tool:"propose_routine",category:"routine",target:"conversation:x",command:null,toolbox:false,
      routine:copy(ROUTINE_DRAFT)},
    created_at:"2026-01-01T00:01:00+00:00"}];
  const ui=await open(data,{now:Date.parse(when)});
  const slot=turnNode(ui).children.find(node=>node.className==="turn-attention");
  assert.deepEqual(slot.children.map(node=>node.children.map(line=>line.textContent)),
    [["助理要排程："+ROUTINE_TEXT,"你允許了這次"]]);
});

const routineView=(id,{enabled=true,version="v-"+id,content=ROUTINE_DRAFT.content}={})=>({definition:{
  request_id:version,routine_id:id,expected_version_id:null,conversation_id:"conversation-"+id+"-abcdef",kind:"routine",
  schedule:{kind:"daily",time_zone:"Asia/Taipei",local_time:"09:00"},content,trigger_data:{},enabled,
  tenant_id:"tenant",team_id:"team",agent_id:"configured-agent",owner_oid:"owner",approved_at:"2026-01-01T00:00:00Z"},
  definition_sha256:"e".repeat(64)});
const routinesRead=(routines,manage=true)=>({schema_version:"fa-console-routines-v1",agent_id:"configured-agent",
  routines,can_manage:manage});
async function openRoutineSettings(read,available=true){
  const ui=await open({...templateData(),...(available?{routines_available:true}:{})},{templateResponses:{
    "/api/agent-template":{ok:true,json:async()=>copy(settingsBase())},
    "/api/agent-template/options":{ok:true,json:async()=>copy(settingsOptions())},
    "/api/agent-template/selection":selectionRead(settingsCurrent,[]),
  }});
  let current=copy(read);
  Object.defineProperty(ui.holder.templateResponses,"/api/routines",{configurable:true,enumerable:true,
    get:()=>reply(current)});
  ui.setRoutines=value=>{current=copy(value)};
  agentGear(ui,"configured-agent").events.click();await ui.tick();await ui.tick();
  if(available){ui.click("settings-tab-routines");await settleReviews(ui)}
  return ui;
}
const routineRows=ui=>ui.nodes.get("routines-list").children;
const routinePosts=ui=>ui.calls.filter(call=>call.method==="POST"&&call.url.startsWith("/api/routines/"));

test("routine settings: the 排程 tab appears only where routines are on",async()=>{
  const off=await openRoutineSettings(routinesRead([]),false);
  assert.equal(off.nodes.get("settings-tab-routines").hidden,true);
  assert.equal(off.calls.some(call=>call.url==="/api/routines"),false);
  const on=await openRoutineSettings(routinesRead([]));
  assert.equal(on.nodes.get("settings-tab-routines").hidden,false);
  assert.equal(on.nodes.get("settings-routines").hidden,false);
  assert.equal(on.nodes.get("settings-catalog").hidden,true);
  assert.deepEqual(shownTabs(on),[...AGENT_TABS,"routines"],"排程 is one Agent's own tab, never on the Team page");
  assert.equal(on.text("routines-note"),"這位助理目前沒有排程。");
  assert.match(html,/排程只能在對話裡建立：助理提出每日時間和要做的事，你在批准卡上按「允許這次」才會生效。/);
});

test("routine settings: the owner disables one schedule against the version shown, and a lost reply resends that same version",async()=>{
  const ui=await openRoutineSettings(routinesRead([routineView("r1"),routineView("r2",{enabled:false})]));
  assert.equal(routineRows(ui).length,2);
  assert.match(routineRows(ui)[0].children[0].textContent,/每天 09:00（Asia\/Taipei）· 啟用中/);
  assert.equal(routineRows(ui)[0].children[1].textContent,ROUTINE_DRAFT.content);
  assert.match(routineRows(ui)[0].children[2].textContent,/來源對話 conversa · 批准於/);
  assert.match(routineRows(ui)[1].children[0].textContent,/已停用/);
  assert.equal(routineRows(ui)[1].children.length,3,"a disabled schedule has no button");
  const path="/api/routines/r1/versions/v-r1/disable";
  ui.holder.templateResponses[path]={ok:false,json:async()=>({error:{code:"LOCAL_OFFLINE"}})};
  routineRows(ui)[0].children[3].children[0].events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.match(ui.holder.confirmation,/停用這個排程？\n每天 09:00（Asia\/Taipei）在這個對話執行：「檢查 Foundry 有沒有新文章」/);
  assert.equal(routinePosts(ui).length,1);
  assert.equal(routinePosts(ui)[0].url,path);assert.equal(routinePosts(ui)[0].body,"{}");
  assert.match(ui.text("routines-note"),/沒有回應/);
  const off=routineView("r1",{enabled:false,version:"v-r1-off"});
  ui.holder.templateResponses[path]=reply({routine:off});
  ui.setRoutines(routinesRead([off,routineView("r2",{enabled:false})]));
  routineRows(ui)[0].children[3].children[0].events.click();await ui.answer(true);await settleReviews(ui,12);
  assert.equal(routinePosts(ui).length,2);
  assert.equal(routinePosts(ui)[1].url,path,"the resend names the same version the owner saw");
  assert.equal(ui.text("routines-note"),"已停用；下一次到點不會執行。");
  assert.match(routineRows(ui)[0].children[0].textContent,/已停用/);
  assert.equal(routineRows(ui)[0].children.length,3);
});

test("routine settings: a cancelled confirmation or a read-only login sends nothing; 確定 sends one disable",async()=>{
  const ui=await openRoutineSettings(routinesRead([routineView("r1")]));
  routineRows(ui)[0].children[3].children[0].events.click();
  assert.match(await ui.answer(false),/^停用這個排程？\n/);await settleReviews(ui);
  assert.equal(routinePosts(ui).length,0,"取消 sends nothing");
  const disable=routineRows(ui)[0].children[3].children[0];
  disable.events.click();disable.events.click();await ui.tick();
  ui.click("page-confirm-ok");ui.click("page-confirm-ok");await settleReviews(ui);
  assert.deepEqual(routinePosts(ui).map(call=>[call.url,call.body]),[["/api/routines/r1/versions/v-r1/disable","{}"]]);
  const reader=await openRoutineSettings(routinesRead([routineView("r1")],false));
  assert.equal(routineRows(reader)[0].children.length,3,"no disable button without Behavior.Approve");
  assert.equal(routinePosts(reader).length,0);
});
// dev 10-07 (#732 P4): a scheduled turn that ran while the owner looked at another Agent only showed after 更新回覆.
// It is none of this page's own turns, so opening the conversation again, or this Agent's push hint, reads it in.
function scheduledTurnState(base){
  const after=copy(base),scheduled=copy(after.tasks[0]);
  Object.assign(scheduled,{task_id:"scheduled-rid",submit_request_id:"scheduled-rid",instruction:null,agent_text:null,
    source:null,routine_text:"每天檢查 Foundry 有沒有新文章",submission:{state:"OBSERVED",method:"GET"},
    routine:{occurrence:{routine_id:"daily",approved_version:"v1",scheduled_for:when,time_zone:"Asia/Taipei"},
      authorization:false}});
  scheduled.reply={data:{text:"排程的回覆"},sha256:"b".repeat(64),byte_length:15,observed_at:when};
  after.tasks.push(scheduled);
  after.composer.conversation.turns.push({request_id:"scheduled-rid",state:"completed"});
  return after;
}
async function fireImmediate(ui){
  for(let round=0;round<4;round++){
    const timer=[...ui.timers.entries()].find(([id])=>ui.timerDelays.get(id)===0);
    if(!timer)return;
    ui.timers.delete(timer[0]);await timer[1]();for(let i=0;i<4;i++)await ui.tick();
  }
}
const scheduledShown=ui=>/排程觸發（不是你的新訊息）/.test(JSON.stringify(ui.nodes.get("conversation-history").children,
  (key,value)=>key==="events"?undefined:value));
test("scheduled turn: opening the conversation again reads its whole history once and shows the scheduled turn",async()=>{
  const data=nativeChat("completed"),after=scheduledTurnState(data);
  data.composer.observed_at=after.composer.observed_at=when;
  const ui=await open(data,{progressResponse:{ok:true,json:async()=>copy(after)}});
  for(let i=0;i<4;i++)await ui.tick();
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")).length;
  assert.equal(reads(),0,"a settled conversation is not polled");
  assert.equal(scheduledShown(ui),false);
  await ui.context.switchThread(data.composer.thread_id);
  await fireImmediate(ui);
  assert.equal(reads(),1,"one whole-conversation read after opening");
  assert.equal(scheduledShown(ui),true);
  await fireImmediate(ui);
  assert.equal(reads(),1,"and only one");
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

test("scheduled turn: a first open reads the latest turn quickly and then the whole conversation once",async()=>{
  const data=nativeChat("completed"),after=scheduledTurnState(data);
  data.composer.observed_at=null;after.composer.observed_at=when;
  const ui=await open(data,{progressResponse:url=>({ok:true,json:async()=>copy(url.startsWith("/api/instructions/progress")?after:data)})});
  for(let i=0;i<4;i++)await ui.tick();
  await ui.context.switchThread(data.composer.thread_id);
  await fireImmediate(ui);
  const urls=ui.calls.map(call=>call.url).filter(url=>url.startsWith("/api/instructions/"));
  assert.ok(urls.indexOf(`/api/instructions/${data.tasks[0].task_id}/status`)>=0,"the quick read of the latest turn");
  assert.equal(urls.filter(url=>url.startsWith("/api/instructions/progress")).length,1);
  assert.ok(urls.indexOf(`/api/instructions/${data.tasks[0].task_id}/status`)<urls.findIndex(url=>url.startsWith("/api/instructions/progress")));
  assert.equal(scheduledShown(ui),true);
});

test("scheduled turn: this Agent's push for a turn this page did not send reads the open conversation once",async()=>{
  const data=nativeChat("completed"),scope=agentChat("agent-one");
  for(const key of ["agent_mode","default_agent_id","selected_agent_id","agent_directory_error","agents"])data[key]=scope[key];
  data.composer.observed_at=when;
  const after=scheduledTurnState(data);
  const ui=await open(data,{realtime:true,progressResponse:{ok:true,json:async()=>copy(after)}});
  const socket=await connectPush(ui);
  for(let i=0;i<4;i++)await ui.tick();
  const reads=()=>ui.calls.filter(call=>call.url.startsWith("/api/instructions/progress")).length,before=reads();
  pushEvent(socket,{type:"turn.finished",agent_id:"agent-two",request_id:"other-agent-rid"});
  await ui.tick();await ui.tick();
  assert.equal(reads(),before,"another Agent's turn reads nothing here");
  pushEvent(socket,{type:"turn.finished",agent_id:"agent-one",request_id:"scheduled-rid"});
  for(let i=0;i<4;i++)await ui.tick();
  assert.equal(reads(),before+1);
  assert.equal(scheduledShown(ui),true);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});
for(const failure of ["503","throw"]){
  test(`scheduled turn: a failed opening read (${failure}) after a switch backs off instead of reading again at once`,async()=>{
    const data=nativeChat("completed");data.composer.observed_at=null;
    const failed=()=>{if(failure==="throw")throw new Error("offline");
      return {ok:false,status:503,json:async()=>({error:{code:"BACKEND_UNAVAILABLE"}})}};
    const ui=await open(data,{progressResponse:url=>url.startsWith("/api/instructions/progress")?{ok:true,json:async()=>copy(data)}:failed()});
    for(let i=0;i<4;i++)await ui.tick();
    const reads=()=>ui.calls.filter(call=>/\/status$|\/api\/instructions\/progress/.test(call.url)).length;
    const before=reads();
    await ui.context.switchThread(data.composer.thread_id);
    await fireImmediate(ui);
    assert.equal(reads()-before,1,"one read, not a 0 ms loop");
    const delays=[...ui.timers.keys()].map(id=>ui.timerDelays.get(id));
    assert.ok(delays.some(delay=>delay>=2000),"the next read waits for its backoff");
    assert.equal(delays.includes(0),false);
  });
}
test("HITL 10-10: a card from the owner's own turn shows 等你決定 without a deadline and stays decidable until it ends",async()=>{
  const data=hitlChat();
  data.composer.attention[0].item.expires_at="2026-01-02T00:00:00Z";
  const view=hitlRead(data),pid=view.readback.approval.pending_id;
  view.readback.pending.command.wait="interactive";
  const ui=await open(data,{now:Date.parse(when)+3*60*60*1000,hitl:async()=>({ok:true,json:async()=>copy(view)})});
  await ui.tick();await ui.tick();
  const card=hitlNodes(ui,"data-hitl-card",pid)[0];
  assert.equal(card.children[1].textContent,"等你決定");
  assert.equal(hitlNodes(ui,"data-hitl-choice","allow_once")[0].disabled,false);
  assert.doesNotMatch(JSON.stringify(hitlNodes(ui,"data-hitl-details",pid)[0]),/原期限|expires_at/);
  view.readback.status="deny";
  hitlNodes(ui,"data-hitl-read",pid)[0].events.click();
  await ui.tick();await ui.tick();
  assert.equal(hitlNodes(ui,"data-hitl-card",pid)[0].children[1].textContent,"你已拒絕這次操作。");
  assert.ok(hitlNodes(ui,"data-hitl-choice","allow_once").every(button=>button.disabled));
});

test("HITL 10-10: a question from the owner's own turn waits without a deadline; a background one keeps its time",async()=>{
  for(const wait of ["interactive","background"]){
    const data=hitlChat(),entry=questionItem(data.composer.thread_id),qid=entry.item.item_id;
    if(wait==="interactive")Object.assign(entry.item,{wait,expires_at:"2026-01-02T00:00:00Z"});
    data.composer.attention=[entry];
    const ui=await open(data,{now:Date.parse(when)+5*60*1000,hitl:async()=>({ok:true,json:async()=>({})})});
    const card=hitlNodes(ui,"data-question-card",qid)[0];
    const note=card.children.find(child=>/這只是回答問題/.test(child.textContent||""));
    assert.equal(note.textContent.split("；")[0],wait==="interactive"?"等你回答":"請在 08:10 前回答");
    assert.equal(hitlNodes(ui,"data-question-choice",qid).every(button=>!button.disabled),true);
  }
});

// C3: Connect cards. The link comes only from the card's own read; one click resumes at most once.
const CONSENT_LINK="https://consent.example.test/login?state=OFFLINE";
function consentItem(thread,need="consent"){
  const cid="d".repeat(64);
  return {thread_id:thread,label:"查 Teams",item:{schema_version:"fa-native-attention-v1",type:"connect_required",
    item_id:cid,consent:{consent_id:cid,consent_digest:"f".repeat(64),call:{agent_id:"configured-agent",
      conversation_id:"original-cid",request_id:"original-rid",sdk_session_id:"original-sdk",tool_call_id:"connect-local-1"}},
    need,connector_id:"teams",server_label:"teams_read",step:"tools/list",created_at:when,
    expires_at:"2026-01-02T00:00:00Z",purpose:"attention-only-not-approval",wait:"interactive"}};
}
function consentRead(item){
  return {read_at:when,commands:[],connector:{display_name:"Microsoft Teams",disclosure:"Teams 也可能包含你的郵件。",
    revoke_url:"https://entra.example.test/apps"},readback:{schema_version:"fa-native-hitl-consent-readback-v1",
    reference:item.consent,status:"pending",decision:null,attention:item,observed_at:when,expired:false,turn_closed:false,
    purpose:"readback-only-not-execution-permission",consent:{consent_id:item.item_id,created_at:when,expires_at:item.expires_at,
      command:{consent:{call:item.consent.call,need:item.need,connector_id:"teams",toolbox_name:"fa-connector-team-one-teams",
        server_label:"teams_read",step:"tools/list",consent_urls:item.need==="consent"?[CONSENT_LINK]:[],attempt:1}}}}};
}
function consentClicked(item,choice,request_id="a".repeat(64)){
  return {request_id,original_request_resent:false,purpose:"readback-only-not-execution-permission",
    commands:[{request_id,kind:"consent_decision",state:"CONFIRMED",thread_id:"attachment-thread",code:null,
      command:{kind:"consent_decision",request_id,consent_id:item.item_id,consent_digest:item.consent.consent_digest,choice},
      selection:item.consent,observation:null,display:{connector_id:"teams",need:item.need,name:"Microsoft Teams"}}]};
}
function consentCase(need){
  const data=hitlChat(),entry=consentItem(data.composer.thread_id,need),reads=[],posts=[];
  data.composer.attention=[entry];
  const hitl=async(url,options)=>{
    if(options.method==="GET"){reads.push(url);return {ok:true,json:async()=>consentRead(entry.item)}}
    const body=JSON.parse(options.body);posts.push({url,body});
    return {ok:true,json:async()=>consentClicked(entry.item,body.choice)};
  };
  return {data,entry,cid:entry.item.item_id,reads,posts,hitl};
}
test("a Connect card shows its link from its own read only, and 「我連好了，繼續」 is sent once",async()=>{
  const c=consentCase("consent");
  const ui=await open(c.data,{now:Date.parse(when),hitl:c.hitl});
  await ui.tick();await ui.tick();await ui.tick();
  assert.deepEqual(c.reads,[`/api/native-hitl/${c.data.composer.thread_id}/consents/${c.cid}`]);
  const card=hitlNodes(ui,"data-consent-card",c.cid)[0];
  assert.equal(card.children[0].textContent,"需要連線：查 Teams");
  assert.equal(card.children[1].textContent,"這位助理要使用 Microsoft Teams，但還沒連線。連線只屬於這位助理，其他助理要各自連一次。");
  assert.equal(hitlNodes(ui,"data-consent-disclosure",c.cid)[0].textContent,"資料範圍：Teams 也可能包含你的郵件。");
  const link=hitlNodes(ui,"data-consent-link",c.cid)[0];
  assert.deepEqual([link.tagName,link.href,link.target,link.rel,link.textContent],
    ["A",CONSENT_LINK,"_blank","noopener noreferrer","連線 Microsoft Teams"]);
  assert.equal(hitlNodes(ui,"data-consent-revoke",c.cid).length,0);
  assert.match(card.children.at(-1).textContent,/原訊息會自動接續；只會接續一次，仍未連好就會停下。/);
  const resume=hitlNodes(ui,"data-consent-resume",c.cid)[0],later=hitlNodes(ui,"data-consent-decline",c.cid)[0];
  assert.equal(resume.textContent,"我連好了，繼續");
  resume.events.click();resume.events.click();later.events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.equal(c.posts.length,1);
  assert.ok(c.posts[0].url.endsWith(`/${c.data.composer.thread_id}/consents`));
  assert.deepEqual(c.posts[0].body,{consent:c.entry.item.consent,choice:"resume"});
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",c.cid)[0].children.map(node=>node.textContent),
    ["助理要連接 Microsoft Teams","你按了「我連好了，繼續」"]);
  assert.equal(hitlNodes(ui,"data-consent-card",c.cid).length,0);
});
test("a revoked connection's card has no consent link, points to the service's page and can be declined",async()=>{
  const c=consentCase("reconnect");
  const ui=await open(c.data,{now:Date.parse(when),hitl:c.hitl});
  await ui.tick();await ui.tick();await ui.tick();
  const card=hitlNodes(ui,"data-consent-card",c.cid)[0];
  assert.equal(card.children[0].textContent,"連線需要重新建立：查 Teams");
  assert.match(card.children[1].textContent,/Microsoft Teams 連線已失效.*FA 不會自動重試。/);
  assert.equal(hitlNodes(ui,"data-consent-link",c.cid).length,0);
  assert.equal(hitlNodes(ui,"data-consent-revoke",c.cid)[0].href,"https://entra.example.test/apps");
  hitlNodes(ui,"data-consent-decline",c.cid)[0].events.click();
  await ui.tick();await ui.tick();await ui.tick();
  assert.deepEqual(c.posts.map(post=>post.body.choice),["decline"]);
  assert.deepEqual(hitlNodes(ui,"data-hitl-record",c.cid)[0].children.map(node=>node.textContent),
    ["助理的 Microsoft Teams 連線需要重新建立","你選了先不用"]);
});

// Connectors (C4): the Teams card in Agent Team 設定 and each Agent's switch. Every change is one confirmed POST with a
// request ID chosen once; an unknown outcome is resent only as the same body; the client secret is never kept.
const CONNECTOR_SECRET="OFFLINE-connector-secret-9f3a";
const TEAMS_DISCLOSURE="Microsoft Teams 經 Work IQ 連線。Agent 能讀取的不只 Teams，也可能包含你的 Microsoft 365 郵件與檔案。";
const connectorCard=(status="not-added",version=null,settings=null)=>({connector_id:"teams",display_name:"Microsoft Teams",
  disclosure:TEAMS_DISCLOSURE,revoke_url:"https://entra.microsoft.com",version,status,settings});
const connectorSettings=(enabled=true,read="never",write="always")=>({enabled,read_approval:read,write_approval:write});
const connectorView=(card=connectorCard(),manage=true)=>({schema_version:"fa-console-team-connectors-v1",can_manage:manage,
  connectors:[card]});
const connectorApplied=(request_id,version,card)=>({schema_version:"fa-console-team-connector-change-v1",request_id,
  state:"applied",toolbox_version:version,connector:card,original_request_resent:false});
function agentConnectorsView(revision=0,on=true,manage=true){
  return {schema_version:"fa-console-agent-connectors-v1",revision,applies_to:"new-conversations-only",can_manage:manage,
    connectors:[{connector_id:"teams",display_name:"Microsoft Teams",on}]};
}
async function openConnectors(view=connectorView()){
  const ui=await settingsUi();
  let current=copy(view);
  Object.defineProperty(ui.holder.templateResponses,"/api/team-connectors",{configurable:true,enumerable:true,
    get:()=>current.error?{ok:false,json:async()=>copy(current)}:reply(current)});
  Object.assign(ui.holder.templateResponses,{"/api/team-mcp/servers":reply(teamMcpView()),
    "/api/team-mcp/connections":reply(teamMcpConnections)});
  ui.setConnectors=value=>{current=copy(value)};
  return openTeamSettings(ui,"team-connectors");
}
const connectorRow=ui=>ui.nodes.get("team-connectors-list").children[0];
const connectorFields=ui=>connectorRow(ui).children.filter(node=>node.tagName==="LABEL").map(node=>node.children[0]);
const connectorButtons=ui=>connectorRow(ui).children.find(node=>node.className==="actions")?.children||[];
const connectorButton=(ui,label)=>connectorButtons(ui).find(node=>node.textContent===label);
const connectorPosts=ui=>ui.calls.filter(call=>call.method==="POST"&&call.url==="/api/team-connectors/changes");
const connectorBody=(ui,index)=>({...JSON.parse(connectorPosts(ui)[index].body),request_id:null});
function typeConnectorSecret(ui,value){
  const secret=connectorFields(ui).find(node=>node.type==="password");secret.value=value;secret.events.input();return secret;
}
// Everything the page holds: its state variables, every node's text and value, and browser storage.
function pageHolds(ui,value){
  const held=vm.runInContext("JSON.stringify([teamConnectors,teamConnectorRetry,agentConnectors,agentConnectorRetry])",ui.context);
  const nodes=[];
  const walk=node=>{if(!node||typeof node!=="object")return;nodes.push(node);(node.children||[]).forEach(walk)};
  ui.nodes.forEach(walk);
  return held.includes(value)||nodes.some(node=>String(node.textContent).includes(value)||String(node.value).includes(value))
    ||[...ui.context.localStorage.values.values()].some(item=>item.includes(value));
}

test("connectors: the Teams card says what it can read and that only a real call proves the connection",async()=>{
  const ui=await openConnectors();
  assert.deepEqual(shownTabs(ui),TEAM_TABS);
  assert.equal(ui.nodes.get("settings-team-connectors").hidden,false);
  assert.equal(ui.nodes.get("settings-team-mcp").hidden,true,"the generic Team MCP form is a separate tab");
  const row=connectorRow(ui);
  assert.deepEqual(row.children.slice(0,3).map(node=>node.textContent),["Microsoft Teams",TEAMS_DISCLOSURE,"尚未加入。"]);
  const about=row.children[3];
  assert.equal(about.tagName,"DETAILS");
  assert.deepEqual(about.children.map(node=>node.textContent),["說明",
    "「已加入」只表示 Team 已設定好這個 connector，不表示授權現在有效。每位助理第一次用到時，對話裡會出現連線卡，用你的帳號同意一次；每位助理各要同意一次。只有實際呼叫成功，才知道當次連得上。",
    "停用不會撤銷授權。要撤銷 Microsoft 365 的授權，請管理員到 Entra 系統管理中心撤銷這個應用程式的權限（會影響整個組織）。",
    "開啟 Entra 系統管理中心"]);
  assert.equal(about.children[3].href,"https://entra.microsoft.com");
  const [read,write,secret]=connectorFields(ui);
  assert.deepEqual([read.value,write.value,secret.type],["never","always","password"]);
  assert.deepEqual(connectorButtons(ui).map(node=>[node.textContent,node.disabled]),[["加入 Microsoft Teams",true]]);
  ui.setConnectors(connectorView(connectorCard("enabled","v1",connectorSettings())));
  ui.click("team-connectors-read");await settleReviews(ui);
  assert.equal(connectorRow(ui).children[2].textContent,"已加入 · 連線狀態：要實際使用才知道");
  assert.deepEqual(connectorButtons(ui).map(node=>[node.textContent,node.disabled]),[["儲存批准設定",true],["停用",false]]);
  assert.equal(connectorFields(ui).some(node=>node.type==="password"),false,"an added connector never asks for the secret");
  assert.doesNotMatch(JSON.stringify(ui.nodes.get("team-connectors-list").children,(key,value)=>key==="events"?undefined:value),
    /client_id|fa-connector-|consent|token/i);
  ui.click("close-settings");
  assert.equal(ui.nodes.get("settings-button").focused,true,"closing Agent Team 設定 returns focus to its ⚙");
  assert.equal(connectorPosts(ui).length,0);
});

test("connectors: the first add sends the secret once in one body, clears it and never keeps or shows it",async()=>{
  const ui=await openConnectors();
  const secret=typeConnectorSecret(ui,CONNECTOR_SECRET);
  assert.equal(connectorButton(ui,"加入 Microsoft Teams").disabled,false);
  connectorButton(ui,"加入 Microsoft Teams").events.click();connectorButton(ui,"加入 Microsoft Teams").events.click();
  ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.equal(connectorPosts(ui).length,1);
  assert.equal(secret.value,"","the field is cleared as soon as the add is sent");
  assert.match(ui.holder.confirmation,/^加入 Microsoft Teams？\nMicrosoft Teams 經 Work IQ 連線。Agent 能讀取的不只 Teams，也可能包含你的 Microsoft 365 郵件與檔案。\n讀取：不用批准，直接執行；寫入：每次都要我批准。\n/);
  assert.doesNotMatch(ui.holder.confirmation,new RegExp(CONNECTOR_SECRET));
  const sent=JSON.parse(connectorPosts(ui)[0].body);
  assert.match(sent.request_id,/^[a-f0-9]{32}$/);
  assert.deepEqual(connectorBody(ui,0),{schema_version:"fa-team-connector-change-v1",request_id:null,connector_id:"teams",
    expected_version:null,enabled:true,read_approval:"never",write_approval:"always",client_secret:CONNECTOR_SECRET});
  ui.holder.pending(connectorApplied(sent.request_id,"v1",connectorCard("enabled","v1",connectorSettings())));
  await settleReviews(ui);
  assert.equal(ui.text("team-connectors-note"),"已加入 Microsoft Teams（toolbox 版本 v1）。");
  assert.equal(connectorRow(ui).children[2].textContent,"已加入 · 連線狀態：要實際使用才知道");
  assert.equal(pageHolds(ui,CONNECTOR_SECRET),false,"the page holds the secret nowhere after the add");
  assert.equal(pageHolds(ui,"Microsoft 365 郵件與檔案"),true,"the same check finds what the page does show");
});

test("connectors: an unknown add is resent only with its original body, after the secret is typed again",async()=>{
  const ui=await openConnectors();
  ui.holder.templateResponses["/api/team-connectors/changes"]={ok:false,
    json:async()=>({error:{code:"TEAM_CONNECTOR_OUTCOME_UNKNOWN"}})};
  typeConnectorSecret(ui,CONNECTOR_SECRET);
  connectorButton(ui,"加入 Microsoft Teams").events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.equal(ui.text("team-connectors-note"),"Connector 變更結果未確認；請按「用原編號重送」，不會建立第二個版本。");
  assert.equal(pageHolds(ui,CONNECTOR_SECRET),false,"an unconfirmed add keeps its body without the secret");
  assert.equal(ui.nodes.get("team-connectors-retry").hidden,false);
  assert.equal(ui.nodes.get("team-connectors-retry").disabled,true,"the secret must be typed again first");
  assert.equal(connectorButton(ui,"加入 Microsoft Teams").disabled,true,"no new change while one is unconfirmed");
  assert.equal(connectorRow(ui).children.at(-2).textContent,"上一個加入的結果未確認：請再輸入一次用戶端密碼，再按上方「用原編號重送」。");
  typeConnectorSecret(ui,CONNECTOR_SECRET);
  assert.equal(ui.nodes.get("team-connectors-retry").disabled,false);
  const first=JSON.parse(connectorPosts(ui)[0].body);
  ui.holder.templateResponses["/api/team-connectors/changes"]=reply(
    connectorApplied(first.request_id,"v1",connectorCard("enabled","v1",connectorSettings())));
  ui.setConnectors(connectorView(connectorCard("enabled","v1",connectorSettings())));
  ui.click("team-connectors-retry");ui.holder.confirmation=await ui.answer(true);await settleReviews(ui,12);
  assert.equal(connectorPosts(ui).length,2);
  assert.equal(connectorPosts(ui)[1].body,connectorPosts(ui)[0].body,"the same body and request ID");
  assert.match(ui.holder.confirmation,/頁面沒有保存用戶端密碼，這次送出你剛重新輸入的。/);
  assert.equal(ui.nodes.get("team-connectors-retry").hidden,true);
  assert.equal(ui.text("team-connectors-note"),"已加入 Microsoft Teams（toolbox 版本 v1）。");
  assert.equal(pageHolds(ui,CONNECTOR_SECRET),false);
});

test("connectors: approval change, disable and re-enable each send one new version from the version read",async()=>{
  const ui=await openConnectors(connectorView(connectorCard("enabled","v1",connectorSettings())));
  const settle=async(version,card)=>{
    const sent=JSON.parse(connectorPosts(ui).at(-1).body);
    ui.holder.pending(connectorApplied(sent.request_id,version,card));await settleReviews(ui);
  };
  const [read]=connectorFields(ui);read.value="always";read.events.change();
  assert.equal(connectorButton(ui,"儲存批准設定").disabled,false);
  connectorButton(ui,"儲存批准設定").events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.match(ui.holder.confirmation,/^變更 Microsoft Teams 的批准設定？\n讀取：每次都要我批准；寫入：每次都要我批准。\n所有對話（含進行中的）從下一回合起套用。/);
  await settle("v2",connectorCard("enabled","v2",connectorSettings(true,"always")));
  assert.equal(ui.text("team-connectors-note"),"已更新 Microsoft Teams 的批准設定（toolbox 版本 v2）。");
  connectorButton(ui,"停用").events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.match(ui.holder.confirmation,/^停用 Microsoft Teams？\n所有助理（含進行中的對話）從下一回合起都不能再用。不會撤銷授權/);
  await settle("v3",connectorCard("disabled","v3",connectorSettings(false,"always")));
  assert.equal(connectorRow(ui).children[2].textContent,"已加入，但已停用：所有助理從下一回合起都不能用 · 連線狀態：要實際使用才知道");
  const [, write]=connectorFields(ui);write.value="never";write.events.change();
  connectorButton(ui,"重新啟用").events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  await settle("v4",connectorCard("enabled","v4",connectorSettings(true,"always")));
  assert.equal(ui.text("team-connectors-note"),"已重新啟用 Microsoft Teams（toolbox 版本 v4）。");
  const change=(expected_version,enabled)=>({schema_version:"fa-team-connector-change-v1",request_id:null,
    connector_id:"teams",expected_version,enabled,read_approval:"always",write_approval:"always"});
  assert.deepEqual([0,1,2].map(index=>connectorBody(ui,index)),[change("v1",true),change("v2",false),change("v3",true)],
    "disable and re-enable keep the saved approvals, not an unsaved edit");
  assert.equal(new Set(connectorPosts(ui).map(call=>JSON.parse(call.body).request_id)).size,3);
});

test("connectors: a stale version is refused, re-read and never resent",async()=>{
  const ui=await openConnectors(connectorView(connectorCard("enabled","v1",connectorSettings())));
  ui.holder.templateResponses["/api/team-connectors/changes"]={ok:false,json:async()=>({error:{code:"TEAM_CONNECTOR_CHANGED"}})};
  const reads=()=>ui.calls.filter(call=>call.url==="/api/team-connectors").length,before=reads();
  ui.setConnectors(connectorView(connectorCard("disabled","v2",connectorSettings(false))));
  connectorButton(ui,"停用").events.click();ui.holder.confirmation=await ui.answer(true);await settleReviews(ui);
  assert.equal(ui.text("team-connectors-note"),"Connector 設定剛被改過，這次沒有套用。已重新讀取，請確認後再改。");
  assert.equal(reads(),before+1);
  assert.equal(ui.nodes.get("team-connectors-retry").hidden,true);
  assert.deepEqual(connectorButtons(ui).map(node=>[node.textContent,node.disabled]),[["儲存批准設定",true],["重新啟用",false]]);
  assert.equal(connectorPosts(ui).length,1);
});

test("connectors: a read-only login sees the card without controls; someone else sees only the owner-only note",async()=>{
  const ui=await openConnectors(connectorView(connectorCard("enabled","v1",connectorSettings()),false));
  assert.equal(ui.text("team-connectors-note"),"只有 Team 主人，且目前登入具備 Behavior.Approve，才能加入、變更或停用；這個登入只能查看。");
  assert.deepEqual(connectorFields(ui),[]);assert.deepEqual(connectorButtons(ui),[]);
  assert.equal(connectorRow(ui).children.at(-1).textContent,"讀取：不用批准，直接執行；寫入：每次都要我批准。");
  ui.setConnectors({error:{code:"TEAM_CONNECTOR_OWNER_ONLY"}});
  ui.click("team-connectors-read");await settleReviews(ui);
  assert.equal(ui.text("team-connectors-note"),"Connectors 只有這個 Team 的真人主人能查看；變更還需要目前登入具備 Behavior.Approve。這個登入不能查看或變更。");
  assert.equal(ui.nodes.get("team-connectors-list").children.length,0);
  assert.equal(ui.calls.some(call=>call.method==="POST"),false);
});

const agentConnectorPosts=ui=>ui.calls.filter(call=>call.method==="POST"&&call.url==="/api/agent-connectors");
const agentConnectorSwitch=ui=>ui.nodes.get("settings-connectors-list").children[0].children[1];
async function openAgentConnectors(){
  const ui=await openSettingsPage();
  ui.click("settings-tab-mcp");await settleReviews(ui);
  return ui;
}

test("an Agent's switch turns a connector off for its new conversations only, sent once",async()=>{
  const ui=await openAgentConnectors();
  delete ui.holder.templateResponses["/api/agent-connectors"];
  assert.equal(ui.nodes.get("settings-connectors").hidden,false);
  const row=ui.nodes.get("settings-connectors-list").children[0];
  assert.deepEqual(row.children[0].children.map(node=>node.textContent),["Microsoft Teams","這位助理的新對話：使用（Team 加入並啟用時）"]);
  const toggle=agentConnectorSwitch(ui);
  assert.deepEqual([toggle.checked,toggle.disabled,toggle.getAttribute("role")],[true,false,"switch"]);
  toggle.checked=false;toggle.events.change();
  assert.equal(await ui.answer(true),"關閉「FA 助理」的 Microsoft Teams？\n只影響這位助理之後的新對話；進行中的對話不變。只送一次。");
  await settleReviews(ui);
  assert.equal(agentConnectorPosts(ui).length,1);
  const sent=JSON.parse(agentConnectorPosts(ui)[0].body);
  assert.match(sent.request_id,/^[a-f0-9]{32}$/);
  assert.deepEqual({...sent,request_id:null},{schema_version:"fa-agent-connectors-command-v1",request_id:null,
    expected_revision:0,disabled_connector_ids:["teams"]});
  ui.holder.pending(agentConnectorsView(1,false));await settleReviews(ui);
  assert.equal(ui.text("settings-connectors-note"),"已關閉 Microsoft Teams；只影響「FA 助理」之後的新對話。");
  assert.deepEqual([agentConnectorSwitch(ui).checked,ui.nodes.get("settings-connectors-list").children[0].children[0].children[1].textContent],
    [false,"這位助理的新對話：不使用"]);
  agentConnectorSwitch(ui).checked=true;agentConnectorSwitch(ui).events.change();await ui.answer(true);await settleReviews(ui);
  assert.deepEqual({...JSON.parse(agentConnectorPosts(ui)[1].body),request_id:null},{schema_version:"fa-agent-connectors-command-v1",
    request_id:null,expected_revision:1,disabled_connector_ids:[]});
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/team-")),false,"an Agent's page writes nothing of the Team");
});

test("an Agent's switch with an unknown outcome is resent only as the same body",async()=>{
  const ui=await openAgentConnectors();
  ui.holder.templateResponses["/api/agent-connectors"]={ok:false,json:async()=>({error:{code:"BACKEND_OFFLINE"}})};
  agentConnectorSwitch(ui).checked=false;agentConnectorSwitch(ui).events.change();await ui.answer(true);await settleReviews(ui);
  assert.equal(ui.nodes.get("settings-connectors-retry").hidden,false);
  assert.equal(agentConnectorSwitch(ui).disabled,true,"no new change while one is unconfirmed");
  ui.holder.templateResponses["/api/agent-connectors"]=reply(agentConnectorsView(1,false));
  ui.click("settings-connectors-retry");
  assert.match(await ui.answer(true),/^用原編號重送上一個開關變更？/);await settleReviews(ui);
  assert.equal(agentConnectorPosts(ui).length,2);
  assert.equal(agentConnectorPosts(ui)[1].body,agentConnectorPosts(ui)[0].body);
  assert.equal(ui.nodes.get("settings-connectors-retry").hidden,true);
  assert.equal(agentConnectorSwitch(ui).checked,false);
});

test("an Agent's switch is read-only without Approve and re-read for each Agent",async()=>{
  const a=agentChat("agent-one"),b=agentChat("agent-two");
  for(const data of [a,b]){data.template_tools_available=true;data.template_selections=[]}
  const ui=await open(a,{agentStates:{"agent-one":a,"agent-two":b},templateResponses:{
    "/api/agent-template":{ok:true,json:async()=>settingsBase()},
    "/api/agent-template/options":{ok:true,json:async()=>settingsOptions()},
    "/api/agent-template/selection":selectionRead(settingsCurrent,[]),
    "/api/agent-connectors":reply(agentConnectorsView(1,false,false))}});
  const reads=agent=>ui.calls.filter(call=>call.url==="/api/agent-connectors"&&call.headers?.["X-Console-Agent"]===agent).length;
  agentGear(ui,"agent-one").events.click();await settleReviews(ui);
  ui.click("settings-tab-mcp");await settleReviews(ui);
  assert.equal(reads("agent-one"),1);
  assert.deepEqual([agentConnectorSwitch(ui).checked,agentConnectorSwitch(ui).disabled],[false,true]);
  assert.equal(ui.text("settings-connectors-note"),"只有 Team 主人，且目前登入具備 Behavior.Approve，才能開關；這個登入只能查看。");
  ui.holder.templateResponses["/api/agent-connectors"]=reply(agentConnectorsView(0,true,true));
  agentGear(ui,"agent-two").events.click();await settleReviews(ui);
  ui.click("settings-tab-mcp");await settleReviews(ui);
  assert.equal(reads("agent-two"),1,"the newly selected Agent's own choice is read");
  assert.deepEqual([agentConnectorSwitch(ui).checked,agentConnectorSwitch(ui).disabled],[true,false]);
  assert.equal(ui.text("settings-title"),"第二位助理 的設定");
  ui.click("close-settings");
  assert.equal(agentGear(ui,"agent-two").focused,true,"closing an Agent's settings returns focus to its ⚙");
  assert.equal(agentConnectorPosts(ui).length,0);
});

// Neal 2026-10-12: a pending card of the open conversation sits in the dock above the composer, never in the chat scroll.
function walkNodes(node,visit){visit(node);for(const child of node.children||[])walkNodes(child,visit)}
test("HITL 10-12: a pending question shows in the dock, the turn keeps a one-liner, and the answer leaves a record inline",async()=>{
  const {data,qid}=inlineQuestionChat("foundry-native-v1");
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>({ok:true,json:async()=>answerReply(
    data.composer.attention[0].item,JSON.parse(options.body))})});
  const dock=ui.nodes.get("attention-dock"),slot=turnNode(ui).children.find(node=>node.className==="turn-attention");
  assert.equal(dock.hidden,false);
  assert.equal(hitlNodes(ui,"data-question-card",qid).length,1);
  const inDock=[];walkNodes(dock,node=>{if(node.attributes?.["data-question-card"]===qid)inDock.push(node)});
  assert.equal(inDock.length,1,"the card is in the dock");
  const inSlot=[];walkNodes(slot,node=>{if(node.attributes?.["data-question-card"]===qid)inSlot.push(node)});
  assert.equal(inSlot.length,0,"not in the turn");
  assert.deepEqual(slot.children.map(node=>node.textContent),["助理有問題要問你，卡片在輸入框上方。"]);
  const ids=[];for(const root of [dock,ui.nodes.get("conversation-history")])walkNodes(root,node=>{if(node.attributes?.id)ids.push(node.attributes.id)});
  assert.equal(new Set(ids).size,ids.length,"no duplicate ids");
  hitlNodes(ui,"data-question-choice",qid)[0].events.click();
  await ui.tick();await ui.tick();
  assert.equal(dock.hidden,true);assert.equal(dock.children.length,0);
  const record=hitlNodes(ui,"data-hitl-record",qid);
  assert.equal(record.length,1);
  assert.ok(record[0].children[1].textContent.startsWith("你的回答："));
});

test("HITL 10-12: a pending approval shows in the dock with its one-liner and a card of another conversation stays out",async()=>{
  const c=teamAttentionCase("agent-one"),pid=c.reference.pending_id;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url,options)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?c.page:c.view)})});
  await ui.tick();await ui.tick();await ui.tick();
  const dock=ui.nodes.get("attention-dock"),slot=turnNode(ui).children.find(node=>node.className==="turn-attention");
  assert.equal(dock.children.length,1);
  assert.equal(dock.children[0].attributes["data-hitl-card"],pid);
  assert.deepEqual(slot.children.map(node=>node.textContent),["正在等你批准，卡片在輸入框上方。"]);
  const focusedIn=card=>{const hit=[];walkNodes(card,node=>{if(node.tagName==="BUTTON"&&node.focused)hit.push(node)});return hit};
  const approvalOpen=[];walkNodes(ui.nodes.get("attention-items"),node=>{if(node.attributes?.["data-attention-overview"])approvalOpen.push(node)});
  assert.equal(approvalOpen.length,1);
  approvalOpen[0].children.find(node=>node.textContent==="查看原回合").events.click();
  assert.deepEqual(focusedIn(dock.children[0]).map(node=>node.textContent),["允許這次"],"focus lands on 允許這次, not the collapsed 重新讀取原操作");
  const other=teamAttentionCase("agent-two");
  other.page.items[0].origin.root={...other.page.items[0].origin.root,conversation_id:"another-cid"};
  other.page.items[0].origin.sender=null;
  const ui2=await open(other.data,{now:Date.parse(when),hitl:async(url,options)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?other.page:other.view)})});
  await ui2.tick();await ui2.tick();await ui2.tick();
  assert.equal(ui2.nodes.get("attention-dock").children.length,0,"a card whose turn is in another conversation is not in this dock");
  assert.equal(ui2.nodes.get("attention-dock").hidden,true);
});

test("HITL dock: a Connect card of the open conversation shows in the dock with its one-liner",async()=>{
  const {data,task}=inlineQuestionChat("foundry-native-v1");
  const c=consentCase("consent"),entry=c.entry;
  Object.assign(entry.item.consent.call,{conversation_id:task.admission.conversation_id,request_id:task.task_id});
  entry.thread_id=data.composer.thread_id;data.composer.attention=[entry];
  const ui=await open(data,{now:Date.parse(when),hitl:c.hitl});
  await ui.tick();await ui.tick();
  const dock=ui.nodes.get("attention-dock"),slot=turnNode(ui).children.find(node=>node.className==="turn-attention");
  assert.equal(dock.hidden,false);
  assert.equal(dock.children.length,1);
  assert.equal(dock.children[0].attributes["data-consent-card"],c.cid);
  assert.deepEqual(slot.children.map(node=>node.textContent),["需要連線，卡片在輸入框上方。"]);
  const consentOpen=[];walkNodes(ui.nodes.get("attention-items"),node=>{if(node.attributes?.["data-attention-overview"])consentOpen.push(node)});
  assert.equal(consentOpen.length,1);
  consentOpen[0].children.find(node=>node.textContent==="查看原回合").events.click();
  const focusedConsent=[];walkNodes(dock.children[0],node=>{if(node.tagName==="BUTTON"&&node.focused)focusedConsent.push(node)});
  assert.deepEqual(focusedConsent.map(node=>node.textContent),["我連好了，繼續"]);
});

test("HITL dock: two pending cards sit oldest first under a count line, and 查看原回合 goes to the dock",async()=>{
  const {data,task,entry}=inlineQuestionChat("foundry-native-v1");
  const older=copy(entry),id2="c".repeat(64);
  older.item.item_id=id2;older.item.question.question_id=id2;older.item.created_at="2025-12-31T23:00:00Z";
  older.item.text="較早的問題";
  data.composer.attention=[entry,older];
  const ui=await open(data,{now:Date.parse(when)});
  const dock=ui.nodes.get("attention-dock");
  assert.equal(dock.children.length,3);
  assert.equal(dock.children[0].textContent,"有 2 張待處理卡片");
  assert.deepEqual(dock.children.slice(1).map(card=>card.attributes["data-question-card"]),[id2,entry.item.item_id]);
  const calls=[];dock.scrollIntoView=()=>calls.push("dock");
  const host=turnNode(ui).children.find(node=>node.className==="turn-attention");host.scrollIntoView=()=>calls.push("turn");
  const overview=[];walkNodes(ui.nodes.get("attention-items"),node=>{if(node.attributes?.["data-attention-overview"])overview.push(node)});
  assert.equal(overview.length,2,"the Team overview lists both pending cards");
  const open1=overview.find(node=>node.attributes["data-attention-overview"]===entry.item.item_id).children.find(node=>node.textContent==="查看原回合");
  open1.events.click();
  assert.deepEqual(calls,["dock"]);
  const buttons=[];walkNodes(dock.children[2],node=>{if(node.tagName==="BUTTON"&&node.focused)buttons.push(node)});
  assert.equal(buttons.length,1,"the first button of that card has focus");
});

// dev 2026-10-18: every render() rebuilt the dock, so a click on 允許／拒絕 could land on a removed card and typed text
// was reset. A redraw now keeps each card whose drawing did not change.
function secondQuestion(entry,text="另一個問題"){
  const later=copy(entry),id="c".repeat(64);
  later.item.item_id=id;later.item.question.question_id=id;later.item.created_at="2026-01-01T00:01:00Z";later.item.text=text;
  return later;
}
test("HITL dock: a redraw with the same cards keeps the card node and what was typed in it",async()=>{
  const {data,qid}=inlineQuestionChat("foundry-native-v1");
  const ui=await open(data,{now:Date.parse(when)});
  const dock=ui.nodes.get("attention-dock"),card=dock.children[0];
  const input=hitlNodes(ui,"data-hitl-rule","question:"+qid)[0];
  input.value="寄給 Ann";
  ui.render(data);
  assert.equal(dock.children[0],card,"the same card node");
  assert.equal(hitlNodes(ui,"data-hitl-rule","question:"+qid)[0],input);
  assert.equal(input.value,"寄給 Ann");
});

test("HITL dock: a button bound before a redraw is still in the dock and still answers after it",async()=>{
  const {data,qid,entry}=inlineQuestionChat("foundry-native-v1");
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>({ok:true,json:async()=>answerReply(
    entry.item,JSON.parse(options.body))})});
  const choice=hitlNodes(ui,"data-question-choice",qid)[0];
  ui.render(data);
  assert.ok(ui.nodes.get("attention-dock").contains(choice),"the button the owner sees is the one in the dock");
  choice.events.click();await ui.tick();await ui.tick();
  const sent=ui.calls.filter(call=>call.method==="POST"&&call.url.startsWith("/api/native-hitl/"));
  assert.equal(sent.length,1);
  assert.equal(JSON.parse(sent[0].body).answer,"Ann");
});

test("HITL dock: an added card joins and a removed card leaves without redrawing the others",async()=>{
  const {data,entry}=inlineQuestionChat("foundry-native-v1");
  const ui=await open(data,{now:Date.parse(when)});
  const dock=ui.nodes.get("attention-dock"),first=dock.children[0],later=secondQuestion(entry);
  data.composer.attention=[entry,later];ui.render(data);
  assert.equal(dock.children.length,3);
  assert.equal(dock.children[0].textContent,"有 2 張待處理卡片");
  assert.equal(dock.children[1],first,"the card already shown keeps its node");
  assert.equal(dock.children[2].attributes["data-question-card"],later.item.item_id);
  const second=dock.children[2];
  data.composer.attention=[later];ui.render(data);
  assert.equal(dock.children.length,1,"the gone card and the count line leave");
  assert.equal(dock.children[0],second,"the other card stays");
  data.composer.attention=[];ui.render(data);
  assert.equal(dock.children.length,0);assert.equal(dock.hidden,true);
});

test("HITL dock: a kept approval card still remembers its opened 詳細 across redraws",async()=>{
  const c=teamAttentionCase("agent-one"),pid=c.reference.pending_id;
  const ui=await open(c.data,{now:Date.parse(when),hitl:async(url,options)=>({ok:true,
    json:async()=>copy(url.includes("/attention")?c.page:c.view)})});
  await ui.tick();await ui.tick();await ui.tick();
  const dock=ui.nodes.get("attention-dock");
  ui.render(ui.holder.current);
  const card=dock.children[0],details=[];
  assert.equal(card.attributes["data-hitl-card"],pid);
  walkNodes(card,node=>{if(node.attributes?.["data-hitl-details"]===pid)details.push(node)});
  assert.equal(details.length,1);
  details[0].open=true;details[0].events.toggle();
  ui.render(ui.holder.current);
  assert.equal(dock.children[0],card,"the kept card's own read button guards its toggle, so it stays");
  assert.equal(details[0].open,true);
});

test("HITL dock: a changed card is drawn again and its unchanged neighbour keeps its node",async()=>{
  const {data,entry}=inlineQuestionChat("foundry-native-v1");
  data.composer.attention=[entry,secondQuestion(entry)];
  const ui=await open(data,{now:Date.parse(when)});
  const dock=ui.nodes.get("attention-dock"),[count,kept,changed]=dock.children;
  assert.equal(changed.attributes["data-question-card"],"c".repeat(64),"the later card is drawn last");
  data.composer.attention=[entry,secondQuestion(entry,"改過的問題")];ui.render(data);
  assert.equal(dock.children[0],count);
  assert.equal(dock.children[1],kept,"the unchanged card keeps its node");
  assert.notEqual(dock.children[2],changed,"the changed card is a new node");
  assert.equal(dock.children[2].attributes["data-question-card"],"c".repeat(64));
  const asked=[];walkNodes(dock.children[2],node=>{if(node.className==="hitl-question")asked.push(node.textContent)});
  assert.deepEqual(asked,["改過的問題"]);
});

// #813: during a deploy window the API may send a stop to no session (not_dispatched). The page says the stop was not
// sent and can be pressed again, not 「傳送結果尚未確認」 or an unknown stop to check.
test("a native stop the API sent to no session says it was not sent and stop stays available",async()=>{
  const data=nativeChat("started"),task=data.tasks[0];
  task.cancellation={state:"REFUSED",code:"NATIVE_SESSION_CHANGED",not_dispatched:true,automatic_retry:false};
  const ui=await open(data);
  assert.equal(ui.text("cancellation-note"),
    "停止請求沒有送出：助理正在換版本，這次沒有送到任何工作環境。可以稍後再按一次「停止這次回覆」。");
  assert.doesNotMatch(ui.text("cancellation-note"),/還不確定停止請求有沒有送到|傳送結果尚未確認/);
  assert.equal(ui.nodes.get("cancel-instruction").hidden,false);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,false);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0);
});

// #813 through the button: the stop is asked in the page, the API sends it to no session, and pressing again asks again
// and posts the same task's stop (the bridge resends the same NativeCancel, same RID and CID, on the same journal row).
test("a native stop refused as not sent asks again in the page and posts the same task's stop on the second click",async()=>{
  const data=nativeChat("started"),task=data.tasks[0];
  const ui=await open(data);
  ui.click("cancel-instruction");await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,false);
  assert.equal(ui.text("page-confirm-message"),STOP_QUESTION);
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,0,"the first click only asks");
  ui.click("page-confirm-ok");await ui.tick();
  let sent=ui.calls.filter(call=>call.method==="POST");
  assert.deepEqual(sent.map(call=>[call.url,JSON.parse(call.body)]),[["/api/instructions/offline/cancel",{}]]);
  task.cancellation={state:"REFUSED",code:"NATIVE_SESSION_CHANGED",not_dispatched:true,automatic_retry:false};
  task.actions.cancel=action(true);
  ui.holder.current=data;ui.holder.pending({task});
  await ui.tick();await ui.tick();
  assert.equal(ui.text("cancellation-note"),
    "停止請求沒有送出：助理正在換版本，這次沒有送到任何工作環境。可以稍後再按一次「停止這次回覆」。");
  assert.equal(ui.nodes.get("cancel-instruction").hidden,false);
  assert.equal(ui.nodes.get("cancel-instruction").disabled,false);
  ui.click("cancel-instruction");await ui.tick();
  assert.equal(ui.nodes.get("page-confirm").hidden,false,"the second stop asks in the page too");
  assert.equal(ui.calls.filter(call=>call.method==="POST").length,1,"asking again sends nothing yet");
  ui.click("page-confirm-ok");await ui.tick();
  sent=ui.calls.filter(call=>call.method==="POST");
  assert.deepEqual(sent.map(call=>[call.url,JSON.parse(call.body)]),
    [["/api/instructions/offline/cancel",{}],["/api/instructions/offline/cancel",{}]]);
});

// #813 review round 4: a delivery acknowledgement the API sent to no session says 「確認收到」 was not sent, not the
// message-send wording; a refused message send keeps its own wording.
test("a native delivery acknowledgement the API sent to no session says the acknowledgement was not sent",async()=>{
  const data=nativeChat("completed"),task=data.tasks[0];
  const ui=await open(data);
  ui.holder.templateResponses={[`/api/instructions/${task.task_id}/confirm-reply`]:{ok:false,status:409,
    json:async()=>({error:{code:"NATIVE_SESSION_CHANGED",not_dispatched:true,request_id:task.task_id,automatic_retry:false}})}};
  assert.equal(ui.nodes.get("acknowledge").disabled,false);
  ui.click("acknowledge");await ui.tick();await ui.tick();
  assert.deepEqual(ui.calls.filter(call=>call.method==="POST").map(call=>call.url),
    [`/api/instructions/${task.task_id}/confirm-reply`]);
  assert.equal(ui.text("error-message"),"確認收到沒有送出，請稍後再按一次。");
  assert.doesNotMatch(ui.text("error-message"),/訊息沒有送出/);
  assert.equal(vm.runInContext('friendlyError({code:"NATIVE_SESSION_CHANGED",not_dispatched:true})',ui.context),
    "助理的新版本還沒準備好，訊息沒有送出，請稍後再送。");
});

// Every confirm-gated action, one row each: 取消 sends nothing to the gated path; 確定 sends exactly one request there.
// Pre-confirmation reads (and settings' /inspect) are not the gated request. A call site that forgets `await` gets a
// truthy Promise and sends while the question is still open, so its 取消 step fails here.
const hitlRulesUi=async()=>{
  const data=hitlChat(),rules=hitlRead(data).rules;
  rules.revision=1;rules.etag="etag-1";rules.rules=[{rule_id:"existing-rule",text:"Existing rule",behavior:"ask_first"}];
  const ui=await open(data,{now:Date.parse(when),hitl:async(url,options)=>options.method==="POST"?new Promise(()=>{}):
    {ok:true,json:async()=>({rules:copy(rules),commands:[],can_approve:true,read_at:when})}});
  ui.click("read-hitl-rules");await ui.tick();await ui.tick();
  return ui;
};
const hitlRuleButton=(ui,label)=>ui.nodes.get("hitl-rule-list").children[0].children.find(node=>node.textContent===label);
const typeHitlRule=(ui,text)=>{const input=ui.nodes.get("hitl-rule-text");input.value=text;input.events.input()};
const unknownOutcome=code=>({ok:false,json:async()=>({error:{code}})});
async function viewedReviews(options,base){
  const ui=await openReviews(options,base);
  reviewButton(reviewRow(ui,"新指南"),"查看內容").events.click();await settleReviews(ui);
  return ui;
}
function replaceSkillUi(){
  const base=settingsBase();
  base.skills=[{name:"old-guide",version_id:"1".repeat(32),sha256:"e".repeat(64),metadata_sha256:"f".repeat(64)}];
  base.settings.capabilities=[{entry_id:"fa-skills-v1",entry_sha256:"6".repeat(64)}];
  return viewedReviews(settingsOptions(),base);
}
const twoTeamMcp=()=>openTeamMcp(teamMcpView({version:"v2",servers:[teamMcpServer("Docs","v2"),teamMcpServer("Wiki","v2")]}));
const enabledConnector=()=>openConnectors(connectorView(connectorCard("enabled","v1",connectorSettings())));
const SKILL_DECISIONS="/api/team-skills/skills/new-guide/decisions",HITL_RULES=/^\/api\/native-hitl\/[^/]+\/rules$/;
const confirmGatedActions=[
  {name:"settings 套用",url:"/api/agent-template/select",open:async()=>{
    const ui=await openSettingsPage();
    ui.click("settings-tab-mcp");settingsSwitch(ui,"WebIQ",true);
    ui.holder.templateResponses["/api/agent-template/inspect"]=reply({...inspectedTemplate(),template_sha256:"a".repeat(64)});
    return ui;
  },trigger:ui=>ui.click("settings-apply")},
  {name:"skill 批准",url:SKILL_DECISIONS,open:()=>viewedReviews(),
    trigger:ui=>reviewButton(reviewRow(ui,"新指南"),"批准").events.click()},
  {name:"skill 拒絕",url:SKILL_DECISIONS,open:()=>viewedReviews(),
    trigger:ui=>reviewButton(reviewRow(ui,"新指南"),"拒絕").events.click()},
  {name:"skill 停用",url:"/api/team-skills/skills/old-guide/deactivations",open:()=>openReviews(),
    trigger:ui=>reviewButton(reviewRow(ui,"舊指南"),"停用").events.click()},
  {name:"skill 用同一個決定編號重送",url:SKILL_DECISIONS,open:async()=>{
    const ui=await viewedReviews();
    ui.holder.templateResponses[SKILL_DECISIONS]=unknownOutcome("BACKEND_OFFLINE");
    reviewButton(reviewRow(ui,"新指南"),"拒絕").events.click();await ui.answer(true);await settleReviews(ui);
    return ui;
  },trigger:ui=>reviewButton(reviewRow(ui,"新指南"),"重新讀取並核對").events.click()},
  {name:"skill 批准並取代",url:SKILL_DECISIONS,open:replaceSkillUi,
    trigger:ui=>reviewButton(reviewRow(ui,"新指南"),"批准並取代").events.click()},
  {name:"skill 建立草稿",url:"/api/team-skills/drafts",open:async()=>{const ui=await openReviews();fillSkillNew(ui);return ui},
    trigger:ui=>ui.click("skill-new-save")},
  {name:"Team MCP 新增",url:"/api/team-mcp/changes",open:async()=>{const ui=await openTeamMcp();fillTeamMcp(ui);return ui},
    trigger:ui=>ui.click("team-mcp-save")},
  {name:"Team MCP 修改",url:"/api/team-mcp/changes",open:twoTeamMcp,
    trigger:ui=>{teamMcpAction(ui,0,"修改").events.click();ui.click("team-mcp-save")}},
  {name:"Team MCP 停用",url:"/api/team-mcp/changes",open:twoTeamMcp,
    trigger:ui=>teamMcpAction(ui,0,"停用").events.click()},
  {name:"Team MCP 用原編號重送",url:"/api/team-mcp/changes",open:async()=>{
    const ui=await twoTeamMcp();
    ui.holder.templateResponses["/api/team-mcp/changes"]=unknownOutcome("TEAM_MCP_OUTCOME_UNKNOWN");
    teamMcpAction(ui,0,"停用").events.click();await ui.answer(true);await settleReviews(ui);
    return ui;
  },trigger:ui=>ui.click("team-mcp-retry")},
  {name:"Team connector 加入",url:"/api/team-connectors/changes",open:()=>openConnectors(),
    trigger:ui=>{typeConnectorSecret(ui,CONNECTOR_SECRET);connectorButton(ui,"加入 Microsoft Teams").events.click()}},
  {name:"Team connector 儲存批准設定",url:"/api/team-connectors/changes",open:enabledConnector,
    trigger:ui=>{const [read]=connectorFields(ui);read.value="always";read.events.change();
      connectorButton(ui,"儲存批准設定").events.click()}},
  {name:"Team connector 停用",url:"/api/team-connectors/changes",open:enabledConnector,
    trigger:ui=>connectorButton(ui,"停用").events.click()},
  {name:"Team connector 重新啟用",url:"/api/team-connectors/changes",
    open:()=>openConnectors(connectorView(connectorCard("disabled","v2",connectorSettings(false)))),
    trigger:ui=>connectorButton(ui,"重新啟用").events.click()},
  {name:"Team connector 用原編號重送",url:"/api/team-connectors/changes",open:async()=>{
    const ui=await enabledConnector();
    ui.holder.templateResponses["/api/team-connectors/changes"]=unknownOutcome("TEAM_CONNECTOR_OUTCOME_UNKNOWN");
    connectorButton(ui,"停用").events.click();await ui.answer(true);await settleReviews(ui);
    return ui;
  },trigger:ui=>ui.click("team-connectors-retry")},
  {name:"助理 connector 開關",url:"/api/agent-connectors",open:async()=>{
    const ui=await openAgentConnectors();delete ui.holder.templateResponses["/api/agent-connectors"];return ui;
  },trigger:ui=>{const toggle=agentConnectorSwitch(ui);toggle.checked=false;toggle.events.change()}},
  {name:"助理 connector 用原編號重送",url:"/api/agent-connectors",open:async()=>{
    const ui=await openAgentConnectors();
    ui.holder.templateResponses["/api/agent-connectors"]=unknownOutcome("BACKEND_OFFLINE");
    const toggle=agentConnectorSwitch(ui);toggle.checked=false;toggle.events.change();await ui.answer(true);await settleReviews(ui);
    return ui;
  },trigger:ui=>ui.click("settings-connectors-retry")},
  {name:"排程停用",url:"/api/routines/r1/versions/v-r1/disable",open:()=>openRoutineSettings(routinesRead([routineView("r1")])),
    trigger:ui=>routineRows(ui)[0].children[3].children[0].events.click()},
  {name:"HITL 規則新增",url:HITL_RULES,open:hitlRulesUi,
    trigger:ui=>{typeHitlRule(ui,"Ask before changing this workspace.");ui.click("save-hitl-rule")}},
  {name:"HITL 規則更新",url:HITL_RULES,open:hitlRulesUi,
    trigger:ui=>{hitlRuleButton(ui,"編輯").events.click();typeHitlRule(ui,"Updated scope");ui.click("save-hitl-rule")}},
  {name:"HITL 規則刪除",url:HITL_RULES,open:hitlRulesUi,
    trigger:ui=>hitlRuleButton(ui,"刪除").events.click()},
  {name:"模板選用",url:"/api/agent-template/select",open:async()=>{
    const ui=await open(templateData());await readyTemplateSelection(ui,selectableTemplate());return ui;
  },trigger:ui=>ui.click("select-template")},
  {name:"停止這次回覆",url:"/api/instructions/offline/cancel",open:()=>open(cancellableChat()),
    trigger:ui=>ui.click("cancel-instruction")},
  {name:"封存目前對話並開新 session",url:"/api/instructions/old-thread/reset",open:()=>{
    const data=fixture();
    data.composer={thread_id:"old-thread",request_id:"old-request",can_reset:true,can_new_conversation:true,
      hosted_session_id:"fa-chat-original",content:"old",editable:false,can_submit:false,models};
    return open(data);
  },trigger:ui=>ui.click("reset-session")},
];
for(const row of confirmGatedActions){
  test(`in-page confirmation gates ${row.name}: 取消 sends nothing, 確定 sends one request`,async()=>{
    const ui=await row.open();
    const gated=()=>ui.calls.filter(call=>call.method==="POST"&&
      (row.url instanceof RegExp?row.url.test(call.url):call.url===row.url)).length;
    const before=gated();
    // Not awaited: a click handler may return the whole action, which waits for the answer below.
    row.trigger(ui);
    assert.ok(await ui.answer(false),"the action asks first");await settleReviews(ui,12);
    assert.equal(gated(),before,`${row.name}: 取消 sends nothing`);
    assert.equal(ui.nodes.get("page-confirm").hidden,true);
    row.trigger(ui);
    assert.ok(await ui.answer(true));await settleReviews(ui,12);
    assert.equal(gated(),before+1,`${row.name}: 確定 sends exactly one request`);
  });
}
test("the confirm-gated action table has one row per action",()=>{
  assert.equal(confirmGatedActions.length,25);
  assert.equal(new Set(confirmGatedActions.map(row=>row.name)).size,confirmGatedActions.length);
});

// ADR-0009 §8 / M4 (phase 2c): the per-Agent notice the API delivers once with an owner's submit answer.
const ENVIRONMENT_NOTICE_SENTENCES=[
  [{kind:"environment_updated"},"助理已更新到新版本，工作檔也一起帶過來了。"],
  [{kind:"environment_switched_without_files"},"上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。"],
  [{kind:"environment_source_retired"},
    "上次換環境時，舊環境已失效或被平台刪除，沒有搬工作檔；之前 VM 裡的工作檔可能已經不在了。"],
  [{kind:"platform_version_changed",from_version:"41",to_version:"40"},
    "平台把助理的執行環境換成了另一個版本（v41→v40）；FA 已記錄，相容就照常執行。"],
  // phase 4d (Neal 2026-10-19); without its work files the no-files sentence follows (M4 #1)
  [{kind:"environment_rolled_back"},"助理已退回先前的版本；剛才中斷的訊息請再送一次。"],
  [{kind:"environment_rolled_back",without_files:true},
    "助理已退回先前的版本；剛才中斷的訊息請再送一次。上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。"],
  // phase 4b-4 (Neal 2026-10-19): U3's third failure vetoed the new version; no version numbers
  [{kind:"environment_upgrade_vetoed"},"助理這次沒有更新，會繼續使用目前的版本。"],
];
async function sendWithNotice(notice,{content="換版後的問題"}={}){
  const ui=await open(switchingChat());
  typeAttachmentText(ui,content);
  ui.click("send-instruction");await ui.tick();
  const post=ui.holder.pendingPosts.at(-1),started=copy(ui.holder.current.tasks[0]);
  Object.assign(started,{task_id:"draft-rid",submit_request_id:"draft-rid",instruction:content,reply:null,
    admission:{...started.admission,turn_number:2},submission:{state:"CONFIRMED"},
    native_receipt:{...started.native_receipt,status:"started",...(notice?{environment_notice:notice}:{})},
    status:{data:{state:"started"},observed_at:when}});
  post.resolve({task:started});await ui.tick();await ui.tick();
  return {ui,started};
}
const occurrences=(ui,sentence)=>[...ui.nodes.values()].filter(node=>node.textContent===sentence).length;
for(const [notice,sentence] of ENVIRONMENT_NOTICE_SENTENCES){
  test(`the ${notice.kind}${notice.without_files?" (without files)":""} notice shows once as a quiet status line, never as an error`,async()=>{
    const {ui}=await sendWithNotice(notice);
    const note=ui.nodes.get("environment-note");
    assert.equal(note.hidden,false);
    assert.equal(ui.text("environment-note"),sentence);
    assert.match(html,/<p id="environment-note" class="reason" role="status" hidden><\/p>/,"a status line, not role=alert");
    assert.equal(occurrences(ui,sentence),1,"one place only: the status line, not the chat bubbles");
    assert.equal(ui.nodes.get("error").hidden,true);
    assert.doesNotMatch(ui.text("error-message"),new RegExp(sentence.slice(0,8)));
    assert.equal(vm.runInContext("lastError",ui.context),null);
    // A later full state read (the receipt read again, without the one-time notice) keeps the line until the next send.
    ui.render(ui.holder.current);
    assert.equal(ui.nodes.get("environment-note").hidden,false);
  });
}
test("the notice ends with the Agent's next accepted message, which carries none",async()=>{
  const {ui}=await sendWithNotice({kind:"environment_switched_without_files"});
  assert.equal(ui.nodes.get("environment-note").hidden,false);
  const later=copy(ui.holder.current);
  Object.assign(later.composer,{request_id:"draft-rid-2",editable:true,can_submit:true,content:null});
  ui.render(later);
  typeAttachmentText(ui,"下一則");
  ui.click("send-instruction");await ui.tick();
  const post=ui.holder.pendingPosts.at(-1),next=copy(later.tasks[0]);
  Object.assign(next,{task_id:"draft-rid-2",submit_request_id:"draft-rid-2",submission:{state:"CONFIRMED"},
    native_receipt:{...next.native_receipt,status:"started"},status:{data:{state:"started"},observed_at:when}});
  delete next.native_receipt.environment_notice;
  post.resolve({task:next});await ui.tick();await ui.tick();
  assert.equal(ui.nodes.get("environment-note").hidden,true);
});
test("a notice kind this page does not know is ignored, never an error",async()=>{
  const {ui}=await sendWithNotice({kind:"environment_later_kind",to_version:"39"});
  assert.equal(ui.nodes.get("environment-note").hidden,true);
  assert.equal(ui.nodes.get("error").hidden,true);
  assert.equal(vm.runInContext("lastError",ui.context),null);
  assert.equal(vm.runInContext('environmentNoticeText({kind:"platform_version_changed",from_version:"x",to_version:"40"})',
    ui.context),null,"a drift notice without its two versions says nothing");
});
test("after a server-side switch the same-RID resend's answer carries the switch notice",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0});
  const accepted=copy(c.accepted.tasks[0]);
  accepted.native_receipt={...(nativeChat("unknown",true).tasks[0].native_receipt),status:"accepted",
    environment_notice:{kind:"environment_switched_without_files"}};
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
    [`/api/tasks/${c.rid}/submit-retry`]:{ok:true,json:async()=>{ui.holder.current=copy(c.accepted);return {task:accepted}}}};
  typeAttachmentText(ui,"換版也只送一次");ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();await fireDelay(ui,3000);
  assert.equal(ui.text("environment-note"),"上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。");
  assert.equal(ui.nodes.get("environment-note").hidden,false);
  assert.equal(vm.runInContext("lastError",ui.context),null);
});
// The bridge reports a refused-then-resent RID by its resend's answer (CONFIRMED), with the switch notice the API sent.
test("after the source environment was deleted, the same-RID resend shows source_retired and clears the sent text",async()=>{
  const c=serverFollowingCase(),ui=await open(c.data,{now:0});
  const accepted=copy(c.accepted.tasks[0]);
  accepted.native_receipt={...(nativeChat("unknown",true).tasks[0].native_receipt),status:"accepted",
    environment_notice:{kind:"environment_source_retired"}};
  accepted.submission={state:"CONFIRMED",intent:{created_at:when},observed_at:when,automatic_retry:false};
  ui.holder.templateResponses={[`/api/instructions/${c.rid}/submit`]:c.rejection,
    [`/api/tasks/${c.rid}/submit-retry`]:{ok:true,json:async()=>{ui.holder.current=copy(c.accepted);return {task:accepted}}}};
  typeAttachmentText(ui,"換版也只送一次");ui.holder.current=copy(c.refused);ui.click("send-instruction");
  await ui.tick();await ui.tick();await fireDelay(ui,3000);
  assert.equal(ui.text("environment-note"),ENVIRONMENT_NOTICE_SENTENCES[2][1]);
  assert.equal(ui.nodes.get("environment-note").hidden,false);
  assert.equal(ui.nodes.get("instruction").value,"");
  assert.equal(vm.runInContext("lastError",ui.context),null);
});
test("a manual retry of the original turn answered without a notice keeps the shown one",async()=>{
  const c=serverFollowingCase(),ui=await open(c.refused,{now:0});
  const sentence="上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。";
  // The Agent's notice came with an earlier answer and is on screen.
  vm.runInContext(`environmentNotices.set(selectedAgent,${JSON.stringify(sentence)});renderEnvironmentNotice()`,ui.context);
  assert.equal(ui.nodes.get("environment-note").hidden,false);
  ui.holder.templateResponses={[`/api/tasks/${c.rid}/submit-retry`]:{ok:true,json:async()=>{
    ui.holder.current=copy(c.accepted);return {task:c.accepted.tasks[0]}}}};
  const button=turnNode(ui).children.at(-1).children.find(node=>node.textContent==="以原內容重送");
  assert.ok(button);
  button.events.click();await ui.tick();await ui.tick();await ui.tick();
  assert.equal(c.accepted.tasks[0].submission.state,"CONFIRMED");
  // That answer carried no notice: the retry of an older message does not end the one shown.
  assert.equal(ui.nodes.get("environment-note").hidden,false);
  assert.equal(ui.text("environment-note"),sentence);
});
