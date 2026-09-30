const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
test('completion summary never replaces a rendered task',async()=>{
 const calls=[],task={id:'one',revision:9,title:'original',fields:{}};
 const c=vm.createContext({current:{id:'one',revision:1},$:()=>({}),notice(){},refreshModelStatus:async()=>{},afterChange:async()=>{assert.equal(c.current,task)},api:async(path)=>{calls.push(path);return path.endsWith('complete-conditions')?{revision:9,proposed_fields:['units']}:task;}});
 vm.runInContext(source.slice(source.indexOf('async function completeConditions('),source.indexOf('async function generateConditions()')),c);
 await c.completeConditions();assert.deepEqual(calls,['/api/tasks/one/complete-conditions','/api/tasks/one']);assert.equal(c.current.title,'original');
});
