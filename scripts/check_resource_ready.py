#!/usr/bin/env python3
"""资源就绪门：进入一篇论文的 A/B 之前，先判断资源包是否具备完整工作流。

判定五项（全部满足才算就绪候选）：
  1) 入口脚本可取
  2) 声明的势函数文件全部存在（需 --screening 才能对照声明；缺省时该判定标为未核验）
  3) 结构闭包成立（read_data 可解析，或结构在脚本内建）
  4) 元素体系有证据
  5) 许可明确

不满足者只登记缺项，**不得启动 A**，也不占 B 的两次额度。

边界（避免误解）：
  * 「就绪」只表示按目录字段**可运行**，不代表科学可用，也不等于已验证；
  * 本工具不授予执行权，不替代控制端的资源核验；
  * 不覆盖引擎兼容性（例如现代 LAMMPS 已移除 `pair_style meam/c`）与内存/规模匹配；
  * `--deep` 联网取仓库树，按**入口目录归一化**解析 pair_coeff / read_data 引用。

用法：
  python3 scripts/check_resource_ready.py --catalog <目录.json> [--screening <screening-state.json>]
  python3 scripts/check_resource_ready.py --catalog <目录.json> --repo owner/name [--deep]
  python3 scripts/check_resource_ready.py --catalog <目录.json> --ready --json
"""
import argparse
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

POT = re.compile(r'[A-Za-z0-9_./-]+\.(?:meam|eam|tersoff|sw|snap|adp|bop|comb|airebo|lammps)\b', re.I)
NEED_LICENSE = {'未声明', 'NOASSERTION', 'NONE', 'undeclared', ''}
TIERS = ('tier_a', 'tier_b', 'tier_c')


def load(catalog, screening=None):
    document = json.loads(Path(catalog).read_text())
    rows = [(tier, row) for tier in TIERS for row in document[tier]]
    bundles = {}
    if screening and Path(screening).is_file():
        bundles = json.loads(Path(screening).read_text()).get('bundles', {})
    return rows, bundles


def declared_potentials(repo, bundles):
    found = set()
    for entry in (bundles.get(repo) or {}).get('decl') or []:
        line = entry.get('line') or ''
        if line.startswith('pair_coeff'):
            found.update(name.split('/')[-1] for name in POT.findall(line))
    return found


def gate(tier, row, bundles):
    """Return (status, reasons). A registry row is metadata only, never runnable."""
    repo = row['repo']
    if (row.get('source') or {}).get('type'):
        return 'registry_metadata_only', ['注册表记录：仅元数据，未下载文件、未运行、未核许可']
    closure = row.get('closure') or {}
    state = closure.get('state')
    if not row.get('inputs'):
        return 'blocked_no_entry', ['缺 LAMMPS 入口脚本']
    if bundles.get(repo) is None:
        declared = None
    else:
        declared = declared_potentials(repo, bundles)
    if declared is not None:
        missing = sorted(declared - {p.split('/')[-1] for p in row.get('potentials') or []})
        if missing:
            return 'blocked_missing_potential', ['声明但未取得的势函数文件：' + '、'.join(missing)]
    if state == 'read_data_missing':
        return 'blocked_missing_structure', ['read_data 引用的结构文件不存在：' + '、'.join(closure.get('read_data_missing') or [])]
    if state in ('unresolved_variable', 'read_data_partial'):
        return 'blocked_unresolved_variable', ['结构路径由变量给出（' + '、'.join(closure.get('unresolved_variables') or []) + '），需先解析']
    if state == 'unknown':
        return 'blocked_unknown_structure', ['结构来源未判定']
    if not row.get('elements'):
        return 'blocked_no_elements', ['元素映射未取得证据']
    if (row.get('license') or '') in NEED_LICENSE:
        return 'blocked_license', ['许可证未核验（不得分发）']
    reasons = []
    if declared is None:
        reasons.append('未提供 --screening：未做声明级势函数核验，仅按目录字段判定')
    if closure.get('state') == 'built_in_script':
        reasons.append('结构在脚本内建（无外部结构文件依赖）')
    return 'ready_candidate', reasons


