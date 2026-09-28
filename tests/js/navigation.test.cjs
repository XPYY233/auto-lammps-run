const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function setup(hash='#home'){
 const c=vm.createContext({location:{hash},visits:[],writes:[],listeners:{},errors:[],document:{querySelectorAll:()=>[]}});
 c.history={pushState:(_,__,h)=>{c.writes.push(['push',h]);c.location.hash=h;},replaceState:(_,__,h)=>{c.writes.push(['replace',h]);c.location.hash=h;}};
 c.window={addEventListener:(name,fn)=>c.listeners[name]=fn};
 c.notice=(m)=>c.errors.push(m);
 vm.runInContext('let busy=false,pendingRoute=null,renderedRoute=null;',c);
 vm.runInContext(source.slice(source.indexOf('async function action('),source.indexOf('async function listTasks(')),c);
 for(const [name,route] of Object.entries({showHome:'#home',renderNew:'#new',showTasks:'#tasks',showPapers:'#papers',showResources:'#resources',showHelp:'#help'}))c[name]=()=>{c.visits.push(route);c.recordRoute(route);};
 c.openTask=async id=>{c.visits.push('#'+id);c.recordRoute('#'+id);};
 return c;
}
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
test('navigation adds history once and native back/forward restores without new entries',async()=>{
 const c=setup();c.restoreRoute();await flush();c.requestRoute('#tasks');await flush();c.requestRoute('#resources');await flush();
 assert.deepEqual(c.writes,[['push','#tasks'],['push','#resources']]);
 c.location.hash='#tasks';c.listeners.popstate();await flush();
 c.location.hash='#resources';c.listeners.popstate();await flush();
 assert.deepEqual(c.visits,['#home','#tasks','#resources','#tasks','#resources']);assert.equal(c.writes.length,2);
});
test('latest navigation survives busy work and an old response cannot replace its address',async()=>{
 const c=setup();c.restoreRoute();await flush();let release;
 const hold=c.action(()=>new Promise(r=>release=r));c.requestRoute('#tasks');c.requestRoute('#help');c.recordRoute('#old');
 assert.equal(c.location.hash,'#help');assert.deepEqual(c.visits,['#home']);release();await hold;await flush();
 assert.deepEqual(c.visits,['#home','#help']);assert.equal(c.location.hash,'#help');
});
test('direct task hash and unknown hash recover without dispatch actions',async()=>{
 const id='a'.repeat(32),c=setup('#'+id);c.restoreRoute();await flush();assert.deepEqual(c.visits,['#'+id]);
 c.location.hash='#invalid';c.listeners.hashchange();await flush();assert.equal(c.visits.at(-1),'#home');assert.equal(c.location.hash,'#home');
});
test('queued route is restored after a failed action, including the previously displayed page',async()=>{
 const c=setup();c.restoreRoute();await flush();let reject;
 const hold=c.action(()=>new Promise((_,r)=>reject=r));c.requestRoute('#help');c.requestRoute('#home');reject(new Error('request failed'));
 await hold;await flush();assert.equal(c.visits.at(-1),'#home');assert.equal(c.visits.length,2);assert.deepEqual(c.errors,['request failed']);
});
test('stale task-open response cannot publish task content after a new navigation request',async()=>{
 const c=setup();let resolve;c.api=()=>new Promise(r=>resolve=r);
 vm.runInContext('let current=null;',c);
 vm.runInContext(source.slice(source.indexOf('async function openTask('),source.indexOf('function showNew(')),c);
 const hold=c.action(()=>c.openTask('a'.repeat(32)));c.requestRoute('#help');resolve({id:'a'.repeat(32)});await hold;await flush();
 assert.equal(vm.runInContext('current',c),null);assert.equal(c.visits.at(-1),'#help');assert.equal(c.location.hash,'#help');
});
