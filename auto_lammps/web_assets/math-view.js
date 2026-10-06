/* Local KaTeX, text nodes only. Model HTML, remote resources and macros are untrusted. */
let scientificMathObserver=null;
const scientificMathExcluded='pre,code,script,style,noscript,textarea,input,option,svg,.katex,.katex-display,.katex-error,.source-hash';
function renderScientificMath(root) {
  if (!root || root.isConnected===false || root.closest(scientificMathExcluded) || !window.renderMathInElement) return;
  // Auto-render replaces text even when parsing fails and retains the raw formula.
  // Observing those replacements creates an endless mutation microtask loop.
  scientificMathObserver?.disconnect();
  try {
    window.renderMathInElement(root, {
      delimiters: [{left:'$$',right:'$$',display:true},{left:'\\[',right:'\\]',display:true},
        {left:'\\(',right:'\\)',display:false},{left:'$',right:'$',display:false}],
      ignoredTags:['script','noscript','style','textarea','pre','code','option','input','svg'],
      ignoredClasses:['katex','katex-display','katex-error','source-hash'],
      throwOnError:false, trust:false, maxExpand:200, maxSize:10, macros:{}
    });
  } finally {
    scientificMathObserver?.observe(document.body,{childList:true,subtree:true});
  }
}
document.addEventListener('DOMContentLoaded',()=>{
  renderScientificMath(document.body);
  scientificMathObserver=new MutationObserver(records=>{
    const roots=new Set();
    for(const record of records) for(const added of record.addedNodes) {
      const root=added.nodeType===1?added:added.parentElement;
      if(!root || root.isConnected===false || root.closest(scientificMathExcluded))continue;
      roots.add(root);
    }
    // A single update often adds both a container and many of its text nodes.
    // Render each connected subtree once, not once per table cell or formula.
    for(const root of roots) if(![...roots].some(other=>other!==root&&other.contains(root)))renderScientificMath(root);
  });
  scientificMathObserver.observe(document.body,{childList:true,subtree:true});
},{once:true});
