const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('async function refreshScientificReviews('),source.indexOf('async function refreshWorkspace(){'));
const analysisId='a'.repeat(64);
const reports={evaluations:[{requests:[{reports:[{id:analysisId,status:'analyzed'}]}]}]};

test('unchanged evidence stays mounted across background refreshes and is posted once',async()=>{
 const requests=[],state={task:null,phase:'idle',reviews:[],error:''};
 const record={id:'b'.repeat(32),analysis_id:analysisId,at:'2026-10-08T00:00:00Z',state:'criteria_missing'};
 let saved=[],renders=0;
 const c=vm.createContext({current:{id:'task'},scientificReviewState:state,scientificReviewGeneration:0,
  renderScientificReviews(){renders++;},api:async(path,body)=>{
   requests.push({path,body});
   if(body){saved=[record];return record;}
   return {configured:true,reviews:[...saved]};
  }});
 vm.runInContext(code,c);
 await c.refreshScientificReviews('task',reports);
 assert.equal(c.scientificReviewState.phase,'ready');
 assert.equal(c.scientificReviewState.reviews.length,1);
 const firstRenders=renders;
 await c.refreshScientificReviews('task',reports);
 assert.equal(renders,firstRenders,'an unchanged card should keep open details and focus');
 assert.equal(requests.filter(item=>item.body).length,1,'the report should have one review record');
 saved=[{id:record.id,analysis_id:analysisId,at:record.at,state:'source_unavailable'}];
 await c.refreshScientificReviews('task',reports);
 assert.equal(c.scientificReviewState.reviews[0].state,'source_unavailable');
 assert.equal(renders,firstRenders+1,'changed source state must replace the old card');
});
