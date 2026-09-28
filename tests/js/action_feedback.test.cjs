// busy 期间的用户动作必须可见：修复前是静默丢弃（用户点了没反应）。
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function harness(){
  const notices=[],elements=new Map();
  const element=()=>({textContent:'',className:'',hidden:true,querySelector:()=>null});
  const c=vm.createContext({
    document:{querySelector:()=>null,querySelectorAll:()=>[]},
    window:{addEventListener:()=>{}},
    history:{pushState:()=>{}},
    $:sel=>{if(!elements.has(sel))elements.set(sel,element());return elements.get(sel)},
    drainRoute:()=>{},
  });
  // 取出声明 + notice + action（action 依赖 busy/notice/drainRoute）
  const start=source.indexOf('let schema, current = null');
  const end=source.indexOf('async function afterChange(');
  vm.runInContext(source.slice(start,end),c);
  return {c,notices,elements};
}
test('an action during a busy period is reported instead of silently dropped',async()=>{
  const {c,elements}=harness();
  let ran=0;
  const slow=new Promise(()=>{});           // 永不结束：模拟启动或长动作占住 busy
  c.action(()=>{ran++;return slow});
  await new Promise(r=>setTimeout(r,0));
  assert.equal(ran,1);
  await c.action(()=>{ran++});               // 第二次点击：busy 仍为真
  assert.equal(ran,1,'busy 期间的第二次动作不应执行');
  assert.match(elements.get('#message').textContent,/请稍候/);
  assert.equal(elements.get('#message').hidden,false,'必须让用户看见');
});
test('once the previous action settles a new one runs',async()=>{
  const {c}=harness();
  const order=[];
  await c.action(async()=>{order.push('first')});
  await c.action(async()=>{order.push('second')});
  assert.deepEqual(order,['first','second']);
});
