/* Fixed local Markdown parser. Model answers are data, never HTML or scripts. */
let researchMarkdownParser=null;
function researchMarkdown() {
  if(researchMarkdownParser)return researchMarkdownParser;
  if(!window.markdownit)return null;
  const md=window.markdownit({html:false,linkify:false,typographer:false,breaks:false});
  md.validateLink=href=>/^https?:\/\//i.test(href);
  const linkOpen=md.renderer.rules.link_open||((tokens,index,options,env,self)=>self.renderToken(tokens,index,options));
  md.renderer.rules.link_open=(tokens,index,options,env,self)=>{
    tokens[index].attrSet('rel','noopener noreferrer');
    tokens[index].attrSet('target','_blank');
    return linkOpen(tokens,index,options,env,self);
  };
  // Keep image descriptions without fetching model-supplied URLs.
  md.renderer.rules.image=(tokens,index)=>md.utils.escapeHtml('[图片：'+tokens[index].content+']');
  // Markdown's backslash/emphasis rules must not consume LaTeX delimiters or
  // underscores. Preserve the complete expression for the existing math view.
  md.inline.ruler.before('escape','research_math',(state,silent)=>{
    const start=state.pos,source=state.src;
    const pair=[['\\[','\\]'],['\\(','\\)'],['$$','$$'],['$','$']]
      .find(([left])=>source.startsWith(left,start));
    if(!pair)return false;
    const [left,right]=pair;
    let end=source.indexOf(right,start+left.length);
    while(end>=0){
      let slashes=0;for(let i=end-1;i>=0&&source[i]==='\\';i--)slashes++;
      if(slashes%2===0)break;
      end=source.indexOf(right,end+right.length);
    }
    if(end<0||end===start+left.length)return false;
    if(!silent){const token=state.push('research_math','',0);token.content=source.slice(start,end+right.length);}
    state.pos=end+right.length;return true;
  });
  md.renderer.rules.research_math=(tokens,index)=>md.utils.escapeHtml(tokens[index].content);
  researchMarkdownParser=md;return md;
}
function renderResearchMarkdown(value) {
  const body=document.createElement('div');body.className='markdown-body';
  const source=String(value??'');
  try {
    const parser=researchMarkdown();
    if(parser)body.innerHTML=parser.render(source);
    else {body.textContent=source;body.classList.add('markdown-fallback');}
  } catch(error) {
    body.textContent=source;body.classList.add('markdown-fallback');
  }
  return body;
}
