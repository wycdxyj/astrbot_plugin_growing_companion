"""从当前实际源码、SQL 和测试报告生成 4.0 增量交付，不覆盖第三版压缩包。"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = 'd6f2a65'  # 本工作区已提交的 v0.3.0 基线，后续提交不会改变比较对象。
sys.path.insert(0, str(ROOT.parent))
from growing_companion.core.behavior_state import BEHAVIOR_SCHEMA  # noqa: E402
from growing_companion.core.db import Database  # noqa: E402


def sections(names: list[str]) -> str:
    content = []
    for name in names:
        path = ROOT / name
        language = {'.py': 'python', '.json': 'json', '.yaml': 'yaml', '.sql': 'sql'}.get(path.suffix, 'text')
        content.append(f'## `{name}`\n\n```{language}\n{path.read_text(encoding="utf-8").strip()}\n```')
    return '\n\n'.join(content)


def incremental_patch() -> bytes:
    """不修改 Git 索引；未跟踪的新模块也纳入可应用补丁。"""
    try:
        git_root = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=ROOT, capture_output=True, check=True)
        if Path(git_root.stdout.decode('utf-8').strip()).resolve() != ROOT:
            raise RuntimeError('不是原插件仓库')
        subprocess.run(['git', 'rev-parse', '--verify', BASE_COMMIT], cwd=ROOT, capture_output=True, check=True)
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        existing = ROOT / 'docs/upgrade-v4.patch'
        if existing.is_file():
            print('No original Git baseline: retaining the bundled v3-to-v4 patch')
            return existing.read_bytes()
        raise RuntimeError('缺少 v0.3.0 Git 基线及随包补丁，无法生成增量补丁') from None
    paths = ['core', 'tests', 'examples', 'main.py', 'metadata.yaml', '_conf_schema.json',
             'requirements.txt', 'README.md', '.gitignore', 'tools', 'docs/CLOUD_VOICE.md',
             'docs/DELIVERY_V4_TEMPLATE.md', 'docs/migrate_v3_to_v4.sql', 'docs/schema_v4.sql']
    result = subprocess.run(['git', '-c', 'core.autocrlf=false', 'diff', '--binary', '--no-ext-diff',
                             BASE_COMMIT, '--', *paths], cwd=ROOT, capture_output=True, check=True).stdout
    names = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '-z', '--', *paths],
                           cwd=ROOT, capture_output=True, check=True).stdout.split(b'\0')
    for name in names:
        if not name:
            continue
        relative = name.decode('utf-8')
        diff = subprocess.run(['git', '-c', 'core.autocrlf=false', 'diff', '--no-index', '--binary',
                               '--', '/dev/null', relative], cwd=ROOT, capture_output=True)
        if diff.returncode not in (0, 1):
            raise RuntimeError(f'无法为新文件生成补丁：{relative}')
        result += diff.stdout
    return result


def build() -> None:
    migration = '-- 适用于 v3 数据库；应用启动也会自动幂等迁移。\nBEGIN IMMEDIATE;\n' + BEHAVIOR_SCHEMA + '\nPRAGMA user_version=4;\nCOMMIT;\n'
    (ROOT / 'docs/migrate_v3_to_v4.sql').write_text(migration, encoding='utf-8', newline='\n')
    db = Database(':memory:')
    try:
        rows = db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name")
        schema = 'PRAGMA foreign_keys=ON;\n\n' + ';\n\n'.join(r['sql'] for r in rows) + ';\nPRAGMA user_version=4;\n'
    finally:
        db.close()
    (ROOT / 'docs/schema_v4.sql').write_text(schema, encoding='utf-8', newline='\n')
    report = ROOT / 'docs/test-results-v4.xml'
    if not report.exists():
        raise RuntimeError('请先运行 pytest --junitxml=docs/test-results-v4.xml')
    suites = ET.parse(report).getroot().findall('testsuite')
    counts = {key: sum(int(s.attrib.get(key, 0)) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    if counts['failures'] or counts['errors']:
        raise RuntimeError('测试报告尚有失败或错误，不生成发行包')
    result = (f"本机测试 **{counts['tests']} 项，失败 {counts['failures']}，错误 {counts['errors']}，跳过 {counts['skipped']}**。"
              '机器可读报告：[docs/test-results-v4.xml](docs/test-results-v4.xml)。')
    adapter_report = ROOT / 'docs/test-adapter-v4.xml'
    if adapter_report.exists():
        adapter_suites = ET.parse(adapter_report).getroot().findall('testsuite')
        adapter_tests = sum(int(s.attrib.get('tests', 0)) for s in adapter_suites)
        if any(int(s.attrib.get('failures', 0)) or int(s.attrib.get('errors', 0)) for s in adapter_suites):
            raise RuntimeError('最终入口复测失败，不生成发行包')
        result += f' 最后入口调整另通过 {adapter_tests} 项复测，见 [入口报告](docs/test-adapter-v4.xml)。'
    groups = {
        'PLANNER': ['core/models.py', 'core/planner.py', 'core/behavior_state.py'],
        'CONFIG': ['metadata.yaml', '_conf_schema.json', 'requirements.txt'],
        'SENDER': ['core/expression.py', 'main.py', 'core/engine.py', 'core/social.py',
                   'core/speech.py', 'core/cloud_voice.py', 'examples/cloud_voice_demo.py'],
        'CONSISTENCY': ['core/consistency.py', 'core/prompts.py'],
        'TESTS': ['tests/test_behavior_v4.py', 'tests/test_cloud_voice.py'],
    }
    template = (ROOT / 'docs/DELIVERY_V4_TEMPLATE.md').read_text(encoding='utf-8')
    for marker, names in groups.items():
        template = template.replace('{{' + marker + '}}', sections(names))
    template = template.replace('{{SQL}}', '```sql\n' + migration + '```').replace('{{RESULT}}', result)
    (ROOT / 'DELIVERY_V4.md').write_text(template, encoding='utf-8', newline='\n')
    (ROOT / 'docs/upgrade-v4.patch').write_bytes(incremental_patch())
    files = [ROOT / name for name in ['main.py', '__init__.py', 'metadata.yaml', '_conf_schema.json',
                                     'requirements.txt', 'README.md', 'DELIVERY_V4.md', 'DELIVERY_V3.md', '.gitignore']]
    for directory in ('core', 'tests', 'examples', 'tools'):
        files += list((ROOT / directory).rglob('*.py'))
    files += [p for p in (ROOT / 'docs').iterdir() if p.is_file()]
    files += list((ROOT / 'core/brainmap/vendor').iterdir())
    archive = ROOT.parent / 'growing_companion_v4.0.0.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(set(files)):
            bundle.write(path, (Path(ROOT.name) / path.relative_to(ROOT)).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii', newline='\n')
    print(f'Created {archive.name}: {archive.stat().st_size} bytes, {len(set(files))} files')


if __name__ == '__main__':
    build()
