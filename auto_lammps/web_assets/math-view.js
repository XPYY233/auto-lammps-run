/* Local KaTeX, text nodes only. Model HTML, remote resources and macros are untrusted. */
function renderScientificMath(root) {
  if (!root || !window.renderMathInElement) return;
  window.renderMathInElement(root, {
    delimiters: [{left:'$$',right:'$$',display:true},{left:'\\[',right:'\\]',display:true},
      {left:'\\(',right:'\\)',display:false},{left:'$',right:'$',display:false}],
    ignoredClasses:['katex','katex-display','source-hash'],
    throwOnError:false, trust:false, maxExpand:200, maxSize:10, macros:{}
  });
}
document.addEventListener('DOMContentLoaded',()=>{
  renderScientificMath(document.body);
  const observer=new MutationObserver(records=>{
    for(const record of records) for(const added of record.addedNodes) {
      const root=added.nodeType===1?added:added.parentElement;
      if(!root || root.closest('pre,code,script,style,textarea,input,.katex,.katex-display'))continue;
      renderScientificMath(root);
    }
  });
  observer.observe(document.body,{childList:true,subtree:true});
});
