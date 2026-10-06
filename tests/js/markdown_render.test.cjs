const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const MarkdownIt=require('../../auto_lammps/web_assets/markdown/markdown-it.min.js');
const source=fs.readFileSync('auto_lammps/web_assets/markdown-view.js','utf8');
function setup(parser=MarkdownIt){
  const context=vm.createContext({window:{markdownit:parser},document:{createElement(){return {classList:{add(){}},textContent:'',innerHTML:''};}}});
  vm.runInContext(source,context);return value=>context.renderResearchMarkdown(value);
}
test('saved scientific answers render headings, lists and numeric tables',()=>{
  const output=setup()('## 尺寸结果\n\n| N | E (eV) |\n|--:|--:|\n|432|3.94|\n\n1. 保留原始结果\n2. 核对单位').innerHTML;
  assert.match(output,/<h2>尺寸结果<\/h2>/);assert.match(output,/<table>/);assert.match(output,/<td[^>]*>3.94<\/td>/);assert.match(output,/<ol>/);
});
test('math keeps delimiters and underscores inside table cells and display equations',()=>{
  const output=setup()('| 指标 | 数值 |\n|---|---|\n| \\(N_{bulk}\\) | 432 |\n\n\\[\nE_f = E_{N-1}^{vac} - \\frac{N-1}{N}E_N^{bulk}\n\\]\n\n$x_y$').innerHTML;
  assert.ok(output.includes('\\(N_{bulk}\\)'));assert.ok(output.includes('\\[\nE_f'));
  assert.ok(output.includes('\\frac{N-1}{N}'));assert.ok(output.includes('$x_y$'));
  assert.doesNotMatch(output,/<em>/);
});
test('HTML, unsafe links and model-supplied image URLs cannot execute or fetch',()=>{
  const output=setup()('<script>alert(1)</script>\n\n<img src="https://example.test/pixel" onerror="bad()">\n\n[x](javascript:alert%281%29) [file](file:///tmp/a) ![说明](https://example.test/pixel)').innerHTML;
  assert.doesNotMatch(output,/<script|<img|href="(?:javascript|file|data):/i);
  assert.match(output,/&lt;script&gt;/);assert.match(output,/\[图片：说明\]/);
});
test('explicit web links get isolated navigation and code remains literal',()=>{
  const output=setup()('[说明](https://example.test)\n\n```lammps\n# input\n$x_y$\n<img>\n```').innerHTML;
  assert.match(output,/rel="noopener noreferrer"/);assert.match(output,/target="_blank"/);
  assert.match(output,/<pre><code class="language-lammps"># input\n\$x_y\$\n&lt;img&gt;/);
});
test('escaped dollars and malformed math remain readable without throwing',()=>{
  const output=setup()('价格 \\$5；公式 \\(unfinished\n\n后续 **文本**').innerHTML;
  assert.match(output,/价格 \$5/);assert.match(output,/<strong>文本<\/strong>/);
});
test('missing or failing renderer retains the full original answer as plain text',()=>{
  const text='## 原始结果\n|a|b|\n\\(x_y\\)';
  assert.equal(setup(null)(text).textContent,text);
  assert.equal(setup(()=>{throw Error('unavailable')})(text).textContent,text);
});
