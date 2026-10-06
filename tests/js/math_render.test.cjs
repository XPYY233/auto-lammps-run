const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/math-view.js','utf8');
function setup(){
  let ready,observer;
  const calls=[];
  const element=(parent=null,excluded=false)=>({nodeType:1,parentElement:parent,isConnected:true,
    closest:()=>excluded?{}:null,contains(other){for(let p=other.parentElement;p;p=p.parentElement)if(p===this)return true;return false;}});
  const body=element();
  class Observer{
    constructor(callback){this.callback=callback;this.records=[];observer=this;}
    observe(){this.active=true;}
    disconnect(){this.active=false;this.records=[];}
    changed(node){if(this.active)this.records.push({addedNodes:[node]});}
    flush(){if(!this.records.length)return;const records=this.records;this.records=[];this.callback(records);}
  }
  const context=vm.createContext({document:{body,addEventListener:(type,fn)=>ready=fn},MutationObserver:Observer,
    window:{renderMathInElement(root,options){calls.push({root,options});
      // KaTeX parse-error fallback replaces a raw formula with the same text.
      // It must never trigger rendering itself again.
      observer?.changed({nodeType:3,parentElement:root});}}});
  vm.runInContext(source,context);ready();calls.length=0;
  return {context,observer,calls,element,body};
}
test('formula parse-error replacements cannot create an observer feedback loop',()=>{
  const {observer,calls,element}=setup();observer.changed(element());observer.flush();
  assert.equal(calls.length,1);assert.equal(observer.records.length,0);
  observer.flush();assert.equal(calls.length,1);assert.equal(observer.active,true);
});
test('one DOM update renders a subtree once despite repeated text additions',()=>{
  const {observer,calls,element}=setup(),parent=element(),child=element(parent);
  observer.changed(parent);observer.changed(child);
  for(let i=0;i<100;i++)observer.changed({nodeType:3,parentElement:child});
  observer.flush();assert.equal(calls.length,1);assert.equal(calls[0].root,parent);
});
test('removed nodes, code, SVG and generated math are left intact',()=>{
  const {observer,calls,element}=setup(),removed=element();removed.isConnected=false;
  observer.changed(removed);observer.changed(element(null,true));observer.flush();assert.equal(calls.length,0);
});
test('a renderer exception restores observation for later genuine page updates',()=>{
  const {context,observer,calls,element}=setup();context.window.renderMathInElement=()=>{throw Error('renderer')};
  observer.changed(element());assert.throws(()=>observer.flush(),/renderer/);assert.equal(observer.active,true);
  context.window.renderMathInElement=(root)=>calls.push({root});observer.changed(element());observer.flush();assert.equal(calls.length,1);
});
test('untrusted commands and expansion limits remain unchanged',()=>{
  const {observer,calls,element}=setup();observer.changed(element());observer.flush();
  const options=calls[0].options;assert.equal(options.trust,false);assert.equal(options.maxExpand,200);assert.equal(options.maxSize,10);
  assert.ok(options.ignoredTags.includes('svg'));assert.ok(options.ignoredClasses.includes('katex-error'));
});