def _gh_api(args):
    result = subprocess.run(['gh', 'api'] + args, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ''


def deep_check(repo, inputs, *, fetch_tree=None, fetch_file=None):
    """Resolve pair_coeff / read_data references relative to each entry directory."""
    fetch_tree = fetch_tree or (lambda name: [line for line in _gh_api(
        [f'repos/{name}/git/trees/HEAD?recursive=1', '--jq', '.tree[].path']).split('\n') if line.strip()])
    fetch_file = fetch_file or (lambda name, path: _gh_api(
        ['-H', 'Accept: application/vnd.github.raw', f'repos/{name}/contents/{path}']))
    tree = fetch_tree(repo)
    by_base = {}
    for path in tree:
        by_base.setdefault(path.split('/')[-1], []).append(path)
    scripts = [item for item in (inputs or [])][:6] or [p for p in tree if p.endswith('.in') or p.endswith('.lmp')][:3]
    refs, findings = {}, []
    for script in scripts:
        for line in fetch_file(repo, script).split('\n'):
            stripped = line.strip()
            if not stripped.lower().startswith(('pair_coeff', 'read_data')):
                continue
            for token in [t for t in stripped.split()[1:] if '/' in t or '.' in t]:
                refs.setdefault(token.split('/')[-1], {'token': token, 'in': script,
                                                       'locations': by_base.get(token.split('/')[-1]) or []})
    for base, info in refs.items():
        token, script = info['token'], info['in']
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(script), token))
        if resolved in tree or resolved[3:] in tree:
            info['resolved'] = resolved if resolved in tree else resolved[3:]
            continue
        if not info['locations']:
            findings.append(f"引用文件在仓库中不存在：{token}（入口 {script}）")
            continue
        dirs = sorted({posixpath.dirname(loc) or '.' for loc in info['locations']})
        findings.append(f"入口目录解析不到：{token}（入口 {script}，按相对路径得到 {resolved}）"
                        f"，文件实际在 {dirs} → 必须先与入口暂存到同一运行目录")
    return {'scripts_scanned': scripts,
            'references': {k: (v.get('resolved') or v['locations']) for k, v in refs.items()},
            'findings': findings}


def main():
    parser = argparse.ArgumentParser(description='资源就绪门（发现侧判定辅助，不授予执行权）')
    parser.add_argument('--catalog', required=True, help='资源目录 JSON（含 tier_a/tier_b/tier_c）')
    parser.add_argument('--screening', help='可选：发现方 screening-state.json，用于声明级势函数对照')
    parser.add_argument('--repo', help='只查单个资源包 owner/name')
    parser.add_argument('--ready', action='store_true', help='只列就绪候选')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--deep', action='store_true', help='联网核验 pair_coeff/read_data 引用能否按入口目录解析')
    args = parser.parse_args()
    rows, bundles = load(args.catalog, args.screening)
    if args.repo:
        rows = [(tier, row) for tier, row in rows if row['repo'].lower() == args.repo.lower()]
        if not rows:
            print('目录中没有该资源包：' + args.repo)
            return 1
    results = []
    for tier, row in rows:
        status, reasons = gate(tier, row, bundles)
        results.append(dict(tier=tier, repo=row['repo'], elements=row.get('elements'),
                            pair_style=row.get('pair_style'), doi=row.get('doi'),
                            potentials=row.get('potentials'), inputs=row.get('inputs'),
                            status=status, reasons=reasons))
    if args.ready:
        results = [item for item in results if item['status'] == 'ready_candidate']
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0
    if args.repo:
        for item in results:
            print(f"[{item['tier']}] {item['repo']} → {item['status']}")
            for reason in item['reasons']:
                print('   - ' + reason)
            if args.deep:
                deep = deep_check(item['repo'], item['inputs'])
                print(f"   深度核验（扫描 {len(deep['scripts_scanned'])} 个入口）:")
                for name, location in deep['references'].items():
                    print(f"     · {name} → {location or '仓库内未找到'}")
                for finding in deep['findings']:
                    print('     ⚠ ' + finding)
                if not deep['findings']:
                    print('     ✓ 引用均可按入口目录解析')
        return 0
    counts = {}
    for item in results:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    for status, count in sorted(counts.items(), key=lambda pair: -pair[1]):
        print(f'{count:4d}  {status}')
    print('\n就绪候选（--ready 查看明细）：')
    for item in results:
        if item['status'] == 'ready_candidate':
            print(f"  [{item['tier']}] {item['repo']} | {item.get('doi') or '(无 DOI)'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
