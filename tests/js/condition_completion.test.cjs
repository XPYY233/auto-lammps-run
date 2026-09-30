const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
test('completion summary never replaces a rendered task',async()=>{
 const calls=[],task={id:'one',revision:9,title:'original',fields:{}};
 const c=vm.createContext({current:{id:'one',revision:1},$:()=>({}),notice(){},refreshModelStatus:async()=>{},afterChange:async()=>{assert.equal(c.current,task)},api:async(path)=>{calls.push(path);return path.endsWith('complete-conditions')?{revision:9,proposed_fields:['units']}:task;}});
 vm.runInContext(source.slice(source.indexOf('async function completeConditions('),source.indexOf('async function generateConditions()')),c);
 await c.completeConditions();assert.deepEqual(calls,['/api/tasks/one/complete-conditions','/api/tasks/one']);assert.equal(c.current.title,'original');
});
test('bulk confirmation is explicit and refuses unresolved fields',async()=>{
 const calls=[];const c=vm.createContext({Object,current:{id:'one',revision:3,mode:'research',fields:{reference:{selected:null},units:{selected:'a',confirmed:false}}},notice(){},afterChange:async()=>{},api:async(path,data)=>{calls.push({path,data});return c.current}});
 vm.runInContext(source.slice(source.indexOf('async function confirmAllConditions('),source.indexOf("$('#confirm-all-conditions').onclick")),c);
 await c.confirmAllConditions();assert.deepEqual(JSON.parse(JSON.stringify(calls[0].data)),{revision:3,fields:['units']});
 c.current.fields.units.selected=null;await c.confirmAllConditions();assert.equal(calls.length,1);
});
